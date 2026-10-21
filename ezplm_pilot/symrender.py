"""KiCad 符号（.kicad_sym，KiCad 6+ S 表达式）→ SVG 预览 + 引脚表。纯 Python，不依赖 KiCad。

只画看符号对不对所需的东西：图形（矩形/折线/圆/圆弧/贝塞尔/文字）和引脚（线、端点圈、名称、编号）；
属性文字（位号、型号等）不画。多单元符号每个单元单独一块（单元 0 = 各单元共用的图形），只画主体样式 1（不画 De Morgan）。
坐标：KiCad 符号库 Y 轴向上、单位 mm；SVG 里取反 Y，viewBox 直接用 mm。
"""
from __future__ import annotations

import math
import re
from html import escape

# 浅色配色（白底、灰色主体、蓝色引脚，和常见元器件商城的预览一致）
BODY, FILL, PIN, PIN_NAME, PIN_NUM = "#4b5563", "#ffffff", "#3b6fd8", "#1f2328", "#3d4450"
ETYPE = {"input": "INPUT", "output": "OUTPUT", "bidirectional": "BIDI", "tri_state": "TRI-STATE", "passive": "PASSIVE",
         "free": "FREE", "unspecified": "UNSPECIFIED", "power_in": "POWER IN", "power_out": "POWER OUT",
         "open_collector": "OPEN COLLECTOR", "open_emitter": "OPEN EMITTER", "no_connect": "NO CONNECT"}


# --------------------------------------------------------------------------- S 表达式
_TOK = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)


def parse_sexpr(text: str):
    """→ 嵌套列表；带引号的字符串包成 Str 以区分关键字。"""
    stack, cur = [], []
    for m in _TOK.finditer(text):
        lp, rp, s, atom = m.groups()
        if lp:
            stack.append(cur)
            cur = []
        elif rp:
            if not stack:
                break
            done, cur = cur, stack.pop()
            cur.append(done)
        elif s is not None:
            cur.append(Str(re.sub(r"\\(.)", r"\1", s)))
        elif atom is not None:
            cur.append(atom)
    return cur[0] if cur else []


class Str(str):
    """带引号的字符串（与关键字原子区分）。"""


def _get(node, key):
    for x in node[1:] if isinstance(node, list) else []:
        if isinstance(x, list) and x and x[0] == key:
            return x
    return None


def _all(node, key):
    return [x for x in node[1:] if isinstance(x, list) and x and x[0] == key]


def _num(node, i, default=0.0):
    try:
        return float(node[i])
    except (TypeError, ValueError, IndexError):
        return default


def _hidden(node) -> bool:
    """旧格式 (pin_names hide) / 新格式 (pin_names (hide yes))；引脚同理。"""
    if not isinstance(node, list):
        return False
    if "hide" in node[1:]:
        return True
    h = _get(node, "hide")
    return bool(h) and (len(h) < 2 or h[1] in ("yes", "true"))


def _font(node, default=1.27) -> float:
    f = _get(_get(node, "effects") or [], "font")
    s = _get(f or [], "size")
    return _num(s, 1, default) if s else default


# --------------------------------------------------------------------------- 符号结构
def load(text: str, name: str | None = None) -> dict | None:
    """解析一个 .kicad_sym，取出要预览的符号 → {name, units:{n: [items]}, pins:[…], pin_names_offset, hide_names, hide_numbers}。
    name 给了就找同名符号，否则取最后一个（被 extends 的父符号通常写在前面）。"""
    root = parse_sexpr(text)
    syms = {str(s[1]): s for s in _all(root, "symbol") if len(s) > 1}
    if not syms:
        return None
    sym = syms.get(name) if name and name in syms else list(syms.values())[-1]
    seen = set()
    while (ext := _get(sym, "extends")) and str(ext[1]) in syms and str(ext[1]) not in seen:   # 派生符号：图形和引脚在父符号里
        seen.add(str(ext[1]))
        sym = syms[str(ext[1])]
    pn = _get(sym, "pin_names")
    off = _get(pn or [], "offset")
    out = {"name": str(sym[1]), "units": {}, "pins": [], "pin_names_offset": _num(off, 1, 0.508) if off else 0.508,
           "hide_names": _hidden(pn), "hide_numbers": _hidden(_get(sym, "pin_numbers"))}
    subs = _all(sym, "symbol") or [sym]
    for sub in subs:
        m = re.search(r"_(\d+)_(\d+)$", str(sub[1]))
        unit, style = (int(m.group(1)), int(m.group(2))) if m else (1, 1)
        if style not in (0, 1):
            continue
        items = out["units"].setdefault(unit, [])
        for it in sub[1:]:
            if not isinstance(it, list) or not it:
                continue
            if it[0] == "pin":
                out["pins"].append(_pin(it, unit))
            elif it[0] in ("rectangle", "polyline", "circle", "arc", "bezier", "text"):
                items.append(it)
    return out


