"""引脚核对：器件实际引脚数 vs 原理图符号引脚 vs PCB 封装焊盘。

器件引脚数从哪来（按可信度）：IHS「Number of Terminals」→ 描述里的"16SOIC""SOT-23, 5 PIN""5引脚"。
是否带裸露焊盘（EP）：IHS 封装形式含 HEAT SINK/SLUG、EXPOSED PAD，或描述/封装名里有 EP、PowerPAD、HTSSOP 等。
IHS 的引脚数一般不含 EP；KiCad 封装的 EP 通常编号为 N+1（也有编号 EP / 0 的）。

核对结果：[(级别, 说明)]，级别 ok / warn / err。
另外：器件有 EP、封装没有时，到库索引里找同尺寸的 -1EP 版本给出建议（ep_variant）。
"""
from __future__ import annotations

import json
import re

from .packages import find_package, pins_in_text

EP_HINT = re.compile(r"HEAT ?SINK|SLUG|EXPOSED|THERMAL ?PAD|POWER ?PAD|PowerPAD|裸露焊盘|散热焊盘|外露焊盘|(?<![A-Za-z])(?:\d?EP|E-?PAD|HTSSOP|HSOP|HVSSOP|HTQFP|eTSSOP)(?![A-Za-z])", re.I)
PKG_PINS_PREFIX = re.compile(r"(?<![\d.])(\d{1,3})\s*-?\s*(?:SOIC|SOP|SSOP|TSSOP|MSOP|QFN|DFN|LQFP|TQFP|QFP|DIP|PDIP|SON|WSON|VSON|BGA|LGA|WLCSP|LFCSP|TSOT|SOT)(?![A-Za-z])", re.I)


def chip_info(db, sp) -> dict:
    """→ {pins, src, ep}；pins / ep 不知道时为 None。"""
    out = {"pins": None, "src": "", "ep": None}
    text = f"{sp.description or ''} {sp.description_en or ''} {sp.new_description_en or ''}"
    if sp.ihs_objid:
        from .staging import IhsRef
        r = db.get(IhsRef, sp.ihs_objid)
        if r:
            prm = json.loads(r.params_json or "{}")
            n = prm.get("Number of Terminals", "")
            if str(n).isdigit() and int(n) > 0:
                out.update(pins=int(n), src="IHS")
            text += f" {r.package or ''} {prm.get('Package Style', '')} {r.description or ''}"
    if out["pins"] is None:
        from .kicad import DESC_PKG_PINS, expected_pins_from_footprint
        m = PKG_PINS_PREFIX.search(text)
        pk = find_package(text)
        n = pins_in_text(text) or (int(m.group(1)) if m else None) or expected_pins_from_footprint(DESC_PKG_PINS.get(pk.upper().replace("-", ""), pk))
        if n:
            out.update(pins=n, src="描述")
    if EP_HINT.search(text):
        out["ep"] = True
    return out


def check(chip: dict, sym_nums: set | None, fp: dict | None) -> list[tuple[str, str]]:
    """sym_nums：符号引脚编号集合；fp：fprender.preview 的结果（numbers / ep / name）。
    级别：符号引脚在封装里找不到焊盘 = err（一定接不上）；封装多出的焊盘 = warn（常是 NC 脚或散热焊盘）；
    器件脚数（IHS/描述）对不上：符号和封装本身也对不上时 = err，符号和封装一致时只 warn（描述"40 pins"、IHS 电阻网络"1 脚"这类说法常不准）。"""
    res = []
    fp_all = set(fp["numbers"]) if fp and fp.get("numbers") else None
    ep = set(fp.get("ep") or []) if fp else set()
    if sym_nums:
        ep -= sym_nums                      # 符号接了的大焊盘是功能脚（TO-252 散热片 = VCC/漏极），不算 EP
    agree = bool(sym_nums) and fp_all is not None and sym_nums == fp_all
    if sym_nums and fp_all is not None:
        miss = sym_nums - fp_all
        extra = fp_all - sym_nums
        if miss:
            res.append(("err", f"符号引脚 {_fmt(miss)} 在封装里没有对应焊盘"))
        if extra and extra <= ep:
            res.append(("warn", f"封装散热焊盘 {_fmt(extra)} 符号里没有引脚（通常应接地：符号加 EP 引脚，或确认可悬空）"))
        elif extra:
            res.append(("warn", f"封装焊盘 {_fmt(extra)} 符号里没有对应引脚（若是 NC 脚可忽略，否则符号或封装不对）"))
        if not miss and not extra:
            res.append(("ok", f"符号引脚与封装焊盘编号一致（{len(sym_nums)}）"))
    pins = chip.get("pins") if (chip.get("pins") or 0) >= 2 else None      # IHS 偶有"1 脚"之类无意义的值
    if pins and fp_all is not None:
        sig = len(fp_all - ep)
        if sig != pins:
            res.append(("warn" if agree else "err", f"器件 {pins} 脚（{chip['src']}），封装信号焊盘 {sig} 个"))
        else:
            res.append(("ok", f"封装信号焊盘数与器件一致（{sig}，来自{chip['src']}）"))
    if chip.get("ep") and fp_all is not None and not ep:
        res.append(("warn", "器件带裸露焊盘（EP），封装里没有散热焊盘"))
    if pins and sym_nums and not agree:
        n = len(sym_nums - ep)
        if n not in (pins, pins + 1):                                    # +1：符号里画了 EP 引脚
            res.append(("warn", f"器件 {pins} 脚（{chip['src']}），符号 {len(sym_nums)} 个引脚"))
    return res


