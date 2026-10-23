"""符号缺口表：暂存库里没有原理图符号的物料 → 分流成"通用"（按分类+引脚数套 KiCad 通用符号）和"专用"（要按型号找符号）。

- 缺符号 = orig_symbol 和 new_symbol 都为空。已有待复核符号建议的也照样列出，标 pending_proposal=1。
- 引脚数：优先从封装名（new/orig footprint）解析，没有再看物料属性里的"封装"字段。
- 厂商：经 config/mfr_lineage.yaml（及 ezPLM 厂商表）归一到当前厂商（Linear/Maxim → ADI，Freescale → NXP …）。
- 通用规则：config/generic_symbols.yaml。--propose-generic 时给通用件直接出符号建议（method=generic-symbol）。
输出 out/symbol_gap.csv（全量，可能上百万行，请用脚本/数据库打开）和 out/symbol_gap_summary.csv。
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import or_, select

from .ihs import Lineage, norm_mpn
from .models import Proposal
from .staging import StagedPart, StagedProposal

HERE = Path(__file__).resolve().parent.parent
RULES_PATH = HERE / "config" / "generic_symbols.yaml"
GAP_COLUMNS = ["part_id", "mpn", "mpn_norm", "manufacturer", "mfr_canonical", "category_path", "package", "pin_count",
               "value_score", "symbol_class", "generic_symbol", "generic_conf", "pending_proposal"]

# --------------------------------------------------------------------------- 引脚数
_TWO_TERM = re.compile(r"^(R|C|CP|L|D|LED|Fuse|Polyfuse|Crystal|Varistor|FB|Ferrite|RV|RES|CAP|IND)[_-]", re.I)
_CHIP_SIZE = re.compile(r"(?<!\d)(01005|0201|0402|0603|0805|1008|1206|1210|1806|1812|2010|2220|2512)(?!\d)")
_B, _E = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"
_TWO_PKG = re.compile(_B + r"(SOD-?\d{2,3}[A-Z]*|SMA|SMB|SMC|SMAF|SMBF|DO-?\d{2,3}[A-Z]*|MELF|MiniMELF|MicroMELF|LL-?34|LL-?41|HC-?49\w*|PowerDI-?123)" + _E, re.I)
_ROWS = re.compile(r"(?<!\d)([12])\s*[xX×*]\s*(\d{1,3})(?!\d)")
_FIXED = [(re.compile(p.replace(r"\b", "(?<![A-Za-z0-9])", 1).replace(r"\b", "(?![A-Za-z0-9])"), re.I), n) for p, n in [
    (r"\b(?:TO|SOT|SC|DO)-?\d{2,3}[A-Z]{0,2}-(\d{1,2})\b", None), (r"\bSOT-?23-?(\d)\b", None), (r"\bSOT-?23\b|\bSOT-?323\b|\bSOT-?416\b|\bSOT-?523\b|\bSC-?70\b(?!-)|\bTO-?92\b|\bSOT-?89\b|\bTO-?220(?!-\d)\b|\bTO-?247(?!-\d)\b", 3),
    (r"\bSOT-?223\b", 4), (r"\bTO-?252\b|\bDPAK\b|\bTO-?263\b|\bD2PAK\b", 3), (r"\bSOT-?363\b|\bSC-?70-?6\b|\bSC-?88\b", 6), (r"\bSOT-?353\b|\bSC-?70-?5\b", 5),
]]
_TAB_SMD = re.compile(_B + r"(?:TO-?252|TO-?263|TO-?268|D2?PAK)-(\d)(?![\d.])", re.I)
_FAMILY_N = re.compile(_B + r"[A-Z]*(?:SOIC|SOP|SSOP|TSSOP|MSOP|QSOP|VSSOP|HTSSOP|DIP|PDIP|QFN|VQFN|WQFN|DFN|TDFN|SON|VSON|WSON|QFP|LQFP|TQFP|PLCC|BGA|LGA|CSP|WLCSP|HSOP|TQFN|UQFN|USON|X2SON|PowerPAK|HVQFN)[-_ ]?(\d{1,4})(?!\d)", re.I)


def pin_count(text: str) -> int | None:
    t = (text or "").split(":")[-1].strip()
    if not t:
        return None
    m = _ROWS.search(t)
    if m and re.search(r"(?i)pin|header|socket|conn|排", t + " "):
        return int(m.group(1)) * int(m.group(2))
    if _TWO_TERM.match(t) or _TWO_PKG.search(t) or (_CHIP_SIZE.search(t) and len(t) <= 30 and not _FAMILY_N.search(t)):
        return 2
    m = _TAB_SMD.search(t)
    if m:                                         # TO-252-2 / TO-263-2（DPAK/D2PAK）：名字里的 2 是引线数，散热片是第 3 个焊盘（KiCad 编号 2）
        return int(m.group(1)) + 1
    for rx, n in _FIXED:
        m = rx.search(t)
        if m:
            return int(m.group(1)) if n is None else n
    m = _FAMILY_N.search(t)
    if m:
        return int(m.group(1))
    return None


def package_text(sp: StagedPart) -> str:
    fp = sp.new_footprint or sp.orig_footprint or ""
    if fp:
        return fp
    try:
        a = json.loads(sp.attributes_json or "{}")
    except ValueError:
        return ""
    if isinstance(a, list):                       # ezPLM 详情接口的属性是 [{name, value}] 列表（处理时拉回来的就是这种）
        a = {x.get("name") or x.get("key") or "": x.get("value") for x in a if isinstance(x, dict)}
    if not isinstance(a, dict):
        return ""
    return next((str(v) for k, v in a.items() if k and ("封装" in k or k.lower() in ("package", "package / case", "封装/外壳"))), "") or ""


# --------------------------------------------------------------------------- 通用符号规则
class GenericRules:
    def __init__(self, path: str | Path = RULES_PATH):
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        self.rules = [dict(r, _path=re.compile(r["path"]), _desc=re.compile(r["desc"], re.I) if r.get("desc") else None,
                           _mpn=re.compile(r["mpn"], re.I) if r.get("mpn") else None, _pkg=re.compile(r["pkg"], re.I) if r.get("pkg") else None,
                           _exclude=re.compile(r["exclude"], re.I) if r.get("exclude") else None)
                      for r in cfg.get("rules", [])]

    def match(self, cat_path: str, text: str, pins: int | None, mpn: str = "", pkg: str = "") -> tuple[str, float] | None:
        for r in self.rules:
            if not r["_path"].search(cat_path or ""):
                continue
            if r["_desc"] and not r["_desc"].search(text or ""):
                continue
            if r["_exclude"] and r["_exclude"].search(text or ""):
                continue
            if r["_mpn"] and not r["_mpn"].search(mpn or ""):          # 引脚排列靠型号系列保证（如 SOT-23 三极管 MMBT/BC8xx 都是 1=B 2=E 3=C）
                continue
            if r["_pkg"] and not r["_pkg"].search(pkg or ""):
                continue
            allowed = r.get("pins") or []
            conf = float(r.get("conf", 0.8))
            if allowed:
                if pins is None:
                    if not r.get("unknown"):
                        continue
                    conf -= 0.05
                elif pins not in allowed:
                    continue
            sym = r["symbol"]
            if "{n}" in sym or "{n2}" in sym:
                if not pins or ("{n2}" in sym and pins % 2):
                    continue
                sym = sym.replace("{n2}", f"{pins // 2:02d}").replace("{n}", f"{pins:02d}")
                if not 1 <= pins <= 80:
                    continue
            return sym, round(conf, 3)
        return None


# --------------------------------------------------------------------------- 主流程
def iter_missing(db, chunk: int = 5000):
    """缺符号的物料，按 id 分页流式读取（118 万条也不会一次装进内存）。"""
    last = ""
    while True:
        q = (select(StagedPart).where(or_(StagedPart.orig_symbol.is_(None), StagedPart.orig_symbol == ""),
                                      or_(StagedPart.new_symbol.is_(None), StagedPart.new_symbol == ""),
                                      StagedPart.id > last).order_by(StagedPart.id).limit(chunk))
        rows = db.execute(q).scalars().all()
        if not rows:
            return
        yield rows
        last = rows[-1].id


def pending_symbol_parts(db) -> set[str]:
    return {pid for (pid,) in db.execute(select(StagedProposal.part_id).where(StagedProposal.field == "symbol",
                                                                             StagedProposal.review_status == "pending"))}


def gap_rows(db, rules: GenericRules | None = None, lineage: Lineage | None = None, log=print):
    from .pipeline import load_taxonomy
    rules = rules or GenericRules()
    lineage = lineage or Lineage(db=db)
    tax = load_taxonomy(db)
    pending = pending_symbol_parts(db)
    n = 0
    for rows in iter_missing(db):
        for sp in rows:
            cid = sp.new_category_id or sp.orig_category_id or ""
            path = tax.path(cid) if cid else (sp.new_category or sp.orig_category or "")
            pkg = package_text(sp)
            pins = pin_count(pkg)
            g = rules.match(path, f"{sp.mpn or ''} {sp.description or ''} {sp.description_en or ''} {pkg}", pins, sp.mpn or "", pkg)
            yield {"part_id": sp.id, "mpn": sp.mpn or "", "mpn_norm": sp.mpn_norm or norm_mpn(sp.mpn), "manufacturer": sp.manufacturer or "",
                   "mfr_canonical": lineage.resolve(sp.manufacturer)["current"] if sp.manufacturer else "",
                   "category_path": path, "package": pkg, "pin_count": pins if pins is not None else "", "value_score": sp.value_score or 0,
                   "symbol_class": "generic" if g else "specific", "generic_symbol": g[0] if g else "", "generic_conf": g[1] if g else "",
                   "pending_proposal": 1 if sp.id in pending else 0}
        n += len(rows)
        log(f"  已扫描 {n} 条缺符号物料")


def export(db, out_dir: str | Path, log=print) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    st, by_root, by_mfr = Counter(), Counter(), Counter()
    with open(out / "symbol_gap.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=GAP_COLUMNS)
        w.writeheader()
        for r in gap_rows(db, log=log):
            w.writerow(r)
            st["missing"] += 1
            st[r["symbol_class"]] += 1
            st["pin_known"] += r["pin_count"] != ""
            st["pending_proposal"] += r["pending_proposal"]
            root = r["category_path"].split(" > ")[0] or "（无分类）"
            by_root[(root, r["symbol_class"])] += 1
            by_mfr[(r["mfr_canonical"] or "（无厂商）", r["symbol_class"])] += 1
    with open(out / "symbol_gap_summary.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["维度", "名称", "缺符号", "通用", "专用"])
        for dim, cnt in (("一级分类", by_root), ("厂商", by_mfr)):
            names = sorted({k[0] for k in cnt}, key=lambda n: -(cnt[(n, "generic")] + cnt[(n, "specific")]))
            for n in names:
                w.writerow([dim, n, cnt[(n, "generic")] + cnt[(n, "specific")], cnt[(n, "generic")], cnt[(n, "specific")]])
    log(f"缺口表：{dict(st)} → {out / 'symbol_gap.csv'}")
    return dict(st)


PLATFORM_SYMBOLS = HERE / "config" / "generic_symbols"
_platform_cache: dict = {}


def platform_symbol(db, name: str) -> dict:
    """config/generic_symbols/<name>.kicad_sym → 写入 symbol_files（按 SHA 去重），返回 {sha256, pin_count, sources, origin, file}。"""
    if name in _platform_cache:
        return _platform_cache[name]
    import gzip
    import hashlib
    from .staging import SymbolFile
    from .symrender import load, pin_table
    data = (PLATFORM_SYMBOLS / f"{name}.kicad_sym").read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    pins = [p["number"] for p in pin_table(load(data.decode("utf-8")))]
    if not db.get(SymbolFile, sha):
        db.add(SymbolFile(sha256=sha, name=name, pin_count=len(pins), pins_json=json.dumps(pins), source="platform",
                          origin="平台通用符号（KiCad Device 库改数字引脚编号，CC-BY-SA 4.0）", filename=f"{name}.kicad_sym",
                          size=len(data), content_gz=gzip.compress(data)))
        db.commit()
    _platform_cache[name] = {"sha256": sha, "pin_count": len(pins), "sources": ["platform"], "origin": "平台通用符号", "file": f"{name}.kicad_sym"}
    return _platform_cache[name]


def recheck_generic(db, rules: GenericRules | None = None, log=print) -> dict:
    """按当前规则复查已有的通用符号建议：不再命中、或命中的符号不同的 → 待复核的驳回，已自动写入的撤销（new_symbol 清空），
    这样改了规则（如 TO-263-2 不是 2 脚、二极管阵列不用单管符号）以后，之前误写的能一键撤回。"""
    from datetime import datetime as _dt
    from .pipeline import load_taxonomy
    rules = rules or GenericRules()
    tax = load_taxonomy(db)
    st = {"checked": 0, "kept": 0, "rejected": 0, "reverted": 0}
    last = 0
    while True:
        prs = db.execute(select(StagedProposal).where(StagedProposal.method == "generic-symbol", StagedProposal.review_status.in_(("auto", "pending")),
                                                      StagedProposal.id > last).order_by(StagedProposal.id).limit(2000)).scalars().all()
        if not prs:
            break
        last = prs[-1].id
        parts = {sp.id: sp for sp in db.execute(select(StagedPart).where(StagedPart.id.in_({p.part_id for p in prs}))).scalars()}
        for pr in prs:
            sp = parts.get(pr.part_id)
            if not sp:
                continue
            st["checked"] += 1
            cid = sp.new_category_id or sp.orig_category_id or ""
            path = tax.path(cid) if cid else (sp.new_category or sp.orig_category or "")
            pkg = package_text(sp)
            g = rules.match(path, f"{sp.mpn or ''} {sp.description or ''} {sp.description_en or ''} {pkg}", pin_count(pkg), sp.mpn or "", pkg)
            if g and g[0] == pr.proposed_label:
                st["kept"] += 1
                continue
            if pr.review_status == "auto":
                if sp.new_symbol == pr.proposed:
                    sp.new_symbol = None
                st["reverted"] += 1
            else:
                st["rejected"] += 1
            pr.review_status, pr.review_note, pr.reviewed_at = "rejected", "recheck: 通用符号规则已不再适用", _dt.utcnow()
            sp.tags = ",".join(t for t in (sp.tags or "").split(",") if t and t not in ("通用符号", "符号自动匹配")) or None
        db.commit()
        log(f"  已复查 {st['checked']}：保留 {st['kept']}，撤销自动写入 {st['reverted']}，驳回 {st['rejected']}")
    return st


def propose_generic(db, auto_min: float = 0.85, job: str | None = None, log=print) -> dict:
    """给"通用"类缺符号物料直接出符号建议（KiCad 官方通用符号），作废同一方法上一轮未复核的建议。"""
    from .ezplm_lib import EzLibrary
    from .pipeline import _save_proposals, load_taxonomy
    job = job or f"symgeneric-{datetime.utcnow():%Y%m%d-%H%M}"
    log(f"先按当前规则复查已有通用符号：{recheck_generic(db, log=log)}")
    tax, ezlib = load_taxonomy(db), EzLibrary(db)
    stats = {"job": job, "proposals": 0, "auto_fill": 0, "flag_mismatch": 0, "review": 0}
    batch, staged = [], {}
    pending = pending_symbol_parts(db)

    def flush():
        if not batch:
            return
        ids = [p.part_id for p in batch]
        for o in db.execute(select(StagedProposal).where(StagedProposal.part_id.in_(ids), StagedProposal.review_status == "pending",
                                                         StagedProposal.method == "generic-symbol")).scalars():
            o.review_status, o.review_note = "rejected", "superseded by symgap generic"
        ezlib.annotate(batch)
        _save_proposals(db, batch, staged, job, tax, stats)
        db.commit()
        batch.clear(); staged.clear()

    for r in gap_rows(db, log=log):
        if r["symbol_class"] != "generic" or r["part_id"] in pending:
            continue
        lib, name = r["generic_symbol"].split(":", 1)
        conf = float(r["generic_conf"])
        act = "auto_fill" if conf >= auto_min else "review"
        ev = {"category_path": r["category_path"], "package": r["package"], "pin_count": r["pin_count"], "library": lib}
        if lib == "platform":                       # 平台自带的通用符号（config/generic_symbols/*.kicad_sym），存进 symbol_files 以便预览/下载/引脚核对
            ev.update(platform_symbol(db, name))
        batch.append(Proposal(r["part_id"], r["mpn"], "symbol", "", name, r["generic_symbol"], conf, act, "generic-symbol", ev,
                              ["符号自动匹配" if act == "auto_fill" else "符号待复核", "通用符号"]))
        if len(batch) >= 2000:
            for sp in db.execute(select(StagedPart).where(StagedPart.id.in_([p.part_id for p in batch]))).scalars():
                staged[sp.id] = sp
            flush()
    if batch:
        for sp in db.execute(select(StagedPart).where(StagedPart.id.in_([p.part_id for p in batch]))).scalars():
            staged[sp.id] = sp
        flush()
    log(f"通用符号建议：{stats}")
    return stats
