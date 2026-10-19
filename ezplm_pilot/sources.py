"""数据接入层。

三种数据源，接口相同（iter_parts / load_taxonomy）：
- SampleSource  : 本仓库自带的后台样本（150 条 + 505 个分类节点），用于离线跑通
- CsvSource     : 数据库导出的 CSV / Parquet（列名见 CSV_COLUMNS，缺列自动补空）
- ApiSource     : 同事开发中的 REST API（契约见 docs/API_CONTRACT.md），也兼容现有
                  /admin-api/partlibs 的返回结构
写回统一走 Writer：默认 dry-run 只落 JSONL；--apply 时才调用 API。
"""
from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from typing import Iterable, Iterator

from .models import Part, Proposal
from .taxonomy import Taxonomy

HERE = Path(__file__).resolve().parent.parent
SAMPLE_DIR = HERE / "data" / "sample"

CSV_COLUMNS = ["id", "mpn", "manufacturer", "manufacturer_id", "category", "category_id",
               "symbol", "symbol_lib", "footprint", "footprint_lib", "has_pdf", "has_image", "url", "description", "description_en", "status"]


def _bool(v) -> bool:
    return str(v).strip().lower() in ("1", "t", "true", "y", "yes")


# ----------------------------------------------------------------------------- Sample
class SampleSource:
    def __init__(self, path: Path = SAMPLE_DIR / "parts_sample.txt"):
        self.path = path

    def iter_parts(self) -> Iterator[Part]:
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                c = [x.strip() for x in line.split("¦")]
                # id mpn manufacturer category category_id symbol footprint hasPdf hasImage description
                c += [""] * (10 - len(c))
                yield Part(id=c[0], mpn=c[1], manufacturer=c[2], category=c[3], category_id=c[4],
                           symbol=c[5], footprint=c[6], has_pdf=_bool(c[7]), has_image=_bool(c[8]), description=c[9])

    def load_taxonomy(self) -> Taxonomy:
        return Taxonomy.from_tsv(SAMPLE_DIR / "categories.tsv")


