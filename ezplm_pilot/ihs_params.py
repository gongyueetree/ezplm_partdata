"""IHS 参数 → ezPLM 分类关键参数。

大模型只在"分类"这一级工作一次：看这个分类的关键参数（category_params）和该分类物料在 IHS 里出现的参数列（含覆盖率、样例值），
输出一张映射表（哪一列 → 哪个参数、单位怎么换算、枚举怎么对应）。映射表人工确认后，由规则给该分类所有可靠匹配（L1–L3）的物料填参数。
一个分类一次模型调用，几十个试点分类就是几十次。

映射表每一项：
    {"target": "iout", "kind": "number", "source": "Output Current-Max", "scale": 1}
    {"target": "vin", "kind": "range", "min": "Input Voltage-Min", "max": "Input Voltage-Max", "scale": 1}
    {"target": "channels", "kind": "enum", "source": "Number of Functions", "value_map": {"1": "1", "2": "2"}}
    {"target": "rail_to_rail", "kind": "bool", "source": "...", "value_map": {"YES": "是", "NO": "否"}}
    {"target": "package", "kind": "text", "source": "_package"}
    {"target": "pg", "kind": "none"}                   # 没有对应的 IHS 列
特殊列：_package = IHS 的 Mfr Package Description，_description = Part Description。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime

from sqlalchemy import delete, insert, select

from .staging import CategoryParam, IhsParamMap, IhsRef, PartParam, StagedPart

LEVELS = ("L1", "L2", "L3")          # 只用型号+厂商对得上的匹配（含经谱系对上的）
KINDS = ("number", "range", "enum", "bool", "text", "none")
_NUM = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")


# --------------------------------------------------------------------------- 分类画像
def _category_parts(db, cid: str, limit: int | None = None):
    """该分类（以新分类优先）里有可靠 IHS 匹配的物料及其 IHS 记录。"""
    eff = (StagedPart.new_category_id == cid) | ((StagedPart.new_category_id.is_(None)) & (StagedPart.orig_category_id == cid))
    q = (select(StagedPart, IhsRef).join(IhsRef, IhsRef.objid == StagedPart.ihs_objid)
         .where(eff, StagedPart.ihs_match.in_(LEVELS)).order_by(StagedPart.value_score.desc(), StagedPart.id))
    if limit:
        q = q.limit(limit)
    return db.execute(q).all()


def _ihs_values(r: IhsRef) -> dict:
    try:
        v = json.loads(r.params_json or "{}")
    except ValueError:
        v = {}
    if r.package:
        v["_package"] = r.package
    if r.description:
        v["_description"] = r.description
    return v


def profile(db, cid: str, max_parts: int = 3000) -> dict:
    """分类里 IHS 参数列的覆盖率和样例值，给模型和映射页看。"""
    rows = _category_parts(db, cid, max_parts)
    cols: dict[str, Counter] = {}
    cats = Counter()
    for _, r in rows:
        cats[r.part_category] += 1
        for k, v in _ihs_values(r).items():
            cols.setdefault(k, Counter())[v] += 1
    n = len(rows)
    columns = sorted(({"column": k, "coverage": round(sum(c.values()) / n, 3) if n else 0, "distinct": len(c),
                       "samples": [v for v, _ in c.most_common(6)]} for k, c in cols.items()), key=lambda x: -x["coverage"])
    return {"n_parts": n, "ihs_categories": cats.most_common(8), "columns": columns}


def targets(db, cid: str) -> list[dict]:
    rows = db.execute(select(CategoryParam).where(CategoryParam.category_id == cid).order_by(CategoryParam.sort, CategoryParam.id)).scalars().all()
    return [{"key": p.key, "name": p.name, "ptype": p.ptype, "unit": p.unit or "", "values": p.values or ""} for p in rows]


# --------------------------------------------------------------------------- 模型生成映射
def build_prompt(path: str, tgts: list[dict], prof: dict, min_coverage: float = 0.05) -> str:
    cols = [c for c in prof["columns"] if c["coverage"] >= min_coverage][:60]
    col_lines = "\n".join(f"- {c['column']} | 覆盖 {c['coverage']:.0%} | 样例: {' ‖ '.join(map(str, c['samples']))[:200]}" for c in cols)
    tgt_lines = "\n".join(f"- {t['key']} | {t['name']} | 类型 {t['ptype']} | 单位 {t['unit'] or '-'}" + (f" | 常见取值 {t['values']}" if t["values"] else "")
                          for t in tgts)
    return (
        "你在做元器件参数数据的字段映射。左边是 ezPLM 某个分类的关键参数，右边是 IHS 参数库（2012 年）里该分类器件出现的参数列。\n"
        "请为每个 ezPLM 参数找出对应的 IHS 列，并给出单位换算。\n\n"
        f"ezPLM 分类：{path}\n该分类可靠匹配到 IHS 的器件数：{prof['n_parts']}；IHS 子类分布：{prof['ihs_categories']}\n\n"
        f"ezPLM 关键参数（key | 名称 | 类型 | 单位）：\n{tgt_lines}\n\n"
        f"IHS 参数列（列名 | 覆盖率 | 常见值）：\n{col_lines}\n"
        "- _package 是 IHS 的封装描述，_description 是 IHS 的器件描述\n\n"
        "规则：\n"
        "1. IHS 的数值一般是 SI 基本单位（V、A、Hz、s、W、Ohm、摄氏度），斜率类（Slew Rate）是 V/us。scale 是把 IHS 数值换算成 ezPLM 单位要乘的系数，"
        "例如 ezPLM 单位 µA 而 IHS 为 A → scale=1e6；单位相同 → scale=1。\n"
        "2. kind 取值：number（单个数值）、range（有 -Min 和 -Max 两列时用 min/max 字段）、enum（枚举，用 value_map 把 IHS 取值对应到 ezPLM 取值，没把握的不要写进 value_map）、"
        "bool（是/否，用 value_map 对应，如 {\"YES\":\"是\",\"NO\":\"否\"}）、text（原样文本，如封装）、none（没有合适的列）。\n"
        "3. 只在含义明确一致时才映射；Max/Min/Nom 选与 ezPLM 参数含义最接近的一列。拿不准就用 none，宁缺毋错。\n"
        "4. 每个 ezPLM 参数输出一项，note 用一句中文说明依据。\n\n"
        '只输出 JSON 数组，例如：[{"target":"iout","kind":"number","source":"Output Current-Max","scale":1,"note":"最大输出电流"},'
        '{"target":"vin","kind":"range","min":"Input Voltage-Min","max":"Input Voltage-Max","scale":1,"note":"输入电压范围"},{"target":"pg","kind":"none","note":"IHS 无此项"}]'
    )


def parse_mapping(txt: str, tgts: list[dict], columns: set[str]) -> list[dict]:
    """解析并校验模型输出：未知参数丢弃；引用不存在的列 → 改为 none 并注明。"""
    m = re.search(r"\[.*\]", txt or "", re.S)
    if not m:
        raise ValueError("模型没有返回 JSON 数组：" + (txt or "")[:200])
    keys = {t["key"] for t in tgts}
    out, seen = [], set()
    for it in json.loads(m.group(0)):
        if not isinstance(it, dict) or it.get("target") not in keys or it["target"] in seen:
            continue
        seen.add(it["target"])
        kind = it.get("kind") if it.get("kind") in KINDS else "none"
        rule = {"target": it["target"], "kind": kind, "note": str(it.get("note") or "")[:120]}
        used = [it.get("min"), it.get("max")] if kind == "range" else [it.get("source")]
        if kind != "none":
            missing = [c for c in used if c and c not in columns]
            if missing or not any(used):
                rule.update(kind="none", note=f"模型引用了不存在的列 {missing or used}，已置为 none")
            else:
                if kind == "range":
                    rule.update(min=it.get("min") or "", max=it.get("max") or "")
                else:
                    rule["source"] = it["source"]
                try:
                    rule["scale"] = float(it.get("scale", 1) or 1)
                except (TypeError, ValueError):
                    rule["scale"] = 1.0
                if isinstance(it.get("value_map"), dict) and kind in ("enum", "bool"):
                    rule["value_map"] = {str(k): str(v) for k, v in it["value_map"].items()}
        out.append(rule)
    for t in tgts:                                   # 模型漏掉的参数补成 none，映射表里每个参数都有一行
        if t["key"] not in seen:
            out.append({"target": t["key"], "kind": "none", "note": "模型未给出"})
    order = {t["key"]: i for i, t in enumerate(tgts)}
    return sorted(out, key=lambda r: order.get(r["target"], 999))


def _call(llm_call, prompt: str) -> str:
    try:
        return llm_call(prompt, max_tokens=4000)
    except TypeError:                                 # 自定义 call(prompt) 不接受 max_tokens
        return llm_call(prompt)


def suggest(db, tax, cid: str, llm_call, model: str = "") -> IhsParamMap:
    tgts = targets(db, cid)
    if not tgts:
        raise ValueError("该分类还没有关键参数，先在分类页挂上内置参数或让模型生成")
    prof = profile(db, cid)
    if not prof["n_parts"]:
        raise ValueError("该分类没有可靠匹配到 IHS 的物料")
    mapping = parse_mapping(_call(llm_call, build_prompt(tax.path(cid), tgts, prof)), tgts, {c["column"] for c in prof["columns"]})
    row = db.get(IhsParamMap, cid) or IhsParamMap(category_id=cid)
    row.mapping_json = json.dumps(mapping, ensure_ascii=False)
    row.status, row.n_parts, row.model, row.updated_at, row.approved_by = "draft", prof["n_parts"], model, datetime.utcnow(), None
    db.merge(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- 用映射算参数值
def _fmt(x: float) -> str:
    return f"{x:.4g}"


def _num(v) -> float | None:
    v = str(v or "").strip()
    return float(v) if _NUM.match(v) else None


def apply_rule(rule: dict, vals: dict, unit: str = "") -> tuple[str, float | None] | None:
    """一条映射作用到一条 IHS 记录 → (展示值, 数值)；取不到返回 None。"""
    kind, scale = rule.get("kind"), float(rule.get("scale", 1) or 1)
    u = f" {unit}" if unit else ""
    if kind == "number":
        x = _num(vals.get(rule.get("source")))
        return (f"{_fmt(x * scale)}{u}", x * scale) if x is not None else None
    if kind == "range":
        lo, hi = _num(vals.get(rule.get("min"))), _num(vals.get(rule.get("max")))
        if lo is None and hi is None:
            return None
        if lo is not None and hi is not None:
            return f"{_fmt(lo * scale)} ~ {_fmt(hi * scale)}{u}", hi * scale
        x = hi if hi is not None else lo
        return f"{'≤' if hi is not None else '≥'} {_fmt(x * scale)}{u}", x * scale
    if kind in ("enum", "bool"):
        raw = str(vals.get(rule.get("source")) or "").strip()
        if not raw:
            return None
        vm = {k.upper(): v for k, v in (rule.get("value_map") or {}).items()}
        if raw.upper() in vm:
            return vm[raw.upper()], _num(vm[raw.upper()])
        return (raw, _num(raw)) if kind == "enum" else None    # bool 没对上的不猜
    if kind == "text":
        raw = str(vals.get(rule.get("source")) or "").strip()
        return (raw[:200], None) if raw else None
    return None


def preview(db, cid: str, mapping: list[dict], n: int = 6) -> list[dict]:
    units = {t["key"]: t["unit"] for t in targets(db, cid)}
    out = []
    for sp, r in _category_parts(db, cid, n):
        vals = _ihs_values(r)
        out.append({"mpn": sp.mpn, "part_id": sp.id, "level": sp.ihs_match,
                    "values": {m["target"]: (apply_rule(m, vals, units.get(m["target"], "")) or ("", None))[0] for m in mapping}})
    return out


def apply(db, cid: str, user: str = "", log=None) -> dict:
    """用已确认的映射给该分类所有可靠匹配的物料写参数（先删该分类旧的 IHS 参数，可重复执行）。"""
    log = log or (lambda m: None)
    row = db.get(IhsParamMap, cid)
    if not row or row.status != "approved":
        raise ValueError("映射还没有确认")
    mapping = [m for m in json.loads(row.mapping_json or "[]") if m.get("kind") != "none"]
    units = {t["key"]: t["unit"] for t in targets(db, cid)}
    db.execute(delete(PartParam).where(PartParam.category_id == cid, PartParam.source == "ihs"))
    n_parts = n_vals = 0
    buf: list[dict] = []
    filled = Counter()
    for sp, r in _category_parts(db, cid):
        vals = _ihs_values(r)
        got = 0
        for m in mapping:
            res = apply_rule(m, vals, units.get(m["target"], ""))
            if res is None:
                continue
            col = m.get("source") or f"{m.get('min')}/{m.get('max')}"
            buf.append({"part_id": sp.id, "category_id": cid, "key": m["target"], "value": res[0][:200], "num": res[1], "unit": units.get(m["target"], ""),
                        "source": "ihs", "detail": f"{sp.ihs_match} IHS#{r.objid} {col}"[:200], "created_at": datetime.utcnow()})
            filled[m["target"]] += 1
            got += 1
        n_parts += bool(got)
        n_vals += got
        if len(buf) >= 2000:
            db.execute(insert(PartParam), buf); buf = []
    if buf:
        db.execute(insert(PartParam), buf)
    row.applied_at, row.applied_parts = datetime.utcnow(), n_parts
    db.commit()
    log(f"写入 {n_parts} 个物料、{n_vals} 个参数值")
    return {"parts": n_parts, "values": n_vals, "by_param": dict(filled)}


# --------------------------------------------------------------------------- 批量（试点分类）
def pilot_categories(db, tax, path_re: str, min_parts: int = 20) -> list[tuple[str, int]]:
    """路径匹配 path_re 的叶子分类里，可靠 IHS 匹配物料数 ≥ min_parts 的 → [(id, n)]，按物料数降序。"""
    from sqlalchemy import func
    pat = re.compile(path_re)
    leaves = {n.id for n in tax.leaves() if pat.search(tax.path(n.id))}
    if not leaves:
        return []
    cat = func.coalesce(StagedPart.new_category_id, StagedPart.orig_category_id)
    rows = db.execute(select(cat, func.count()).where(StagedPart.ihs_match.in_(LEVELS), cat.in_(leaves)).group_by(cat)).all()
    return sorted(((c, n) for c, n in rows if n >= min_parts), key=lambda x: -x[1])


def bulk_suggest(llm_call, path_re: str, min_parts: int = 20, model: str = "", only_missing: bool = True, stop=None, log=None, progress=None) -> dict:
    from .pipeline import load_taxonomy
    from .staging import get_session
    log = log or (lambda m: None)
    db = get_session()
    tax = load_taxonomy(db)
    cats = pilot_categories(db, tax, path_re, min_parts)
    if only_missing:
        have = {c for (c,) in db.execute(select(IhsParamMap.category_id))}
        cats = [(c, n) for c, n in cats if c not in have]
    log(f"匹配到 {len(cats)} 个分类（可靠 IHS 匹配 ≥ {min_parts} 条）")
    st = {"categories": len(cats), "done": 0, "failed": 0, "skipped": 0}
    for i, (cid, n) in enumerate(cats):
        if stop and stop():
            break
        try:
            row = suggest(db, tax, cid, llm_call, model)
            mp = json.loads(row.mapping_json)
            st["done"] += 1
            log(f"[{i + 1}/{len(cats)}] {tax.path(cid)}（{n} 条）→ 映射 {sum(m['kind'] != 'none' for m in mp)}/{len(mp)} 个参数")
        except ValueError as e:
            st["skipped"] += 1
            log(f"[{i + 1}/{len(cats)}] {tax.path(cid)} 跳过：{e}")
        except Exception as e:  # noqa
            db.rollback()
            st["failed"] += 1
            log(f"[{i + 1}/{len(cats)}] {tax.path(cid)} 失败：{str(e)[:150]}")
        if progress:
            progress({"done": i + 1, "total": len(cats)})
    db.close()
    return st


def bulk_apply(stop=None, log=None, progress=None) -> dict:
    from .pipeline import load_taxonomy
    from .staging import get_session
    log = log or (lambda m: None)
    db = get_session()
    tax = load_taxonomy(db)
    cids = [c for (c,) in db.execute(select(IhsParamMap.category_id).where(IhsParamMap.status == "approved"))]
    st = {"categories": len(cids), "parts": 0, "values": 0}
    for i, cid in enumerate(cids):
        if stop and stop():
            break
        r = apply(db, cid)
        st["parts"] += r["parts"]
        st["values"] += r["values"]
        log(f"[{i + 1}/{len(cids)}] {tax.path(cid)} → {r['parts']} 个物料、{r['values']} 个参数值")
        if progress:
            progress({"done": i + 1, "total": len(cids)})
    db.close()
    return st
