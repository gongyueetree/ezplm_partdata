"""命令行入口。

python -m ezplm_pilot.cli profile   --source sample                      # 缺口分析报告
python -m ezplm_pilot.cli classify  --source sample [--llm]              # 分类校验 → out/proposals_category.jsonl
python -m ezplm_pilot.cli scan      --dir data/sources/espressif-kicad --kind vendor-official   # 建符号/封装索引
python -m ezplm_pilot.cli symbols   --source sample                      # 符号匹配 → out/proposals_symbol.jsonl
python -m ezplm_pilot.cli packages  --source sample                      # 通用封装映射 → out/proposals_footprint.jsonl
python -m ezplm_pilot.cli urls      --source sample [--no-verify]        # 官网/数据手册 → out/proposals_url.jsonl
python -m ezplm_pilot.cli summary                                        # 汇总所有建议 → out/summary.html
python -m ezplm_pilot.cli writeback --file out/proposals_category.jsonl --apply   # 经 API 写回（需 EZPLM_API_*）

# —— 暂存平台（推荐工作方式：ezPLM 只读，结果进暂存库，复核完再回写）——
python -m ezplm_pilot.cli ingest   --source api --batch 500 [--max-batches 20] [--reset]   # 分批拉取到暂存库，可随时中断续跑
python -m ezplm_pilot.cli process  --batch 200 [--llm] [--urls] [--max-batches N]        # 分批处理暂存库里未处理的物料
python -m ezplm_pilot.cli serve    --port 8080                                            # 启动复核网站
python -m ezplm_pilot.cli export   [--apply]                                              # 生成回写包；--apply 才真正写回 ezPLM
python -m ezplm_pilot.cli status                                                          # 进度一览
暂存库默认 ./staging.sqlite，可设 EZPLM_STAGING_DB=postgresql://user:pw@host/db

--source: sample | csv（--parts x.csv [--categories cats.tsv|json]） | api（EZPLM_API_BASE / EZPLM_API_TOKEN）
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .sources import open_source, Writer, ApiSource
from .models import Proposal

from .settings import OUT_DIR as OUT, LIBINDEX_PATH


def _src(a):
    return open_source(a.source, getattr(a, "parts", None), getattr(a, "categories", None))


def _parts(src, a):
    it = src.iter_parts()
    if getattr(a, "limit", 0):
        from itertools import islice
        it = islice(it, a.limit)
    return list(it)


def cmd_profile(a):
    from .gap import to_frame, profile, write_report
    src = _src(a); tax = src.load_taxonomy(); parts = _parts(src, a)
    df = to_frame(parts, tax)
    tables = profile(df)
    p = write_report(tables, OUT)
    print(tables["overall"].T.to_string())
    print(f"\n报告：{p}（另有 gap_*.csv）")


def cmd_classify(a):
    from .classify import Classifier, Policy
    src = _src(a); tax = src.load_taxonomy(); parts = _parts(src, a)
    pol = Policy(use_llm=a.llm, auto_fill_min=a.auto_min, flag_min=a.flag_min)
    clf = Classifier(tax, policy=pol).fit([p for p in parts if p.category_id])
    if clf.rules.unresolved:
        print("规则中未能解析到分类树的名字：", clf.rules.unresolved, file=sys.stderr)
    props = [clf.classify(p) for p in parts]
    res = Writer(OUT / "proposals_category.jsonl").write(props)
    print(Counter(p.action for p in props), res)
    if clf.llm:
        print("LLM 调用次数：", clf.llm.n_calls)
    for p in props:
        if p.action in ("auto_fill", "flag_mismatch"):
            print(f"  {p.action:14s} {p.mpn:22s} {tax.path(p.current) if p.current else '(空)':40s} -> {p.proposed_label}  conf={p.confidence} [{p.method}]")


def cmd_scan(a):
    from .kicad import scan, LibIndex
    idx = LibIndex(LIBINDEX_PATH)
    for d in a.dir:
        r = scan(d, a.kind)
        idx.add(r)
        print(f"{d}: symbols={len(r['symbols'])} footprints={len(r['footprints'])} instances={len(r['instances'])} errors={len(r['errors'])}")
        for e in r["errors"][:5]:
            print("   !", e["file"], e["error"])
    print("索引累计：", idx.stats())


def cmd_symbols(a):
    from .kicad import LibIndex, propose_symbols
    src = _src(a); parts = _parts(src, a)
    idx = LibIndex(LIBINDEX_PATH)
    props = list(propose_symbols(parts, idx, a.auto_min))
    res = Writer(OUT / "proposals_symbol.jsonl").write(props)
    print(Counter(p.action for p in props), res)
    for p in props[:50]:
        print(f"  {p.action:10s} {p.mpn:22s} -> {p.proposed_label:40s} conf={p.confidence} [{p.method}] qa={'ok' if p.evidence['qa']['ok'] else p.evidence['qa']['issues']}")


def cmd_packages(a):
    from .packages import PackageMapper
    src = _src(a); tax = src.load_taxonomy(); parts = _parts(src, a)
    attr = {}
    if a.package_attr:
        import csv
        with open(a.package_attr, encoding="utf-8-sig") as f:
            attr = {r["part_id"]: r["package"] for r in csv.DictReader(f)}
    props = list(PackageMapper(tax).propose(parts, attr))
    res = Writer(OUT / "proposals_footprint.jsonl").write(props)
    print(Counter(p.action for p in props), res)
    for p in props[:50]:
        print(f"  {p.action:10s} {p.mpn:22s} {p.evidence['package_text']:12s} -> {p.proposed_label} conf={p.confidence}")


def cmd_urls(a):
    from .urls import UrlFiller
    src = _src(a); parts = _parts(src, a)
    props = list(UrlFiller().propose(parts, verify=not a.no_verify))
    res = Writer(OUT / "proposals_url.jsonl").write(props)
    print(Counter((p.field, p.action) for p in props), res)
    for p in props[:50]:
        print(f"  {p.action:10s} {p.field:9s} {p.mpn:22s} {p.proposed}  [{p.evidence['verify']}]")


def cmd_summary(a):
    rows = []
    for f in sorted(OUT.glob("proposals_*.jsonl")):
        for line in open(f, encoding="utf-8"):
            rows.append(json.loads(line))
    c = Counter((r["field"], r["action"]) for r in rows)
    html = ["<meta charset='utf-8'><title>ezPLM 补全建议汇总</title><style>body{font-family:-apple-system,'PingFang SC',sans-serif;margin:24px}table{border-collapse:collapse;font-size:12px}th,td{border:1px solid #ddd;padding:3px 6px}th{background:#f3f4f6}</style>",
            "<h1>补全建议汇总</h1><table><tr><th>字段</th><th>动作</th><th>条数</th></tr>"]
    for (fld, act), n in sorted(c.items()):
        html.append(f"<tr><td>{fld}</td><td>{act}</td><td>{n}</td></tr>")
    html.append("</table><h2>明细（前 2000 条）</h2><table><tr><th>动作</th><th>字段</th><th>型号</th><th>现值</th><th>建议</th><th>置信度</th><th>方法</th><th>标签</th><th>证据</th></tr>")
    for r in rows[:2000]:
        ev = json.dumps(r["evidence"], ensure_ascii=False)[:300]
        html.append(f"<tr><td>{r['action']}</td><td>{r['field']}</td><td>{r['mpn']}</td><td>{r['current'][:40]}</td><td>{r['proposed_label'] or r['proposed']}</td><td>{r['confidence']}</td><td>{r['method']}</td><td>{','.join(r['tags'])}</td><td>{ev}</td></tr>")
    html.append("</table>")
    p = OUT / "summary.html"; p.write_text("\n".join(html), encoding="utf-8")
    print(dict(c)); print("汇总：", p)


def cmd_writeback(a):
    props = [Proposal(**json.loads(l)) for l in open(a.file, encoding="utf-8")]
    api = ApiSource() if a.apply else None
    res = Writer(OUT / ("applied_" + Path(a.file).name), api=api, apply=a.apply).write(props, only_actions=tuple(a.actions.split(",")))
    print(res, "" if a.apply else "（dry-run：未写回，加 --apply 才会调用 API）")


def cmd_ingest(a):
    from . import pipeline
    print(pipeline.ingest(_src(a), batch=a.batch, max_batches=a.max_batches, reset=a.reset))


def cmd_process(a):
    from . import pipeline
    print(pipeline.process(batch=a.batch, max_batches=a.max_batches, use_llm=a.llm, do_urls=a.urls))


def cmd_export(a):
    from . import pipeline
    api = ApiSource() if a.apply else None
    print(pipeline.export(OUT / "writeback.jsonl", apply=a.apply, api=api), "" if a.apply else "（dry-run：只生成回写包）")


def cmd_status(a):
    from . import pipeline
    print(json.dumps(pipeline.summary(), ensure_ascii=False, indent=1, default=str))


def cmd_serve(a):
    import uvicorn
    uvicorn.run("ezplm_pilot.web:app", host=a.host, port=a.port, reload=False)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ezplm_pilot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--source", default="sample", choices=["sample", "csv", "api"])
        sp.add_argument("--parts"); sp.add_argument("--categories"); sp.add_argument("--limit", type=int, default=0)

    s = sub.add_parser("profile"); common(s); s.set_defaults(fn=cmd_profile)
    s = sub.add_parser("classify"); common(s); s.add_argument("--llm", action="store_true"); s.add_argument("--auto-min", type=float, default=0.85); s.add_argument("--flag-min", type=float, default=0.85); s.set_defaults(fn=cmd_classify)
    s = sub.add_parser("scan"); s.add_argument("--dir", nargs="+", required=True); s.add_argument("--kind", default="community", choices=["kicad-official", "vendor-official", "lcsc-easyeda", "digikey-library", "oshw-product", "community"]); s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("symbols"); common(s); s.add_argument("--auto-min", type=float, default=0.85); s.set_defaults(fn=cmd_symbols)
    s = sub.add_parser("packages"); common(s); s.add_argument("--package-attr", help="CSV: part_id,package"); s.set_defaults(fn=cmd_packages)
    s = sub.add_parser("urls"); common(s); s.add_argument("--no-verify", action="store_true"); s.set_defaults(fn=cmd_urls)
    s = sub.add_parser("summary"); s.set_defaults(fn=cmd_summary)
    s = sub.add_parser("writeback"); s.add_argument("--file", required=True); s.add_argument("--apply", action="store_true"); s.add_argument("--actions", default="auto_fill"); s.set_defaults(fn=cmd_writeback)

    s = sub.add_parser("ingest"); common(s); s.add_argument("--batch", type=int, default=500); s.add_argument("--max-batches", type=int); s.add_argument("--reset", action="store_true"); s.set_defaults(fn=cmd_ingest)
    s = sub.add_parser("process"); s.add_argument("--batch", type=int, default=200); s.add_argument("--max-batches", type=int); s.add_argument("--llm", action="store_true"); s.add_argument("--urls", action="store_true"); s.set_defaults(fn=cmd_process)
    s = sub.add_parser("export"); s.add_argument("--apply", action="store_true"); s.set_defaults(fn=cmd_export)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("serve"); s.add_argument("--host", default="0.0.0.0"); s.add_argument("--port", type=int, default=8080); s.set_defaults(fn=cmd_serve)

    a = ap.parse_args(argv)
    OUT.mkdir(exist_ok=True)
    a.fn(a)


if __name__ == "__main__":
    main()
