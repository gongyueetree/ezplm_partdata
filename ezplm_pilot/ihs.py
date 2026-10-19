"""IHS 参数库参考数据：厂商谱系、本地导入、型号匹配、生成建议。

IHS 导出（2011-12 Initial Load + 2012-01 / 2012-02 增量，93 个 *.csv.gz，约 7000 万器件）不进 ezPLM，
只把与暂存物料型号相同的记录导进 ihs_ref 表，作为参考证据：

    python -m ezplm_pilot.cli ihs-load --dir "<IHS数据目录>"     # 本地跑；DATABASE_URL 指向 Railway Postgres 即可直接写线上暂存库
    python -m ezplm_pilot.cli ihs-match                          # 对已处理过的物料补跑 IHS 建议（process 也会自动跑）

匹配级别（同一规范化型号下）：
    L1 型号+厂商完全一致 · L2 规范化型号+厂商一致 · L3 厂商经并购谱系一致（Micrel→Microchip）
    L4 ezPLM 无厂商 · L5 厂商不同（同号异厂，只作参考，不出建议）
建议：description_en（英文描述填空）、footprint（IHS 封装描述 → KiCad 封装）、manufacturer（空厂商补全；旧厂商名提示已并购）。
数据是 2012 年快照，生命周期/数据手册链接只在物料详情页展示，不生成建议。
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import re
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from .models import Part, Proposal

HERE = Path(__file__).resolve().parent.parent
LINEAGE_PATH = HERE / "config" / "mfr_lineage.yaml"

COMMON = {"Manufacturer Part Number", "Manufacturer Name", "Part Description", "Mfr Package Description", "Lead Free",
          "EU RoHS Compliant", "China RoHS Compliant", "Manufacturer URL", "Datasheet URL (latest)", "Status",
          "Generic Number", "Objectid", "Part Category"}

LEVEL_LABEL = {"L1": "L1 型号+厂商", "L2": "L2 规范化型号+厂商", "L3": "L3 厂商谱系", "L4": "L4 ezPLM 无厂商", "L5": "L5 同号异厂"}
LINEAGE_FACTOR = {"A": 0.92, "B": 0.80, "C": 0.65}

# ezPLM 一级分类 → IHS 大类提示（B 级谱系按大类拆分时用）
ROOT_FAMILY_HINT = [("存储器", "MEMORY_IC"), ("集成电路", "X_IC"), ("分立半导体", "DIODE TRANSISTOR"), ("光电", "OPTOELECTRONIC"),
                    ("射频", "RF_MICROWAVE"), ("传感器", "SENSOR"), ("电容", "CAPACITOR"), ("电阻", "RESISTOR"), ("连接器", "CONNECTOR")]


def norm_mpn(s: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (s or "").upper())


_CN_LEGAL = re.compile(r"(股份有限公司|有限责任公司|有限公司|株式会社|公司|（[^）]*）|\([^)]*\))")
_LEGAL = re.compile(r"\b(INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LIMITED|LTD|LLC|AG|GMBH|PLC|SA|NV|BV|KK|OYJ|AB)\b")


def mfr_key(name: str) -> str:
    """厂商名比较键：去掉法律后缀与符号。'Integrated Silicon Solution Inc.' == 'INTEGRATED SILICON SOLUTION INC'"""
    s = _CN_LEGAL.sub(" ", (name or "").upper())
    s = re.sub(r"[^0-9A-Z一-鿿]+", " ", s).strip()
    s = re.sub(r"\b(S A|A G|N V|B V)$", " ", s)          # 'SIEMENS A G'、'RADIALL S A'
    s = _LEGAL.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- 厂商谱系
class Lineage:
    def __init__(self, path: str | Path = LINEAGE_PATH):
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        self.canon = [(re.compile(c["match"], re.I), c["name"]) for c in cfg.get("canonical", [])]
        self.entries: dict[str, dict] = {}
        self.matchers: list[tuple[re.Pattern, dict]] = []
        for e in cfg.get("lineage", []):
            k = mfr_key(e["from"])
            if k in self.entries:            # 同一厂商可能先手写（带 match）再由映射表生成，取第一条
                continue
            e = dict(e, key=k, rules=[(re.compile(r["families"]), r["to"]) for r in e.get("rules", [])])
            self.entries[k] = e
            if e.get("match"):
                self.matchers.append((re.compile(e["match"], re.I), e))
        self._cache: dict = {}

    def canonical(self, name: str) -> str:
        name = (name or "").strip()
        for pat, n in self.canon:
            if pat.search(name):
                return n
        return name

    def entry(self, name: str) -> dict | None:
        e = self.entries.get(mfr_key(name))
        if e:
            return e
        for pat, e in self.matchers:
            if pat.search(name or ""):
                return e
        return None

    def resolve(self, name: str, family: str = "") -> dict:
        """→ {current, conf, note, origin}。current 为规范化后的当前归属；conf 为空表示未被并购。
        origin 是"出厂时的厂商"比较键：两条记录 origin 相同说明原厂商就是同一家（L1/L2），不同但 current 相同说明经谱系对上（L3）。"""
        ck = (name, family)
        if ck in self._cache:
            return self._cache[ck]
        e = self.entry(name)
        if e:
            to = e["to"]
            for pat, t in e["rules"]:
                if family and pat.search(family):
                    to = t
                    break
            cur = self.canonical(to)
            out = {"current": cur, "conf": e.get("conf", ""), "note": e.get("note", ""), "origin": e["key"]}
            if mfr_key(cur) == mfr_key(self.canonical(name)):        # 映射到自己（如 TE CONNECTIVITY LTD）= 未变更
                out.update(conf="", note="")
        else:
            cur = self.canonical(name)
            out = {"current": cur, "conf": "", "note": "", "origin": mfr_key(cur)}
        self._cache[ck] = out
        return out


def family_hint(root_category: str) -> str:
    for kw, fam in ROOT_FAMILY_HINT:
        if kw in (root_category or ""):
            return fam
    return ""


# --------------------------------------------------------------------------- 本地导入
_KEYS: set | None = None
_FAMILIES: re.Pattern | None = None


def _init_worker(keys, families):
    global _KEYS, _FAMILIES
    _KEYS, _FAMILIES = keys, re.compile(families) if families else None


def _parse_file(path: str) -> list[dict]:
    """解析一个 IHS *.csv.gz，返回需要导入的行（型号在暂存库里出现，或大类命中 --families）。"""
    csv.field_size_limit(10 ** 9)
    batch = Path(path).parent.name[:10]
    family = re.sub(r"\d+$", "", Path(path).name.replace(".csv.gz", ""))
    take_all = bool(_FAMILIES and _FAMILIES.search(family))
    out = []
    with gzip.open(path, "rt", encoding="latin-1", newline="") as f:      # IHS 导出不是 UTF-8
        r = csv.reader(f)
        head = next(r)
        ix = {k: i for i, k in enumerate(head)}
        get = lambda row, k: (row[ix[k]].strip() if k in ix and ix[k] < len(row) else "")
        for row in r:
            mpn = get(row, "Manufacturer Part Number")
            k = norm_mpn(mpn)
            if not k or not (take_all or (_KEYS is not None and k in _KEYS)):
                continue
            params = {h: row[i].strip() for i, h in enumerate(head) if h not in COMMON and i < len(row) and row[i].strip()}
            out.append({"objid": get(row, "Objectid"), "mpn": mpn, "mpn_norm": k, "mfr_ihs": get(row, "Manufacturer Name"),
                        "family": family, "part_category": get(row, "Part Category"), "description": get(row, "Part Description"),
                        "package": get(row, "Mfr Package Description"), "status": get(row, "Status"), "generic": get(row, "Generic Number"),
                        "datasheet": get(row, "Datasheet URL (latest)"), "params_json": json.dumps(params, ensure_ascii=False), "batch": batch})
    return out


def ensure_mpn_norm(db, chunk: int = 5000) -> int:
    """给还没有 mpn_norm 的暂存物料补上（ingest 新拉的会自动带）。"""
    from sqlalchemy import select
    from .staging import StagedPart
    n = 0
    while True:
        rows = db.execute(select(StagedPart).where(StagedPart.mpn_norm.is_(None)).limit(chunk)).scalars().all()
        if not rows:
            return n
        for r in rows:
            r.mpn_norm = norm_mpn(r.mpn)
        db.commit()
        n += len(rows)


def load(ihs_dir: str | Path, families: str = "", workers: int | None = None, log=print) -> dict:
    """把 IHS 记录导入 ihs_ref：型号（规范化）在暂存库中出现的全部导入；families（IHS 大类正则，如 "_IC"）命中的整类导入。
    批次按目录名排序依次 upsert，后到的增量覆盖先到的。可重复运行（ezPLM 陆续拉取后再跑一次即可补上新物料的匹配）。"""
    from multiprocessing import Pool
    from sqlalchemy import delete, insert, select, func
    from .staging import IhsRef, StagedPart, get_session

    db = get_session()
    n_norm = ensure_mpn_norm(db)
    if n_norm:
        log(f"补算 mpn_norm {n_norm} 条")
    keys = {k for (k,) in db.execute(select(StagedPart.mpn_norm).where(StagedPart.mpn_norm.isnot(None))) if k}
    log(f"暂存库规范化型号 {len(keys)} 个" + (f"；整类导入 {families}" if families else ""))
    files = sorted(str(p) for p in Path(ihs_dir).glob("*/*.csv.gz"))
    if not files:
        raise FileNotFoundError(f"{ihs_dir} 下没有找到 */*.csv.gz")
    lin = Lineage()
    stats = {"files": len(files), "rows": 0, "parts_matched": 0}
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    with Pool(workers, initializer=_init_worker, initargs=(keys, families)) as pool:
        for i, rows in enumerate(pool.imap(_parse_file, files)):        # imap 保持文件顺序 → 增量按日期覆盖
            for r in rows:
                res = lin.resolve(r["mfr_ihs"], r["family"])
                r["mfr_current"], r["lineage_conf"] = res["current"], res["conf"]
            for j in range(0, len(rows), 2000):
                chunk = rows[j:j + 2000]
                db.execute(delete(IhsRef).where(IhsRef.objid.in_([r["objid"] for r in chunk])))
                db.execute(insert(IhsRef), chunk)
            db.commit()
            stats["rows"] += len(rows)
            log(f"[{i + 1}/{len(files)}] {Path(files[i]).parent.name[:10]} {Path(files[i]).name}: {len(rows)} 条")
    stats["ihs_ref_total"] = db.execute(select(func.count(IhsRef.objid))).scalar()
    stats["parts_matched"] = db.execute(select(func.count(StagedPart.id)).where(StagedPart.mpn_norm.in_(select(IhsRef.mpn_norm)))).scalar()
    db.close()
    return stats


# --------------------------------------------------------------------------- 匹配与建议
class IhsMatcher:
    def __init__(self, db, tax=None, pkg=None, auto_min: float = 0.85, lineage: Lineage | None = None):
        self.db, self.tax, self.pkg, self.auto_min = db, tax, pkg, auto_min
        self.lineage = lineage or Lineage()
        self.best: dict[str, tuple] = {}          # part_id -> (IhsRef, level, conf)

    @staticmethod
    def n_ref(db) -> int:
        from sqlalchemy import select, func
        from .staging import IhsRef
        return db.execute(select(func.count(IhsRef.objid))).scalar() or 0

    def lookup(self, norms: Iterable[str]) -> dict[str, list]:
        from sqlalchemy import select
        from .staging import IhsRef
        norms = [n for n in set(norms) if n]
        out: dict[str, list] = {}
        for i in range(0, len(norms), 500):
            for r in self.db.execute(select(IhsRef).where(IhsRef.mpn_norm.in_(norms[i:i + 500]))).scalars():
                out.setdefault(r.mpn_norm, []).append(r)
        return out

    def _root(self, p: Part) -> str:
        return self.tax.path(p.category_id).split(" > ")[0] if (self.tax and p.category_id in self.tax.nodes) else ""

    def match(self, p: Part, rows: list) -> list[tuple]:
        """→ [(IhsRef, level, conf)]，置信度从高到低。"""
        if not rows:
            return []
        out = []
        fam = family_hint(self._root(p))
        mine = self.lineage.resolve(p.manufacturer, fam) if p.manufacturer else None
        currents = {mfr_key(r.mfr_current) for r in rows}
        for r in rows:
            exact = (r.mpn or "").strip().upper() == (p.mpn or "").strip().upper()
            if not mine:
                level, conf = "L4", (0.70 if len(currents) == 1 else 0.45)
            elif mfr_key(r.mfr_current) != mfr_key(mine["current"]):
                level, conf = "L5", 0.30
            elif self.lineage.resolve(r.mfr_ihs, r.family)["origin"] == mine["origin"]:
                level, conf = ("L1", 0.97) if exact else ("L2", 0.93)
            else:
                level, conf = "L3", LINEAGE_FACTOR.get(r.lineage_conf or mine["conf"] or "A", 0.8)
            if not exact and level in ("L3", "L4"):
                conf -= 0.02
            out.append((r, level, round(conf, 3)))
        out.sort(key=lambda x: (-x[2], x[0].batch or "", x[0].objid))
        return out

    def propose(self, parts: list[Part]) -> list[Proposal]:
        from .packages import PKG_IN_DESC
        cands = self.lookup(norm_mpn(p.mpn) for p in parts)
        props: list[Proposal] = []
        self.best = {}
        for p in parts:
            # 旧厂商名提示（不依赖 IHS 记录）：ezPLM 里写的是已被并购的厂商
            if p.manufacturer:
                res = self.lineage.resolve(p.manufacturer, family_hint(self._root(p)))
                if res["conf"]:
                    conf = {"A": 0.95, "B": 0.75, "C": 0.6}.get(res["conf"], 0.6)
                    props.append(Proposal(p.id, p.mpn, "manufacturer", p.manufacturer, res["current"], res["current"], conf, "flag_mismatch",
                                          "mfr-lineage", {"lineage_conf": res["conf"], "note": res["note"]}, ["厂商已并购"]))
            ms = self.match(p, cands.get(norm_mpn(p.mpn), []))
            if not ms:
                continue
            r, level, conf = ms[0]
            self.best[p.id] = ms[0]
            if level == "L5" or conf < 0.6:
                continue
            ev = {"ihs_objid": r.objid, "ihs_mpn": r.mpn, "ihs_mfr": r.mfr_ihs, "mfr_current": r.mfr_current, "level": LEVEL_LABEL[level],
                  "lineage_conf": r.lineage_conf or "", "ihs_category": r.part_category, "status_2012": r.status, "candidates": len(ms)}
            method = f"ihs:{level}"
            if not p.description_en and r.description:
                act = "auto_fill" if conf >= self.auto_min else "review"
                props.append(Proposal(p.id, p.mpn, "description_en", "", r.description, r.description, conf, act, method, ev,
                                      ["英文描述自动补全" if act == "auto_fill" else "英文描述待复核"]))
            if not p.manufacturer and level == "L4" and conf >= 0.7:
                props.append(Proposal(p.id, p.mpn, "manufacturer", "", r.mfr_current, r.mfr_current, conf, "review", method, ev, ["厂商待复核"]))
            if not p.footprint and self.pkg:
                m = PKG_IN_DESC.search(f"{r.package} {r.description}")
                hit = self.pkg.map_name(m.group(1), self._root(p)) if m else None
                if hit:
                    fc = round(hit[1] * conf * 0.95, 3)
                    act = "auto_fill" if fc >= self.auto_min else "review"
                    props.append(Proposal(p.id, p.mpn, "footprint", "", hit[0].split(":")[-1], hit[0], fc, act, "ihs-package",
                                          dict(ev, package_text=m.group(1)), ["封装自动映射" if act == "auto_fill" else "封装待复核"]))
        return props


def reference_rows(db, part, tax=None) -> dict:
    """物料详情页的 IHS 参考面板数据。"""
    if not part.mpn_norm and not part.mpn:
        return {"rows": [], "mine": None}
    m = IhsMatcher(db, tax=tax)
    p = Part(id=part.id, mpn=part.mpn or "", manufacturer=part.manufacturer or "",
             category_id=part.new_category_id or part.orig_category_id or "")
    rows = m.lookup([part.mpn_norm or norm_mpn(part.mpn)]).get(part.mpn_norm or norm_mpn(part.mpn), [])
    out = []
    for r, level, conf in m.match(p, rows):
        try:
            params = json.loads(r.params_json or "{}")
        except ValueError:
            params = {}
        out.append({"r": r, "level": LEVEL_LABEL[level], "lv": level, "conf": conf, "params": params})
    mine = m.lineage.resolve(part.manufacturer, family_hint(m._root(p))) if part.manufacturer else None
    return {"rows": out[:20], "total": len(out), "mine": mine}
