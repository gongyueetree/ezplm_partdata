"""只读诊断：看 ezPLM /partlibs 列表和详情接口里有哪些字段，找出"官网链接"实际叫什么。

用法（在自己的终端，DATABASE_URL 已指向平台的 Postgres；接口地址和 token 从平台设置里读，不会打印）：
    python scripts/api_fields.py [条数，默认 20]
只打印字段名，以及看起来像网址的字段的值；带签名的下载链接（qn.ezplm.com、含 token/sign/Expires 的）只显示"已隐藏"。
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ezplm_pilot.settings import api_source  # noqa: E402

SIGNED = re.compile(r"qn\.ezplm|token|sign|expires|x-amz|ossaccess", re.I)
URLISH = re.compile(r"url|link|web|site|homepage|官网|href", re.I)


def show(v):
    s = str(v)
    if SIGNED.search(s):
        return "（带签名的下载链接，已隐藏）"
    return s[:120]


def walk(d, prefix="", out=None):
    out = out if out is not None else []
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                walk(v if isinstance(v, dict) else (v[0] if v and isinstance(v[0], dict) else {}), p, out)
            out.append((p, v))
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    api = api_source()
    js = api._get("/partlibs", current=1, pageSize=n)
    items = (js.get("data") or {}).get("data") or []
    print(f"列表接口 /partlibs：取到 {len(items)} 条")
    keys, filled, samples = Counter(), Counter(), {}
    for x in items:
        for p, v in walk(x):
            keys[p] += 1
            if v not in (None, "", [], {}):
                filled[p] += 1
                if URLISH.search(p) and p not in samples and not isinstance(v, (dict, list)):
                    samples[p] = show(v)
    print("字段（有值条数 / 出现条数）：")
    for k in sorted(keys):
        print(f"  {k:40s} {filled[k]}/{keys[k]}")
    print("像网址的字段示例：", samples or "无")
    if items:
        d = api.get_part_detail(items[0]["id"])
        print(f"\n详情接口 /partlibs/{{id}}（第 1 条）字段：")
        for p, v in walk(d):
            extra = f" = {show(v)}" if URLISH.search(p) and v not in (None, "", [], {}) and not isinstance(v, (dict, list)) else ""
            print(f"  {p}{extra}")


if __name__ == "__main__":
    main()
