"""KiCad 符号 / 封装 / 工程文件提取、型号匹配与 QA。

支持的输入：
- 符号库 *.kicad_sym（KiCad 6+）
- 封装库 *.pretty/*.kicad_mod
- 工程原理图 *.kicad_sch（提取 lib_symbols 里内嵌的符号，以及每个元件实例的 MPN/Value/Footprint 字段）
- 工程 PCB *.kicad_pcb（提取板上封装实例）
其它格式（Altium SchLib/PcbLib、EasyEDA JSON、Eagle lbr）先用转换器转成以上格式再进来，见 docs/README.md。

产出（SQLite + JSON）：
- symbols   : name, lib, source, license, pins[(num,name,type)], pin_count, footprint_field, datasheet, keywords, description, extends
- footprints: name, lib, source, license, pad_count, pads_hash, model3d, attr(smd/thru_hole)
- matches   : 器件 MPN ↔ 符号/封装 的候选匹配，带得分与 QA 结果
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Iterator

from .models import Part, Proposal

# 来源可信度（0~1）。多来源一致时取最高并加成。
SOURCE_TRUST = {
    "kicad-official": 1.0,
    "vendor-official": 1.0,      # 乐鑫 / RPi / ST 官方库
    "lcsc-easyeda": 0.9,
    "digikey-library": 0.9,
    "oshw-product": 0.8,         # 有实物产品的开源工程
    "community": 0.5,
}

SUFFIX_RE = re.compile(r"(TR|RL|T&R|-?TR\d?|R|T|E3|/96|-13|-7|-7A|-G|-E|-Q1|-H|-J|-K)$", re.I)


# --------------------------------------------------------------------------- 数据结构
@dataclass
class SymbolRec:
    name: str
    lib: str
    source: str
    source_kind: str
    license: str = ""
    extends: str = ""
    pins: list = field(default_factory=list)      # [(number, name, etype)]
    pin_count: int = 0
    footprint_field: str = ""
    datasheet: str = ""
    keywords: str = ""
    description: str = ""
    fp_filters: list = field(default_factory=list)
    mpn_field: str = ""
    commit: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class FootprintRec:
    name: str
    lib: str
    source: str
    source_kind: str
    license: str = ""
    pad_count: int = 0
    pads_hash: str = ""
    attr: str = ""
    model3d: str = ""
    description: str = ""
    tags: str = ""
    commit: str = ""

    def to_dict(self):
        return asdict(self)


# --------------------------------------------------------------------------- 解析
def _git_commit(path: Path) -> str:
    import subprocess
    for d in [path] + list(path.parents):
        if (d / ".git").exists():
            try:
                return subprocess.check_output(["git", "-C", str(d), "rev-parse", "--short", "HEAD"], text=True).strip()
            except Exception:
                return ""
    return ""


def _license_of(path: Path) -> str:
    for d in [path] + list(path.parents):
        for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
            f = d / name
            if f.exists():
                head = f.read_text(encoding="utf-8", errors="ignore")[:400]
                for key in ("CC-BY-SA", "CC BY-SA", "MIT", "Apache", "GPL", "BSD", "CERN-OHL", "TAPR"):
                    if key.lower() in head.lower():
                        return key
                return name
        if (d / ".git").exists():
            break
    return ""


def _sym_props(sym) -> dict:
    return {p.key: p.value for p in getattr(sym, "properties", [])}


def _sym_pins(sym, lib_index: dict | None = None) -> list:
    pins = []
    for u in getattr(sym, "units", []) or []:
        for pin in getattr(u, "pins", []) or []:
            pins.append((pin.number, pin.name, pin.electricalType))
    for pin in getattr(sym, "pins", []) or []:
        pins.append((pin.number, pin.name, pin.electricalType))
    if not pins and lib_index and getattr(sym, "extends", None):
        base = lib_index.get(sym.extends)
        if base is not None:
            return _sym_pins(base, lib_index)
    return pins


def _load_symbols(path: Path):
    """读取一个 .kicad_sym 文件，kiutils 失败时用正则兜底（只取名字/extends/属性/管脚号）。"""
    from kiutils.symbol import SymbolLib
    return SymbolLib.from_file(str(path)).symbols


def parse_symbol_lib(path: str | Path, source_kind: str = "community") -> Iterator[SymbolRec]:
    """解析一个 .kicad_sym 文件，或 KiCad 9+ 的 X.kicad_symdir 目录（每个符号一个文件，extends 跨文件）。"""
    path = Path(path)
    if path.is_dir():                                   # X.kicad_symdir：整个目录是一个库
        lib_name = path.name[:-len(".kicad_symdir")] if path.name.endswith(".kicad_symdir") else path.stem
        entries = []                                    # [(symbol, file)]
        for f in sorted(path.glob("*.kicad_sym")):
            try:
                entries += [(s, f) for s in _load_symbols(f)]
            except Exception:  # noqa
                continue
        lic, commit = _license_of(path), _git_commit(path)
    else:
        lib_name = path.stem
        if path.parent.name.endswith(".kicad_symdir"):  # 单文件但位于 symdir 内：库名取目录名，extends 在同目录找
            lib_name = path.parent.name[:-len(".kicad_symdir")]
            entries = []
            for f in sorted(path.parent.glob("*.kicad_sym")):
                try:
                    entries += [(s, f) for s in _load_symbols(f)]
                except Exception:  # noqa
                    continue
            entries = [(s, f) for s, f in entries if f == path] + [(s, f) for s, f in entries if f != path]
        else:
            entries = [(s, path) for s in _load_symbols(path)]
        lic, commit = _license_of(path.parent), _git_commit(path.parent)
    index = {s.entryName: s for s, _ in entries}
    for s, f in entries:
        if not path.is_dir() and f != path:
            continue
        props = _sym_props(s)
        pins = _sym_pins(s, index)
        yield SymbolRec(
            name=s.entryName, lib=lib_name, source=str(f), source_kind=source_kind, license=lic,
            extends=s.extends or "", pins=pins, pin_count=len({p[0] for p in pins}),
            footprint_field=props.get("Footprint", ""), datasheet=props.get("Datasheet", ""),
            keywords=props.get("ki_keywords", ""), description=props.get("Description", "") or props.get("ki_description", ""),
            fp_filters=(props.get("ki_fp_filters", "") or "").split(), mpn_field=props.get("MPN", props.get("Manufacturer Part Number", "")),
            commit=commit,
        )


def parse_footprint(path: str | Path, source_kind: str = "community") -> FootprintRec:
    from kiutils.footprint import Footprint
    path = Path(path)
    try:
        fp = Footprint.from_file(str(path))
    except Exception:
        return _parse_footprint_fallback(path, source_kind)
    pads = sorted((str(p.number), p.type, p.shape, round(p.position.X, 3), round(p.position.Y, 3),
                   round(p.size.X, 3), round(p.size.Y, 3)) for p in fp.pads)
    nums = {p[0] for p in pads if p[0] not in ("", "~")}
    h = hashlib.sha1(json.dumps(pads).encode()).hexdigest()[:12]
    attr = "smd" if getattr(fp.attributes, "type", "") == "smd" else ("thru_hole" if getattr(fp.attributes, "type", "") == "through_hole" else "")
    return FootprintRec(name=fp.entryName, lib=path.parent.name.replace(".pretty", ""), source=str(path), source_kind=source_kind,
                        license=_license_of(path.parent), pad_count=len(nums), pads_hash=h, attr=attr,
                        model3d=fp.models[0].path if fp.models else "", description=fp.description or "", tags=fp.tags or "",
                        commit=_git_commit(path.parent))


def _parse_footprint_fallback(path: Path, source_kind: str) -> FootprintRec:
    """kiutils 跟不上新版 KiCad 格式时的兜底：正则数焊盘。"""
    txt = path.read_text(encoding="utf-8", errors="ignore")
    nums = set(re.findall(r'\(pad\s+"([^"]*)"', txt))
    nums.discard(""); nums.discard("~")
    m3 = re.search(r'\(model\s+"([^"]+)"', txt)
    attr = "smd" if re.search(r"\(attr\s+smd", txt) else ("thru_hole" if re.search(r"\(attr\s+through_hole", txt) else "")
    return FootprintRec(name=path.stem, lib=path.parent.name.replace(".pretty", ""), source=str(path), source_kind=source_kind,
                        license=_license_of(path.parent), pad_count=len(nums), pads_hash=hashlib.sha1(txt.encode()).hexdigest()[:12],
                        attr=attr, model3d=m3.group(1) if m3 else "", description="(fallback-parser)", commit=_git_commit(path.parent))


def parse_schematic(path: str | Path, source_kind: str = "oshw-product") -> tuple[list[SymbolRec], list[dict]]:
    """返回 (内嵌符号列表, 元件实例列表[{ref, value, footprint, mpn, lib_id}])。"""
    from kiutils.schematic import Schematic
    path = Path(path)
    sch = Schematic.from_file(str(path))
    lic, commit = _license_of(path.parent), _git_commit(path.parent)
    index = {s.entryName: s for s in sch.libSymbols}
    syms = []
    for s in sch.libSymbols:
        props = _sym_props(s)
        pins = _sym_pins(s, index)
        syms.append(SymbolRec(name=s.entryName.split(":")[-1], lib=s.entryName.split(":")[0] if ":" in s.entryName else path.stem,
                              source=str(path), source_kind=source_kind, license=lic, extends=s.extends or "", pins=pins,
                              pin_count=len({p[0] for p in pins}), footprint_field=props.get("Footprint", ""),
                              datasheet=props.get("Datasheet", ""), keywords=props.get("ki_keywords", ""),
                              description=props.get("Description", "") or props.get("ki_description", ""),
                              mpn_field=props.get("MPN", props.get("Manufacturer Part Number", props.get("Part Number", ""))), commit=commit))
    inst = []
    for c in sch.schematicSymbols:
        props = {p.key: p.value for p in c.properties}
        inst.append({"ref": props.get("Reference", ""), "value": props.get("Value", ""), "footprint": props.get("Footprint", ""),
                     "mpn": props.get("MPN", props.get("Manufacturer Part Number", props.get("Part Number", props.get("PN", "")))),
                     "lib_id": c.libId, "source": str(path)})
    return syms, inst


def parse_board_footprints(path: str | Path, source_kind: str = "oshw-product") -> list[FootprintRec]:
    from kiutils.board import Board
    path = Path(path)
    board = Board.from_file(str(path))
    lic, commit = _license_of(path.parent), _git_commit(path.parent)
    out, seen = [], set()
    for fp in board.footprints:
        if fp.libId in seen:
            continue
        seen.add(fp.libId)
        pads = sorted((str(p.number), p.type, p.shape, round(p.size.X, 3), round(p.size.Y, 3)) for p in fp.pads)
        nums = {p[0] for p in pads if p[0] not in ("", "~")}
        lib, _, name = fp.libId.rpartition(":")
        out.append(FootprintRec(name=name, lib=lib, source=str(path), source_kind=source_kind, license=lic, pad_count=len(nums),
                                pads_hash=hashlib.sha1(json.dumps(pads).encode()).hexdigest()[:12],
                                attr="smd" if getattr(fp.attributes, "type", "") == "smd" else "", model3d=fp.models[0].path if fp.models else "",
                                description=fp.description or "", tags=fp.tags or "", commit=commit))
    return out


def scan(root: str | Path, source_kind: str = "community") -> dict:
    """递归扫描目录，返回 {"symbols": [...], "footprints": [...], "instances": [...], "errors": [...]}。"""
    root = Path(root)
    res = {"symbols": [], "footprints": [], "instances": [], "errors": []}
    for p in root.rglob("*"):
        try:
            if p.is_dir() and p.name.endswith(".kicad_symdir"):
                res["symbols"] += list(parse_symbol_lib(p, source_kind))
            elif p.suffix == ".kicad_sym":
                if p.parent.name.endswith(".kicad_symdir"):
                    continue                                # 已随目录整体解析
                res["symbols"] += list(parse_symbol_lib(p, source_kind))
            elif p.suffix == ".kicad_mod":
                res["footprints"].append(parse_footprint(p, source_kind))
            elif p.suffix == ".kicad_sch":
                s, i = parse_schematic(p, source_kind)
                res["symbols"] += s; res["instances"] += i
            elif p.suffix == ".kicad_pcb":
                res["footprints"] += parse_board_footprints(p, source_kind)
        except Exception as e:  # noqa
            res["errors"].append({"file": str(p), "error": str(e)[:200]})
    return res


# --------------------------------------------------------------------------- 索引 / 存储
class LibIndex:
    def __init__(self, db_path: str | Path = ":memory:"):
        self.con = sqlite3.connect(str(db_path))
        self.con.executescript("""
        create table if not exists symbols(name, norm, lib, source, source_kind, license, extends, pin_count int, pins, footprint_field, datasheet, keywords, description, mpn_field, commit_, unique(name, source));
        create table if not exists footprints(name, lib, source, source_kind, license, pad_count int, pads_hash, attr, model3d, description, tags, commit_, unique(name, source));
        create table if not exists instances(ref, value, footprint, mpn, lib_id, source);
        create index if not exists ix_sym_norm on symbols(norm);
        create index if not exists ix_fp_name on footprints(name);
        create table if not exists symbol_mpns(symbol_id int, mpn, norm, manufacturer, origin, created_at, unique(symbol_id, norm));
        create table if not exists footprint_mpns(footprint_id int, mpn, norm, manufacturer, origin, created_at, unique(footprint_id, norm));
        create index if not exists ix_smpn on symbol_mpns(norm);
        create index if not exists ix_fmpn on footprint_mpns(norm);
        """)

    def add(self, scanned: dict):
        self.con.executemany("insert or replace into symbols values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             [(s.name, norm_name(s.name), s.lib, s.source, s.source_kind, s.license, s.extends, s.pin_count,
                               json.dumps(s.pins), s.footprint_field, s.datasheet, s.keywords, s.description, s.mpn_field, s.commit) for s in scanned["symbols"]])
        self.con.executemany("insert or replace into footprints values(?,?,?,?,?,?,?,?,?,?,?,?)",
                             [(f.name, f.lib, f.source, f.source_kind, f.license, f.pad_count, f.pads_hash, f.attr, f.model3d, f.description, f.tags, f.commit) for f in scanned["footprints"]])
        self.con.executemany("insert into instances values(?,?,?,?,?,?)",
                             [(i["ref"], i["value"], i["footprint"], i["mpn"], i["lib_id"], i["source"]) for i in scanned["instances"]])
        self.con.commit()
        self._harvest_mpns(scanned)

    def _harvest_mpns(self, scanned: dict):
        """把符号 MPN 字段、工程实例的 MPN/Value 收进映射表（一个符号/封装 ↔ 多个 MPN）。"""
        import datetime as _dt
        now = _dt.datetime.utcnow().isoformat(timespec="seconds")
        rows = []
        for s in scanned["symbols"]:
            sid = self.con.execute("select rowid from symbols where name=? and source=?", (s.name, s.source)).fetchone()
            if not sid:
                continue
            for m in re.split(r"\s*[,;]\s*", s.mpn_field or ""):
                if m.strip():
                    rows.append((sid[0], m.strip(), norm_name(m), "", "symbol-field", now))
        self.con.executemany("insert or ignore into symbol_mpns values(?,?,?,?,?,?)", rows)
        # 工程实例：lib_id → 符号，footprint 字段 → 封装，MPN 字段/Value 作为料号
        rows_s, rows_f = [], []
        for i in scanned["instances"]:
            mpn = (i.get("mpn") or "").strip()
            if not mpn or len(mpn) < 4:
                continue
            sym = i["lib_id"].split(":")[-1]
            for (sid,) in self.con.execute("select rowid from symbols where name=? and source=?", (sym, i["source"])).fetchall():
                rows_s.append((sid, mpn, norm_name(mpn), "", "project-instance", now))
            fp = (i.get("footprint") or "").split(":")[-1]
            if fp:
                for (fid,) in self.con.execute("select rowid from footprints where name=? order by source_kind limit 1", (fp,)).fetchall():
                    rows_f.append((fid, mpn, norm_name(mpn), "", "project-instance", now))
        self.con.executemany("insert or ignore into symbol_mpns values(?,?,?,?,?,?)", rows_s)
        self.con.executemany("insert or ignore into footprint_mpns values(?,?,?,?,?,?)", rows_f)
        self.con.commit()

    # ---------- 浏览 / 维护（给管理后台用） ----------
    def list_items(self, kind: str, q: str = "", has_mpn: str = "", lib: str = "", source_kind: str = "", page: int = 1, size: int = 50):
        t, mt, key = ("symbols", "symbol_mpns", "symbol_id") if kind == "symbols" else ("footprints", "footprint_mpns", "footprint_id")
        where, args = ["1=1"], []
        if q:
            where.append(f"({t}.name like ? or {t}.lib like ? or exists(select 1 from {mt} m where m.{key}={t}.rowid and m.norm like ?))")
            args += [f"%{q}%", f"%{q}%", f"%{norm_name(q)}%"]
        if lib:
            where.append(f"{t}.lib = ?"); args.append(lib)
        if source_kind:
            where.append(f"{t}.source_kind = ?"); args.append(source_kind)
        if has_mpn == "1":
            where.append(f"exists(select 1 from {mt} m where m.{key}={t}.rowid)")
        elif has_mpn == "0":
            where.append(f"not exists(select 1 from {mt} m where m.{key}={t}.rowid)")
        w = " and ".join(where)
        total = self.con.execute(f"select count(*) from {t} where {w}", args).fetchone()[0]
        cols = "rowid, name, lib, source, source_kind, license, pin_count, footprint_field, mpn_field, description" if kind == "symbols" else "rowid, name, lib, source, source_kind, license, pad_count, attr, model3d, description"
        rows = self.con.execute(f"select {cols}, (select count(*) from {mt} m where m.{key}={t}.rowid) as n_mpn from {t} where {w} order by name limit ? offset ?", args + [size, (page - 1) * size]).fetchall()
        return total, rows

    def facets(self, kind: str):
        t, mt, key = ("symbols", "symbol_mpns", "symbol_id") if kind == "symbols" else ("footprints", "footprint_mpns", "footprint_id")
        libs = [r[0] for r in self.con.execute(f"select lib from {t} where lib<>'' group by lib order by lib limit 1000")]
        kinds = [r[0] for r in self.con.execute(f"select source_kind from {t} group by source_kind")]
        n_with = self.con.execute(f"select count(*) from {t} where exists(select 1 from {mt} m where m.{key}={t}.rowid)").fetchone()[0]
        n_total = self.con.execute(f"select count(*) from {t}").fetchone()[0]
        return {"libs": libs, "kinds": kinds, "with_mpn": n_with, "without_mpn": n_total - n_with, "total": n_total}

    def get_item(self, kind: str, rowid: int):
        t = "symbols" if kind == "symbols" else "footprints"
        r = self.con.execute(f"select rowid, * from {t} where rowid=?", (rowid,)).fetchone()
        if not r:
            return None
        cols = [d[0] for d in self.con.execute(f"select rowid, * from {t} limit 0").description]
        d = dict(zip(cols, r))
        if kind == "symbols":
            try:
                d["pins_list"] = json.loads(d.get("pins") or "[]")
            except Exception:
                d["pins_list"] = []
        return d

    def mpns(self, kind: str, rowid: int):
        mt, key = ("symbol_mpns", "symbol_id") if kind == "symbols" else ("footprint_mpns", "footprint_id")
        return self.con.execute(f"select mpn, manufacturer, origin, created_at from {mt} where {key}=? order by origin, mpn", (rowid,)).fetchall()

    def add_mpn(self, kind: str, rowid: int, mpn: str, manufacturer: str = "", origin: str = "manual"):
        import datetime as _dt
        mt = "symbol_mpns" if kind == "symbols" else "footprint_mpns"
        self.con.execute(f"insert or ignore into {mt} values(?,?,?,?,?,?)", (rowid, mpn.strip(), norm_name(mpn), manufacturer, origin, _dt.datetime.utcnow().isoformat(timespec="seconds")))
        self.con.commit()

    def del_mpn(self, kind: str, rowid: int, mpn: str):
        mt, key = ("symbol_mpns", "symbol_id") if kind == "symbols" else ("footprint_mpns", "footprint_id")
        self.con.execute(f"delete from {mt} where {key}=? and norm=?", (rowid, norm_name(mpn)))
        self.con.commit()

    def footprint_by_mpn(self, variants: list[str]):
        """按 MPN 映射表找封装：返回 (name, lib, source, source_kind, origin) 列表。"""
        out = []
        for v in variants:
            for r in self.con.execute("select f.name, f.lib, f.source, f.source_kind, m.origin from footprint_mpns m join footprints f on f.rowid=m.footprint_id where m.norm=?", (v,)):
                out.append(r)
        return out

    def stats(self) -> dict:
        q = lambda s: self.con.execute(s).fetchone()[0]
        return {"symbols": q("select count(*) from symbols"), "footprints": q("select count(*) from footprints"),
                "instances": q("select count(*) from instances"), "symbols_with_pins": q("select count(*) from symbols where pin_count>0")}

    def all_symbols(self) -> list[tuple]:
        """第 10 列（索引 9）为该符号的全部 MPN（映射表聚合，逗号分隔）。"""
        return self.con.execute("""select s.name, s.norm, s.lib, s.source, s.source_kind, s.license, s.pin_count, s.footprint_field, s.datasheet,
                                   coalesce((select group_concat(m.mpn, ',') from symbol_mpns m where m.symbol_id=s.rowid), s.mpn_field), s.description from symbols s""").fetchall()

    def footprint(self, name: str):
        return self.con.execute("select name, lib, source, source_kind, pad_count, model3d from footprints where name=? order by source_kind limit 1", (name,)).fetchone()


# --------------------------------------------------------------------------- 匹配
def norm_name(s: str) -> str:
    return re.sub(r"[^A-Z0-9X]", "", (s or "").upper())


def mpn_variants(mpn: str) -> list[str]:
    """型号规范化：原样 / 去包装后缀 / 去括号内容。"""
    base = re.sub(r"\(.*?\)", "", mpn or "").strip()
    out = [base]
    cur = base
    for _ in range(3):
        nxt = SUFFIX_RE.sub("", cur)
        if nxt == cur or len(nxt) < 4:
            break
        out.append(nxt); cur = nxt
    return [norm_name(x) for x in dict.fromkeys(out) if x]


def symbol_pattern(sym_name: str) -> re.Pattern:
    """KiCad 常用 x 作通配（STM32H503KBUx、ZPYxx）。"""
    n = norm_name(sym_name)
    return re.compile("^" + "".join(".?" if ch == "X" else re.escape(ch) for ch in n))


class SymbolMatcher:
    def __init__(self, index: LibIndex):
        self.rows = index.all_symbols()
        self.by_norm: dict[str, list] = {}
        self.by_mpn_field: dict[str, list] = {}
        for r in self.rows:
            self.by_norm.setdefault(r[1], []).append(r)
            for m in re.split(r"\s*[,;]\s*", r[9] or ""):   # 符号 MPN 字段可能列多个料号
                for v in mpn_variants(m):
                    self.by_mpn_field.setdefault(v, []).append(r)
        self.wild = [(symbol_pattern(r[0]), r) for r in self.rows if "X" in r[1] and len(r[1]) >= 5]
        self.index = index

    def match(self, p: Part) -> list[dict]:
        cands = []
        variants = mpn_variants(p.mpn)
        for v in variants:
            for r in self.by_mpn_field.get(v, []):
                cands.append(self._cand(r, 0.98 if v == variants[0] else 0.9, "mpn-field"))
            for r in self.by_norm.get(v, []):
                cands.append(self._cand(r, 1.0 if v == variants[0] else 0.9, "exact" if v == variants[0] else "suffix-stripped"))
        if not cands:
            for pat, r in self.wild:
                for v in variants:
                    m = pat.match(v)
                    if m and m.end() >= len(v) - 3:
                        cands.append(self._cand(r, 0.85, "wildcard"))
                        break
        if not cands:                       # 前缀匹配（符号名是型号的前缀，长度≥6）
            v0 = variants[0]
            for r in self.rows:
                n = r[1]
                if len(n) >= 6 and "X" not in n and v0.startswith(n) and len(v0) - len(n) <= 4:
                    cands.append(self._cand(r, 0.7, "prefix"))
        # 去重 + 排序
        best: dict[str, dict] = {}
        for c in cands:
            k = c["symbol"] + "|" + c["source"]
            if k not in best or c["score"] > best[k]["score"]:
                best[k] = c
        return sorted(best.values(), key=lambda c: -c["score"])

    def _cand(self, r, base: float, how: str) -> dict:
        name, norm, lib, source, kind, lic, pin_count, fp_field, datasheet, mpn_field, desc = r
        score = round(base * (0.6 + 0.4 * SOURCE_TRUST.get(kind, 0.5)), 3)
        return {"symbol": name, "lib": lib, "source": source, "source_kind": kind, "license": lic, "pin_count": pin_count,
                "footprint_field": fp_field, "datasheet": datasheet, "score": score, "how": how, "description": desc}


# --------------------------------------------------------------------------- QA
PKG_PIN_RE = re.compile(r"(?:^|[-_])(\d{1,3})(?:[-_]|$)")


def expected_pins_from_footprint(fp_name: str) -> int | None:
    """从 KiCad 封装名猜引脚数：SOIC-16_..., QFN-32-1EP_..., SOT-23-6, TO-252-2。"""
    m = re.match(r"^[A-Za-z]+[A-Za-z0-9]*?-(\d{1,3})(?:-\d+EP)?(?:[_-]|$)", fp_name or "")
    return int(m.group(1)) if m else None


def qa_symbol(cand: dict, part: Part, index: LibIndex) -> dict:
    """引脚数 vs 封装焊盘数 / 封装名中的引脚数；符号 Footprint 字段 vs 器件封装。"""
    issues, checks = [], {}
    fp_name = part.footprint or cand.get("footprint_field", "").split(":")[-1]
    exp = expected_pins_from_footprint(fp_name)
    checks["expected_pins_from_fp_name"] = exp
    if exp and cand["pin_count"] and abs(cand["pin_count"] - exp) > 1:     # 允许 EP 焊盘差 1
        issues.append(f"符号引脚数 {cand['pin_count']} ≠ 封装名引脚数 {exp}")
    fp = index.footprint(fp_name) if fp_name else None
    if fp:
        checks["footprint_pads"] = fp[4]
        if cand["pin_count"] and abs(cand["pin_count"] - fp[4]) > 1:
            issues.append(f"符号引脚数 {cand['pin_count']} ≠ 封装焊盘数 {fp[4]}")
    if part.footprint and cand.get("footprint_field") and part.footprint not in cand["footprint_field"]:
        issues.append("符号默认封装与器件封装不一致（可能是同系列不同封装）")
    if cand["pin_count"] == 0:
        issues.append("符号无引脚（可能是派生符号未解析到根）")
    return {"ok": not issues, "issues": issues, "checks": checks}


def propose_footprints_by_mpn(parts: Iterable[Part], index: LibIndex, auto_min: float = 0.85) -> Iterator[Proposal]:
    """封装 ← MPN 映射表（工程实例 / 人工维护）。"""
    for p in parts:
        if p.footprint:
            continue
        hits = index.footprint_by_mpn(mpn_variants(p.mpn))
        if not hits:
            continue
        name, lib, source, kind, origin = hits[0]
        conf = round((0.95 if origin == "manual" else 0.9) * (0.6 + 0.4 * SOURCE_TRUST.get(kind, 0.5)), 3)
        action = "auto_fill" if conf >= auto_min else "review"
        yield Proposal(p.id, p.mpn, "footprint", "", name, f"{lib}:{name}", conf, action, f"kicad-mpn:{kind}:{origin}",
                       {"source": source, "alternatives": [f"{h[1]}:{h[0]}" for h in hits[1:4]]}, ["封装自动匹配"] if action == "auto_fill" else ["封装待复核"])


def propose_symbols(parts: Iterable[Part], index: LibIndex, auto_min: float = 0.85, matcher: "SymbolMatcher | None" = None) -> Iterator[Proposal]:
    """matcher 可复用：构建一次要把整个符号索引读进内存（2 万多个符号约 1 秒），每批重建很浪费。"""
    m = matcher or SymbolMatcher(index)
    for p in parts:
        if p.symbol:
            continue
        cands = m.match(p)
        if not cands:
            continue
        best = cands[0]
        qa = qa_symbol(best, p, index)
        conf = best["score"] if qa["ok"] else round(best["score"] * 0.5, 3)
        if len(cands) > 1 and cands[1]["symbol"] == best["symbol"]:
            conf = min(0.99, conf + 0.05)   # 多来源一致
        action = "auto_fill" if conf >= auto_min else "review"
        yield Proposal(p.id, p.mpn, "symbol", "", best["symbol"], f"{best['lib']}:{best['symbol']}", conf, action,
                       f"kicad:{best['source_kind']}:{best['how']}",
                       {"source": best["source"], "license": best["license"], "pin_count": best["pin_count"], "qa": qa,
                        "alternatives": [c["lib"] + ":" + c["symbol"] for c in cands[1:4]], "datasheet": best["datasheet"]},
                       ["符号自动匹配"] if action == "auto_fill" else ["符号待复核"])
