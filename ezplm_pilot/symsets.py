"""三批已对应 ezPLM 物料 ID 的原理图符号合并导入（本地运行，文件在本机）。

来源（默认路径在桌面）：
- send   同事整理：PartData/Send/匹配成功.csv（partlib_id → symbols/厂商/型号.kicad_sym，Altium 厂商库转换，型号+厂商完全匹配）
- gpt    GPT 整理：Codex_Data/Symbols_Database_Import.xlsx「符号回传」（ID → 文件名）+ KiCad_Symbols_All_4_Batches_Matched.zip
- claude Claude 整理：PartData/symbol_index.csv（ID → PartData/symbols/文件，带质量等级 A/B/C 和原始来源）

合并：按 ezPLM 物料 ID，比较各来源符号的引脚编号集合——
- 一致 → 一条建议（按优先级取一个文件；多个来源一致加分），置信度够就自动写入
- 不一致 → 每个候选各出一条"复核"建议，打"符号冲突"
已有符号的物料、暂存库里没有的物料跳过并计数。符号文件按 SHA-256 去重、gzip 存进 symbol_files。
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from .models import Proposal
from .staging import StagedPart, StagedProposal, SymbolFile

DESKTOP = Path.home() / "Desktop"
DEFAULTS = {
    "send_csv": DESKTOP / "PartData/Send/匹配成功.csv",
    "gpt_xlsx": DESKTOP / "Codex_Data/Symbols_Database_Import.xlsx",
    "gpt_zip": DESKTOP / "Codex_Data/KiCad_Symbols_All_4_Batches_Matched.zip",
    "claude_csv": DESKTOP / "PartData/symbol_index.csv",
    "claude_dir": DESKTOP / "PartData/symbols",
}
# 来源/等级 → 基础置信度（多个来源引脚一致时再加分）
BASE = {("claude", "A"): 0.95, ("send", ""): 0.92, ("gpt", ""): 0.90, ("claude", "A-"): 0.88, ("claude", "B"): 0.85, ("claude", "C"): 0.75}
_PIN = re.compile(r'\(number "([^"]*)"')
_NAME = re.compile(r'\(symbol "([^"]+)"')


def _sym_info(text: str) -> tuple[str, list[str]]:
    m = _NAME.search(text)
    return (m.group(1) if m else ""), sorted(set(_PIN.findall(text)))


def _cand(source, grade, part_id, mpn, mfr, data: bytes, filename, origin) -> dict:
    text = data.decode("utf-8", "ignore")
    name, pins = _sym_info(text)
    return {"source": source, "grade": grade or "", "part_id": part_id, "mpn": mpn, "manufacturer": mfr, "data": data,
            "sha256": hashlib.sha256(data).hexdigest(), "name": name or Path(filename).stem, "pins": pins,
            "filename": filename, "origin": origin, "base": BASE.get((source, grade or ""), BASE.get((source, ""), 0.8))}


def read_sources(paths: dict | None = None, log=print) -> list[dict]:
    p = {**DEFAULTS, **(paths or {})}
    out, missing = [], Counter()
    # ---- send
    f = Path(p["send_csv"])
    if f.exists():
        with open(f, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                fp = f.parent / r["AD符号文件"]
                if not fp.exists():
                    missing["send"] += 1
                    continue
                out.append(_cand("send", "", r["partlib_id"], r.get("原厂型号MPN", ""), r.get("厂商名称", ""), fp.read_bytes(),
                                 r["AD符号文件"], f"同事整理（Altium 厂商库转换，{r.get('状态', '')}）"))
        log(f"  send：{sum(1 for c in out if c['source'] == 'send')} 条")
    # ---- gpt
    fx, fz = Path(p["gpt_xlsx"]), Path(p["gpt_zip"])
    if fx.exists() and fz.exists():
        import pandas as pd
        df = pd.read_excel(fx, sheet_name="符号回传", dtype=str).fillna("")
        zf = zipfile.ZipFile(fz)
        names = {Path(n).name: n for n in zf.namelist() if n.endswith(".kicad_sym")}
        n0 = len(out)
        for r in df.to_dict("records"):
            zn = names.get(r["名称"])
            if not zn:
                missing["gpt"] += 1
                continue
            out.append(_cand("gpt", "", r["ID"], r.get("原厂型号", ""), r.get("制造商", ""), zf.read(zn), r["名称"], "GPT 整理（许可证见 zip 内 LICENSES）"))
        log(f"  gpt：{len(out) - n0} 条")
    # ---- claude
    fc, fd = Path(p["claude_csv"]), Path(p["claude_dir"])
    if fc.exists():
        n0 = len(out)
        with open(fc, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                fp = fd / r["Symbol file"]
                if not fp.exists():
                    missing["claude"] += 1
                    continue
                out.append(_cand("claude", (r.get("Grade") or "").strip(), r["ID"], r.get("MPN", ""), r.get("Manufacturer", ""), fp.read_bytes(),
                                 r["Symbol file"], f"Claude 整理（{(r.get('Source') or '').strip()}）"))
        log(f"  claude：{len(out) - n0} 条")
    if missing:
        log(f"  ! 找不到符号文件：{dict(missing)}")
    return out


def merge(cands: list[dict]) -> dict[str, list[dict]]:
    """按物料 ID 分组 → {part_id: [候选]}，同一物料同一文件（SHA）的候选只留优先级最高的。"""
    by_part: dict[str, dict] = defaultdict(dict)
    for c in cands:
        cur = by_part[c["part_id"]].get(c["sha256"])
        if cur is None or c["base"] > cur["base"]:
            by_part[c["part_id"]][c["sha256"]] = c
    return {pid: sorted(v.values(), key=lambda c: -c["base"]) for pid, v in by_part.items()}


def plan(groups: dict[str, list[dict]], auto_min: float = 0.85) -> list[tuple[dict, Proposal]]:
    """每个物料 → 建议。引脚编号集合一致（或都解析不出引脚）视为同一个符号。"""
    out = []
    for pid, cs in groups.items():
        pinsets = {tuple(c["pins"]) for c in cs if c["pins"]}
        if len(pinsets) <= 1:
            best = cs[0]
            srcs = sorted({c["source"] for c in cs})
            conf = round(min(0.98, best["base"] + 0.02 * (len(srcs) - 1)), 3)
            act = "auto_fill" if conf >= auto_min else "review"
            ev = {"sha256": best["sha256"], "sources": srcs, "pin_count": len(best["pins"]), "origin": best["origin"], "file": best["filename"],
                  "source_mpn": best["mpn"], "source_manufacturer": best["manufacturer"]}
            out.append((best, Proposal(pid, best["mpn"], "symbol", "", best["name"], best["name"], conf, act, f"symset:{best['source']}", ev,
                                       ["符号自动匹配" if act == "auto_fill" else "符号待复核"])))
        else:                                                         # 引脚不一致：每个候选都进复核
            for c in cs:
                ev = {"sha256": c["sha256"], "sources": [c["source"]], "pin_count": len(c["pins"]), "origin": c["origin"], "file": c["filename"],
                      "conflict": [{"source": x["source"], "pin_count": len(x["pins"]), "file": x["filename"]} for x in cs if x is not c]}
                out.append((c, Proposal(pid, c["mpn"], "symbol", "", c["name"], c["name"], 0.6, "review", f"symset:{c['source']}", ev,
                                        ["符号冲突", "符号待复核"])))
    return out


def run(db, paths: dict | None = None, auto_min: float = 0.85, job: str | None = None, log=print) -> dict:
    from .ezplm_lib import EzLibrary
    from .pipeline import _save_proposals, load_taxonomy
    job = job or f"symset-{datetime.utcnow():%Y%m%d-%H%M}"
    log("读取三批符号…")
    groups = merge(read_sources(paths, log))
    st = Counter(parts_in_sets=len(groups))
    ids = list(groups)
    staged = {}
    for i in range(0, len(ids), 1000):
        for sp in db.execute(select(StagedPart).where(StagedPart.id.in_(ids[i:i + 1000]))).scalars():
            staged[sp.id] = sp
    st["not_in_staging"] = len(ids) - len(staged)
    todo = {}
    for pid, cs in groups.items():
        sp = staged.get(pid)
        if not sp:
            continue
        if sp.new_symbol or sp.orig_symbol:
            st["already_has_symbol"] += 1
            continue
        todo[pid] = cs
    planned = plan(todo, auto_min)
    st["conflict_parts"] = len({p.part_id for _, p in planned if "符号冲突" in p.tags})

    # 符号文件入库（按 SHA 去重，已存在的不重复写）
    shas = {c["sha256"]: c for c, _ in planned}
    have = set()
    keys = list(shas)
    for i in range(0, len(keys), 1000):
        have |= {s for (s,) in db.execute(select(SymbolFile.sha256).where(SymbolFile.sha256.in_(keys[i:i + 1000])))}
    new_files = [c for s, c in shas.items() if s not in have]
    for i in range(0, len(new_files), 500):
        for c in new_files[i:i + 500]:
            db.add(SymbolFile(sha256=c["sha256"], name=c["name"][:200], pin_count=len(c["pins"]), pins_json=json.dumps(c["pins"]), source=c["source"],
                              origin=c["origin"][:300], filename=c["filename"][:300], size=len(c["data"]), content_gz=gzip.compress(c["data"])))
        db.commit()
        log(f"  符号文件 {min(i + 500, len(new_files))}/{len(new_files)}")
    st["symbol_files_new"] = len(new_files)

    # 作废这些物料上一轮的同类建议，再写新建议
    pids = list(todo)
    for i in range(0, len(pids), 1000):
        for o in db.execute(select(StagedProposal).where(StagedProposal.part_id.in_(pids[i:i + 1000]), StagedProposal.review_status == "pending",
                                                         StagedProposal.method.like("symset%"))).scalars():
            o.review_status, o.review_note = "rejected", "superseded by symset import"
    props = [p for _, p in planned]
    EzLibrary(db).annotate(props)
    stats = {"job": job, "proposals": 0, "auto_fill": 0, "flag_mismatch": 0, "review": 0}
    for i in range(0, len(props), 5000):
        chunk = props[i:i + 5000]
        _save_proposals(db, chunk, {p.part_id: staged[p.part_id] for p in chunk}, job, load_taxonomy(db), stats)
        db.commit()
    st.update({k: v for k, v in stats.items() if k != "job"})
    st["by_source"] = dict(Counter(p.method.split(":")[1] for p in props))
    log(f"完成：{dict(st)}")
    return dict(st)


def file_content(db, sha256: str) -> tuple[str, bytes] | None:
    row = db.get(SymbolFile, sha256)
    return (row.filename or f"{row.name}.kicad_sym", gzip.decompress(row.content_gz)) if row else None
