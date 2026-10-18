"""缺口分析：字段缺失率 × 分类 × 制造商，以及每个器件的"价值分"。

价值分（0~100）用于决定补全顺序，只用库内已有信息即可算出；
后续接入立创库存 / 开源工程出现次数后把对应权重打开即可。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd

from .models import Part
from .taxonomy import Taxonomy

FIELDS = ["category_id", "symbol", "footprint", "has_pdf", "has_image", "url"]

TIER1_MANUFACTURERS = re.compile(
    r"texas instruments|stmicro|microchip|analog devices|nxp|infineon|renesas|onsemi|on semiconductor|"
    r"nexperia|diodes inc|vishay|rohm|罗姆|toshiba|东芝|murata|村田|tdk|samsung|三星|micron|美光|"
    r"espressif|乐鑫|兆易|gigadevice|圣邦|sg micro|韦尔|will semi|纳芯微|novosense|芯海|矽力杰|silergy|"
    r"南芯|上海贝岭|士兰微|华大|杰华特|思瑞浦|3peak|wch|沁恒|hdsc|雅特力|artery|国民技术|极海|apm32",
    re.I,
)
GENERIC_PKG = re.compile(r"^(SOT|SOD|SOIC|SOP|TSSOP|SSOP|MSOP|QFN|DFN|TO-|DO-|LQFP|QFP|TQFP|BGA|SMA|SMB|SMC|D_|R_|C_|L_)", re.I)


def value_score(p: Part) -> int:
    s = 20
    if TIER1_MANUFACTURERS.search(p.manufacturer or ""):
        s += 25
    if p.has_pdf:
        s += 10
    if p.has_image:
        s += 5
    if p.footprint:
        s += 10
    if p.symbol:
        s += 10
    if p.description and len(p.description) > 12 and "器件。" not in p.description:
        s += 10          # 非模板化描述通常来自更可信的采集源
    if GENERIC_PKG.match(p.footprint or ""):
        s += 5           # 通用封装 → 封装补全成本低
    # TODO: +20 立创有库存 / +20 在开源工程中出现 —— 接入外部数据后打开
    return min(s, 100)


def to_frame(parts: Iterable[Part], tax: Taxonomy | None = None) -> pd.DataFrame:
    rows = []
    for p in parts:
        d = p.to_dict()
        d["value_score"] = value_score(p)
        d["root_category"] = tax.path(p.category_id).split(" > ")[0] if (tax and p.category_id in tax.nodes) else ("" if not p.category_id else "<未知分类ID>")
        d["category_path"] = tax.path(p.category_id) if (tax and p.category_id in tax.nodes) else p.category
        rows.append(d)
    df = pd.DataFrame(rows)
    for f in FIELDS:
        df[f"miss_{f}"] = (df[f].astype(str).str.strip() == "") if f not in ("has_pdf", "has_image") else ~df[f].astype(bool)
    return df


def profile(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    con = duckdb.connect()
    con.register("parts", df)
    miss_cols = ", ".join(f"round(100.0*avg(miss_{f}::int),1) as miss_{f}" for f in FIELDS)
    out = {
        "overall": con.execute(f"select count(*) as n, {miss_cols}, round(avg(value_score),1) as avg_value from parts").df(),
        "by_root": con.execute(f"select root_category, count(*) as n, {miss_cols} from parts group by 1 order by n desc").df(),
        "by_category": con.execute(f"select category_path, count(*) as n, {miss_cols} from parts group by 1 order by n desc limit 200").df(),
        "by_manufacturer": con.execute(f"select manufacturer, count(*) as n, {miss_cols}, round(avg(value_score),1) as avg_value from parts group by 1 order by n desc limit 200").df(),
        "priority_queue": con.execute(
            "select mpn, manufacturer, category_path, value_score, miss_symbol, miss_footprint, miss_has_pdf, miss_url "
            "from parts where miss_symbol or miss_footprint or miss_has_pdf or miss_url or miss_category_id "
            "order by value_score desc, mpn limit 500").df(),
        "no_category": con.execute("select mpn, manufacturer, description, value_score from parts where miss_category_id order by value_score desc").df(),
    }
    return out


def write_report(tables: dict[str, pd.DataFrame], out_dir: str | Path) -> Path:
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    for k, t in tables.items():
        t.to_csv(out_dir / f"gap_{k}.csv", index=False, encoding="utf-8-sig")
    html = ["<meta charset='utf-8'><title>ezPLM 物料库缺口分析</title>",
            "<style>body{font-family:-apple-system,'PingFang SC',sans-serif;margin:24px;color:#222}table{border-collapse:collapse;font-size:13px;margin:8px 0 28px}"
            "th,td{border:1px solid #ddd;padding:4px 8px;text-align:left}th{background:#f3f4f6}h2{margin-top:32px}</style>",
            "<h1>ezPLM 物料库缺口分析</h1><p>miss_* 列为缺失率（%）。价值分越高越应优先补全。</p>"]
    titles = {"overall": "总体", "by_root": "按一级分类", "by_category": "按叶子分类（前 200）",
              "by_manufacturer": "按制造商（前 200）", "priority_queue": "补全优先队列（前 500）", "no_category": "未分类器件"}
    for k, t in tables.items():
        html.append(f"<h2>{titles.get(k, k)}</h2>" + t.to_html(index=False, float_format=lambda x: f"{x:.1f}"))
    p = out_dir / "gap_report.html"
    p.write_text("\n".join(html), encoding="utf-8")
    return p
