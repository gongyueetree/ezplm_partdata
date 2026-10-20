"""按型号批量匹配原理图符号（第 1 阶段）：缺口表里的"专用"物料 × 本地符号仓库索引。

符号仓库：PartData/symbols_all（KiCad 官方、CERN、Altium AD10 厂商库、Celestial、Antmicro、EAGLE、Digi-Key、SparkFun，
全部已转成 KiCad 10 .kicad_sym，一个 MPN 一个文件），索引表 symbols_all_index.csv。build-index 把它们读成一个 SQLite 索引
（DATA_DIR/symindex.sqlite）；以后有新来源（开源工程、厂商参考设计、生成的符号）也追加进同一个索引。

匹配（对 118 万条只是字典查找，不逐个遍历符号）：
  型号变体（原样 / 去包装后缀 T&R、/NOPB、-REEL7、#PBF / Microchip "T-" / NXP 去 "/…" 与 ",118" / NXP 样片前缀 PK→MK 等）
  ① 精确：符号键 == 变体，或变体 = 符号键 + 包装后缀                         → A
  ② 通配：符号名里的 x（KiCad 习惯）或 Microchip 的 "24XX"                  → A
  ③ 基础型号：符号键是型号前缀（后面跟字母）且封装名/引脚数对得上           → B（封装名一致）/ C（只有引脚数一致）
  封装引脚数与符号引脚数冲突 → 丢弃；厂商不同（双方都确定）→ 降一级并打"厂商不同"。
"""
from __future__ import annotations

import csv
import fnmatch
import gzip
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from .ihs import Lineage, mfr_key
from .models import Proposal
from .staging import StagedPart, StagedProposal, SymbolFile

DESKTOP = Path.home() / "Desktop"
DEFAULT_ROOT = DESKTOP / "PartData/symbols_all"
DEFAULT_INDEX_CSV = DESKTOP / "PartData/symbols_all_index.csv"

SOURCE_TAGS = [("KiCad official", "kicad-official", 1.0), ("CERN", "cern", 0.98), ("Altium AD10", "altium-ad10", 0.97),
               ("Celestial", "celestial", 0.95), ("Antmicro", "antmicro", 0.95), ("Digi-Key", "digikey", 0.93),
               ("EAGLE", "eagle", 0.92), ("SparkFun", "sparkfun", 0.92), ("LCSC", "easyeda", 0.9)]       # EAGLE 的来源说明里含 SparkFun，须排在前面
TRUST = {t: w for _, t, w in SOURCE_TAGS}
SUFFIX = re.compile(r"^(?:T|R|TR|TR7|TR13|Z|RZ|ZR|RL|RL7|RL13|REEL|REEL7|REEL13|R7|R2|R4|Y|K|J|QL|FP|TRPBF|TRMPBF|MPBF|G4|E4|E3|NOPB|PBF|"
                    r"VAO|VSL|SL|T1|ACZ|AZ|M3|Q1|TBD|T13|T7|ND|500|250|2500|3000)*$")
CONF = {"A": 0.92, "B": 0.75, "C": 0.6}
NXP_RX = re.compile(r"NXP|恩智浦|FREESCALE|飞思卡尔|PHILIPS|飞利浦", re.I)
MCHP_RX = re.compile(r"MICROCHIP|微芯|ATMEL|爱特梅尔|MICROSEMI|MICREL", re.I)


def norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def source_tag(text: str) -> str:
    return next((t for k, t, _ in SOURCE_TAGS if k.lower() in (text or "").lower()), "other")


# --------------------------------------------------------------------------- 索引
_FP = re.compile(r'\(property "Footprint" "([^"]*)"')
_FILT = re.compile(r'\(property "ki_fp_filters" "([^"]*)"')
_PIN = re.compile(r'\(number "([^"]*)"')


