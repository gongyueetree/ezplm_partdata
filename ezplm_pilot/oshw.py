"""开源硬件工程 BOM → 元器件封装（第一阶段，纯规则，不用大模型）。

数据：开源硬件工作区（约 14,000 个 KiCad 工程）里 ezPLM 已解析好的 BOM 行，由后端导出成 CSV（格式见 docs/OSHW_EXPORT.md）。
开源 BOM 的 mpn 字段大多为空，IC 的型号通常写在 value 里：
  - 完整型号（TPS2061CDBVT + SOT-23-5）→ 直接对上物料
  - 通用型号（TL074、MAX202）→ 对上以它开头的物料，但必须封装一致（TL074 + SOIC-14 → TL074CD，不是 DIP-14 的 TL074CN）
电阻电容等无源件（位号 R/C/L/FB…、value 是 10k / 0.1uF）不参与。

同一型号在多个工程里出现时按工程数投票；只对还没有封装的物料出建议，封装名必须是 ezPLM 或已扫描 KiCad 库里有的
（工程自带的非标准封装留给第二阶段：下载工程 zip 提取符号和封装）。已有封装的物料用来统计"开源用法与现有封装一致的比例"，作为匹配质量的参考。
"""
from __future__ import annotations

import bisect
import csv
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, insert, select

from .ihs import norm_mpn
from .models import Proposal
from .staging import OshwBom, OshwItem, OshwMatch, StagedPart, StagedProposal

BASE_CONF = {"mpn": 0.90, "value": 0.85, "generic": 0.75}
MAX_GENERIC_FANOUT = 60                     # 通用型号匹配到的物料太多（如 "LM"）就放弃
MAX_SUFFIX = 8                              # 通用型号后面最多再接几个字符

_PASSIVE_DES = re.compile(r"^(R|RN|RV|VR|C|CP|L|FB|F|TP|JP|MH|H|FID|NT|LOGO|REF|G|TH)$", re.I)
_PASSIVE_VAL = re.compile(r"^\s*[\d.,]+\s*([pnuµμmkKMG]|meg)?\s*(F|H|Ω|R|ohm|ohms)?\s*(\d+%|[\d.]+V)?\s*$", re.I)


# --------------------------------------------------------------------------- 工具
def parse_notes(notes: str) -> dict:
    n = notes or ""
    g = lambda pat: (re.search(pat, n) or [None, ""])[1].strip()
    return {"repo": g(r"来源[：:]\s*(https?://[^\s；;，,]+)"), "commit_": g(r"提交[：:]\s*([0-9a-f]{7,40})"),
            "subdir": g(r"目录[：:]\s*([^；;]+)"), "license": g(r"许可[：:]\s*([^，,；;\s]+)")}


def match_key(value: str, mpn: str, designators: str) -> str:
    """参与匹配的规范化型号；无源件、太短、不像型号的返回空串。"""
    if mpn and norm_mpn(mpn):
        return norm_mpn(mpn)
    first = (designators or "").split(",")[0].strip()
    prefix = re.match(r"[A-Za-z]+", first)
    if prefix and _PASSIVE_DES.match(prefix.group(0)):
        return ""
    v = (value or "").strip()
    if not v or _PASSIVE_VAL.match(v) or len(v) > 40 or " " in v.strip():
        return ""
    k = norm_mpn(v)
    if len(k) < 4 or not re.search(r"[A-Z]", k) or not re.search(r"\d", k):
        return ""
    return k


_PKG = [  # (正则, 封装族, 默认引脚数)；按封装名常见写法
    (r"SOT-?23-?(\d)?", "SOT23", 3), (r"SOT-?223", "SOT223", 4), (r"SOT-?89", "SOT89", 3), (r"SC-?70-?(\d)?", "SC70", 3),
    (r"SOT-?363", "SC70", 6), (r"SOT-?353", "SC70", 5), (r"TSSOP-?(\d+)", "TSSOP", None), (r"MSOP-?(\d+)", "MSOP", None),
    (r"SSOP-?(\d+)", "SSOP", None), (r"SOIC-?(\d+)|SO-(\d+)\b|SOP-?(\d+)", "SOIC", None), (r"P?DIP-?(\d+)", "DIP", None),
    (r"QFN-?(\d+)", "QFN", None), (r"DFN-?(\d+)", "DFN", None), (r"LQFP-?(\d+)", "LQFP", None), (r"TQFP-?(\d+)", "TQFP", None),
    (r"TO-?220-?(\d)?", "TO220", 3), (r"TO-?263-?(\d)?", "TO263", 3), (r"TO-?252-?(\d)?|DPAK", "TO252", 3), (r"TO-?92", "TO92", 3),
    (r"SOD-?123", "SOD123", 2), (r"SOD-?323", "SOD323", 2), (r"DO-?41", "DO41", 2), (r"DO-?214AC|\bSMA\b", "SMA", 2),
]
_PKG = [(re.compile(p, re.I), fam, pins) for p, fam, pins in _PKG]


