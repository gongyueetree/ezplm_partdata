"""分类关键参数：LLM 建议 + 人工维护 + 批量生成 + 导出。"""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import select, func

from .staging import CategoryParam, StagedPart, get_session
from .taxonomy import Taxonomy

PTYPES = {"range": "数值范围", "enum": "多选一", "bool": "是 / 否", "text": "文本"}
PRIORITIES = {"decisive": "决定选择", "important": "通常重要", "reference": "参考"}


def list_params(db, category_id: str) -> list[CategoryParam]:
    return db.execute(select(CategoryParam).where(CategoryParam.category_id == category_id).order_by(CategoryParam.sort, CategoryParam.id)).scalars().all()


def counts(db) -> dict:
    return dict(db.execute(select(CategoryParam.category_id, func.count()).group_by(CategoryParam.category_id)).all())


def save_params(db, category_id: str, rows: list[dict]):
    """整体替换该分类的参数（表单提交）。rows: [{name,key,ptype,unit,vmin,vmax,values,priority,source}]"""
    for old in list_params(db, category_id):
        db.delete(old)
    for i, r in enumerate(rows):
        if not (r.get("name") or "").strip():
            continue
        key = (r.get("key") or "").strip() or re.sub(r"[^a-z0-9]+", "_", r["name"].lower()).strip("_")
        db.add(CategoryParam(category_id=category_id, name=r["name"].strip(), key=key, ptype=r.get("ptype") or "range", unit=(r.get("unit") or "").strip(),
                             vmin=(r.get("vmin") or "").strip(), vmax=(r.get("vmax") or "").strip(), values=(r.get("values") or "").strip(),
                             priority=r.get("priority") or "important", source=r.get("source") or "manual", sort=i))
    db.commit()


def _sample_descriptions(db, category_id: str, n: int = 12) -> list[str]:
    rows = db.execute(select(StagedPart.mpn, StagedPart.description).where((StagedPart.new_category_id == category_id) | (StagedPart.orig_category_id == category_id))
                      .order_by(StagedPart.value_score.desc()).limit(n)).all()
    return [f"{m}: {(d or '')[:80]}" for m, d in rows]


def suggest_params(tax: Taxonomy, category_id: str, llm_call, samples: list[str] | None = None) -> list[dict]:
    """让模型给出该分类的关键参数。llm_call(prompt)->str。返回 [{name,key,ptype,unit,vmin,vmax,values,priority}]"""
    path = tax.path(category_id)
    ex = ("\n该分类下的器件样例：\n" + "\n".join(samples)) if samples else ""
    prompt = (
        "你是资深硬件工程师。请给出在 ezPLM 元器件库中，工程师为下面这个元器件分类做选型时需要询问的关键参数，以及每个参数常见的取值范围。\n"
        f"分类：{path}{ex}\n\n"
        "要求：6~10 个参数；每个参数给出：name（英文参数名）、key（小写下划线键）、ptype（range=数值范围 / enum=多选一 / bool=是否）、unit（SI 单位，无则空）、"
        "vmin/vmax（range 类型的常见最小/最大值，用纯数字或科学计数）、values（enum 类型的常见取值，逗号分隔）、"
        "priority（decisive=决定选择 / important=通常重要 / reference=参考）。封装(Package)一般作为 enum、priority=reference。\n"
        '只输出 JSON 数组，例如：[{"name":"Gain Bandwidth Product","key":"gbw","ptype":"range","unit":"Hz","vmin":"1e3","vmax":"1e7","values":"","priority":"decisive"}]'
    )
    txt = llm_call(prompt)
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        raise ValueError("模型未返回 JSON 数组：" + txt[:200])
    out = []
    for r in json.loads(m.group(0)):
        if not isinstance(r, dict) or not r.get("name"):
            continue
        r["ptype"] = r.get("ptype") if r.get("ptype") in PTYPES else "range"
        r["priority"] = r.get("priority") if r.get("priority") in PRIORITIES else "important"
        r["source"] = "model"
        out.append({k: ("" if r.get(k) is None else str(r.get(k))) for k in ("name", "key", "ptype", "unit", "vmin", "vmax", "values", "priority", "source")})
    return out


def bulk_generate(llm_call, only_missing: bool = True, leaves_only: bool = True, stop=None, log=None, progress=None) -> dict:
    """为所有分类批量生成参数建议（已有参数的默认跳过）。"""
    from .pipeline import load_taxonomy
    log = log or (lambda m: None)
    db = get_session()
    tax = load_taxonomy(db)
    have = counts(db)
    nodes = tax.leaves() if leaves_only else list(tax.nodes.values())
    todo = [n for n in nodes if not (only_missing and have.get(n.id))]
    stats = {"total": len(todo), "done": 0, "failed": 0}
    for i, n in enumerate(todo):
        if stop and stop():
            break
        try:
            rows = suggest_params(tax, n.id, llm_call, _sample_descriptions(db, n.id))
            save_params(db, n.id, rows)
            stats["done"] += 1
            log(f"[{i+1}/{len(todo)}] {tax.path(n.id)} → {len(rows)} 个参数")
        except Exception as e:  # noqa
            stats["failed"] += 1
            log(f"[{i+1}/{len(todo)}] {tax.path(n.id)} 失败：{str(e)[:120]}")
        if progress:
            progress({"done": i + 1, "total": len(todo)})
    return stats


def seed_load(db=None, tax: Taxonomy | None = None, leaves_only: bool = True, replace_seed: bool = False) -> dict:
    """把内置参数模板（param_seed.py）挂到分类上，不调用模型。

    only_missing=True：只给还没有参数的分类挂；replace_seed=True：另外把 source=seed 的旧内置建议也刷新
    （人工 / 模型生成的参数永远不动）。返回 {"seeded": n, "skipped": m}。
    """
    from . import param_seed
    from .pipeline import load_taxonomy
    db = db or get_session()
    tax = tax or load_taxonomy(db)
    if not tax.nodes:
        return {"seeded": 0, "skipped": 0}
    have = counts(db)
    nodes = tax.leaves() if leaves_only else list(tax.nodes.values())
    seeded = skipped = 0
    for n in nodes:
        if have.get(n.id):
            if not replace_seed:
                skipped += 1
                continue
            cur = list_params(db, n.id)
            if any(p.source != "seed" for p in cur):
                skipped += 1
                continue
        save_params(db, n.id, param_seed.params_for(tax.path(n.id)))
        seeded += 1
    return {"seeded": seeded, "skipped": skipped}


def export_all(db, tax: Taxonomy) -> list[dict]:
    out = []
    for cid, n in tax.nodes.items():
        ps = list_params(db, cid)
        if ps:
            out.append({"category_id": cid, "category": n.name, "path": tax.path(cid),
                        "params": [{k: getattr(p, k) for k in ("name", "key", "ptype", "unit", "vmin", "vmax", "values", "priority", "source")} for p in ps]})
    return out
