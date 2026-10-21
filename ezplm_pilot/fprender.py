"""KiCad 封装（.kicad_mod）→ SVG 预览 + 焊盘表。纯 Python。

画正面：F.Fab（外形）、F.CrtYd（占位框）、F.SilkS（丝印）和焊盘（带编号）；背面层、文字属性不画。
封装坐标 Y 轴向下、单位 mm，与 SVG 一致，不用翻转。浅色配色。
"""
from __future__ import annotations

import math
import re
from html import escape

from .symrender import _Box, _all, _arc_path, _get, _natkey, _num, parse_sexpr

BG = "#ffffff"                                                     # 浅色：白底、蓝焊盘、橙丝印、灰外形、紫占位框
LAYER = {"F.Fab": ("#b4b8bf", 0), "F.CrtYd": ("#e83cf0", 1), "F.SilkS": ("#f0a030", 2)}   # 颜色, 叠放次序
PAD, PAD_TH, HOLE, PAD_TEXT = "#3d7fe0", "#3d7fe0", "#9aa0a6", "#ffe14d"


def _layer(it) -> str:
    ly = _get(it, "layer")
    return str(ly[1]) if ly and len(ly) > 1 else ""


def _width(it, default=0.12) -> float:
    w = _get(_get(it, "stroke") or [], "width") or _get(it, "width")
    v = _num(w, 1, 0) if w else 0
    return v if v > 0 else default


def _filled(it) -> bool:
    f = _get(it, "fill")
    return bool(f) and len(f) > 1 and f[1] in ("solid", "yes")


def _shape(it, box: _Box) -> tuple[int, str] | None:
    ly = _layer(it)
    if ly not in LAYER:
        return None
    color, z = LAYER[ly]
    w = _width(it)
    fill = color if _filled(it) else "none"
    st = f'stroke="{color}" stroke-width="{w:.3f}" fill="{fill}" stroke-linecap="round"'
    k = it[0].replace("fp_", "")
    if k == "line":
        s, e = _get(it, "start"), _get(it, "end")
        x0, y0, x1, y1 = _num(s, 1), _num(s, 2), _num(e, 1), _num(e, 2)
        box.add(x0, y0, w); box.add(x1, y1, w)
        return z, f'<line x1="{x0:.3f}" y1="{y0:.3f}" x2="{x1:.3f}" y2="{y1:.3f}" {st}/>'
    if k == "rect":
        s, e = _get(it, "start"), _get(it, "end")
        x0, y0, x1, y1 = _num(s, 1), _num(s, 2), _num(e, 1), _num(e, 2)
        box.add(x0, y0, w); box.add(x1, y1, w)
        return z, f'<rect x="{min(x0, x1):.3f}" y="{min(y0, y1):.3f}" width="{abs(x1 - x0):.3f}" height="{abs(y1 - y0):.3f}" {st}/>'
    if k == "circle":
        c, e = _get(it, "center"), _get(it, "end")
        cx, cy = _num(c, 1), _num(c, 2)
        r = math.hypot(_num(e, 1) - cx, _num(e, 2) - cy)
        box.add(cx, cy, r + w)
        return z, f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="{r:.3f}" {st}/>'
    if k == "arc":
        s, m, e = _get(it, "start"), _get(it, "mid"), _get(it, "end")
        if not (s and m and e):
            return None
        pts = [(_num(p, 1), _num(p, 2)) for p in (s, m, e)]
        for x, y in pts:
            box.add(x, y, w)
        (sx, sy), (mx, my), (ex, ey) = pts
        return z, f'<path d="{_arc_path(sx, sy, mx, my, ex, ey)}" {st}/>'
    if k == "poly":
        pts = [(_num(p, 1), _num(p, 2)) for p in _all(_get(it, "pts") or [], "xy")]
        if not pts:
            return None
        for x, y in pts:
            box.add(x, y, w)
        return z, f'<polygon points="{" ".join(f"{x:.3f},{y:.3f}" for x, y in pts)}" {st}/>'
    return None


def _pads(root) -> list[dict]:
    out = []
    for it in _all(root, "pad"):
        at, size = _get(it, "at"), _get(it, "size")
        drill = _get(it, "drill")
        dr = [float(x) for x in (drill or [])[1:] if isinstance(x, str) and re.fullmatch(r"-?[\d.]+", x)]
        rr = _get(it, "roundrect_rratio")
        out.append({"number": str(it[1]) if len(it) > 1 else "", "type": it[2] if len(it) > 2 else "", "shape": it[3] if len(it) > 3 else "",
                    "x": _num(at, 1), "y": _num(at, 2), "angle": _num(at, 3), "w": _num(size, 1), "h": _num(size, 2),
                    "drill": dr, "rratio": _num(rr, 1, 0.25) if rr else 0.25,
                    "front": any("F.Cu" in str(x) or "*.Cu" in str(x) for x in (_get(it, "layers") or [])[1:])})
    return out