def _pin(it, unit) -> dict:
    at = _get(it, "at") or ["at", 0, 0, 0]
    nm, no = _get(it, "name"), _get(it, "number")
    return {"etype": it[1] if len(it) > 1 else "", "shape": it[2] if len(it) > 2 and not isinstance(it[2], list) else "line",
            "x": _num(at, 1), "y": _num(at, 2), "angle": _num(at, 3), "length": _num(_get(it, "length") or [], 1, 2.54),
            "name": str(nm[1]) if nm and len(nm) > 1 else "", "number": str(no[1]) if no and len(no) > 1 else "",
            "name_size": _font(nm or [], 1.27), "num_size": _font(no or [], 1.27), "hidden": _hidden(it), "unit": unit}


def _natkey(s: str):
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.findall(r"\d+|\D+", s or "")]


def pin_table(sym: dict) -> list[dict]:
    """引脚表：编号自然排序；多单元时带单元字母。同一编号在多个单元重复出现（共用电源脚）只列一次。"""
    multi = len([u for u in sym["units"] if u]) > 1
    seen, out = set(), []
    for p in sorted(sym["pins"], key=lambda p: (_natkey(p["number"]), p["unit"])):
        k = (p["number"], p["name"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"number": p["number"], "name": _plain(p["name"]), "type": ETYPE.get(p["etype"], p["etype"].upper()),
                    "unit": chr(64 + p["unit"]) if multi and p["unit"] else "", "hidden": p["hidden"]})
    return out


def _plain(s: str) -> str:
    """KiCad 上划线写法 ~{RESET} → RESET（表格里加 ‾ 前缀标出低有效）。"""
    return re.sub(r"~\{([^}]*)\}", lambda m: "‾" + m.group(1), s or "")


# --------------------------------------------------------------------------- SVG
class _Box:
    def __init__(self):
        self.x0 = self.y0 = math.inf
        self.x1 = self.y1 = -math.inf

    def add(self, x, y, pad=0.0):
        self.x0, self.y0 = min(self.x0, x - pad), min(self.y0, y - pad)
        self.x1, self.y1 = max(self.x1, x + pad), max(self.y1, y + pad)

    def ok(self):
        return self.x0 < math.inf


def _stroke(it, default=0.254) -> float:
    w = _get(_get(it, "stroke") or [], "width")
    v = _num(w, 1, 0) if w else 0
    return max(v, 0.25) if v > 0 else default                         # 太细的线缩略图里看不见，最细按 0.25mm 画


def _fill(it) -> str:
    t = _get(_get(it, "fill") or [], "type")
    t = t[1] if t and len(t) > 1 else "none"
    return {"background": FILL, "outline": BODY}.get(t, "none")


def _text_svg(x, y, s, size, color, anchor="middle", baseline="central", angle=0.0) -> str:
    """文字：~{X} 画上划线。angle 为 KiCad 角度（逆时针为正）；SVG 里取反。"""
    parts, pos = [], 0
    for m in re.finditer(r"~\{([^}]*)\}", s):
        parts.append(escape(s[pos:m.start()]))
        parts.append(f'<tspan text-decoration="overline">{escape(m.group(1))}</tspan>')
        pos = m.end()
    parts.append(escape(s[pos:]))
    rot = f' transform="rotate({-angle:g} {x:.3f} {y:.3f})"' if angle else ""
    return (f'<text x="{x:.3f}" y="{y:.3f}" font-size="{size:.3f}" fill="{color}" text-anchor="{anchor}" '
            f'dominant-baseline="{baseline}"{rot}>{"".join(parts)}</text>')


def _arc_path(sx, sy, mx, my, ex, ey) -> str | None:
    """三点圆弧 → SVG path（已是 SVG 坐标）。三点共线时退化为直线。"""
    d = 2 * (sx * (my - ey) + mx * (ey - sy) + ex * (sy - my))
    if abs(d) < 1e-9:
        return f"M{sx:.3f},{sy:.3f} L{ex:.3f},{ey:.3f}"
    ux = ((sx * sx + sy * sy) * (my - ey) + (mx * mx + my * my) * (ey - sy) + (ex * ex + ey * ey) * (sy - my)) / d
    uy = ((sx * sx + sy * sy) * (ex - mx) + (mx * mx + my * my) * (sx - ex) + (ex * ex + ey * ey) * (mx - sx)) / d
    r = math.hypot(sx - ux, sy - uy)
    cross = (mx - sx) * (ey - sy) - (my - sy) * (ex - sx)          # 中点在起终点连线哪一侧 → 扫描方向
    a1, a2, am = (math.atan2(p[1] - uy, p[0] - ux) for p in ((sx, sy), (ex, ey), (mx, my)))
    sweep = 1 if cross > 0 else 0
    span = (a2 - a1) % (2 * math.pi) if sweep else (a1 - a2) % (2 * math.pi)
    large = 1 if span > math.pi else 0
    return f"M{sx:.3f},{sy:.3f} A{r:.3f},{r:.3f} 0 {large} {sweep} {ex:.3f},{ey:.3f}"


