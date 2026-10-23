"""暂存平台流水线：ingest（拉取）→ process（处理）→ review（复核）→ export（回写包）。

全部分批、可中断续跑：
- ingest  : 从 ezPLM API（或 CSV/样本）按页拉取，游标存 sync_state；已存在的物料只刷新 orig_*，不动 new_*
- process : 取 processed_at 为空的物料，按价值分从高到低，每批 N 条跑分类/封装/符号/URL，建议入 proposals，
            auto_fill 直接写 new_*；其余等复核
- review  : approve → 写 new_*；reject → 记录；都写 reviews 字段，作为后续规则/近邻的训练样本
- export  : 把 new_* 与 orig_* 不同且未回写的物料打成回写包（JSONL），--apply 时经 ezPLM API PATCH 写回并标记 exported_at
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import func, select, or_

from .models import Part, Proposal
from .staging import StagedPart, StagedProposal, StagedCategory, SyncState, JobLog, get_session
from .taxonomy import Taxonomy, Node
from .gap import value_score
from .ihs import IhsMatcher, norm_mpn

PROCESS_VERSION = "0.2"


# --------------------------------------------------------------------------- helpers
def _state(db, key, default=None):
    row = db.get(SyncState, key)
    return row.value if row else default


def _set_state(db, key, value):
    row = db.get(SyncState, key)
    if row:
        row.value = str(value)
    else:
        db.add(SyncState(key=key, value=str(value)))


def load_taxonomy(db) -> Taxonomy:
    rows = db.execute(select(StagedCategory)).scalars().all()
    return Taxonomy(Node(r.id, r.name, r.parent or "", r.depth or 0) for r in rows)


def _to_part(sp: StagedPart) -> Part:
    return Part(id=sp.id, mpn=sp.mpn or "", manufacturer=sp.manufacturer or sp.new_manufacturer or "", manufacturer_id=sp.manufacturer_id or "",
                category=sp.new_category or sp.orig_category or "", category_id=sp.new_category_id or sp.orig_category_id or "",
                symbol=sp.new_symbol or sp.orig_symbol or "", symbol_lib=sp.orig_symbol_lib or "",
                footprint=sp.new_footprint or sp.orig_footprint or "", footprint_lib=sp.orig_footprint_lib or "",
                has_pdf=bool(sp.orig_has_pdf), has_image=bool(sp.orig_has_image), url=sp.new_url or sp.orig_url or "",
                description=sp.description or "", description_en=sp.new_description_en or sp.description_en or "", status=sp.status or "")


def _add_tags(sp: StagedPart, tags: Iterable[str]):
    cur = [t for t in (sp.tags or "").split(",") if t]
    for t in tags:
        if t and t not in cur:
            cur.append(t)
    sp.tags = ",".join(cur)


FIELD_TO_NEW = {"category_id": "new_category_id", "symbol": "new_symbol", "footprint": "new_footprint", "url": "new_url", "datasheet": "new_datasheet",
                "description_en": "new_description_en", "manufacturer": "new_manufacturer"}
# 回写字段（ezPLM PATCH set 的键）→ 暂存列。回写前逐条核对 ezPLM 当前值，只填空
EXPORT_FIELDS = (("categoryId", "new_category_id"), ("symbol", "new_symbol"), ("footprint", "new_footprint"), ("url", "new_url"),
                 ("datasheetUrl", "new_datasheet"), ("descriptionEn", "new_description_en"), ("manufacturer", "new_manufacturer"))


def apply_proposal(db, sp: StagedPart, pr: StagedProposal, tax: Taxonomy | None = None):
    setattr(sp, FIELD_TO_NEW[pr.field], pr.proposed)
    if pr.field == "category_id" and tax and pr.proposed in tax.nodes:
        sp.new_category = tax.nodes[pr.proposed].name
    _add_tags(sp, (pr.tags or "").split(","))


# --------------------------------------------------------------------------- ingest
def ingest(source, batch: int = 500, max_batches: int | None = None, job: str | None = None, reset: bool = False,
           stop=None, log=None, progress=None, incremental: bool = False) -> dict:
    """source: sources.SampleSource / CsvSource / ApiSource。ApiSource 走游标续跑。
    stop(): 返回 True 时在当前批次后停止；log(msg) 日志回调；progress(dict) 进度回调。"""
    log = log or (lambda m: None)
    db = get_session()
    job = job or f"ingest-{datetime.utcnow():%Y%m%d-%H%M}"
    # 分类树
    tax = source.load_taxonomy()
    for n in tax.nodes.values():
        db.merge(StagedCategory(id=n.id, parent=n.parent or None, depth=n.depth, name=n.name))
    db.commit()
    try:                                       # 新分类自动挂上内置关键参数建议
        from .params import seed_load
        st = seed_load(db, load_taxonomy(db))
        if st["seeded"]:
            log(f"已为 {st['seeded']} 个分类挂上内置关键参数建议")
    except Exception as e:  # noqa
        log(f"内置参数挂载跳过：{str(e)[:100]}")
    if reset:
        _set_state(db, "ingest:page", "1"); db.commit()

    n_total, n_new, n_changed, batch_no, buf = 0, 0, 0, 0, []
    start_page = 1 if incremental else int(_state(db, "ingest:page", "1"))
    if incremental:
        log("只拉新增模式：从第 1 页开始，遇到整批都已存在即停止")
    it = source.iter_parts(start_page=start_page) if hasattr(source, "iter_parts") and "start_page" in source.iter_parts.__code__.co_varnames else source.iter_parts()

    def flush():
        nonlocal buf, batch_no, n_new, n_changed
        if not buf:
            return 0
        batch_no += 1
        ids = [p.id for p in buf]
        existing = {r.id: r for r in db.execute(select(StagedPart).where(StagedPart.id.in_(ids))).scalars()}
        batch_new = 0
        for p in buf:
            sp = existing.get(p.id)
            if sp is None:
                sp = StagedPart(id=p.id); db.add(sp); n_new += 1; batch_new += 1
            else:
                # 原值变更检测：ezPLM 那边已经填上、而平台也给了新值 → 打标签提示复核
                changed = []
                for fld, orig_col, new_col, val in (("分类", "orig_category_id", "new_category_id", p.category_id), ("符号", "orig_symbol", "new_symbol", p.symbol),
                                                    ("封装", "orig_footprint", "new_footprint", p.footprint), ("链接", "orig_url", "new_url", p.url)):
                    if val and val != (getattr(sp, orig_col) or "") and getattr(sp, new_col):
                        changed.append(fld)
                if changed:
                    _add_tags(sp, ["原值已变更"]); n_changed += 1
            sp.mpn, sp.manufacturer, sp.manufacturer_id = p.mpn, p.manufacturer, p.manufacturer_id
            sp.mpn_norm = norm_mpn(p.mpn)
            sp.description, sp.description_en, sp.status = p.description, p.description_en, p.status
            sp.orig_category_id, sp.orig_category = p.category_id, p.category
            sp.orig_symbol, sp.orig_symbol_lib, sp.orig_footprint, sp.orig_footprint_lib = p.symbol, p.symbol_lib, p.footprint, p.footprint_lib
            sp.orig_has_pdf, sp.orig_has_image, sp.orig_url = p.has_pdf, p.has_image, p.url
            if getattr(p, "attributes", None):                 # 列表接口不带属性：重新拉取时别把处理时从详情接口补来的属性清掉
                sp.attributes_json = json.dumps(p.attributes, ensure_ascii=False)
            elif sp.attributes_json is None:
                sp.attributes_json = "{}"
            sp.value_score = value_score(p)
            sp.fetched_at = datetime.utcnow()
        db.add(JobLog(job=job, stage="ingest", batch_no=batch_no, n_items=len(buf), finished_at=datetime.utcnow()))
        if getattr(source, "current_page", None) and not incremental:
            _set_state(db, "ingest:page", source.current_page)
        db.commit()
        log(f"批次 {batch_no}: 写入 {len(buf)} 条（新 {batch_new}，累计新 {n_new}，原值变更 {n_changed}）" + ("" if incremental else f"；下次从第 {_state(db, 'ingest:page', '1')} 页续"))
        if progress:
            progress({"done": n_total, "total": getattr(source, "total", 0) or 0})
        buf = []
        return batch_new

    for p in it:
        buf.append(p); n_total += 1
        if len(buf) >= batch:
            bn = flush()
            if incremental and bn == 0:
                log("整批都已存在，新增同步完成"); break
            if (max_batches and batch_no >= max_batches) or (stop and stop()):
                break
    flush()
    out = {"job": job, "fetched": n_total, "new": n_new, "orig_changed": n_changed, "batches": batch_no,
           "parts_in_staging": db.execute(select(func.count(StagedPart.id))).scalar()}
    db.close()
    return out


# --------------------------------------------------------------------------- process
def _trusted_sample(db, per_category: int = 300) -> list[Part]:
    """近邻库：每个分类最多取 N 条"可信"物料（有分类且有 PDF 或符号/封装或非模板描述）。"""
    out: list[Part] = []
    cats = db.execute(select(StagedPart.orig_category_id, func.count()).where(StagedPart.orig_category_id != "").group_by(StagedPart.orig_category_id)).all()
    for cid, n in cats:
        q = select(StagedPart).where(StagedPart.orig_category_id == cid).order_by(StagedPart.value_score.desc()).limit(per_category)
        out += [_to_part(r) for r in db.execute(q).scalars()]
    return out


def process(batch: int = 200, max_batches: int | None = None, use_llm: bool = False, do_urls: bool = False,
            job: str | None = None, libindex: str | Path | None = None, llm_call=None, stop=None, log=None, progress=None,
            auto_fill_min: float = 0.85, flag_min: float = 0.85, fetch_details: bool = True) -> dict:
    from .classify import Classifier, Policy
    from .packages import PackageMapper
    from .urls import UrlFiller
    from .kicad import LibIndex, propose_symbols, propose_footprints_by_mpn

    from .settings import LIBINDEX_PATH, apply_env
    apply_env()                      # 把设置里的 LLM 提供商/密钥放进环境变量
    log = log or (lambda m: None)
    libindex = libindex or LIBINDEX_PATH
    db = get_session()
    job = job or f"process-{datetime.utcnow():%Y%m%d-%H%M}"
    tax = load_taxonomy(db)
    log("构建近邻库（每分类最多 300 条可信物料）…")
    trusted = _trusted_sample(db)
    clf = Classifier(tax, policy=Policy(use_llm=use_llm, auto_fill_min=auto_fill_min, flag_min=flag_min), llm_call=llm_call).fit(trusted)
    if clf.llm:
        log(f"LLM 提供商 {clf.llm.provider} / 模型 {clf.llm.model}")
    log(f"近邻库 {len(trusted)} 条；LLM {'开' if (clf.llm and clf.llm.available) else '关'}；符号索引 {'有' if Path(libindex).exists() else '无'}")
    todo_total = db.execute(select(func.count(StagedPart.id)).where(StagedPart.processed_at.is_(None))).scalar() or 0
    pkg = PackageMapper(tax)
    urlf = UrlFiller() if do_urls else None
    idx = LibIndex(libindex) if Path(libindex).exists() else None
    ihs = IhsMatcher(db, tax=tax, pkg=pkg, auto_min=auto_fill_min)
    log(f"IHS 参考记录 {IhsMatcher.n_ref(db)} 条（无记录时只做厂商谱系提示）")
    from .kicad import SymbolMatcher
    from .ezplm_lib import EzLibrary
    sym_matcher = SymbolMatcher(idx) if idx else None
    ezlib = EzLibrary(db)
    log("ezPLM 库：" + ("未拉取（符号/封装建议不标是否已在 ezPLM）" if ezlib.empty else f"符号 {len(ezlib.sym_by_name)} / 封装 {len(ezlib.fp_by_name)}；厂商标准 {len(ihs.lineage.ez_alias)} 个名称/别名"))

    stats = {"job": job, "batches": 0, "parts": 0, "proposals": 0, "auto_fill": 0, "flag_mismatch": 0, "review": 0}
    while True:
        t0 = time.perf_counter()
        timing: dict = {}                    # 本批各步骤耗时（秒），写进日志，用来找慢在哪
        rows = db.execute(select(StagedPart).where(StagedPart.processed_at.is_(None)).order_by(StagedPart.value_score.desc(), StagedPart.id).limit(batch)).scalars().all()
        timing["取批"] = time.perf_counter() - t0
        if not rows:
            break
        stats["batches"] += 1
        parts = [_to_part(r) for r in rows]
        by_id = {r.id: r for r in rows}
        attrs = {}
        need_detail = [r for r in rows if not (r.new_footprint or r.orig_footprint) and (r.attributes_json or "{}") in ("{}", "")]
        t1 = time.perf_counter()
        if need_detail and fetch_details:
            try:
                from .settings import api_source
                api = api_source()
                from concurrent.futures import ThreadPoolExecutor
                def grab(r):
                    try:
                        d = api.get_part_detail(r.id)
                        return r.id, d.get("attributes") or {}, d.get("notes") or ""
                    except Exception as e:  # noqa
                        return r.id, None, str(e)[:100]
                with ThreadPoolExecutor(6) as ex:
                    for pid, a, notes in ex.map(grab, need_detail):
                        if a is not None:
                            by_id[pid].attributes_json = json.dumps(a, ensure_ascii=False)
                            if notes and not by_id[pid].notes:
                                by_id[pid].notes = notes
                log(f"  拉取物料属性 {len(need_detail)} 条")
            except Exception as e:  # noqa
                log(f"  ! 拉取物料属性失败：{str(e)[:120]}")
        if need_detail and fetch_details:
            timing["拉属性"] = time.perf_counter() - t1
        for r in rows:
            try:
                a = json.loads(r.attributes_json or "{}")
                if isinstance(a, list):          # 属性可能是 [{name, value}] 列表
                    a = {x.get("name") or x.get("key") or "": x.get("value") for x in a if isinstance(x, dict)}
                attrs[r.id] = next((v for k, v in a.items() if k and ("封装" in k or k.lower() in ("package", "package / case", "封装/外壳"))), "") or ""
            except Exception:
                pass
        props: list[Proposal] = []
        # IHS 先匹配：缺描述的物料用 IHS 描述补进分类输入（只作分类依据，写不写描述仍走 IHS 建议）
        t1 = time.perf_counter()
        try:
            ihs_props = ihs.propose(parts)
        except Exception as e:  # noqa
            log(f"  ! IHS模块出错，本批跳过该模块：{str(e)[:200]}")
            ihs_props, ihs.best = [], None                    # None：本批不改物料上已记的 IHS 匹配（{} 会把它们清空）
        timing["IHS"] = time.perf_counter() - t1
        clf_parts, ihs_desc = _with_ihs_desc(parts, ihs.best or {})
        for name, fn in (("分类", lambda: _tag_ihs_desc(clf.classify_many(clf_parts), ihs_desc)), ("封装(MPN映射)", lambda: list(propose_footprints_by_mpn(parts, idx)) if idx else []),
                         ("封装", lambda: list(pkg.propose(parts, attrs))),
                         ("ezPLM 符号", lambda: ezlib.propose_symbols_by_mpn(parts, auto_fill_min)),
                         ("符号", lambda: list(propose_symbols([p for p in parts if p.id not in {x.part_id for x in props if x.field == "symbol"}], idx, matcher=sym_matcher)) if idx else []),
                         ("链接", lambda: list(urlf.propose(parts)) if urlf else []),
                         ("IHS 去重", lambda: _ihs_props(ihs_props, by_id, props, ihs.best))):
            t1 = time.perf_counter()
            try:
                props += fn()
            except Exception as e:  # noqa  —— 单个模块出错不影响本批其它模块
                log(f"  ! {name}模块出错，本批跳过该模块：{str(e)[:200]}")
            timing[name] = time.perf_counter() - t1
        if not ezlib.empty:
            ezlib.annotate(props)                # 符号/封装建议标出 ezPLM ID，或打"需新建"
        t1 = time.perf_counter()
        # 重跑时作废上一轮同物料、同字段的待复核建议，避免重复
        old = db.execute(select(StagedProposal).where(StagedProposal.part_id.in_(list(by_id)), StagedProposal.review_status == "pending",
                                                       _regenerated())).scalars().all()
        for o in old:
            o.review_status = "rejected"; o.review_note = "superseded by reprocess"
        by_field = _save_proposals(db, props, by_id, job, tax, stats)
        for r in rows:
            r.processed_at = datetime.utcnow(); r.process_version = PROCESS_VERSION
        stats["parts"] += len(rows)
        db.add(JobLog(job=job, stage="process", batch_no=stats["batches"], n_items=len(rows), finished_at=datetime.utcnow(),
                      detail=json.dumps({k: v for k, v in stats.items() if k != "job"})))
        db.commit()
        timing["写库"] = time.perf_counter() - t1
        took = " ".join(f"{k}{v:.1f}" for k, v in timing.items() if v >= 0.05)
        log(f"批次 {stats['batches']}: {len(rows)} 条 → 建议 {stats['proposals']}（自动 {stats['auto_fill']} / 疑似错误 {stats['flag_mismatch']} / 复核 {stats['review']}）本批按字段 {by_field}"
            + (f"；LLM 累计 {clf.llm.n_calls} 次/失败 {clf.llm.n_errors}" if clf.llm else "")
            + f"\n    耗时 {time.perf_counter() - t0:.1f}s：{took}")
        if progress:
            progress({"done": stats["parts"], "total": todo_total})
        if (max_batches and stats["batches"] >= max_batches) or (stop and stop()):
            break
    if clf.llm:
        stats["llm_calls"] = clf.llm.n_calls
        stats["llm_errors"] = clf.llm.n_errors
        if clf.llm.n_errors:
            log(f"  ! LLM 调用失败 {clf.llm.n_errors}/{clf.llm.n_calls} 次，最后一次错误：{clf.llm.last_error}")
    db.close()
    return stats


def _with_ihs_desc(parts: list[Part], best: dict) -> tuple[list[Part], set]:
    """分类输入：中英文描述都空、且 IHS 有可信匹配（非 L5、置信度 ≥ 0.6）的物料，借用 IHS 描述。返回 (分类用物料, 借用了的物料 ID)。"""
    out, used = [], set()
    for p in parts:
        b = best.get(p.id)
        if b and not (p.description or p.description_en) and b[1] != "L5" and b[2] >= 0.6 and b[0].description:
            p = replace(p, description_en=b[0].description)
            used.add(p.id)
        out.append(p)
    return out, used


def _tag_ihs_desc(props: list[Proposal], used: set) -> list[Proposal]:
    for p in props:
        if p.part_id in used:
            p.evidence["ihs_desc"] = True
            p.tags = [*p.tags, "依据IHS描述"]
    return props


def _ihs_props(ihs_props: list[Proposal], by_id: dict, existing: list[Proposal], best: dict) -> list[Proposal]:
    """IHS 建议；与本批其它模块重复（同物料、同字段、同建议值）的不再重复出。顺带记下每个物料的最佳 IHS 匹配。"""
    for sp in by_id.values():
        if sp.mpn_norm is None:
            sp.mpn_norm = norm_mpn(sp.mpn)
    seen = {(p.part_id, p.field, p.proposed) for p in existing}
    out = [p for p in ihs_props if (p.part_id, p.field, p.proposed) not in seen]
    if best is None:                                  # IHS 模块本批出错：保留物料原有的 IHS 匹配
        return out
    for pid, sp in by_id.items():
        b = best.get(pid)
        sp.ihs_objid, sp.ihs_match = (b[0].objid, b[1]) if b else (None, None)
    return out


def _save_proposals(db, props: list[Proposal], by_id: dict, job: str, tax, stats: dict) -> dict:
    by_field: dict = {}
    for pr in props:
        if pr.action in ("ok", "skip"):
            if pr.action == "skip":
                _add_tags(by_id[pr.part_id], pr.tags)
            continue
        by_field[pr.field] = by_field.get(pr.field, 0) + 1
        sp = by_id[pr.part_id]
        rec = StagedProposal(part_id=pr.part_id, mpn=pr.mpn, field=pr.field, current=pr.current, proposed=pr.proposed,
                             proposed_label=pr.proposed_label, confidence=pr.confidence, action=pr.action, method=pr.method,
                             evidence_json=json.dumps(pr.evidence, ensure_ascii=False), tags=",".join(pr.tags), job=job,
                             review_status="auto" if pr.action == "auto_fill" else "pending")
        db.add(rec)
        stats["proposals"] += 1
        stats[pr.action] = stats.get(pr.action, 0) + 1
        if pr.action == "auto_fill":
            apply_proposal(db, sp, rec, tax)
        else:
            _add_tags(sp, pr.tags)
    return by_field


def ihs_match(batch: int = 500, job: str | None = None, only_matched: bool = True, stop=None, log=None, progress=None,
              auto_fill_min: float = 0.85) -> dict:
    """只跑 IHS 模块（不重置处理状态）：导入 IHS 数据后，给已处理过的物料补出 IHS 建议。
    重跑时作废这些物料上一轮 IHS / 厂商谱系的待复核建议。only_matched=True 时只看型号在 ihs_ref 里出现的物料。"""
    from .packages import PackageMapper
    from .staging import IhsRef
    log = log or (lambda m: None)
    db = get_session()
    job = job or f"ihs-{datetime.utcnow():%Y%m%d-%H%M}"
    tax = load_taxonomy(db)
    ihs = IhsMatcher(db, tax=tax, pkg=PackageMapper(tax), auto_min=auto_fill_min)
    from .ihs import ensure_mpn_norm
    ensure_mpn_norm(db)
    base = select(StagedPart)
    if only_matched:
        base = base.where(StagedPart.mpn_norm.in_(select(IhsRef.mpn_norm)))
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    log(f"IHS 参考记录 {IhsMatcher.n_ref(db)} 条；待匹配物料 {total} 条")
    stats = {"job": job, "batches": 0, "parts": 0, "proposals": 0, "auto_fill": 0, "flag_mismatch": 0, "review": 0, "matched": 0}
    last = ""
    while True:
        rows = db.execute(base.where(StagedPart.id > last).order_by(StagedPart.id).limit(batch)).scalars().all()
        if not rows:
            break
        last = rows[-1].id
        stats["batches"] += 1
        by_id = {r.id: r for r in rows}
        for o in db.execute(select(StagedProposal).where(StagedProposal.part_id.in_(list(by_id)), StagedProposal.review_status == "pending",
                                                         or_(StagedProposal.method.like("ihs%"), StagedProposal.method == "mfr-lineage"))).scalars():
            o.review_status = "rejected"; o.review_note = "superseded by ihs-match"
        props = _ihs_props(ihs.propose([_to_part(r) for r in rows]), by_id, [], ihs.best)
        by_field = _save_proposals(db, props, by_id, job, tax, stats)
        stats["parts"] += len(rows)
        stats["matched"] += len(ihs.best)
        db.add(JobLog(job=job, stage="ihs", batch_no=stats["batches"], n_items=len(rows), finished_at=datetime.utcnow(),
                      detail=json.dumps({k: v for k, v in stats.items() if k != "job"})))
        db.commit()
        log(f"批次 {stats['batches']}: {len(rows)} 条，命中 IHS {len(ihs.best)} → 建议 {sum(by_field.values())} {by_field}")
        if progress:
            progress({"done": stats["parts"], "total": total})
        if stop and stop():
            break
    db.close()
    return stats


# 不是 ② 处理生成的建议（三批符号导入、符号仓库匹配、开源工程、通用符号）：重新处理不会再生成，所以重跑时不能作废
EXTERNAL_METHODS = ("symset", "symmatch", "oshw", "generic-symbol")


def _regenerated():
    """重新处理会重新生成的建议（作废旧建议时只动这些）。"""
    return ~or_(*[StagedProposal.method.like(m + "%") for m in EXTERNAL_METHODS])


def reset_parts_with_pending(field: str = "category_id") -> int:
    """规则改了以后只重跑受影响的物料：把还有该字段待复核建议的物料标成未处理（其待复核建议随后由 void_stale_pending 作废，
    重新处理时按新规则重新生成）。比"重置处理状态"全量重跑快得多。"""
    from sqlalchemy import update
    db = get_session()
    ids = select(StagedProposal.part_id).where(StagedProposal.review_status == "pending", StagedProposal.field == field)
    n = db.execute(update(StagedPart).where(StagedPart.id.in_(ids), StagedPart.processed_at.isnot(None)).values(processed_at=None),
                   execution_options={"synchronize_session": False}).rowcount
    db.commit()
    db.close()
    return n or 0


def recheck_footprints(stop=None, log=None, progress=None) -> dict:
    """按新的封装把关规则复查已有的封装建议（描述映射 / IHS 封装）：
    - 引脚数对不上（"SOT-23-6封装"被映射成 3 脚 SOT-23）、阵列/排阻配了单个片式封装 → 待复核的驳回；已自动写入的撤销
    - 这些物料标成未处理，② 处理时按新规则重新出建议
    另外把重跑时被误作废的导入建议（三批符号、符号仓库匹配、开源工程、通用符号）恢复成待复核。"""
    from sqlalchemy import update
    from .packages import footprint_conflict
    from .staging import IhsRef
    log = log or (lambda m: None)
    db = get_session()
    q = select(StagedProposal).where(StagedProposal.field == "footprint", StagedProposal.review_status.in_(("auto", "pending")),
                                     or_(StagedProposal.method.like("package-map%"), StagedProposal.method.like("ihs%")))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    st = {"checked": 0, "rejected": 0, "reverted": 0, "parts_reset": 0, "restored": 0}
    last = 0
    while not (stop and stop()):
        prs = db.execute(q.where(StagedProposal.id > last).order_by(StagedProposal.id).limit(2000)).scalars().all()
        if not prs:
            break
        last = prs[-1].id
        parts = {sp.id: sp for sp in db.execute(select(StagedPart).where(StagedPart.id.in_({x.part_id for x in prs}))).scalars()}
        evs = {x.id: json.loads(x.evidence_json or "{}") for x in prs}
        objids = {e.get("ihs_objid") for e in evs.values() if e.get("ihs_objid")}
        ihs = {r.objid: r for r in db.execute(select(IhsRef).where(IhsRef.objid.in_(objids))).scalars()} if objids else {}
        reset = set()
        for x in prs:
            sp = parts.get(x.part_id)
            if not sp:
                continue
            e = evs[x.id]
            r = ihs.get(e.get("ihs_objid"))
            text = " ".join(filter(None, [e.get("package_text"), r and r.package, r and r.description, sp.description, sp.description_en]))
            why = footprint_conflict(x.proposed_label or x.proposed, text)
            st["checked"] += 1
            if not why:
                continue
            if x.review_status == "auto":
                if sp.new_footprint == x.proposed:
                    sp.new_footprint = None
                st["reverted"] += 1
            else:
                st["rejected"] += 1
            x.review_status, x.review_note, x.reviewed_at = "rejected", f"recheck: {why}", datetime.utcnow()
            reset.add(sp.id)
        for pid in reset:
            parts[pid].processed_at = None
        st["parts_reset"] += len(reset)
        db.commit()
        log(f"  已复查 {st['checked']}/{total}：驳回 {st['rejected']}，撤销自动写入 {st['reverted']}")
        if progress:
            progress({"done": st["checked"], "total": total})
    # 重跑时被误作废的导入建议 → 恢复待复核（作废原因是"重新处理"或"未处理物料上的旧建议"）
    n = db.execute(update(StagedProposal).where(~_regenerated(), StagedProposal.review_status == "rejected",
                                                StagedProposal.review_note.in_(("superseded by reprocess", "stale: part unprocessed")))
                   .values(review_status="pending", review_note=None, reviewed_at=None), execution_options={"synchronize_session": False}).rowcount
    st["restored"] = n or 0
    db.commit()
    db.close()
    log(f"完成：{st}")
    return st


def reset_parts_ihs_lost() -> int:
    """型号在 IHS 参考库里、却没有记下 IHS 匹配的已处理物料 → 标成未处理（IHS 模块出错的那轮处理留下的），重新处理时补回。"""
    from sqlalchemy import update
    from .staging import IhsRef
    db = get_session()
    n = db.execute(update(StagedPart).where(StagedPart.processed_at.isnot(None), StagedPart.ihs_objid.is_(None),
                                            StagedPart.mpn_norm.in_(select(IhsRef.mpn_norm)))
                   .values(processed_at=None), execution_options={"synchronize_session": False}).rowcount
    db.commit()
    db.close()
    return n or 0


def void_stale_pending() -> int:
    """作废"未处理"物料上的待复核建议：它们是旧规则留下的（物料被重置或从未按当前规则处理），重新处理时会重新生成。"""
    from sqlalchemy import update
    db = get_session()
    stale = select(StagedPart.id).where(StagedPart.processed_at.is_(None))
    n = db.execute(update(StagedProposal).where(StagedProposal.review_status == "pending", StagedProposal.part_id.in_(stale), _regenerated())
                   .values(review_status="rejected", review_note="stale: part unprocessed", reviewed_at=datetime.utcnow()),
                   execution_options={"synchronize_session": False}).rowcount
    db.commit()
    db.close()
    return n or 0


def dedupe_pending(log=None) -> int:
    """同一物料、同一字段、同一建议值的多条待复核建议只留最新一条，其余作废。
    （两个实例同时处理同一批物料时会出现，见 jobs.py 的跨实例锁。）"""
    from sqlalchemy import update
    log = log or (lambda m: None)
    db = get_session()
    rows = db.execute(select(StagedProposal.id, StagedProposal.part_id, StagedProposal.field, StagedProposal.proposed)
                      .where(StagedProposal.review_status == "pending").order_by(StagedProposal.id.desc())).all()
    seen, dup = set(), []
    for pid_, part_id, field, proposed in rows:
        k = (part_id, field, proposed)
        if k in seen:
            dup.append(pid_)
        else:
            seen.add(k)
    for i in range(0, len(dup), 1000):
        db.execute(update(StagedProposal).where(StagedProposal.id.in_(dup[i:i + 1000]))
                   .values(review_status="rejected", review_note="duplicate", reviewed_at=datetime.utcnow()))
    db.commit()
    db.close()
    log(f"待复核 {len(rows)} 条，作废重复 {len(dup)} 条")
    return len(dup)


# --------------------------------------------------------------------------- review
def review(proposal_id: int, decision: str, user: str = "admin", note: str = "") -> StagedProposal:
    db = get_session()
    pr = db.get(StagedProposal, proposal_id)
    if not pr:
        raise KeyError(proposal_id)
    sp = db.get(StagedPart, pr.part_id)
    pr.review_status = "approved" if decision == "approve" else "rejected"
    pr.reviewed_by, pr.reviewed_at, pr.review_note = user, datetime.utcnow(), note
    if decision == "approve":
        apply_proposal(db, sp, pr, load_taxonomy(db))
        # 同一字段的其它待复核建议自动作废
        for other in db.execute(select(StagedProposal).where(StagedProposal.part_id == pr.part_id, StagedProposal.field == pr.field,
                                                             StagedProposal.id != pr.id, StagedProposal.review_status == "pending")).scalars():
            other.review_status = "rejected"; other.review_note = "superseded"
    elif decision == "revert":          # 撤销一次自动写入
        pr.review_status = "rejected"
        setattr(sp, FIELD_TO_NEW[pr.field], None)
        if pr.field == "category_id":
            sp.new_category = None
    db.commit()
    db.close()
    return pr


# --------------------------------------------------------------------------- 分组复核
NO_RULE = "__none__"                                 # 分组复核：没有规则参与（只靠近邻 / 大模型）的建议


def rule_filter(stmt, rule: str | None):
    """按给出建议的规则原文筛选（规则记在 evidence_json 的 "rule" 里）；NO_RULE = 没有规则参与的。"""
    if rule is None:
        return stmt
    if rule == NO_RULE:
        return stmt.where(~StagedProposal.evidence_json.contains('"rule": ', autoescape=True))
    return stmt.where(StagedProposal.evidence_json.contains(json.dumps({"rule": rule}, ensure_ascii=False)[1:-1], autoescape=True))


def _group_filter(stmt, field: str, current: str | None, proposed: str | None, action: str = "", minconf: float | None = None, rule: str | None = None):
    stmt = rule_filter(stmt, rule)
    stmt = stmt.where(StagedProposal.review_status == "pending", StagedProposal.field == field)
    if current is not None:
        stmt = stmt.where(or_(StagedProposal.current == "", StagedProposal.current.is_(None)) if current == "" else StagedProposal.current == current)
    if proposed is not None:
        stmt = stmt.where(StagedProposal.proposed == proposed)
    if action:
        stmt = stmt.where(StagedProposal.action == action)
    if minconf is not None:
        stmt = stmt.where(StagedProposal.confidence >= minconf)
    return stmt


def review_groups_by_rule(db, field: str = "category_id", action: str = "", minconf: float | None = None, limit: int = 300) -> tuple[list[dict], int]:
    """待复核建议按"给出它的规则 → 建议值"分组（一条规则对就整组通过，错就整组驳回并修规则）。在 Python 里汇总 evidence。"""
    rows = db.execute(_group_filter(select(StagedProposal.current, StagedProposal.proposed, StagedProposal.action, StagedProposal.confidence,
                                           StagedProposal.evidence_json), field, None, None, action, minconf)).all()
    agg: dict = {}
    for cur, prop, act, conf, ev in rows:
        try:
            rule = json.loads(ev or "{}").get("rule") or NO_RULE
        except ValueError:
            rule = NO_RULE
        k = (cur or "", prop, act, rule)
        g = agg.setdefault(k, {"current": k[0], "proposed": prop, "action": act, "rule": rule, "n": 0, "sum": 0.0, "min": 1.0, "max": 0.0})
        c = float(conf or 0)
        g["n"] += 1; g["sum"] += c; g["min"] = min(g["min"], c); g["max"] = max(g["max"], c)
    groups = sorted(agg.values(), key=lambda g: -g["n"])
    for g in groups:
        g["avg"] = g.pop("sum") / g["n"]
    return groups[:limit], len(groups)


def review_groups(db, field: str = "category_id", action: str = "", minconf: float | None = None, limit: int = 300) -> tuple[list[dict], int]:
    """待复核建议按"现值 → 建议值"分组，条数多的在前。返回 (前 limit 组, 总组数)。"""
    cur = func.coalesce(StagedProposal.current, "")
    base = _group_filter(select(cur.label("cur"), StagedProposal.proposed, StagedProposal.action, func.count().label("n"),
                                func.avg(StagedProposal.confidence), func.min(StagedProposal.confidence), func.max(StagedProposal.confidence)),
                         field, None, None, action, minconf).group_by(cur, StagedProposal.proposed, StagedProposal.action)
    n_groups = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    rows = db.execute(base.order_by(func.count().desc()).limit(limit)).all()
    return [{"current": c, "proposed": p, "action": a, "n": n, "avg": float(avg or 0), "min": float(mn or 0), "max": float(mx or 0)}
            for c, p, a, n, avg, mn, mx in rows], n_groups


def review_group(field: str, current: str, proposed: str, decision: str, action: str = "", minconf: float | None = None, user: str = "admin",
                 stop=None, log=None, progress=None, rule: str | None = None) -> dict:
    """整组通过 / 驳回。通过时写新值，并作废同物料同字段的其它待复核建议；驳回记为 group-rejected（可作为规则改进的样本）。"""
    if decision not in ("approve", "reject"):
        raise ValueError(decision)
    log = log or (lambda m: None)
    db = get_session()
    tax = load_taxonomy(db)
    total = db.execute(select(func.count()).select_from(_group_filter(select(StagedProposal.id), field, current, proposed, action, minconf, rule).subquery())).scalar() or 0
    done, now = 0, datetime.utcnow()
    while True:
        prs = db.execute(_group_filter(select(StagedProposal), field, current, proposed, action, minconf, rule).order_by(StagedProposal.id).limit(1000)).scalars().all()
        if not prs:
            break
        parts = {sp.id: sp for sp in db.execute(select(StagedPart).where(StagedPart.id.in_({p.part_id for p in prs}))).scalars()}
        for pr in prs:
            pr.review_status = "approved" if decision == "approve" else "rejected"
            pr.reviewed_by, pr.reviewed_at, pr.review_note = user, now, f"group-{decision}"
            if decision == "approve" and pr.part_id in parts:
                apply_proposal(db, parts[pr.part_id], pr, tax)
        if decision == "approve":
            for other in db.execute(select(StagedProposal).where(StagedProposal.part_id.in_(list(parts)), StagedProposal.field == field,
                                                                 StagedProposal.review_status == "pending")).scalars():
                other.review_status, other.review_note = "rejected", "superseded"
        db.commit()
        done += len(prs)
        log(f"已{'通过' if decision == 'approve' else '驳回'} {done}/{total}")
        if progress:
            progress({"done": done, "total": total})
        if stop and stop():
            break
    db.close()
    return {"decision": decision, "field": field, "rule": rule, "done": done, "total": total}


REVIEW_FILTERS = ("field", "action", "minconf", "current", "proposed", "rule", "mfr", "cat", "method", "tag")


def review_stmt(cols, status: str = "pending", field: str = "", action: str = "", minconf=None, current=None, proposed=None,
                rule=None, mfr=None, cat=None, method=None, tag=None):
    """复核队列的筛选（列表页和"按筛选整批通过/驳回"共用一套条件）。
    mfr：厂商名包含；cat：物料分类（新分类或原分类）等于。"""
    stmt = select(*cols).join(StagedPart, StagedPart.id == StagedProposal.part_id).where(StagedProposal.review_status == status,
                                                                                        StagedProposal.field != "manual")
    if field:
        stmt = stmt.where(StagedProposal.field == field)
    if action:
        stmt = stmt.where(StagedProposal.action == action)
    if minconf not in (None, ""):
        stmt = stmt.where(StagedProposal.confidence >= float(minconf))
    if current is not None:
        stmt = stmt.where(or_(StagedProposal.current == "", StagedProposal.current.is_(None)) if current == "" else StagedProposal.current == current)
    if proposed is not None:
        stmt = stmt.where(StagedProposal.proposed == proposed)
    if mfr:
        stmt = stmt.where(StagedPart.manufacturer.ilike(f"%{mfr}%"))
    if cat:
        stmt = stmt.where(func.coalesce(StagedPart.new_category_id, StagedPart.orig_category_id) == cat)
    if method:                                   # 方法前缀，如 kicad:kicad-official:wildcard、generic-symbol、symset
        stmt = stmt.where(StagedProposal.method.like(method + "%"))
    if tag:                                      # 物料标签，如 引脚不一致
        stmt = stmt.where(StagedPart.tags.contains(tag))
    return rule_filter(stmt, rule)


def review_filtered(filters: dict, decision: str, user: str = "admin", stop=None, log=None, progress=None) -> dict:
    """按复核队列当前筛选条件整批通过 / 驳回（如"Vishay 的电阻阵列、建议封装都是 SOIC-16"）。逻辑同 review_group。"""
    if decision not in ("approve", "reject"):
        raise ValueError(decision)
    log = log or (lambda m: None)
    f = {k: filters.get(k) for k in REVIEW_FILTERS}
    db = get_session()
    tax = load_taxonomy(db)
    total = db.execute(select(func.count()).select_from(review_stmt([StagedProposal.id], **f).subquery())).scalar() or 0
    done, now, last = 0, datetime.utcnow(), 0
    while not (stop and stop()):
        prs = db.execute(review_stmt([StagedProposal], **f).where(StagedProposal.id > last).order_by(StagedProposal.id).limit(1000)).scalars().all()
        if not prs:
            break
        last = prs[-1].id
        parts = {sp.id: sp for sp in db.execute(select(StagedPart).where(StagedPart.id.in_({p.part_id for p in prs}))).scalars()}
        ids = {pr.id for pr in prs}
        for pr in prs:
            pr.review_status = "approved" if decision == "approve" else "rejected"
            pr.reviewed_by, pr.reviewed_at, pr.review_note = user, now, f"filter-{decision}"
            if decision == "approve" and pr.part_id in parts:
                apply_proposal(db, parts[pr.part_id], pr, tax)
        if decision == "approve":                       # 同物料同字段的其它待复核建议作废
            for pr in prs:
                for other in db.execute(select(StagedProposal).where(StagedProposal.part_id == pr.part_id, StagedProposal.field == pr.field,
                                                                     StagedProposal.review_status == "pending", StagedProposal.id.notin_(ids))).scalars():
                    other.review_status, other.review_note = "rejected", "superseded"
        db.commit()
        done += len(prs)
        log(f"已{'通过' if decision == 'approve' else '驳回'} {done}/{total}")
        if progress:
            progress({"done": done, "total": total})
    db.close()
    return {"decision": decision, "filters": {k: v for k, v in f.items() if v not in (None, "")}, "done": done, "total": total}


# --------------------------------------------------------------------------- export
def export(out_path: str | Path | None = None, apply: bool = False, api=None, batch: int = 200, only_fields=None, job: str | None = None,
           stop=None, log=None, progress=None) -> dict:
    from .settings import OUT_DIR
    log = log or (lambda m: None)
    out_path = out_path or (OUT_DIR / "writeback.jsonl")
    db = get_session()
    job = job or f"export-{datetime.utcnow():%Y%m%d-%H%M}"
    cond = or_(*[getattr(StagedPart, col).isnot(None) for _, col in EXPORT_FIELDS])
    rows = db.execute(select(StagedPart).where(cond, StagedPart.exported_at.is_(None))).scalars().all()
    # 有待复核建议的物料先不回写（等复核完）
    pending = {r[0] for r in db.execute(select(StagedProposal.part_id).where(StagedProposal.review_status == "pending")).all()}
    updates, held = [], 0
    for sp in rows:
        if sp.id in pending:
            held += 1; continue
        st = {}
        for f, col in EXPORT_FIELDS:
            v = getattr(sp, col)
            if v and (not only_fields or f in only_fields):
                st[f] = v
        if st:
            updates.append({"id": sp.id, "mpn": sp.mpn, "set": st, "tags": [t for t in (sp.tags or "").split(",") if t],
                            "provenance": {"job": job, "staging": True}})
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for u in updates:
            f.write(json.dumps(u, ensure_ascii=False) + "\n")
    applied, dropped = 0, 0
    if apply and api:
        for i in range(0, len(updates), batch):
            chunk = updates[i:i + batch]
            # 回写前逐条核对 ezPLM 当前值：只填仍为空的字段，不覆盖同事后来手工填的内容
            verified = []
            for u in chunk:
                try:
                    live = api.get_part(u["id"])
                except Exception as e:  # noqa
                    log(f"  ! {u['mpn']} 读取当前值失败，跳过：{e}"); dropped += 1; continue
                keep = {k: v for k, v in u["set"].items() if not live.get(k)}
                if len(keep) < len(u["set"]):
                    dropped += len(u["set"]) - len(keep)
                if keep:
                    verified.append({**u, "set": keep})
            if not verified:
                continue
            chunk = verified
            api.patch_parts([{k: v for k, v in u.items() if k != "mpn"} for u in chunk])
            now = datetime.utcnow()
            for u in chunk:
                db.get(StagedPart, u["id"]).exported_at = now
            applied += len(chunk)
            db.commit()
            log(f"已回写 {applied}/{len(updates)}")
            if progress:
                progress({"done": applied, "total": len(updates)})
            if stop and stop():
                break
    db.add(JobLog(job=job, stage="export", batch_no=0, n_items=len(updates), finished_at=datetime.utcnow(), detail=json.dumps({"applied": applied, "held_pending_review": held, "dropped_nonempty": dropped})))
    db.commit()
    db.close()
    return {"job": job, "ready": len(updates), "held_pending_review": held, "applied": applied, "skipped_fields_already_filled": dropped, "file": str(out_path)}


# --------------------------------------------------------------------------- dashboard numbers
def summary() -> dict:
    db = get_session()
    q = lambda s: db.execute(s).scalar() or 0
    total = q(select(func.count(StagedPart.id)))
    out = {
        "parts_total": total,
        "parts_processed": q(select(func.count(StagedPart.id)).where(StagedPart.processed_at.isnot(None))),
        "parts_exported": q(select(func.count(StagedPart.id)).where(StagedPart.exported_at.isnot(None))),
        "missing": {
            "category": q(select(func.count(StagedPart.id)).where(or_(StagedPart.orig_category_id == "", StagedPart.orig_category_id.is_(None)))),
            "symbol": q(select(func.count(StagedPart.id)).where(or_(StagedPart.orig_symbol == "", StagedPart.orig_symbol.is_(None)))),
            "footprint": q(select(func.count(StagedPart.id)).where(or_(StagedPart.orig_footprint == "", StagedPart.orig_footprint.is_(None)))),
            "pdf": q(select(func.count(StagedPart.id)).where(StagedPart.orig_has_pdf.is_(False))),
            "url": q(select(func.count(StagedPart.id)).where(or_(StagedPart.orig_url == "", StagedPart.orig_url.is_(None)))),
        },
        "filled": {
            "category": q(select(func.count(StagedPart.id)).where(StagedPart.new_category_id.isnot(None))),
            "symbol": q(select(func.count(StagedPart.id)).where(StagedPart.new_symbol.isnot(None))),
            "footprint": q(select(func.count(StagedPart.id)).where(StagedPart.new_footprint.isnot(None))),
            "url": q(select(func.count(StagedPart.id)).where(StagedPart.new_url.isnot(None))),
            "datasheet": q(select(func.count(StagedPart.id)).where(StagedPart.new_datasheet.isnot(None))),
            "description_en": q(select(func.count(StagedPart.id)).where(StagedPart.new_description_en.isnot(None))),
        },
        "ihs_matched": q(select(func.count(StagedPart.id)).where(StagedPart.ihs_objid.isnot(None))),
        "proposals": {k: v for k, v in db.execute(select(StagedProposal.review_status, func.count()).group_by(StagedProposal.review_status)).all()},
        # 只统计当前有效的建议（待复核 + 自动写入），已作废/驳回/通过的不算
        "by_field_action": [(f, a, n) for f, a, n in db.execute(select(StagedProposal.field, StagedProposal.action, func.count())
                            .where(StagedProposal.review_status.in_(("pending", "auto")))
                            .group_by(StagedProposal.field, StagedProposal.action)).all()],
        "jobs": [dict(job=j.job, stage=j.stage, batch_no=j.batch_no, n=j.n_items, at=j.finished_at) for j in db.execute(select(JobLog).order_by(JobLog.id.desc()).limit(15)).scalars()],
        "ingest_page": _state(db, "ingest:page", "1"),
    }
    return out