def pkg_sig(text: str) -> tuple[str, int | None] | None:
    """封装名/封装描述 → (封装族, 引脚数)。SOIC-16_3.9x9.9mm_P1.27mm → ("SOIC", 16)；认不出返回 None。"""
    t = text or ""
    for pat, fam, default in _PKG:
        m = pat.search(t)
        if m:
            nums = [g for g in m.groups() if g] if m.groups() else []
            return fam, int(nums[0]) if nums else default
    return None


def same_package(a, b) -> bool:
    if not a or not b or a[0] != b[0]:
        return False
    return a[1] is None or b[1] is None or a[1] == b[1]


# --------------------------------------------------------------------------- 导入 CSV
def load_csv(path: str | Path, db, log=print) -> dict:
    """导入后端导出的 BOM 行 CSV。按 bom_id 先删后插，可重复导入（后导的覆盖先导的）。"""
    boms: dict[str, dict] = {}
    items: list[dict] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        missing = {"project_id", "bom_id", "item_id", "value", "footprint", "designators"} - set(r.fieldnames or [])
        if missing:
            raise ValueError(f"CSV 缺少列：{sorted(missing)}（格式见 docs/OSHW_EXPORT.md）")
        for row in r:
            g = lambda k: (row.get(k) or "").strip()
            bid = g("bom_id")
            if bid and bid not in boms:
                boms[bid] = dict(bom_id=bid, bom_no=g("bom_no"), project_id=g("project_id"), project_name=g("project_name")[:300],
                                 project_category=g("project_category")[:120], eda_tool=g("eda_tool"), parse_status=g("parse_status"),
                                 zip_file_id=g("zip_file_id"), zip_url=g("zip_url"), **{k: v[:500] for k, v in parse_notes(g("bom_notes")).items()})
            if g("item_id"):
                try:
                    qty = int(float(g("quantity") or 0))
                except ValueError:
                    qty = 0
                items.append(dict(item_id=g("item_id"), bom_id=bid, value=g("value")[:300], mpn=g("mpn")[:200], manufacturer=g("manufacturer")[:200],
                                  footprint=g("footprint")[:300], designators=g("designators"), quantity=qty, part_id=g("part_id") or None,
                                  key_norm=match_key(g("value"), g("mpn"), g("designators")) or None))
    items = list({i["item_id"]: i for i in items}.values())
    ids = list(boms)
    for i in range(0, len(ids), 1000):
        db.execute(delete(OshwItem).where(OshwItem.bom_id.in_(ids[i:i + 1000])))
        db.execute(delete(OshwBom).where(OshwBom.bom_id.in_(ids[i:i + 1000])))
    for i in range(0, len(ids), 2000):
        db.execute(insert(OshwBom), [boms[b] for b in ids[i:i + 2000]])
    for i in range(0, len(items), 2000):
        db.execute(insert(OshwItem), items[i:i + 2000])
        log(f"  BOM 行 {min(i + 2000, len(items))}/{len(items)}")
    db.commit()
    st = {"projects": len({b["project_id"] for b in boms.values()}), "boms": len(boms), "items": len(items),
          "items_with_key": sum(1 for i in items if i["key_norm"]), "boms_parse_failed": sum(1 for b in boms.values() if b["parse_status"] not in ("", "succeeded")),
          "licenses": dict(Counter(b["license"] or "未知" for b in boms.values()).most_common(8))}
    log(f"导入完成：{st}")
    return st