def _graphic(it, box: _Box) -> str:
    k, w = it[0], _stroke(it)
    style = f'stroke="{BODY}" stroke-width="{w:.3f}" fill="{_fill(it)}"'
    if k == "rectangle":
        s, e = _get(it, "start"), _get(it, "end")
        x0, y0, x1, y1 = _num(s, 1), -_num(s, 2), _num(e, 1), -_num(e, 2)
        box.add(x0, y0, w); box.add(x1, y1, w)
        return f'<rect x="{min(x0, x1):.3f}" y="{min(y0, y1):.3f}" width="{abs(x1 - x0):.3f}" height="{abs(y1 - y0):.3f}" {style}/>'
    if k in ("polyline", "bezier"):
        pts = [(_num(p, 1), -_num(p, 2)) for p in _all(_get(it, "pts") or [], "xy")]
        if not pts:
            return ""
        for x, y in pts:
            box.add(x, y, w)
        if k == "bezier" and len(pts) == 4:
            (a, b), (c, d), (e, f), (g, h) = pts
            return f'<path d="M{a:.3f},{b:.3f} C{c:.3f},{d:.3f} {e:.3f},{f:.3f} {g:.3f},{h:.3f}" {style}/>'
        return f'<polyline points="{" ".join(f"{x:.3f},{y:.3f}" for x, y in pts)}" {style} stroke-linejoin="round"/>'
    if k == "circle":
        c, r = _get(it, "center"), _num(_get(it, "radius") or [], 1)
        x, y = _num(c, 1), -_num(c, 2)
        box.add(x, y, r + w)
        return f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{r:.3f}" {style}/>'
    if k == "arc":
        s, m, e = _get(it, "start"), _get(it, "mid"), _get(it, "end")
        if not (s and e):
            return ""
        sx, sy, ex, ey = _num(s, 1), -_num(s, 2), _num(e, 1), -_num(e, 2)
        mx, my = (_num(m, 1), -_num(m, 2)) if m else ((sx + ex) / 2, (sy + ey) / 2)
        for x, y in ((sx, sy), (mx, my), (ex, ey)):
            box.add(x, y, w)
        d = _arc_path(sx, sy, mx, my, ex, ey)
        return f'<path d="{d}" {style}/>' if d else ""
    if k == "text" and len(it) > 1:
        at = _get(it, "at") or ["at", 0, 0, 0]
        x, y, size = _num(at, 1), -_num(at, 2), _font(it)
        s = str(it[1])
        box.add(x - len(s) * size * 0.3, y - size, 0); box.add(x + len(s) * size * 0.3, y + size, 0)
        return _text_svg(x, y, s, size, BODY, angle=_num(at, 3) % 180)
    return ""