def _fmt(nums) -> str:
    from .symrender import _natkey
    xs = sorted(nums, key=_natkey)
    return "、".join(xs[:8]) + (f" 等 {len(xs)} 个" if len(xs) > 8 else "")


def ep_variant(fp_name: str) -> str | None:
    """SOIC-16_3.9x9.9mm_P1.27mm → 库索引里的 SOIC-16-1EP_3.9x9.9mm_P1.27mm…（同尺寸、名字最短的那个，即不带散热过孔的）。"""
    from .previews import _idx
    m = re.match(r"^([A-Za-z]+(?:-[A-Za-z]+)?-\d{1,3})_(.+)$", (fp_name or "").split(":")[-1])
    if not m or "EP" in fp_name:
        return None
    con = _idx()
    if not con:
        return None
    rows = con.execute("select name, lib from footprints where name like ? order by length(name) limit 1",
                       (f"{m.group(1)}-1EP_{m.group(2)}%",)).fetchall()
    return f"{rows[0][1]}:{rows[0][0]}" if rows else None


def for_part(db, sp, pv: dict) -> dict:
    """详情页用：pv 是 previews.for_part 的结果。"""
    chip = chip_info(db, sp)
    sym = pv.get("symbol") or {}
    sym_nums = {x["number"] for x in sym.get("pins") or [] if x.get("number")} or None
    fp = pv.get("footprint") if (pv.get("footprint") or {}).get("numbers") else None
    res = check(chip, sym_nums, fp)
    fix = ep_variant(fp.get("name", "")) if fp and chip.get("ep") and not fp.get("ep") else None
    return {"chip": chip, "results": res, "worst": _worst(res), "ep_fix": fix,
            "sym_n": len(sym_nums) if sym_nums else None, "fp_n": len(fp["numbers"]) if fp else None}


def _worst(res) -> str:
    lv = {x for x, _ in res}
    return "err" if "err" in lv else ("warn" if "warn" in lv else ("ok" if res else ""))


TAGS = {"err": "引脚不一致", "warn": "引脚待确认", "ok": "引脚一致"}


def run_all(stop=None, log=None, progress=None) -> dict:
    """批量引脚核对：有符号（新值/建议/已写入）的物料逐个核对，结果打标签（引脚不一致 / 引脚待确认 / 引脚一致），
    物料列表按标签筛选即可。器件带 EP、封装没有时记下同尺寸的 -1EP 封装（证据里），不自动改。"""
    from sqlalchemy import or_, select
    from . import previews
    from .pipeline import _add_tags
    from .staging import StagedPart, StagedProposal, get_session
    log = log or (lambda m: None)
    db = get_session()
    with_sym = select(StagedProposal.part_id).where(StagedProposal.field == "symbol", StagedProposal.review_status.in_(("auto", "approved", "pending")))
    base = select(StagedPart).where(or_(StagedPart.id.in_(with_sym), StagedPart.new_symbol.isnot(None)))
    from sqlalchemy import func
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    # 先清掉"已经没有符号"的物料身上的旧核对标签（如通用符号被撤销后，上一轮打的「引脚不一致」还留着）
    stale = 0
    for sp in db.execute(select(StagedPart).where(or_(*[StagedPart.tags.contains(t) for t in list(TAGS.values()) + ["可换EP封装"]]),
                                                  StagedPart.id.notin_(base.with_only_columns(StagedPart.id)))).scalars():
        sp.tags = ",".join(t for t in (sp.tags or "").split(",") if t and t not in TAGS.values() and t != "可换EP封装") or None
        stale += 1
    db.commit()
    log(f"待核对物料 {total} 条；清掉已无符号物料上的旧核对标签 {stale} 条")
    st = {"checked": 0, "err": 0, "warn": 0, "ok": 0, "no_data": 0, "ep_fix": 0}
    last = ""
    while not (stop and stop()):
        rows = db.execute(base.where(StagedPart.id > last).order_by(StagedPart.id).limit(300)).scalars().all()
        if not rows:
            break
        last = rows[-1].id
        props: dict = {}
        for pr in db.execute(select(StagedProposal).where(StagedProposal.part_id.in_([r.id for r in rows]),
                                                          StagedProposal.field.in_(("symbol", "footprint")))).scalars():
            props.setdefault(pr.part_id, []).append(pr)
        for sp in rows:
            try:
                pv = previews.for_part(db, sp, props.get(sp.id, []))
                pc = for_part(db, sp, pv)
            except Exception as e:  # noqa —— 单个物料文件异常不影响整批
                log(f"  ! {sp.mpn}: {str(e)[:120]}")
                continue
            st["checked"] += 1
            sp.tags = ",".join(t for t in (sp.tags or "").split(",") if t and t not in TAGS.values() and t != "可换EP封装") or None
            if not pc["worst"]:
                st["no_data"] += 1
                continue
            st[pc["worst"]] += 1
            _add_tags(sp, [TAGS[pc["worst"]]] + (["可换EP封装"] if pc["ep_fix"] else []))
            st["ep_fix"] += bool(pc["ep_fix"])
        db.commit()
        log(f"  已核对 {st['checked']}/{total}：不一致 {st['err']}，待确认 {st['warn']}，一致 {st['ok']}，缺数据 {st['no_data']}")
        if progress:
            progress({"done": st["checked"], "total": total})
    db.close()
    log(f"完成：{st}。物料信息页按标签「引脚不一致」「引脚待确认」「可换EP封装」筛选查看")
    return st
