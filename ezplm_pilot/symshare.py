"""同系列共享符号：同厂商、去掉包装后缀后型号相同、封装相同的物料，引脚排列相同，可以共用一个符号。

例：100EL11M / 100EL11MX（X = 卷带）、10BQ015 / 10BQ015TR、OPA2333AIDR / OPA2333AIDT、24LC256-I/SN / 24LC256T-I/SN。
- 供体：已有符号的物料（ezPLM 原值、已通过 / 已自动写入的建议；通用符号除外，通用件另有规则）。打了「引脚不一致」的不当供体。
- 一组里供体符号不止一个（各说各的）→ 不出建议。
- 只去包装/卷带/无铅后缀，不碰电压档、温度档、"-1" 这类会改变引脚的后缀；另外要求封装名完全相同，双保险。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime

from sqlalchemy import or_, select

from .ihs import mfr_key, norm_mpn
from .models import Proposal
from .staging import StagedPart, StagedProposal

# 包装 / 卷带 / 无铅后缀（反复去，最多 3 次）
_PACK = re.compile(r"(?:[-/#,]?(?:TRPBF|PBF|TR|T&R|REEL7|REEL13|REEL|RL7|RL13|RL|R7|R13|T7|T13|TRL|TRR|NOPB|G4|E4|GE3|HE3|E3|LT1G|LT3G|T1G|T3G|LT1|LT3)"
                   r"|-(?:7|13)(?:-F)?|-F|-(?:GS)?(?:08|18)|,\d{3})$", re.I)
_REEL_LETTER = re.compile(r"(?<=[A-Z]{2})[RT]$|(?<=[A-Z])X$", re.I)   # TI DBVR/DBVT（前面至少两个字母，避免 LM317T 这类封装字母）、onsemi …MX
_MCHP_T = re.compile(r"T(?=-[IEHV]/)", re.I)                     # Microchip 24LC256T-I/SN 的 T = 卷带


def base_mpn(mpn: str) -> str:
    s = (mpn or "").strip().upper()
    s = _MCHP_T.sub("", s)
    for _ in range(3):
        t = _PACK.sub("", s)
        if t == s:
            break
        s = t
    s = _REEL_LETTER.sub("", s)
    return norm_mpn(s)


def _fp(sp) -> str:
    return (sp.new_footprint or sp.orig_footprint or "").split(":")[-1].strip()


def run(db, auto_min: float = 0.85, job: str | None = None, log=print) -> dict:
    from .ezplm_lib import EzLibrary
    from .pipeline import _save_proposals, load_taxonomy
    job = job or f"symshare-{datetime.utcnow():%Y%m%d-%H%M}"
    st = {"donor_parts": 0, "groups": 0, "conflict_groups": 0, "targets": 0}

    # 已通过 / 自动写入的符号建议（带符号文件 SHA 等证据，共享时一并带上，预览和引脚核对才能用）
    accepted: dict[str, tuple] = {}
    for pid, prop, lab, ev, meth in db.execute(select(StagedProposal.part_id, StagedProposal.proposed, StagedProposal.proposed_label,
                                                      StagedProposal.evidence_json, StagedProposal.method)
                                               .where(StagedProposal.field == "symbol", StagedProposal.review_status.in_(("approved", "auto")))):
        accepted[pid] = (prop, lab, ev, meth)

    donors: dict[tuple, dict] = defaultdict(dict)                    # key -> {symbol: (label, evidence, method, from_part, from_mpn)}
    last = ""
    while True:
        rows = db.execute(select(StagedPart.id, StagedPart.mpn, StagedPart.manufacturer, StagedPart.orig_symbol, StagedPart.orig_symbol_lib,
                                 StagedPart.new_symbol, StagedPart.new_footprint, StagedPart.orig_footprint, StagedPart.tags)
                          .where(StagedPart.id > last, or_(StagedPart.orig_symbol.isnot(None), StagedPart.new_symbol.isnot(None)))
                          .order_by(StagedPart.id).limit(20000)).all()
        if not rows:
            break
        last = rows[-1].id
        for r in rows:
            fp = _fp(r)
            if not fp or not r.mpn or "引脚不一致" in (r.tags or ""):
                continue
            if r.new_symbol:
                a = accepted.get(r.id)
                if not a or a[3] == "generic-symbol" or a[0] != r.new_symbol:
                    continue
                sym, lab, ev, meth = a[0], a[1] or a[0], a[2] or "{}", a[3]
            elif r.orig_symbol:
                sym, lab, ev, meth = r.orig_symbol, (f"{r.orig_symbol_lib}:{r.orig_symbol}" if r.orig_symbol_lib else r.orig_symbol), "{}", "ezplm"
            else:
                continue
            key = (mfr_key(r.manufacturer), base_mpn(r.mpn), fp)
            if key[1]:
                donors[key].setdefault(sym, (lab, ev, meth, r.id, r.mpn))
                st["donor_parts"] += 1
    st["groups"] = len(donors)
    log(f"供体：{st['donor_parts']} 条物料，{len(donors)} 个（厂商 + 基础型号 + 封装）组")

    pending = {pid for (pid,) in db.execute(select(StagedProposal.part_id).where(StagedProposal.field == "symbol", StagedProposal.review_status == "pending"))}
    tax, ezlib = load_taxonomy(db), EzLibrary(db)
    stats = {"job": job, "proposals": 0, "auto_fill": 0, "flag_mismatch": 0, "review": 0}
    conflicts = set()
    last, batch, staged = "", [], {}

    def flush():
        if not batch:
            return
        ids = [p.part_id for p in batch]
        for o in db.execute(select(StagedProposal).where(StagedProposal.part_id.in_(ids), StagedProposal.review_status == "pending",
                                                         StagedProposal.method.like("symshare%"))).scalars():
            o.review_status, o.review_note = "rejected", "superseded by symshare"
        ezlib.annotate(batch)
        _save_proposals(db, batch, staged, job, tax, stats)
        db.commit()
        batch.clear(); staged.clear()

    while True:
        rows = db.execute(select(StagedPart).where(StagedPart.id > last, or_(StagedPart.orig_symbol.is_(None), StagedPart.orig_symbol == ""),
                                                   or_(StagedPart.new_symbol.is_(None), StagedPart.new_symbol == ""))
                          .order_by(StagedPart.id).limit(5000)).scalars().all()
        if not rows:
            break
        last = rows[-1].id
        for sp in rows:
            fp = _fp(sp)
            if not fp or not sp.mpn or sp.id in pending:
                continue
            key = (mfr_key(sp.manufacturer), base_mpn(sp.mpn), fp)
            d = donors.get(key)
            if not d:
                continue
            if len(d) > 1:
                conflicts.add(key)
                continue
            sym, (lab, ev_json, meth, from_id, from_mpn) = next(iter(d.items()))
            try:
                ev = json.loads(ev_json or "{}")
            except ValueError:
                ev = {}
            ev = {k: v for k, v in ev.items() if k in ("sha256", "sources", "pin_count", "origin", "file", "source", "license", "library")}
            ev.update(from_part=from_id, from_mpn=from_mpn, base_mpn=key[1], footprint=fp, from_method=meth)
            conf = 0.9
            act = "auto_fill" if conf >= auto_min else "review"
            batch.append(Proposal(sp.id, sp.mpn, "symbol", "", sym, lab, conf, act, f"symshare:{meth.split(':')[0]}", ev,
                                  ["符号自动匹配" if act == "auto_fill" else "符号待复核", "同系列共享"]))
            staged[sp.id] = sp
            st["targets"] += 1
        if len(batch) >= 2000:
            flush()
        log(f"  已扫描到 {last[:8]}…：出建议 {st['targets']}")
    flush()
    st["conflict_groups"] = len(conflicts)
    st.update({k: v for k, v in stats.items() if k != "job"})
    log(f"完成：{st}")
    return {"job": job, **st}