def _pin_svg(p: dict, sym: dict, box: _Box) -> str:
    if p["hidden"]:
        return ""
    a = math.radians(p["angle"])
    dx, dy = round(math.cos(a), 6), -round(math.sin(a), 6)            # SVG 方向（Y 向下）
    x0, y0 = p["x"], -p["y"]
    L = p["length"]
    x1, y1 = x0 + dx * L, y0 + dy * L                                  # 引脚伸进主体的一端
    vertical = abs(dy) > 0.5
    out = [f'<line x1="{x0:.3f}" y1="{y0:.3f}" x2="{x1:.3f}" y2="{y1:.3f}" stroke="{PIN}" stroke-width="0.2"/>']
    if p["shape"] in ("inverted", "inverted_clock"):
        r = 0.5
        out.append(f'<circle cx="{x1 - dx * r:.3f}" cy="{y1 - dy * r:.3f}" r="{r}" fill="none" stroke="{PIN}" stroke-width="0.152"/>')
    if p["shape"] in ("clock", "inverted_clock"):
        nx, ny = -dy, dx
        out.append(f'<polyline points="{x1 + nx * 0.6:.3f},{y1 + ny * 0.6:.3f} {x1 + dx * 0.9:.3f},{y1 + dy * 0.9:.3f} {x1 - nx * 0.6:.3f},{y1 - ny * 0.6:.3f}" '
                   f'fill="none" stroke="{BODY}" stroke-width="0.152"/>')
    box.add(x0, y0, 0.6); box.add(x1, y1, 0.6)
    rot = 90 if vertical else 0
    name, num = p["name"], p["number"]
    show_name = name and name != "~" and not sym["hide_names"]
    off = sym["pin_names_offset"]
    if show_name:
        ns = p["name_size"]
        if off > 0:                                                    # 名称在主体内侧，紧挨引脚末端
            tx, ty = x1 + dx * off, y1 + dy * off
            # 文字从引脚末端往主体里排；竖直引脚文字转 90°（从下往上读），向下伸进主体的引脚要用 end 对齐
            anchor = ("start" if dy < 0 else "end") if vertical else ("start" if dx > 0 else "end")
            out.append(_text_svg(tx, ty, name, ns, PIN_NAME, anchor=anchor, angle=rot))
            ext = len(name) * ns * 0.65
            box.add(tx + dx * ext, ty + dy * ext, ns)
        else:                                                          # 偏移 0：名称写在引脚线上方
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            nx, ny = (-0.4, 0) if vertical else (0, -0.4)
            out.append(_text_svg(mx + nx, my + ny, name, ns, PIN_NAME, baseline="auto", angle=rot))
            box.add(mx, my - ns, len(name) * ns * 0.35)
    if num and not sym["hide_numbers"]:
        ns = p["num_size"]
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        if show_name and off <= 0:                                     # 名称占了上方 → 编号放下方
            nx, ny = (0.4, 0) if vertical else (0, 0.4)
            out.append(_text_svg(mx + nx, my + ny, num, ns, PIN_NUM, baseline="hanging", angle=rot))
        else:
            nx, ny = (-0.4, 0) if vertical else (0, -0.4)
            out.append(_text_svg(mx + nx, my + ny, num, ns, PIN_NUM, baseline="auto", angle=rot))
        box.add(mx, my - ns, len(num) * ns * 0.35)
    return "".join(out)


def render_units(sym: dict) -> list[tuple[str, str]]:
    """→ [(单元标签, svg 字符串)]。单元 0 的图形/引脚并入每个单元。"""
    units = sorted(u for u in sym["units"] if u) or [0]
    multi = len(units) > 1
    out = []
    for u in units:
        box = _Box()
        body = [_graphic(it, box) for it in sym["units"].get(0, []) + (sym["units"].get(u, []) if u else [])]
        pins = [_pin_svg(p, sym, box) for p in sym["pins"] if p["unit"] in (0, u)]
        if not box.ok():
            continue
        pad = 1.5
        x, y, w, h = box.x0 - pad, box.y0 - pad, box.x1 - box.x0 + 2 * pad, box.y1 - box.y0 + 2 * pad
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x:.2f} {y:.2f} {w:.2f} {h:.2f}" '
               f'width="{w * 6:.0f}" height="{h * 6:.0f}" font-family="Helvetica,Arial,sans-serif" style="max-width:100%;height:auto;background:#fff">'
               + "".join(body) + "".join(pins) + "</svg>")
        out.append((f"单元 {chr(64 + u)}" if multi else "", svg))
    return out


def render_svg(sym: dict) -> str:
    """所有单元横向拼成一张 SVG（复核列表里的缩略图用）。"""
    parts = render_units(sym)
    if not parts:
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 10" width="240" height="60"><text x="2" y="6" font-size="3">无图形</text></svg>'
    if len(parts) == 1:
        return parts[0][1]
    # 每块按自身 viewBox 宽高排在一行，中间留 4mm
    boxes = []
    for _, s in parts:
        x, y, w, h = map(float, re.search(r'viewBox="([^"]+)"', s).group(1).split())
        boxes.append((x, y, w, h, s))
    gap = 4.0
    W = sum(b[2] for b in boxes) + gap * (len(boxes) - 1)
    H = max(b[3] for b in boxes)
    cx, inner = 0.0, []
    for x, y, w, h, s in boxes:
        content = s[s.index(">") + 1:-len("</svg>")]
        inner.append(f'<g transform="translate({cx - x:.3f},{-y:.3f})">{content}</g>')
        cx += w + gap
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.2f} {H:.2f}" width="{W * 6:.0f}" height="{H * 6:.0f}" '
            f'font-family="Helvetica,Arial,sans-serif" style="max-width:100%;height:auto;background:#fff">' + "".join(inner) + "</svg>")


def preview(text: str, name: str | None = None) -> dict | None:
    """一次拿齐：{name, units:[(标签, svg)], pins:[…], n_pins}。解析失败返回 None。"""
    try:
        sym = load(text, name)
    except Exception:  # noqa —— 文件格式异常时不让页面报错
        return None
    if not sym:
        return None
    pins = pin_table(sym)
    return {"name": sym["name"], "units": render_units(sym), "pins": pins, "n_pins": len(pins)}
