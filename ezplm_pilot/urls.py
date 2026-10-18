"""官网链接 / 数据手册 URL：按制造商模板生成候选，HTTP 验证存活后才产出建议。"""
from __future__ import annotations

import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from .models import Part, Proposal
from .kicad import SUFFIX_RE

HERE = Path(__file__).resolve().parent.parent
SEARCH_PAGE_HINT = re.compile(r"search|result|keywords=|q=", re.I)
UA = "Mozilla/5.0 (compatible; ezPLM-datafill/0.1; +https://www.ezplm.cn)"


def _fill(tpl: str, mpn: str, gpn_re: str | None = None) -> str:
    mpn = (mpn or "").strip()
    base = re.sub(r"\(.*?\)", "", mpn).strip()
    base = SUFFIX_RE.sub("", base) if len(SUFFIX_RE.sub("", base)) >= 4 else base
    series = re.match(r"^[A-Za-z]+\d+", base)
    # 通用料号 GPN：优先用厂商专属正则（取 group 1），否则去掉结尾的封装/温度码（1~4 个字母 + 可选数字）
    m = re.match(gpn_re, base, re.I) if gpn_re else None
    gpn = next((g for g in m.groups() if g), None) if m else None     # 多分支正则：取第一个命中的分组
    if not gpn:
        gpn = re.sub(r"-?[A-Za-z]{1,4}\d?$", "", base)
    if not gpn or len(gpn) < 4:
        gpn = base
    return tpl.format(mpn=mpn, mpn_lower=mpn.lower(), mpn_base=base, mpn_base_lower=base.lower(),
                      mpn_url=urllib.parse.quote(mpn, safe=""), mpn_series=(series.group(0).lower() if series else base.lower()),
                      mpn_gpn=gpn, mpn_gpn_lower=gpn.lower())


class UrlFiller:
    def __init__(self, cfg_path: str | Path = HERE / "config" / "manufacturers.yaml", timeout: float = 12, workers: int = 8, sleep: float = 0.1):
        cfg = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))
        self.rules = [(re.compile(r["match"], re.I), r) for r in cfg["manufacturers"]]
        self.timeout, self.workers, self.sleep = timeout, workers, sleep

    def candidates(self, p: Part) -> list[dict]:
        if not p.mpn or len(p.mpn.strip()) < 3:
            return []
        for pat, r in self.rules:
            if pat.search(p.manufacturer or ""):
                out, seen = [], set()
                for fld, key in (("url", "product"), ("datasheet", "datasheet")):
                    tpls = r.get(key) or []
                    for tpl in ([tpls] if isinstance(tpls, str) else tpls):   # 支持多个模板，按顺序尝试，首个验证通过者胜出
                        u = _fill(tpl, p.mpn, r.get("gpn"))
                        if u and u not in seen:
                            seen.add(u); out.append({"field": fld, "url": u, "verify": r.get("verify", "get")})
                return out
        return []

    def verify(self, url: str, mode: str) -> tuple[bool, str]:
        if mode == "none":
            return (not SEARCH_PAGE_HINT.search(url), "unverified-template")
        import requests
        try:
            fn = requests.head if mode == "head" else requests.get
            r = fn(url, timeout=self.timeout, allow_redirects=True, headers={"User-Agent": UA}, stream=True)
            ok = 200 <= r.status_code < 300 and not SEARCH_PAGE_HINT.search(r.url)
            if ok and mode == "get":
                ctype = r.headers.get("content-type", "")
                if url.lower().endswith(".pdf") and "pdf" not in ctype:
                    ok = False
                elif "text/html" in ctype:
                    head = r.raw.read(20000, decode_content=True).decode("utf-8", "ignore").lower()
                    if re.search(r"page not found|404|no results|not available|产品不存在", head) and "<title>" in head:
                        ok = False
            r.close()
            return ok, f"{r.status_code} {r.url}"
        except Exception as e:  # noqa
            return False, f"error {str(e)[:80]}"

    def propose(self, parts: Iterable[Part], verify: bool = True) -> Iterator[Proposal]:
        jobs = []
        for p in parts:
            if p.url:
                continue
            for c in self.candidates(p):
                if c["field"] == "datasheet" and p.has_pdf:
                    continue
                jobs.append((p, c))

        def run(job):
            p, c = job
            if verify:
                ok, info = self.verify(c["url"], c["verify"])
                time.sleep(self.sleep)
            else:
                ok, info = (c["verify"] == "none" and not SEARCH_PAGE_HINT.search(c["url"])), "not-verified"
            return p, c, ok, info

        done: set[tuple[str, str]] = set()
        with ThreadPoolExecutor(self.workers) as ex:
            for p, c, ok, info in ex.map(run, jobs):
                if not ok or (p.id, c["field"]) in done:
                    continue
                done.add((p.id, c["field"]))
                conf = 0.9 if info.startswith("2") else 0.6
                yield Proposal(p.id, p.mpn, c["field"], "", c["url"], c["url"], conf, "auto_fill" if conf >= 0.85 else "review",
                               "url-template", {"verify": info}, ["链接自动补全"] if conf >= 0.85 else ["链接待复核"])