# --------------------------------------------------------------------------- 匹配
def _footprint_resolver(db):
    """封装名 → (ezPLM ID 或 None, 库/分类)；只认 ezPLM 或已扫描 KiCad 库里有的封装。"""
    from .ezplm_lib import EzLibrary
    from .settings import LIBINDEX_PATH
    ez = EzLibrary(db)
    kicad = {}
    if Path(LIBINDEX_PATH).exists():
        from .kicad import LibIndex
        for name, lib in LibIndex(LIBINDEX_PATH).con.execute("select name, lib from footprints"):
            kicad.setdefault(name, lib)

    def resolve(fp: str):
        name = (fp or "").split(":")[-1].strip()
        if name in ez.fp_by_name:
            hit = ez.fp_by_name[name]
            return name, hit[0], hit[2]
        if name in kicad:
            return name, None, kicad[name]
        return None
    return resolve, ez


def match(db, log=print, auto_min: float = 0.85, job: str | None = None, stop=None) -> dict:
    """开源 BOM 行 ↔ 暂存物料；结果写 oshw_matches，并给还没有封装的物料出封装建议。可重复执行。"""
    from .pipeline import _save_proposals, load_taxonomy
    job = job or f"oshw-{datetime.utcnow():%Y%m%d-%H%M}"
    items = db.execute(select(OshwItem.item_id, OshwItem.bom_id, OshwItem.key_norm, OshwItem.mpn, OshwItem.footprint)
                       .join(OshwBom, OshwBom.bom_id == OshwItem.bom_id)
                       .where(OshwItem.key_norm.isnot(None), OshwBom.parse_status.in_(("succeeded", "")))).all()
    log(f"参与匹配的开源 BOM 行 {len(items)} 条（已排除无源件和解析失败的工程）")
    if not items:
        return {"items": 0}

    # 暂存物料：规范化型号 → [(物料 ID, 现有封装)]；有序列表用于"以通用型号开头"的前缀查找
    parts: dict[str, list] = defaultdict(list)
    for pid, k, fp_new, fp_orig in db.execute(select(StagedPart.id, StagedPart.mpn_norm, StagedPart.new_footprint, StagedPart.orig_footprint)
                                              .where(StagedPart.mpn_norm.isnot(None), StagedPart.mpn_norm != "")):
        parts[k].append((pid, fp_new or fp_orig or ""))
    keys_sorted = sorted(parts)
    log(f"暂存物料规范化型号 {len(parts)} 个")

    pkg_cache: dict[str, tuple | None] = {}

    def part_pkg(pid: str, fp: str):
        """物料的封装特征：先看现有封装名，再看 IHS 封装描述。"""
        if fp:
            return pkg_sig(fp)
        if pid not in pkg_cache:
            from .staging import IhsRef
            r = db.execute(select(IhsRef.package, IhsRef.description).join(StagedPart, StagedPart.ihs_objid == IhsRef.objid)
                           .where(StagedPart.id == pid)).first()
            pkg_cache[pid] = (pkg_sig(r[0]) or pkg_sig(r[1])) if r else None
        return pkg_cache[pid]

    rows, st = [], Counter()
    for item_id, bom_id, k, mpn, fp in items:
        sig = pkg_sig(fp)
        if k in parts:                                                     # 完整型号
            how = "mpn" if mpn else "value"
            for pid, pfp in parts[k]:
                rows.append(dict(part_id=pid, item_id=item_id, bom_id=bom_id, how=how, footprint=fp, package_check="same" if same_package(sig, part_pkg(pid, pfp)) else "unknown"))
            st[how] += 1
            continue
        if not sig:                                                        # 通用型号必须靠封装区分，认不出封装就不猜
            st["generic_no_pkg"] += 1
            continue
        lo = bisect.bisect_left(keys_sorted, k)
        cands = []
        for kk in keys_sorted[lo:lo + MAX_GENERIC_FANOUT + 1]:
            if not kk.startswith(k):
                break
            if len(kk) - len(k) <= MAX_SUFFIX:
                cands += parts[kk]
        if not cands or len(cands) > MAX_GENERIC_FANOUT:
            st["generic_none" if not cands else "generic_too_many"] += 1
            continue
        hit = [(pid, pfp) for pid, pfp in cands if same_package(sig, part_pkg(pid, pfp))]
        for pid, _ in hit:
            rows.append(dict(part_id=pid, item_id=item_id, bom_id=bom_id, how="generic", footprint=fp, package_check="same"))
        st["generic" if hit else "generic_pkg_mismatch"] += 1
        if stop and stop():
            break
    db.execute(delete(OshwMatch))
    for i in range(0, len(rows), 5000):
        db.execute(insert(OshwMatch), rows[i:i + 5000])
    db.commit()
    log(f"匹配：{dict(st)}；物料-BOM 行对应 {len(rows)} 条，涉及物料 {len({r['part_id'] for r in rows})} 个")

    # ---------------- 投票 → 封装建议
    resolve, ez = _footprint_resolver(db)
    proj = dict(db.execute(select(OshwBom.bom_id, OshwBom.project_id)).all())
    pname = dict(db.execute(select(OshwBom.project_id, OshwBom.project_name)).all())
    by_part: dict[str, list] = defaultdict(list)
    for r in rows:
        by_part[r["part_id"]].append(r)
    staged = {}
    pids = list(by_part)
    for i in range(0, len(pids), 1000):
        for sp in db.execute(select(StagedPart).where(StagedPart.id.in_(pids[i:i + 1000]))).scalars():
            staged[sp.id] = sp
    for o in db.execute(select(StagedProposal).where(StagedProposal.review_status == "pending", StagedProposal.method.like("oshw%"))).scalars():
        o.review_status, o.review_note = "rejected", "superseded by oshw-match"

    props, agree = [], Counter()
    for pid, ms in by_part.items():
        sp = staged.get(pid)
        if not sp:
            continue
        votes: dict[str, set] = defaultdict(set)                          # 封装名 → 用到它的工程集合（同一工程只算一票）
        best_how: dict[str, str] = {}
        for m in ms:
            res = resolve(m["footprint"])
            if not res:
                continue
            votes[res[0]].add(proj.get(m["bom_id"], m["bom_id"]))
            if BASE_CONF[m["how"]] > BASE_CONF.get(best_how.get(res[0], ""), 0):
                best_how[res[0]] = m["how"]
        if not votes:
            agree["no_standard_footprint"] += 1
            continue
        top, top_projects = max(votes.items(), key=lambda kv: (len(kv[1]), kv[0]))
        total = sum(len(v) for v in votes.values())
        current = sp.new_footprint or sp.orig_footprint
        if current:                                                        # 已有封装：只用来衡量匹配质量
            agree["agree" if current == top else "differ"] += 1
            continue
        n = len(top_projects)
        conf = round(min(0.97, BASE_CONF[best_how[top]] + 0.03 * min(n - 1, 3)) * (len(top_projects) / total), 3)
        _, ez_id, lib = resolve(top)
        act = "auto_fill" if conf >= auto_min and ez_id else "review"
        ev = {"n_projects": n, "share": round(len(top_projects) / total, 2), "match": best_how[top],
              "projects": [pname.get(p, p) for p in sorted(top_projects)[:5]],
              "alternatives": {k: len(v) for k, v in sorted(votes.items(), key=lambda kv: -len(kv[1])) if k != top}}
        if ez_id:
            ev["ezplm_id"] = ez_id
        pr = Proposal(pid, sp.mpn, "footprint", "", top, f"{lib}:{top}", conf, act, f"oshw:{best_how[top]}", ev,
                      ["封装自动匹配" if act == "auto_fill" else "封装待复核", "开源工程"])
        props.append(pr)
    ez.annotate(props)
    stats = {"job": job, "proposals": 0, "auto_fill": 0, "flag_mismatch": 0, "review": 0}
    by_field = _save_proposals(db, props, staged, job, load_taxonomy(db), stats)
    db.commit()
    out = {**{k: v for k, v in stats.items() if k != "job"}, "match": dict(st), "pairs": len(rows), "parts_matched": len(by_part),
           "existing_footprint_agree": agree["agree"], "existing_footprint_differ": agree["differ"], "no_standard_footprint": agree["no_standard_footprint"]}
    if agree["agree"] + agree["differ"]:
        out["agree_rate"] = round(agree["agree"] / (agree["agree"] + agree["differ"]), 3)
    log(f"封装建议：{by_field}；已有封装的物料里，开源工程最常用的封装与现有一致的比例 {out.get('agree_rate', '-')}")
    return out