def _pad_svg(p: dict, box: _Box) -> str:
    if not p["front"] and p["type"] not in ("thru_hole", "np_thru_hole"):
        return ""
    x, y, w, h = p["x"], p["y"], p["w"], p["h"]
    box.add(x, y, max(w, h) / 2 * 1.42)
    color = PAD_TH if p["type"] == "thru_hole" else PAD
    rot = f' transform="rotate({-p["angle"]:g} {x:.3f} {y:.3f})"' if p["angle"] else ""   # KiCad 角度逆时针为正，Y 向下 → SVG 取反
    sh = p["shape"]
    if p["type"] == "np_thru_hole":
        d = p["drill"][0] if p["drill"] else min(w, h)
        return f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{d / 2:.3f}" fill="none" stroke="{HOLE}" stroke-width="0.05"/>'
    if sh == "circle":
        g = f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{w / 2:.3f}" fill="{color}"/>'
    else:
        r = 0.0
        if sh == "oval":
            r = min(w, h) / 2
        elif sh == "roundrect":
            r = min(w, h) * p["rratio"]
        g = f'<rect x="{x - w / 2:.3f}" y="{y - h / 2:.3f}" width="{w:.3f}" height="{h:.3f}" rx="{r:.3f}" fill="{color}"{rot}/>'
    if p["drill"]:
        dw = p["drill"][0]
        dh = p["drill"][1] if len(p["drill"]) > 1 and p["drill"][1] > 0 else dw
        g += f'<ellipse cx="{x:.3f}" cy="{y:.3f}" rx="{dw / 2:.3f}" ry="{dh / 2:.3f}" fill="{BG}"/>'
    if p["number"]:
        fs = max(0.15, min(min(w, h) * 0.55, 1.0, max(w, h) / max(1, len(p["number"])) * 0.9))
        g += (f'<text x="{x:.3f}" y="{y:.3f}" font-size="{fs:.3f}" fill="{PAD_TEXT}" text-anchor="middle" '
              f'dominant-baseline="central">{escape(p["number"])}</text>')
    return g


def exposed_pads(pads: list[dict]) -> list[str]:
    """散热焊盘（EP）的编号：面积远大于普通焊盘（> 4 倍中位数）的编号焊盘，最多 2 个编号（拆成多块的 EP 同号）。"""
    area: dict = {}
    for p in pads:
        if p["number"] and p["type"] != "np_thru_hole":
            area[p["number"]] = max(area.get(p["number"], 0), p["w"] * p["h"])
    if len(area) < 3:
        return []
    med = sorted(area.values())[len(area) // 2]
    big = [n for n, a in area.items() if med and a > 4 * med]
    return big if 0 < len(big) <= 2 else []


def preview(text: str) -> dict | None:
    """→ {name, svg, pads:[{number,type,shape,size,drill,count}], n_pads, size:"宽×高 mm"}；解析失败返回 None。"""
    try:
        root = parse_sexpr(text)
    except Exception:  # noqa
        return None
    if not root or root[0] not in ("footprint", "module"):
        return None
    box, shapes = _Box(), []
    for it in root[1:]:
        if isinstance(it, list) and it and it[0] in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
            s = _shape(it, box)
            if s:
                shapes.append(s)
    pads = _pads(root)
    pad_svg = "".join(_pad_svg(p, box) for p in pads)
    if not box.ok():
        return None
    pad = 0.6
    x, y, w, h = box.x0 - pad, box.y0 - pad, box.x1 - box.x0 + 2 * pad, box.y1 - box.y0 + 2 * pad
    k = 360 / max(w, h)                                               # 长边约 360px
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x:.2f} {y:.2f} {w:.2f} {h:.2f}" width="{w * k:.0f}" height="{h * k:.0f}" '
           f'font-family="Helvetica,Arial,sans-serif" style="max-width:100%;height:auto;background:{BG}">'
           + "".join(s for _, s in sorted(shapes, key=lambda t: t[0])) + pad_svg + "</svg>")
    # 焊盘表：同编号多块（散热焊盘拆分）合并计数
    rows: dict = {}
    for p in sorted(pads, key=lambda p: _natkey(p["number"])):
        k2 = p["number"] or "（无编号）"
        r = rows.get(k2)
        if r:
            r["count"] += 1
            continue
        rows[k2] = {"number": k2, "type": {"smd": "SMD", "thru_hole": "通孔", "np_thru_hole": "非金属孔", "connect": "连接"}.get(p["type"], p["type"]),
                    "shape": p["shape"], "size": f'{p["w"]:g}×{p["h"]:g}', "drill": "×".join(f"{d:g}" for d in p["drill"]), "count": 1}
    numbered = {p["number"] for p in pads if p["number"]}
    return {"name": str(root[1]) if len(root) > 1 else "", "svg": svg, "pads": list(rows.values()), "n_pads": len(numbered),
            "numbers": sorted(numbered, key=_natkey), "ep": exposed_pads(pads),
            "size": f"{box.x1 - box.x0:.2f}×{box.y1 - box.y0:.2f} mm"}
