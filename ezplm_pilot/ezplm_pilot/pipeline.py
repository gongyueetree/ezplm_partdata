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
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import func, select, or_

from .models import Part, Proposal
from .staging import StagedPart, StagedProposal, StagedCategory, SyncState, JobLog, get_session
from .taxonomy import Taxonomy, Node
from .gap import value_score

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
    return Part(id=sp.id, mpn=sp.mpn or "", manufacturer=sp.manufacturer or "", manufacturer_id=sp.manufacturer_id or "",
                category=sp.new_category or sp.orig_category or "", category_id=sp.new_category_id or sp.orig_category_id or "",
                symbol=sp.new_symbol or sp.orig_symbol or "", symbol_lib=sp.orig_symbol_lib or "",
                footprint=sp.new_footprint or sp.orig_footprint or "", footprint_lib=sp.orig_footprint_lib or "",
                has_pdf=bool(sp.orig_has_pdf), has_image=bool(sp.orig_has_image), url=sp.new_url or sp.orig_url or "",
                description=sp.description or "", description_en=sp.description_en or "", status=sp.status or "")


def _add_tags(sp: StagedPart, tags: Iterable[str]):
    cur = [t for t in (sp.tags or "").split(",") if t]
    for t in tags:
        if t and t not in cur:
            cur.append(t)
    sp.tags = ",".join(cur)


FIELD_TO_NEW = {"category_id": "new_category_id", "symbol": "new_symbol", "footprint": "new_footprint", "url": "new_url", "datasheet": "new_datasheet"}


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
            sp.description, sp.description_en, sp.status = p.description, p.description_en, p.status
            sp.orig_category_id, sp.orig_category = p.category_id, p.category
            sp.orig_symbol, sp.orig_symbol_lib, sp.orig_footprint, sp.orig_footprint_lib = p.symbol, p.symbol_lib, p.footprint, p.footprint_lib
            sp.orig_has_pdf, sp.orig_has_image, sp.orig_url = p.has_pdf, p.has_image, p.url
            sp.attributes_json = json.dumps(getattr(p, "attributes", {}) or {}, ensure_ascii=False)
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
    return {"job": job, "fetched": n_total, "new": n_new, "orig_changed": n_changed, "batches": batch_no,
            "parts_in_staging": db.execute(select(func.count(StagedPart.id))).scalar()}


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
            auto_fill_min: float = 0.85, flag_min: float = 0.85) -> dict:
    from .classify import Classifier, Policy
    from .packages import PackageMapper
    from .urls import UrlFiller
    from .kicad import LibIndex, propose_symbols

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

    stats = {"job": job, "batches": 0, "parts": 0, "proposals": 0, "auto_fill": 0, "flag_mismatch": 0, "review": 0}
    while True:
        rows = db.execute(select(StagedPart).where(StagedPart.processed_at.is_(None)).order_by(StagedPart.value_score.desc(), StagedPart.id).limit(batch)).scalars().all()
        if not rows:
            break
        stats["batches"] += 1
        parts = [_to_part(r) for r in rows]
        by_id = {r.id: r for r in rows}
        attrs = {}
        for r in rows:
            try:
                a = json.loads(r.attributes_json or "{}")
                attrs[r.id] = a.get("封装") or a.get("Package") or a.get("package") or ""
            except Exception:
                pass
        props: list[Proposal] = []
        for name, fn in (("分类", lambda: [clf.classify(p) for p in parts]), ("封装", lambda: list(pkg.propose(parts, attrs))),
                         ("符号", lambda: list(propose_symbols(parts, idx)) if idx else []), ("链接", lambda: list(urlf.propose(parts)) if urlf else [])):
            try:
                props += fn()
            except Exception as e:  # noqa  —— 单个模块出错不影响本批其它模块
                log(f"  ! {name}模块出错，本批跳过该模块：{str(e)[:200]}")
        for pr in props:
            if pr.action == "ok":
                continue
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
        for r in rows:
            r.processed_at = datetime.utcnow(); r.process_version = PROCESS_VERSION
        stats["parts"] += len(rows)
        db.add(JobLog(job=job, stage="process", batch_no=stats["batches"], n_items=len(rows), finished_at=datetime.utcnow(),
                      detail=json.dumps({k: v for k, v in stats.items() if k != "job"})))
        db.commit()
        log(f"批次 {stats['batches']}: {len(rows)} 条 → 建议 {stats['proposals']}（自动 {stats['auto_fill']} / 疑似错误 {stats['flag_mismatch']} / 复核 {stats['review']}）")
        if progress:
            progress({"done": stats["parts"], "total": todo_total})
        if (max_batches and stats["batches"] >= max_batches) or (stop and stop()):
            break
    if clf.llm:
        stats["llm_calls"] = clf.llm.n_calls
        stats["llm_errors"] = clf.llm.n_errors
        if clf.llm.n_errors:
            log(f"  ! LLM 调用失败 {clf.llm.n_errors}/{clf.llm.n_calls} 次，最后一次错误：{clf.llm.last_error}")
    return stats


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
    return pr


# --------------------------------------------------------------------------- export
def export(out_path: str | Path | None = None, apply: bool = False, api=None, batch: int = 200, only_fields=None, job: str | None = None,
           stop=None, log=None, progress=None) -> dict:
    from .settings import OUT_DIR
    log = log or (lambda m: None)
    out_path = out_path or (OUT_DIR / "writeback.jsonl")
    db = get_session()
    job = job or f"export-{datetime.utcnow():%Y%m%d-%H%M}"
    cond = or_(StagedPart.new_category_id.isnot(None), StagedPart.new_symbol.isnot(None), StagedPart.new_footprint.isnot(None),
               StagedPart.new_url.isnot(None), StagedPart.new_datasheet.isnot(None))
    rows = db.execute(select(StagedPart).where(cond, StagedPart.exported_at.is_(None))).scalars().all()
    # 有待复核建议的物料先不回写（等复核完）
    pending = {r[0] for r in db.execute(select(StagedProposal.part_id).where(StagedProposal.review_status == "pending")).all()}
    updates, held = [], 0
    for sp in rows:
        if sp.id in pending:
            held += 1; continue
        st = {}
        for f, col in (("categoryId", "new_category_id"), ("symbol", "new_symbol"), ("footprint", "new_footprint"), ("url", "new_url"), ("datasheetUrl", "new_datasheet")):
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
        },
        "proposals": {k: v for k, v in db.execute(select(StagedProposal.review_status, func.count()).group_by(StagedProposal.review_status)).all()},
        "by_field_action": [(f, a, n) for f, a, n in db.execute(select(StagedProposal.field, StagedProposal.action, func.count()).group_by(StagedProposal.field, StagedProposal.action)).all()],
        "jobs": [dict(job=j.job, stage=j.stage, batch_no=j.batch_no, n=j.n_items, at=j.finished_at) for j in db.execute(select(JobLog).order_by(JobLog.id.desc()).limit(15)).scalars()],
        "ingest_page": _state(db, "ingest:page", "1"),
    }
    return out