def build_index(out_path: str | Path, root: str | Path = DEFAULT_ROOT, index_csv: str | Path = DEFAULT_INDEX_CSV, log=print) -> dict:
    """symbols_all_index.csv + 符号文件 → SQLite 索引（键、厂商、来源、引脚数、默认封装、封装过滤器、文件路径）。"""
    root, out_path = Path(root), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(out_path))
    con.executescript("""drop table if exists syms;
        create table syms(id integer primary key, key text, norm text, manufacturer text, mfr_guessed int, source text, origin text,
                          file text, npins int, footprint text, filters text);""")
    rows, n = [], 0
    with open(index_csv, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            f = root / r["Symbol file"]
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            fp, fl = _FP.search(txt), _FILT.search(txt)
            mfr = r.get("Manufacturer", "")
            rows.append((r["MPN / symbol name"], norm(r["MPN / symbol name"]), mfr.replace(" (guessed)", ""), int("guessed" in mfr or mfr == "Unknown"),
                         source_tag(r.get("Source", "")), f"{r.get('Source', '')}: {r.get('Source symbol', '')}", str(f),
                         len(set(_PIN.findall(txt))), fp.group(1) if fp else "", fl.group(1) if fl else ""))
            n += 1
            if len(rows) >= 5000:
                con.executemany("insert into syms(key,norm,manufacturer,mfr_guessed,source,origin,file,npins,footprint,filters) values(?,?,?,?,?,?,?,?,?,?)", rows)
                con.commit(); rows.clear()
                log(f"  索引 {n}")
    if rows:
        con.executemany("insert into syms(key,norm,manufacturer,mfr_guessed,source,origin,file,npins,footprint,filters) values(?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("create index ix_norm on syms(norm)")
    con.commit()
    st = {"symbols": n, "by_source": dict(con.execute("select source, count(*) from syms group by source").fetchall())}
    con.close()
    log(f"索引完成：{st} → {out_path}")
    return st


# --------------------------------------------------------------------------- 型号变体
def nxp_alts(m: str) -> list[str]:
    """NXP 样片/试产料号 → 量产料号（PKL13Z64VLK4 → MKL13Z64VLK4，PIMXRT… → MIMXRT…，PS32K… → S32K…）。"""
    m = m.upper()
    out = []
    for pat, rep in ((r"^PS32", "S32"), (r"^S9", "MC9"), (r"^KMPC", "MPC"), (r"^PPC", "SPC"), (r"^PC", "MC"), (r"^PK", "MK"),
                     (r"^PIMX", "MIMX"), (r"^PCIMX", "MCIMX"), (r"^PMIMX", "MIMX"), (r"^P(?=[A-Z]{2}\d)", "M"),
                     (r"^K(?=MC|MK|MPC)", ""), (r"^XS32", "S32"), (r"^XMIMX", "MIMX")):
        if re.match(pat, m):
            out.append(re.sub(pat, rep, m, count=1))
    return out


def variants(mpn: str, mfr: str) -> set[str]:
    def base(m):
        out = {norm(m), norm(re.sub(r"T(-|/)", r"\1", m, count=1))}        # Microchip 编带 PIC..T-I/PT
        for v in list(out):
            out.add(re.sub(r"(R2|R|T|TR|RL7|REEL7?)$", "", v))
        return out
    vs = base(mpn)
    if NXP_RX.search(mfr or ""):
        b = re.split(r"[/,:]", mpn)[0]
        if len(b) >= 6:
            vs |= base(b)
        for a in nxp_alts(mpn):
            vs |= base(a) | base(re.split(r"[/,:]", a)[0])
    return {v for v in vs if len(v) >= 3}


SUFFIX_ALT = SUFFIX.pattern[len("^(?:"):-len(")*$")]


def _pattern(key: str) -> tuple[str, re.Pattern, re.Pattern] | None:
    """含通配的符号名 → (字面前缀, 整串正则, 基础型号正则)；不含通配返回 None。
    整串正则 = 通配部分 + 可选包装后缀，用 fullmatch，让通配回溯到正确长度（TPS6291x ↔ TPS62918RPYR 不会把 8RP 吃掉）；
    基础型号正则要求通配之后紧跟字母。"""
    s = re.sub(r"(?i)xmega", "XMEGA", key)
    s = re.sub(r"(?<=\d)XX", "xx", s)
    if "x" not in s:
        return None
    rx, lit, seen_wild = "", "", False
    for ch in s:
        if ch == "x":
            rx += "[A-Z0-9]{0,3}"; seen_wild = True
        elif ch.isalnum():
            rx += re.escape(ch.upper())
            if not seen_wild:
                lit += ch.upper()
    if len(lit) < 3:
        return None
    return lit, re.compile(rx + "(?:" + SUFFIX_ALT + ")*"), re.compile(rx + "(?=[A-Z])")


# --------------------------------------------------------------------------- 封装校验
def _pkg_pins(pkg: str):
    from .symgap import pin_count
    n = pin_count(pkg)
    if n is None:
        return None
    return n, n + (1 if re.search(r"-1EP|_EP|EP\d|PowerPAD|ThermalVias|Tab|TO-?2(52|63)|D2?PAK|DFN|QFN|SON", pkg or "", re.I) else 0)


def pkg_ok(pkg: str, sym: dict):
    """True（封装名一致/过滤器命中）/ 'pins'（引脚数一致）/ False（冲突）/ None（无法判断）。"""
    if not pkg:
        return None
    fp = (sym["footprint"] or "").split(":")[-1]
    p = pkg.split(":")[-1]
    if fp and fp.upper() == p.upper():
        return True
    for f in (sym["filters"] or "").split():
        if fnmatch.fnmatch(p.upper(), f.upper()):
            return True
    pp = _pkg_pins(p)
    if pp and sym["npins"]:
        return "pins" if sym["npins"] in pp else False
    return None


# --------------------------------------------------------------------------- 匹配器
class SymbolIndexMatcher:
    def __init__(self, index_path: str | Path, lineage: Lineage | None = None):
        con = sqlite3.connect(str(index_path))
        cols = ["id", "key", "norm", "manufacturer", "mfr_guessed", "source", "origin", "file", "npins", "footprint", "filters"]
        self.syms = [dict(zip(cols, r)) for r in con.execute("select " + ",".join(cols) + " from syms")]
        con.close()
        self.exact: dict[str, list[dict]] = defaultdict(list)
        self.wild: dict[str, list[tuple[re.Pattern, re.Pattern, dict]]] = defaultdict(list)
        for s in self.syms:
            pat = _pattern(s["key"])
            if pat:
                self.wild[pat[0][:3]].append((pat[1], pat[2], s))
            else:
                self.exact[s["norm"]].append(s)
        self.lineage = lineage or Lineage()
        self._mk: dict[str, str] = {}

    def _mfr_key(self, name: str) -> str:
        if name not in self._mk:
            self._mk[name] = mfr_key(self.lineage.resolve(name)["current"]) if name else ""
        return self._mk[name]

    def _grade(self, v: str, s: dict, end: int, pkg: str):
        rest = v[end:]
        ok = pkg_ok(pkg, s)
        if ok is False:
            return None
        if rest == "" or SUFFIX.match(rest):
            return "A", ok
        key_last = s["key"][-1:] if s["key"] else ""
        if (not rest[0].isdigit() and not key_last.isdigit()) or (rest[0].isalpha() and ok):
            if ok is True:
                return "B", ok
            if ok == "pins":
                return "C", ok
        return None

    def match(self, mpn: str, mfr: str, pkg: str) -> list[dict]:
        cands = []
        for v in variants(mpn, mfr):
            for s in self.exact.get(v, []):                                     # ① 精确
                g = self._grade(v, s, len(v), pkg)
                if g:
                    cands.append((g, len(v), s, "exact"))
            for k in range(len(v) - 1, 3, -1):                                  # ①' 包装后缀 / ③ 基础型号：符号键是型号前缀
                for s in self.exact.get(v[:k], []):
                    g = self._grade(v, s, k, pkg)
                    if g:
                        cands.append((g, k, s, "suffix" if g[0] == "A" else "base"))
            for rx_full, rx_base, s in self.wild.get(v[:3], []):                # ② 通配
                if rx_full.fullmatch(v):
                    g = self._grade(v, s, len(v), pkg)
                    if g:
                        cands.append((g, len(v), s, "wildcard"))
                        continue
                m = rx_base.match(v)
                if m:
                    g = self._grade(v, s, m.end(), pkg)
                    if g:
                        cands.append((g, m.end(), s, "wildcard-base"))
        mine = self._mfr_key(mfr)
        out, seen = [], set()
        for (grade, ok), end, s, how in cands:
            if s["id"] in seen:
                continue
            seen.add(s["id"])
            tags = []
            theirs = "" if s["mfr_guessed"] else self._mfr_key(s["manufacturer"])
            if mine and theirs and mine != theirs:
                grade = {"A": "B", "B": "C", "C": ""}[grade]
                tags.append("厂商不同")
                if not grade:
                    continue
            conf = round(CONF[grade] * (TRUST.get(s["source"], 0.85) if grade == "A" else 1.0), 3)
            out.append({"grade": grade, "conf": conf, "how": how, "pkg_check": ok, "sym": s, "matched_len": end, "tags": tags})
        out.sort(key=lambda c: (c["grade"], -c["matched_len"], -TRUST.get(c["sym"]["source"], 0.8)))
        return out


# --------------------------------------------------------------------------- 主流程
def run(db, index_path: str | Path, out_dir: str | Path, auto_min: float = 0.88, write: bool = True, gap_csv: str | Path | None = None,
        job: str | None = None, log=print) -> dict:
    """缺口表（专用）→ 匹配 → 建议（write=True 时写暂存库）+ out/symbol_matches.csv + 覆盖率报告。
    gap_csv 给出时直接读这个缺口表（不连暂存库也能出匹配和报告，但 write 必须为 False）。"""
    from .symgap import gap_rows
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    job = job or f"symmatch-{datetime.utcnow():%Y%m%d-%H%M}"
    lineage = Lineage(db=db) if db is not None else Lineage()
    log("加载符号索引…")
    m = SymbolIndexMatcher(index_path, lineage)
    log(f"  {len(m.syms)} 个符号（精确键 {len(m.exact)}，通配 {sum(len(v) for v in m.wild.values())}）")
    if gap_csv:
        fh = open(gap_csv, encoding="utf-8-sig", newline="")
        rows = csv.DictReader(fh)
    else:
        rows = gap_rows(db, lineage=lineage, log=log)
    cov: dict[tuple, Counter] = defaultdict(Counter)
    st = Counter()
    pending_props: list[tuple[Proposal, dict]] = []
    mw = csv.writer(open(out / "symbol_matches.csv", "w", encoding="utf-8-sig", newline=""))
    mw.writerow(["part_id", "mpn", "manufacturer", "mfr_canonical", "category_path", "package", "grade", "confidence", "how", "pkg_check",
                 "symbol_key", "symbol_source", "symbol_manufacturer", "symbol_pins", "symbol_file", "alternatives", "tags"])

    def flush():
        if not (write and pending_props):
            return
        _save(db, pending_props, job, auto_min, st, log)
        pending_props.clear()

    for r in rows:
        root = (r["category_path"] or "").split(" > ")[0] or "（无分类）"
        mfr = r["mfr_canonical"] or "（无厂商）"
        st["missing"] += 1
        for k in ((("root", root)), ("mfr", mfr)):
            cov[k]["missing"] += 1
            cov[k][r["symbol_class"]] += 1
        st[r["symbol_class"]] += 1
        if r["symbol_class"] != "specific":
            continue
        if str(r.get("pending_proposal", "0")) == "1":
            st["skipped_pending"] += 1
            for k in (("root", root), ("mfr", mfr)):
                cov[k]["pending"] += 1
            continue
        cands = m.match(r["mpn"], r["manufacturer"], r["package"])
        if not cands:
            st["unmatched"] += 1
            for k in (("root", root), ("mfr", mfr)):
                cov[k]["unmatched"] += 1
            continue
        best = cands[0]
        s = best["sym"]
        st["grade_" + best["grade"]] += 1
        st["src_" + s["source"]] += 1
        for k in (("root", root), ("mfr", mfr)):
            cov[k]["grade_" + best["grade"]] += 1
        mw.writerow([r["part_id"], r["mpn"], r["manufacturer"], r["mfr_canonical"], r["category_path"], r["package"], best["grade"], best["conf"],
                     best["how"], best["pkg_check"], s["key"], s["source"], s["manufacturer"], s["npins"], s["file"],
                     "|".join(c["sym"]["key"] for c in cands[1:4]), ",".join(best["tags"])])
        if write:
            pending_props.append((r, best, cands))
            if len(pending_props) >= 2000:
                flush()
    flush()
    if gap_csv:
        fh.close()
    report = write_report(out, st, cov)
    log(f"匹配完成：{dict(st)}；报告 {report}")
    return dict(st)


def _save(db, items, job, auto_min, st, log):
    from .ezplm_lib import EzLibrary
    from .pipeline import _save_proposals, load_taxonomy
    ids = [r["part_id"] for r, _, _ in items]
    staged = {sp.id: sp for sp in db.execute(select(StagedPart).where(StagedPart.id.in_(ids))).scalars()}
    for o in db.execute(select(StagedProposal).where(StagedProposal.part_id.in_(ids), StagedProposal.review_status == "pending",
                                                     StagedProposal.method.like("symmatch%"))).scalars():
        o.review_status, o.review_note = "rejected", "superseded by symmatch"
    props = []
    for r, best, cands in items:
        if r["part_id"] not in staged:
            continue
        s = best["sym"]
        data = Path(s["file"]).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if db.get(SymbolFile, sha) is None:
            db.add(SymbolFile(sha256=sha, name=s["key"][:200], pin_count=s["npins"], pins_json=json.dumps(sorted(set(_PIN.findall(data.decode("utf-8", "ignore"))))),
                              source="symmatch", origin=s["origin"][:300], filename=Path(s["file"]).name[:300], size=len(data), content_gz=gzip.compress(data)))
            st["symbol_files_new"] += 1
        act = "auto_fill" if best["conf"] >= auto_min and best["grade"] == "A" else "review"
        tags = ["符号自动匹配" if act == "auto_fill" else "符号待复核"] + best["tags"] + ([f"符号{best['grade']}级"] if best["grade"] != "A" else [])
        ev = {"sha256": sha, "grade": best["grade"], "how": best["how"], "pkg_check": best["pkg_check"], "source": s["source"], "origin": s["origin"],
              "symbol_manufacturer": s["manufacturer"], "pin_count": s["npins"], "file": Path(s["file"]).name,
              "alternatives": [c["sym"]["key"] for c in cands[1:4]]}
        props.append(Proposal(r["part_id"], r["mpn"], "symbol", "", s["key"], s["key"], best["conf"], act, f"symmatch:{s['source']}", ev, tags))
    EzLibrary(db).annotate(props)
    stats = {"proposals": 0, "auto_fill": 0, "flag_mismatch": 0, "review": 0}
    _save_proposals(db, props, staged, job, load_taxonomy(db), stats)
    db.commit()
    for k, v in stats.items():
        st["saved_" + k] += v
    log(f"  已写建议 {st['saved_proposals']}（自动 {st['saved_auto_fill']}）")


# --------------------------------------------------------------------------- 覆盖率报告
def write_report(out: Path, st: Counter, cov: dict) -> Path:
    cols = ["missing", "generic", "specific", "pending", "grade_A", "grade_B", "grade_C", "unmatched"]
    with open(out / "symbol_coverage.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["维度", "名称"] + cols + ["专用匹配率"])
        for dim in ("root", "mfr"):
            keys = sorted((k for k in cov if k[0] == dim), key=lambda k: -cov[k]["missing"])
            for k in keys:
                c = cov[k]
                spec = c["specific"] - c["pending"]
                rate = (c["grade_A"] + c["grade_B"] + c["grade_C"]) / spec if spec else 0
                w.writerow([{"root": "一级分类", "mfr": "厂商"}[dim], k[1]] + [c[x] for x in cols] + [f"{rate:.1%}"])
    spec = st["specific"] - st["skipped_pending"]
    matched = st["grade_A"] + st["grade_B"] + st["grade_C"]
    gen = st["generic"]

    def table(dim, limit):
        keys = sorted((k for k in cov if k[0] == dim), key=lambda k: -cov[k]["missing"])[:limit]
        h = "".join(f"<th>{x}</th>" for x in ["名称", "缺符号", "通用", "专用", "已有待复核", "A", "B", "C", "未匹配", "专用匹配率"])
        body = ""
        for k in keys:
            c = cov[k]
            sp = c["specific"] - c["pending"]
            rate = (c["grade_A"] + c["grade_B"] + c["grade_C"]) / sp if sp else 0
            body += (f"<tr><td>{k[1]}</td>" + "".join(f"<td>{c[x]:,}</td>" for x in ["missing", "generic", "specific", "pending", "grade_A", "grade_B", "grade_C", "unmatched"])
                     + f"<td>{rate:.1%}</td></tr>")
        return f"<table><tr>{h}</tr>{body}</table>"

    srcs = sorted(((k[4:], v) for k, v in st.items() if k.startswith("src_")), key=lambda x: -x[1])
    html = f"""<!doctype html><meta charset="utf-8"><title>符号覆盖率</title>
<style>body{{font:14px -apple-system,"PingFang SC",sans-serif;margin:24px;color:#111}}table{{border-collapse:collapse;margin:8px 0 24px}}
td,th{{border:1px solid #ddd;padding:4px 8px;text-align:right}}td:first-child,th:first-child{{text-align:left}}th{{background:#f3f4f6}}
.k{{display:inline-block;margin-right:24px}}.k b{{font-size:22px;display:block}}</style>
<h1>原理图符号覆盖率（第 1 阶段：现有符号索引）</h1><p>生成时间 {datetime.now():%Y-%m-%d %H:%M}</p>
<div class="k"><b>{st['missing']:,}</b>缺符号物料</div><div class="k"><b>{gen:,}</b>通用（套通用符号）</div>
<div class="k"><b>{st['specific']:,}</b>专用</div><div class="k"><b>{matched:,}</b>专用已匹配（{matched / spec if spec else 0:.1%}）</div>
<div class="k"><b>{gen + matched:,}</b>合计可补（{(gen + matched) / st['missing'] if st['missing'] else 0:.1%}）</div>
<p>专用匹配等级：A {st['grade_A']:,} · B {st['grade_B']:,} · C {st['grade_C']:,} · 未匹配 {st['unmatched']:,} · 已有待复核建议（跳过）{st['skipped_pending']:,}</p>
<p>符号来源：{' · '.join(f'{k} {v:,}' for k, v in srcs)}</p>
<h2>按一级分类</h2>{table('root', 60)}<h2>按厂商（前 80）</h2>{table('mfr', 80)}"""
    p = out / "symbol_coverage.html"
    p.write_text(html, encoding="utf-8")
    return p
