"""厂商整理：ezPLM 厂商表（约 3200 家）→ 盘点去重 → 抓官网核对 → 补缺失官网（Gemini 搜索）→ 建议值 → 人工复核 / 修改。

建议值约定（用户确认）：显示名 = 常用品牌名（Texas Instruments、SGMICRO）；法定全称、中文名单独存；原来不同的写法进别名。
所有建议只写在 company_review 表里，不动 ezPLM；人工可直接改任何建议字段。
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import unescape
from urllib.parse import urlparse

from sqlalchemy import func, or_, select

from .ihs import Lineage, mfr_key
from .staging import CompanyReview, EzCompany, StagedPart

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 ezPLM-datacheck"
_CJK = re.compile(r"[一-鿿]")
_LEGAL_SUFFIX = re.compile(r"\b(Inc\.?|Incorporated|Corp\.?|Corporation|Co\.,? ?Ltd\.?|Ltd\.?|Limited|LLC|GmbH|AG|S\.?A\.?|N\.?V\.?|B\.?V\.?|plc|K\.?K\.?|Pte\.? Ltd\.?|SE)\b", re.I)
PARKED = re.compile(r"domain (is )?for sale|buy this domain|parked free|godaddy|sedo\.com|dan\.com|this domain may be for sale|域名.*(出售|转让)", re.I)


# --------------------------------------------------------------------------- 1. 盘点
def inventory(db, log=print) -> dict:
    """ez_companies → company_review（保留已有的检查结果和人工修改）。重复组按名称比较键归组，组内物料最多的是主记录。"""
    counts = dict(db.execute(select(StagedPart.manufacturer_id, func.count()).where(StagedPart.manufacturer_id.isnot(None))
                             .group_by(StagedPart.manufacturer_id)).all())
    lin = Lineage(db=db)
    comps = db.execute(select(EzCompany)).scalars().all()
    groups = defaultdict(list)
    for c in comps:
        groups[mfr_key(c.name) or c.id].append(c)
    have = {r.id: r for r in db.execute(select(CompanyReview)).scalars()}
    st = Counter()
    for key, cs in groups.items():
        cs.sort(key=lambda c: (-(counts.get(c.id, 0)), not c.is_manufacturer, c.name or ""))
        primary = cs[0]
        for c in cs:
            r = have.get(c.id)
            if r is None:
                r = CompanyReview(id=c.id, review_status="pending")
                db.add(r)
                st["new"] += 1
            r.group_key, r.is_primary = key, c is primary
            r.cur_name, r.cur_aliases, r.cur_website = c.name, c.aliases_json or "[]", (c.website or "").strip() or None
            r.is_manufacturer, r.is_supplier = bool(c.is_manufacturer), bool(c.is_supplier)
            r.part_count = counts.get(c.id, 0)
            res = lin.resolve(c.name or "")
            r.lineage_to = res["current"] if res["conf"] in ("A", "B") else None
            if c is not primary and r.review_status == "pending" and not r.p_merge_into:
                r.p_merge_into = primary.id                # 建议并入主记录（人工确认）
            if len(cs) > 1:
                st["in_dup_groups"] += 1
        st["groups"] += 1
    db.commit()
    st.update(companies=len(comps), dup_groups=sum(1 for cs in groups.values() if len(cs) > 1),
              with_website=sum(1 for c in comps if (c.website or "").strip()), with_parts=sum(1 for c in comps if counts.get(c.id)))
    log(f"盘点：{dict(st)}")
    return dict(st)


# --------------------------------------------------------------------------- 2. 官网核对
def _norm_url(u: str) -> str | None:
    u = (u or "").strip()
    if not u:
        return None
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    return u


def _domain(u: str) -> str:
    h = (urlparse(u or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _root(h: str) -> str:
    parts = h.split(".")
    if len(parts) >= 3 and parts[-2] in ("com", "co", "net", "org", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _meta(html: str) -> dict:
    out = {}
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        out["title"] = re.sub(r"\s+", " ", unescape(m.group(1))).strip()[:300]
    m = re.search(r"<meta[^>]+property=[\"']og:site_name[\"'][^>]+content=[\"']([^\"']+)", html, re.I) or \
        re.search(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:site_name", html, re.I)
    if m:
        out["site_name"] = unescape(m.group(1)).strip()[:200]
    for blk in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.I | re.S)[:6]:
        try:
            js = json.loads(blk.strip())
        except ValueError:
            continue
        for o in (js if isinstance(js, list) else js.get("@graph", [js]) if isinstance(js, dict) else []):
            if isinstance(o, dict) and str(o.get("@type", "")).lower() in ("organization", "corporation"):
                out.setdefault("org_name", str(o.get("name") or "")[:200])
                if o.get("legalName"):
                    out["legal_name"] = str(o["legalName"])[:200]
    text = re.sub(r"<[^>]+>", " ", html[-60000:])
    m = re.search(r"(?:©|&copy;|Copyright)\s*(?:©)?\s*(?:\d{4}\s*[-–~]\s*)?(?:\d{4})?\s*,?\s*([A-Z一-鿿][^|<>\n.;©]{2,80}?)(?:\.|\s+All rights|\s+版权所有|\s*\||\s{2,}|$)", unescape(text), re.I)
    if m:
        out["copyright"] = m.group(1).strip(" ,")[:200]
    return out


BLOCK_PAGE = re.compile(r"Just a moment|Access Denied|Attention Required|Request unsuccessful|Page not available|Pardon Our Interruption|captcha|安全验证", re.I)


def check_one(url: str, timeout: float = 12) -> dict:
    """抓官网首页 → {status, http, final_url, redirected_to, title, site_name, org_name, legal_name, copyright, note}。
    status：ok = 正常；blocked = 站点在但拦了脚本（403/429/超时/验证页，大厂常见）；dead = 域名解析不了 / 连不上 / 停放页；none = 没官网。
    逐跳跟随跳转并记录：即使最后一跳被拦，也能知道 linear.com 跳到了 analog.com。"""
    import requests
    u = _norm_url(url)
    if not u:
        return {"status": "none"}
    h = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en,zh;q=0.8"}
    out, cur, chain = {}, u, []
    for _ in range(8):
        try:
            r = requests.get(cur, headers=h, timeout=(6, timeout), allow_redirects=False)
        except requests.exceptions.SSLError:
            if cur.startswith("https://"):
                cur = "http://" + cur[8:]
                continue
            return {**out, "status": "dead", "note": "SSL 握手失败", "chain": chain}
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout):
            out.update(status="blocked", note="超时（大站常对脚本限流）", final_url=cur)
            break
        except requests.exceptions.ConnectionError as e:
            msg = str(e)
            dns = re.search(r"NameResolution|Name or service not known|nodename nor servname|getaddrinfo", msg)
            refused = re.search(r"Connection refused|No route to host|Network is unreachable", msg)
            # 连接被重置 / 中断：多是防火墙或跨境线路（国内厂商站从海外访问常见），站点不一定挂了
            out.update(status="dead" if dns or refused else "blocked", note=("域名解析失败" if dns else "连接失败：" + msg[:100]), final_url=cur)
            break
        except Exception as e:  # noqa
            out.update(status="dead", note=str(e)[:120], final_url=cur)
            break
        if r.is_redirect and r.headers.get("location"):
            from urllib.parse import urljoin
            nxt = urljoin(cur, r.headers["location"])
            chain.append(nxt)
            cur = nxt
            continue
        out["http"], out["final_url"] = r.status_code, r.url
        html = r.text[:400000] if "html" in (r.headers.get("content-type") or "html") else ""
        out.update(_meta(html))
        if PARKED.search(html[:20000]):
            out.update(status="dead", note="域名停放 / 出售页")
        elif r.status_code in (401, 403, 404, 429, 503) and (r.status_code != 404 or BLOCK_PAGE.search(html[:5000]) or urlparse(r.url).path in ("", "/")):
            out.update(status="blocked", note=f"HTTP {r.status_code}（站点在，但拦了脚本）")
        elif r.status_code >= 400:
            out.update(status="dead", note=f"HTTP {r.status_code}")
        elif BLOCK_PAGE.search(out.get("title", "")):
            out.update(status="blocked", note="验证 / 拦截页")
        else:
            out["status"] = "ok"
        break
    else:
        out.update(status="blocked", note="跳转次数过多", final_url=cur)
    out["chain"] = chain
    fin = out.get("final_url") or cur
    if _root(_domain(fin)) != _root(_domain(u)):
        out["redirected_to"] = _domain(fin)
    for k in ("title", "site_name", "org_name"):            # 拦截页的标题不是公司名
        if out.get(k) and BLOCK_PAGE.search(out[k]):
            out.pop(k)
    return out


def _brand(s: str) -> str:
    s = _CJK.sub("", s or "") if _CJK.search(s or "") and re.search(r"[A-Za-z]{3}", s or "") else (s or "")
    s = re.sub(r"\s*[-|–:·]\s*.*$", "", s).strip()         # "TI | Home" → "TI"
    return _LEGAL_SUFFIX.sub("", s).strip(" ,.")


def propose(r: CompanyReview, chk: dict, domains: dict) -> None:
    """检查结果 → 建议值（只填空着的建议字段，人工改过的不覆盖）。"""
    ev = json.loads(r.evidence_json or "{}")
    ev["site"] = {k: v for k, v in chk.items() if v}
    cur = r.cur_name or ""
    aliases = [a for a in json.loads(r.cur_aliases or "[]") if a]
    latin = next((a for a in [cur] + aliases if not _CJK.search(a) and re.search(r"[A-Za-z]{2}", a)), None)   # 中文名的厂商用英文别名比对
    keys = [k for k in {mfr_key(x).replace(" ", "") for x in [cur] + aliases} if len(k) >= 2]
    names = [chk.get(k) for k in ("legal_name", "copyright", "org_name", "site_name") if chk.get(k)]
    legal = chk.get("legal_name") or (chk.get("copyright") if chk.get("copyright") and _LEGAL_SUFFIX.search(chk["copyright"]) else None)
    match = any(mfr_key(n) and any(k in mfr_key(n).replace(" ", "") or mfr_key(n).replace(" ", "") in k for k in keys) for n in names)
    status, acquirer, conf = "active", None, 0.5
    if chk.get("status") == "dead":
        status, conf = "unknown", 0.3
    elif chk.get("status") == "blocked" and not chk.get("redirected_to"):
        conf = 0.6                                                         # 站点在，但拿不到页面里的公司名
    elif chk.get("redirected_to"):
        owner = domains.get(_root(chk["redirected_to"]))
        if owner and mfr_key(owner) != r.group_key:
            status, acquirer, conf = "acquired", owner, 0.8               # 官网跳到另一家厂商的域名
    if r.lineage_to and mfr_key(r.lineage_to) != r.group_key:
        status, acquirer, conf = "acquired", r.lineage_to, max(conf, 0.85)
    if status == "active" and chk.get("status") == "ok":
        conf = 0.9 if match else 0.6
    r.p_status = r.p_status or status
    r.p_acquirer = r.p_acquirer or acquirer
    if chk.get("status") in ("ok", "blocked") and chk.get("final_url") and status != "acquired":
        r.p_website = r.p_website or f"{urlparse(chk['final_url']).scheme}://{urlparse(chk['final_url']).hostname}/"
    if legal and (match or not cur):
        r.p_legal_name = r.p_legal_name or legal
    if not r.p_name:
        if _CJK.search(cur) and not re.search(r"[A-Za-z]{2}", cur):      # 现名是中文全称：显示名取英文别名 / 官网站点名
            r.p_name = latin or (chk.get("site_name") if match else None) or (_brand(legal) if legal else None) or cur
        else:
            r.p_name = _brand(cur) or cur
    if not r.p_name_cn:
        cn = next((a for a in [cur] + aliases if _CJK.search(a or "")), None)
        r.p_name_cn = cn
    r.confidence = conf
    r.source = r.source or "site"
    r.evidence_json = json.dumps(ev, ensure_ascii=False)


def check_all(db, only_missing: bool = False, workers: int = 8, stop=None, log=print, progress=None) -> dict:
    """抓所有主记录的官网（物料多的先抓）。only_missing=True 只抓还没检查过的。"""
    q = select(CompanyReview).where(CompanyReview.is_primary.is_(True), CompanyReview.cur_website.isnot(None))
    if only_missing:
        q = q.where(CompanyReview.checked_at.is_(None))
    rows = db.execute(q.order_by(CompanyReview.part_count.desc())).scalars().all()
    domains = {_root(_domain(_norm_url(r.cur_website) or "")): r.cur_name for r in db.execute(select(CompanyReview).where(CompanyReview.cur_website.isnot(None))).scalars()}
    st = Counter()
    log(f"待检查官网 {len(rows)} 家（物料多的先查）")
    for i in range(0, len(rows), 40):
        if stop and stop():
            break
        chunk = rows[i:i + 40]
        with ThreadPoolExecutor(workers) as ex:
            res = list(ex.map(lambda r: check_one(r.cur_website), chunk))
        now = datetime.utcnow()
        for r, chk in zip(chunk, res):
            st[chk.get("status", "error")] += 1
            if chk.get("redirected_to"):
                st["redirected"] += 1
            r.check_status = "redirect_other" if chk.get("redirected_to") and chk.get("status") in ("ok", "blocked") else chk.get("status")
            r.http_status, r.final_url = chk.get("http"), chk.get("final_url")
            r.site_name = chk.get("site_name") or chk.get("org_name")
            r.legal_seen = chk.get("legal_name") or chk.get("copyright")
            r.page_title, r.check_note, r.checked_at = chk.get("title"), chk.get("note"), now
            if r.review_status == "pending":
                propose(r, chk, domains)
        db.commit()
        log(f"  已检查 {min(i + 40, len(rows))}/{len(rows)}：{dict(st)}")
        if progress:
            progress({"done": min(i + 40, len(rows)), "total": len(rows)})
    log(f"完成：{dict(st)}")
    return dict(st)


# --------------------------------------------------------------------------- 3. 补缺失官网（猜域名 → Gemini 搜索）
def _gemini_search(name: str, aliases: list[str]) -> dict | None:
    import os
    import requests
    from .settings import apply_env
    s = apply_env()
    key = os.environ.get("GEMINI_API_KEY") or s.get("gemini_api_key")
    if not key:
        raise RuntimeError("设置里没有 Gemini API Key")
    model = s.get("llm_model") if "gemini" in (s.get("llm_model") or "") else "gemini-2.5-flash"
    prompt = (f"Find the official corporate website of the electronic component manufacturer \"{name}\""
              + (f" (also known as: {', '.join(aliases[:5])})" if aliases else "") +
              ". Reply with ONLY a JSON object: {\"website\": homepage URL or null, \"brand_name\": common English brand name, "
              "\"legal_name\": full legal company name, \"chinese_name\": Chinese company name or null, "
              "\"status\": \"active\"|\"acquired\"|\"defunct\", \"acquired_by\": acquirer name or null, \"confidence\": 0-1}. "
              "Use null when unsure; never guess a URL.")
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", params={"key": key}, timeout=90,
                      json={"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}], "generationConfig": {"temperature": 0}})
    r.raise_for_status()
    text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"].get("parts", []))
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else None


def find_missing(db, use_gemini: bool = True, limit: int | None = None, stop=None, log=print, progress=None) -> dict:
    """没有官网 / 官网打不开的主记录：先按名字猜域名并核对，猜不到再用 Gemini（带 Google 搜索）找；结果只作建议（source=gemini）。"""
    q = select(CompanyReview).where(CompanyReview.is_primary.is_(True), CompanyReview.review_status == "pending",
                                    or_(CompanyReview.cur_website.is_(None), CompanyReview.check_status == "dead"),
                                    or_(CompanyReview.source.is_(None), CompanyReview.source != "gemini"))
    rows = db.execute(q.order_by(CompanyReview.part_count.desc())).scalars().all()
    if limit:
        rows = rows[:limit]
    domains = {_root(_domain(_norm_url(r.cur_website) or "")): r.cur_name for r in db.execute(select(CompanyReview).where(CompanyReview.cur_website.isnot(None))).scalars()}
    st = Counter()
    log(f"待补官网 {len(rows)} 家")
    for i, r in enumerate(rows):
        if stop and stop():
            break
        slug = re.sub(r"[^a-z0-9]", "", _brand(r.cur_name or "").lower())
        found = None
        for tld in ("com", "com.cn", "cn") if len(slug) >= 3 else ():
            chk = check_one(f"https://www.{slug}.{tld}", timeout=8)
            names = [chk.get(k) for k in ("legal_name", "copyright", "org_name", "site_name", "title") if chk.get(k)]
            first = (mfr_key(r.cur_name).split() or [""])[0]
            if chk.get("status") == "ok" and first and any(first in mfr_key(n) for n in names):
                found = ("guess", chk)
                break
            if chk.get("status") == "blocked" and not chk.get("redirected_to") and len(slug) >= 5:
                found = ("guess", chk)                     # 站点拦脚本拿不到公司名，但域名就是品牌名（diodes.com）：低置信，复核
                break
        if not found and use_gemini:
            try:
                g = _gemini_search(r.cur_name, [a for a in json.loads(r.cur_aliases or "[]") if a])
            except Exception as e:  # noqa
                log(f"  ! {r.cur_name}: Gemini 出错 {str(e)[:120]}")
                st["gemini_error"] += 1
                g = None
            if g and g.get("website"):
                chk = check_one(g["website"])
                chk["gemini"] = g
                found = ("gemini", chk)
            elif g:
                ev = json.loads(r.evidence_json or "{}"); ev["gemini"] = g
                r.evidence_json = json.dumps(ev, ensure_ascii=False)
                r.p_status = r.p_status or g.get("status")
                r.p_acquirer = r.p_acquirer or g.get("acquired_by")
                r.source = "gemini"
            time.sleep(0.5)
        if found:
            how, chk = found
            st[how] += 1
            g = chk.get("gemini") or {}
            r.p_legal_name = r.p_legal_name or g.get("legal_name")
            r.p_name = r.p_name or g.get("brand_name")
            r.p_name_cn = r.p_name_cn or g.get("chinese_name")
            if g.get("status") == "acquired":
                r.p_status, r.p_acquirer = r.p_status or "acquired", r.p_acquirer or g.get("acquired_by")
            r.check_status, r.final_url, r.site_name = chk.get("status"), chk.get("final_url"), chk.get("site_name") or chk.get("org_name")
            r.legal_seen, r.page_title, r.checked_at = chk.get("legal_name") or chk.get("copyright"), chk.get("title"), datetime.utcnow()
            propose(r, chk, domains)
            r.source = how
            r.confidence = min(r.confidence or 0.5, 0.7 if how == "gemini" else 0.8)    # 找来的官网一律复核
        else:
            st["not_found"] += 1
        if i % 20 == 19:
            db.commit()
            log(f"  已处理 {i + 1}/{len(rows)}：{dict(st)}")
            if progress:
                progress({"done": i + 1, "total": len(rows)})
    db.commit()
    log(f"完成：{dict(st)}")
    return dict(st)


# --------------------------------------------------------------------------- 汇总 / 导出
def summary(db) -> dict:
    q = lambda *w: db.execute(select(func.count()).select_from(CompanyReview).where(*w)).scalar() or 0
    return {"total": q(), "primary": q(CompanyReview.is_primary.is_(True)), "dups": q(CompanyReview.is_primary.is_(False)),
            "with_parts": q(CompanyReview.part_count > 0), "no_website": q(CompanyReview.is_primary.is_(True), CompanyReview.cur_website.is_(None)),
            "checked": q(CompanyReview.checked_at.isnot(None)), "ok": q(CompanyReview.check_status == "ok"), "blocked": q(CompanyReview.check_status == "blocked"),
            "dead": q(CompanyReview.check_status == "dead"), "redirect_other": q(CompanyReview.check_status == "redirect_other"),
            "acquired": q(CompanyReview.p_status == "acquired"), "pending": q(CompanyReview.review_status == "pending"),
            "approved": q(CompanyReview.review_status == "approved"), "rejected": q(CompanyReview.review_status == "rejected")}


EXPORT_COLS = ["id", "is_primary", "p_merge_into", "part_count", "cur_name", "p_name", "p_legal_name", "p_name_cn", "cur_aliases",
               "cur_website", "p_website", "check_status", "http_status", "final_url", "site_name", "legal_seen", "p_status", "p_acquirer",
               "lineage_to", "confidence", "source", "review_status", "reviewed_by", "reviewed_at", "notes"]