# ----------------------------------------------------------------------------- CSV / Parquet
class CsvSource:
    def __init__(self, parts_path: str, categories_path: str | None = None):
        self.parts_path = parts_path
        self.categories_path = categories_path

    def iter_parts(self) -> Iterator[Part]:
        if self.parts_path.endswith(".parquet"):
            import pandas as pd
            df = pd.read_parquet(self.parts_path).fillna("")
            rows = df.to_dict("records")
        else:
            with open(self.parts_path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
        for r in rows:
            d = {k: r.get(k, "") for k in CSV_COLUMNS}
            d["has_pdf"] = _bool(d["has_pdf"])
            d["has_image"] = _bool(d["has_image"])
            yield Part(**d)

    def load_taxonomy(self) -> Taxonomy:
        p = self.categories_path or str(SAMPLE_DIR / "categories.tsv")
        if p.endswith(".json"):
            return Taxonomy.from_api_json(json.loads(Path(p).read_text(encoding="utf-8")))
        return Taxonomy.from_tsv(p)


# ----------------------------------------------------------------------------- API
class ApiSource:
    """REST 适配器。

    环境变量：EZPLM_API_BASE（如 https://www.ezplm.cn/admin-api）、EZPLM_API_TOKEN。
    读接口兼容两种风格：
      1) 现有后台：GET {base}/partlibs?current=N&pageSize=M         -> data.data[] / data.total
      2) 契约接口：GET {base}/parts?cursor=...&pageSize=M&missing=... -> data.items[] / data.nextCursor
    """

    def __init__(self, base: str | None = None, token: str | None = None, page_size: int = 200, sleep: float = 0.2):
        self.base = (base or os.environ.get("EZPLM_API_BASE", "")).rstrip("/")
        self.token = token or os.environ.get("EZPLM_API_TOKEN", "")
        self.page_size = page_size
        self.sleep = sleep
        self.total = 0
        self.current_page = 1
        if not self.base:
            raise RuntimeError("请设置 EZPLM_API_BASE / EZPLM_API_TOKEN")

    def _get(self, path: str, **params):
        import requests
        r = requests.get(f"{self.base}{path}", params=params, headers={"Authorization": f"Bearer {self.token}"}, timeout=60)
        if r.status_code == 400 and params.get("pageSize", 0) and int(params["pageSize"]) > 100:
            self.page_size = 100                       # ezPLM 后台每页上限 100
            params["pageSize"] = 100
            r = requests.get(f"{self.base}{path}", params=params, headers={"Authorization": f"Bearer {self.token}"}, timeout=60)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _to_part(x: dict) -> Part:
        cat = x.get("category") or {}
        mf = x.get("manufacturer") or {}
        sym = x.get("symbol") or {}
        fp = x.get("footprint") or {}
        return Part(
            id=x["id"], mpn=x.get("mpn", ""),
            manufacturer=mf.get("name", "") if isinstance(mf, dict) else str(mf or ""),
            manufacturer_id=mf.get("id", "") if isinstance(mf, dict) else "",
            category=cat.get("name", "") if isinstance(cat, dict) else str(cat or ""),
            category_id=cat.get("id", "") if isinstance(cat, dict) else "",
            symbol=sym.get("name", "") if isinstance(sym, dict) else str(sym or ""),
            symbol_lib=(sym.get("category") or {}).get("name", "") if isinstance(sym, dict) and isinstance(sym.get("category"), dict) else (sym.get("category", "") if isinstance(sym, dict) else ""),
            footprint=fp.get("name", "") if isinstance(fp, dict) else str(fp or ""),
            footprint_lib=(fp.get("category") or {}).get("name", "") if isinstance(fp, dict) and isinstance(fp.get("category"), dict) else (fp.get("category", "") if isinstance(fp, dict) else ""),
            has_pdf=_bool(x.get("hasPdf", x.get("has_pdf", False))),
            has_image=bool(x.get("image") or x.get("imageUrl")),
            url=x.get("url", "") or "", description=x.get("description", "") or "",
            description_en=x.get("descriptionEn", "") or "", status=x.get("status", "") or "",
        )

    def iter_parts(self, missing: str | None = None, max_pages: int | None = None, start_page: int = 1) -> Iterator[Part]:
        page, cursor = start_page, None
        self.current_page = page
        while True:
            if cursor is not None or missing:
                js = self._get("/parts", cursor=cursor, pageSize=self.page_size, missing=missing)
                data = js.get("data", js)
                items, cursor = data.get("items", []), data.get("nextCursor")
            else:
                js = self._get("/partlibs", current=page, pageSize=self.page_size)
                data = js.get("data", js)
                items = data.get("data", data.get("items", []))
                self.total = data.get("total", 0)
                cursor = None
            for x in items:
                yield self._to_part(x)
            if not items or (cursor is None and len(items) < self.page_size):
                break
            page += 1
            self.current_page = page
            if max_pages and page > max_pages:
                break
            time.sleep(self.sleep)

    def load_taxonomy(self) -> Taxonomy:
        js = self._get("/admin-categories", taxonomy="partlib")
        return Taxonomy.from_api_json(js)

    def test(self) -> dict:
        js = self._get("/partlibs", current=1, pageSize=1)
        data = js.get("data", js)
        return {"ok": True, "total": data.get("total"), "sample": (data.get("data") or data.get("items") or [None])[0]}

    def get_part_detail(self, part_id: str) -> dict:
        """原始详情（含 attributes / notes / pdf）。"""
        return self._get(f"/partlibs/{part_id}").get("data", {})

    def get_part(self, part_id: str) -> dict:
        """读取单条当前值，返回与回写 set 同名的键：categoryId/symbol/footprint/url/datasheetUrl/descriptionEn/manufacturer。"""
        d = self._get(f"/partlibs/{part_id}").get("data", {})
        cat, sym, fp, pdf = d.get("category") or {}, d.get("symbol") or {}, d.get("footprint") or {}, d.get("pdf") or {}
        mf = d.get("manufacturer") or {}
        return {"categoryId": cat.get("id") if isinstance(cat, dict) else cat, "symbol": sym.get("name") if isinstance(sym, dict) else sym,
                "footprint": fp.get("name") if isinstance(fp, dict) else fp, "url": d.get("url") or "",
                "datasheetUrl": (pdf.get("url") if isinstance(pdf, dict) else pdf) or "",
                "descriptionEn": d.get("descriptionEn") or "", "manufacturer": (mf.get("name") if isinstance(mf, dict) else mf) or ""}

    def patch_parts(self, updates: list[dict]) -> dict:
        import requests
        r = requests.patch(f"{self.base}/parts/batch", json={"updates": updates},
                           headers={"Authorization": f"Bearer {self.token}"}, timeout=120)
        r.raise_for_status()
        return r.json()


# ----------------------------------------------------------------------------- Writer
class Writer:
    """把 Proposal 落成 JSONL（永远落）；apply=True 且给了 ApiSource 时再按批写回。"""

    def __init__(self, out_path: str | Path, api: ApiSource | None = None, apply: bool = False, batch: int = 200):
        self.out_path = Path(out_path)
        self.api, self.apply, self.batch = api, apply, batch
        self.out_path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, proposals: Iterable[Proposal], only_actions=("auto_fill",)) -> dict:
        n, applied, pending = 0, 0, []
        with open(self.out_path, "w", encoding="utf-8") as f:
            for p in proposals:
                f.write(json.dumps(p.to_dict(), ensure_ascii=False) + "\n")
                n += 1
                if self.apply and self.api and p.action in only_actions:
                    pending.append(self._to_update(p))
                    if len(pending) >= self.batch:
                        self.api.patch_parts(pending); applied += len(pending); pending = []
        if pending:
            self.api.patch_parts(pending); applied += len(pending)
        return {"written": n, "applied": applied, "file": str(self.out_path)}

    @staticmethod
    def _to_update(p: Proposal) -> dict:
        return {"id": p.part_id, "set": {p.field: p.proposed}, "tags": p.tags,
                "provenance": {"method": p.method, "confidence": p.confidence, "evidence": p.evidence}}


def open_source(kind: str, parts: str | None = None, categories: str | None = None):
    if kind == "sample":
        return SampleSource(Path(parts)) if parts else SampleSource()
    if kind == "csv":
        return CsvSource(parts, categories)
    if kind == "api":
        return ApiSource()
    raise ValueError(kind)
