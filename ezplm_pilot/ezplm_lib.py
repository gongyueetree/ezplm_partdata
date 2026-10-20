"""ezPLM 基础数据：厂商、符号、封装清单。

平台以 ezPLM 自己的库为准（KiCad 官方 / 厂商库 / 开源工程只用来补缺）：
- 厂商：ezPLM 的厂商名和别名是标准；config/mfr_lineage.yaml 只补收购关系，最终映射到 ezPLM 的厂商
- 符号 / 封装：建议里能在 ezPLM 找到的标出 ezPLM ID；找不到的打"需新建"，整理完随数据一起补回 ezPLM
- ezPLM 有大量以型号命名的派生符号（如 MUN5112DW1，根符号 Q_Dual_PNP_PNP_BRT_...），型号对上即可直接匹配

拉取走平台已配置的 ezPLM 后台接口（与拉物料同一个地址和 Token），只读。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable

from sqlalchemy import delete, func, insert, select

from .models import Part, Proposal
from .staging import EzCompany, EzFootprint, EzSymbol

PAGE = 100                                   # ezPLM 后台每页上限


def _pages(api, path: str, page_size: int = PAGE, **params):
    """按页拉一轮；每页返回 (items, total)。"""
    page = 1
    while True:
        js = api._get(path, current=page, pageSize=page_size, **params)
        data = js.get("data", js)
        items = data.get("data") or data.get("items") or []
        total = data.get("total") or 0
        yield items, total
        if not items or page * page_size >= total:
            break
        page += 1


# 补漏策略：ezPLM 列表按创建时间排序，批量导入的记录创建时间相同；同一个查询总是同样地切分这些"并列"的记录，
# 所以换一种查询（排序字段 / 每页条数）重拉，才能拿到上一轮被挤掉的那些。sort 的格式接口没有文档，不认的会报错，直接跳过。
ROUND_VARIANTS = [{}, {"sort": "id"}, {"sort": "name"}, {"sort": "-id"}, {"sort": "id:asc"}, {"sort": "name:desc"},
                  {"page_size": 97}, {"page_size": 89}, {"page_size": 71}, {"page_size": 53}, {"page_size": 37}, {"page_size": 23}, {"page_size": 17}, {"page_size": 13}]


def _pull_all(api, path: str, log, stop=None, **params) -> list[dict] | None:
    """拉全表：第一轮按默认查询；不够总数就依次换查询方式再拉，直到拿全或策略用完。收到停止信号返回 None。
    （厂商表默认查询一轮只拿到 4242 条里的 3215 条，原样重拉一条也补不到。）"""
    seen: dict[str, dict] = {}
    total = 0
    for k, variant in enumerate(ROUND_VARIANTS, 1):
        v = dict(variant)
        page_size = v.pop("page_size", PAGE)
        before = len(seen)
        try:
            for items, total in _pages(api, path, page_size=page_size, **params, **v):
                for x in items:
                    if x.get("id"):
                        seen[x["id"]] = x
                if stop and stop():
                    return None
        except Exception as e:  # noqa —— 接口不认这种 sort 写法等：换下一种
            log(f"  {path}：第 {k} 轮（{variant or '默认'}）跳过：{str(e)[:80]}")
            continue
        log(f"  {path}：第 {k} 轮（{variant or '默认'}），累计 {len(seen)} / {total} 条（本轮新增 {len(seen) - before}）")
        if len(seen) >= total:
            break
    if len(seen) < total:
        log(f"  ! {path}：试完 {len(ROUND_VARIANTS)} 种查询方式仍差 {total - len(seen)} 条，先写入已拿到的")
    return list(seen.values())


def _tf(v) -> bool:
    return str(v).strip().lower() in ("t", "true", "1", "yes")


def pull(api, db, what=("companies", "symbols", "footprints"), log=None, stop=None) -> dict:
    """整表重拉（先删后插，数量级几万条，几分钟）。"""
    from .kicad import norm_name
    log = log or (lambda m: None)
    out = {}
    specs = {
        "companies": (EzCompany, "/companies", {"source": "system"},
                      lambda x: {"id": x["id"], "name": x.get("name") or "", "website": x.get("website") or "", "status": x.get("status") or "",
                                 "aliases_json": json.dumps([a if isinstance(a, str) else a.get("alias", "") for a in (x.get("aliases") or [])], ensure_ascii=False),
                                 "is_manufacturer": bool(x.get("isManufacturer", True)), "is_supplier": bool(x.get("isSupplier", False))}),
        "symbols": (EzSymbol, "/symbols", {},
                    lambda x: {"id": x["id"], "name": x.get("name") or "", "norm": norm_name(x.get("name") or ""),
                               "category": (x.get("category") or {}).get("name", ""), "root_id": (x.get("root") or {}).get("id"),
                               "root_name": (x.get("root") or {}).get("name")}),
        "footprints": (EzFootprint, "/footprints", {},
                       lambda x: {"id": x["id"], "name": x.get("name") or "", "category": (x.get("category") or {}).get("name", ""),
                                  "has_3d": _tf(x.get("has3D")), "has_pdf": _tf(x.get("hasPdf"))}),
    }
    for key in what:
        model, path, params, conv = specs[key]
        raw = _pull_all(api, path, log, stop=stop, **params)
        if raw is None:
            log("  收到停止信号，本表不写入")
            return out
        rows = [conv(x) for x in raw]
        now = datetime.utcnow()
        db.execute(delete(model))
        for i in range(0, len(rows), 2000):
            db.execute(insert(model), [dict(r, fetched_at=now) for r in rows[i:i + 2000]])
        db.commit()
        out[key] = len(rows)
        log(f"  {key}：写入 {len(rows)} 条")
    return out


def overlap_with_kicad(db, index) -> dict:
    """ezPLM 符号/封装 与平台已扫描的 KiCad 库按名称的重合情况。"""
    ez_sym = {n for (n,) in db.execute(select(EzSymbol.name))}
    ez_fp = {n for (n,) in db.execute(select(EzFootprint.name))}
    k_sym = {r[0] for r in index.all_symbols()} if index else set()
    k_fp = {r[0] for r in index.con.execute("select name from footprints")} if index else set()
    return {"ezplm_symbols": len(ez_sym), "kicad_symbols": len(k_sym), "symbols_in_both": len(ez_sym & k_sym),
            "ezplm_footprints": len(ez_fp), "kicad_footprints": len(k_fp), "footprints_in_both": len(ez_fp & k_fp),
            "ezplm_footprints_not_in_kicad": len(ez_fp - k_fp)}


# --------------------------------------------------------------------------- 查找（处理时用）
class EzLibrary:
    """一次性把 ezPLM 符号/封装清单读进内存（几万条），供每批处理查找。"""

    def __init__(self, db):
        self.sym_by_name: dict[str, tuple] = {}
        self.sym_by_norm: dict[str, list] = {}
        for i, n, norm, cat, rid, rname in db.execute(select(EzSymbol.id, EzSymbol.name, EzSymbol.norm, EzSymbol.category, EzSymbol.root_id, EzSymbol.root_name)):
            self.sym_by_name[n] = (i, n, cat, rname)
            self.sym_by_norm.setdefault(norm, []).append((i, n, cat, rname))
        self.fp_by_name = {n: (i, n, cat, h3, hp) for i, n, cat, h3, hp in
                           db.execute(select(EzFootprint.id, EzFootprint.name, EzFootprint.category, EzFootprint.has_3d, EzFootprint.has_pdf))}

    @staticmethod
    def count(db) -> dict:
        return {"companies": db.execute(select(func.count(EzCompany.id))).scalar() or 0,
                "symbols": db.execute(select(func.count(EzSymbol.id))).scalar() or 0,
                "footprints": db.execute(select(func.count(EzFootprint.id))).scalar() or 0}

    @property
    def empty(self) -> bool:
        return not self.sym_by_name and not self.fp_by_name

    def propose_symbols_by_mpn(self, parts: Iterable[Part], auto_min: float = 0.85) -> list[Proposal]:
        """ezPLM 里以型号命名的符号：型号（规范化 / 去包装后缀）对上就直接建议，带 ezPLM ID。"""
        from .kicad import mpn_variants
        out = []
        for p in parts:
            if p.symbol:
                continue
            variants = mpn_variants(p.mpn)
            for k, v in enumerate(variants):
                hits = self.sym_by_norm.get(v)
                if not hits:
                    continue
                sid, name, cat, root = hits[0]
                conf = 0.97 if k == 0 else 0.9
                act = "auto_fill" if conf >= auto_min and len(hits) == 1 else "review"
                out.append(Proposal(p.id, p.mpn, "symbol", "", name, f"{cat}:{name}", conf, act, "ezplm-symbol:" + ("exact" if k == 0 else "suffix-stripped"),
                                    {"ezplm_id": sid, "root": root, "alternatives": [h[1] for h in hits[1:4]]},
                                    ["符号自动匹配"] if act == "auto_fill" else ["符号待复核"]))
                break
        return out

    def annotate(self, props: list[Proposal]):
        """给符号/封装建议标出 ezPLM ID；ezPLM 没有的打"需新建"（不改建议本身，只加证据和标签）。"""
        for pr in props:
            if pr.field == "symbol" and not pr.evidence.get("ezplm_id"):
                hit = self.sym_by_name.get(pr.proposed)
            elif pr.field == "footprint" and not pr.evidence.get("ezplm_id"):
                hit = self.fp_by_name.get(pr.proposed)
            else:
                continue
            if hit:
                pr.evidence["ezplm_id"] = hit[0]
            else:
                pr.evidence["ezplm_new"] = True
                if "需新建" not in pr.tags:
                    pr.tags = list(pr.tags) + ["需新建"]


def company_aliases(db) -> list[tuple[str, str, bool]]:
    """[(别名或名称, ezPLM 厂商名, 是否只是代理商)]，给 Lineage 用。"""
    out = []
    for name, aliases, is_m, is_s in db.execute(select(EzCompany.name, EzCompany.aliases_json, EzCompany.is_manufacturer, EzCompany.is_supplier)):
        only_supplier = bool(is_s) and not bool(is_m)
        try:
            al = json.loads(aliases or "[]")
        except ValueError:
            al = []
        for a in [name] + [x for x in al if x]:
            out.append((a, name, only_supplier))
    return out
