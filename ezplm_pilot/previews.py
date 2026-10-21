"""物料详情页 / 复核列表的在线预览：原理图符号、PCB 封装、3D 模型。

文件从哪来：
- 符号：三批导入的符号文件（symbol_files，按 SHA）；或扫描过的 KiCad 符号库（libindex.sqlite 记了文件路径）
- 封装：libindex.sqlite 记的 .kicad_mod 路径；服务器上没有（没扫描 / 卷被清）时按"库名.pretty/封装名"到 KiCad 官方 GitLab 取，缓存到 DATA_DIR/cache
- 3D：封装文件里 (model "${KICADx_3DMODEL_DIR}/库.3dshapes/X.step")，按相对路径到 KiCad 官方 kicad-packages3D 取 STEP，缓存后给浏览器（浏览器端用 occt-import-js 解析）
ezPLM 自有的符号/封装没有文件，预览不了，页面上写明。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from . import fprender, settings, symrender

RAW = "https://gitlab.com/kicad/libraries/{repo}/-/raw/master/{path}"
CACHE = settings.DATA_DIR / "cache"
MODEL_REL = re.compile(r"^[\w.+-]+\.3dshapes/[\w.+\-]+\.(step|stp)$", re.I)       # 只允许官方 3D 库里的文件，防路径穿越
FP_NAME = re.compile(r"^[\w.+\-]+$")


def _fetch(repo: str, path: str, dest: Path, timeout: float = 20) -> bytes | None:
    if dest.exists():
        return dest.read_bytes()
    try:
        import requests
        r = requests.get(RAW.format(repo=repo, path=path), timeout=timeout)
        if r.status_code != 200 or not r.content:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return r.content
    except Exception:  # noqa —— 网络不通时预览缺一块，不报错
        return None


def _idx():
    p = settings.LIBINDEX_PATH
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True) if p.exists() else None


# --------------------------------------------------------------------------- 符号
def symbol_by_sha(db, sha: str) -> dict | None:
    from .symsets import file_content
    got = file_content(db, sha)
    if not got:
        return None
    pv = symrender.preview(got[1].decode("utf-8", "ignore"))
    if pv:
        pv["source"] = "导入符号文件"
    return pv


def symbol_by_name(name: str) -> dict | None:
    """扫描过的 KiCad 符号库里按名字找（名字可带 "库:" 前缀）。KiCad 9+ 的 .kicad_symdir 每个符号一个文件，extends 的父符号一起读。"""
    lib, _, nm = (name or "").rpartition(":")
    con = _idx()
    if not con or not nm:
        return None
    rows = con.execute("select source, lib from symbols where name=?" + (" and lib=?" if lib else "") + " limit 5", (nm, lib) if lib else (nm,)).fetchall()
    for src, lb in rows:
        p = Path(src)
        texts = []
        if p.is_dir():
            f = p / f"{nm}.kicad_sym"
            if f.exists():
                texts.append(f.read_text("utf-8", "ignore"))
                m = re.search(r'\(extends\s+"([^"]+)"', texts[0])
                if m and (p / f"{m.group(1)}.kicad_sym").exists():
                    texts.insert(0, (p / f"{m.group(1)}.kicad_sym").read_text("utf-8", "ignore"))
        elif p.exists():
            texts.append(p.read_text("utf-8", "ignore"))
        if texts:
            # 多个文件拼成一个库：去掉各自的外层 (kicad_symbol_lib … ) 只留 symbol 块
            body = "".join(t[t.find("(symbol"):t.rstrip().rfind(")")] for t in texts)
            pv = symrender.preview(f"(kicad_symbol_lib {body})", nm)
            if pv:
                pv["source"] = f"KiCad 库 {lb}"
                return pv
    return None


# --------------------------------------------------------------------------- 封装 / 3D
def footprint_text(name: str) -> tuple[str, str] | None:
    """→ (库名, 文件内容)。名字可带 "库:" 前缀。"""
    lib, _, nm = (name or "").rpartition(":")
    if not nm or not FP_NAME.match(nm) or (lib and not FP_NAME.match(lib)):
        return None
    con = _idx()
    rows = con.execute("select source, lib from footprints where name=?" + (" and lib=?" if lib else "") + " limit 5",
                       (nm, lib) if lib else (nm,)).fetchall() if con else []
    for src, lb in rows:
        p = Path(src)
        if p.suffix == ".kicad_mod" and p.exists():
            return lb, p.read_text("utf-8", "ignore")
    for lb in ([lib] if lib else []) + [r[1] for r in rows if r[1]]:
        data = _fetch("kicad-footprints", f"{lb}.pretty/{nm}.kicad_mod", CACHE / "footprints" / f"{lb}.pretty" / f"{nm}.kicad_mod")
        if data:
            return lb, data.decode("utf-8", "ignore")
    return None


def model_ref(fp_text: str) -> dict | None:
    """封装文件里第一个 3D 模型 → {rel, offset, scale, rotate}；只认 KiCad 官方 3D 库路径。"""
    m = re.search(r'\(model\s+"([^"]+)"', fp_text)
    if not m:
        return None
    rel = re.sub(r"^\$\{[^}]+\}/", "", m.group(1))
    rel = re.sub(r"\.wrl$", ".step", rel, flags=re.I)                 # 旧封装写 .wrl，新版官方库只有 .step
    if not MODEL_REL.match(rel):
        return None
    tail = fp_text[m.end():m.end() + 400]

    def xyz(key, default):
        mm = re.search(rf"\({key}\s+\(xyz\s+([-\d.e]+)\s+([-\d.e]+)\s+([-\d.e]+)\)", tail)
        return [float(v) for v in mm.groups()] if mm else default
    return {"rel": rel, "offset": xyz("offset", [0, 0, 0]), "scale": xyz("scale", [1, 1, 1]), "rotate": xyz("rotate", [0, 0, 0])}


def model_bytes(rel: str) -> bytes | None:
    if not MODEL_REL.match(rel or ""):
        return None
    return _fetch("kicad-packages3D", rel, CACHE / "3d" / rel, timeout=40)


def footprint_preview(name: str) -> dict | None:
    got = footprint_text(name)
    if not got:
        return None
    lib, text = got
    pv = fprender.preview(text)
    if pv:
        pv["lib"] = lib
        pv["model"] = model_ref(text)
    return pv


# --------------------------------------------------------------------------- 详情页汇总
def for_part(db, p, props) -> dict:
    """详情页三块：符号、封装、3D。每块给出预览和"它是哪个值、什么状态"。"""
    import json
    out = {"symbol": None, "footprint": None}
    sym_props = [r for r in props if r.field == "symbol" and r.review_status in ("auto", "approved", "pending")]
    order = {"approved": 0, "auto": 1, "pending": 2}
    for r in sorted(sym_props, key=lambda r: (order[r.review_status], -(r.confidence or 0))):
        ev = json.loads(r.evidence_json or "{}")
        if ev.get("sha256"):
            pv = symbol_by_sha(db, ev["sha256"])
            if pv:
                pv.update(value=r.proposed, status={"approved": "已通过", "auto": "已自动写入", "pending": "建议（待复核）"}[r.review_status])
                out["symbol"] = pv
                break
    if not out["symbol"]:
        for val, st in ((p.new_symbol, "新值"), (p.orig_symbol, "ezPLM 现值")):
            if val:
                pv = symbol_by_name(val)
                out["symbol"] = pv and {**pv, "value": val, "status": st} or {"missing": val, "status": st}
                break
    fp_vals = [(p.new_footprint, "新值"), (p.orig_footprint, "ezPLM 现值")]
    fp_vals += [(r.proposed, "建议（待复核）") for r in sorted((r for r in props if r.field == "footprint" and r.review_status == "pending"),
                                                       key=lambda r: -(r.confidence or 0))[:1]]
    for val, st in fp_vals:
        if val:
            pv = footprint_preview(val)
            out["footprint"] = pv and {**pv, "value": val, "status": st} or {"missing": val, "status": st}
            break
    return out
