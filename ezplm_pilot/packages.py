"""通用封装映射：器件的封装属性 / 描述中的封装叫法 → KiCad 官方封装名。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from .models import Part, Proposal
from .taxonomy import Taxonomy

HERE = Path(__file__).resolve().parent.parent
PKG_IN_DESC = re.compile(                                            # 边界用字母数字环视（\b 在"1206封装"这类中英文相连处不成立）
    r"(?<![A-Za-z0-9])(SOT-?\d{2,3}(?:-\d)?|SOD-?\d{2,3}F?L?|SOIC-?\d{1,2}W?|\d{1,2}-?SOIC|TSSOP-?\d{1,2}|\d{1,2}-?TSSOP|MSOP-?\d{1,2}|SSOP-?\d{1,2}|"
    r"QFN-?\d{1,3}|DFN-?\d{1,2}|LQFP-?\d{2,3}|TQFP-?\d{2,3}|DIP-?\d{1,2}|TO-?\d{2,3}(?:-\d)?|DPAK|D2PAK|SMA|SMB|SMC|SC-?70(?:-\d)?|"
    r"0201|0402|0603|0805|1206|1210|2512)(?![A-Za-z0-9])", re.I)


# 文字里写的引脚数："5 PIN" "5-pin" "5引脚" "5脚"，IHS 描述里的 JEDEC 叫法 "PDSO5" "PQFP48"
PINS_IN_TEXT = re.compile(r"(?<![\d.])(\d{1,3})\s*-?\s*(?:PINS?|LEADS?|引脚|脚|针)(?![A-Za-z])|\b[PRC]?(?:DSO|QFP|DIP|QCC|BGA|SON|QFN)(\d{1,3})\b", re.I)
# 多元件（排阻、排容、阵列）：不能用单个两端片式封装
ARRAY_WORDS = re.compile(r"阵列|排阻|排容|网络|Array|Network|\d\s*(?:个|路)?\s*(?:电阻|电容|元件)(?!器)|\b(?:Dual|Quad)\b|双路|四路", re.I)
# 封装名本身不带引脚数、但同名有多种脚数的：SOT-23(3/5/6)、SC-70(3/5/6)
FAMILY_WITH_PINS = re.compile(r"^(SOT-?23|SC-?70)$", re.I)


def pins_in_text(text: str) -> int | None:
    m = PINS_IN_TEXT.search(text or "")
    return int(m.group(1) or m.group(2)) if m else None


def find_package(text: str) -> str:
    """文字里的封装叫法；"SOT-23, 5 PIN" 这类补成 "SOT-23-5"。"""
    m = PKG_IN_DESC.search(text or "")
    if not m:
        return ""
    pkg = m.group(1)
    n = pins_in_text(text)
    if n and FAMILY_WITH_PINS.match(pkg):
        pkg = f"{pkg}-{n}"
    return pkg


def footprint_conflict(kicad: str, text: str) -> str | None:
    """映射出的封装和文字矛盾时给出原因（不出建议）：引脚数对不上；多元件器件配了两端片式封装。"""
    from .kicad import expected_pins_from_footprint
    name = kicad.split(":")[-1]
    n = pins_in_text(text)
    exp = expected_pins_from_footprint(name)
    if exp is None and name.startswith(("SOT-23", "SOT-323")) and not re.search(r"-\d$", name):
        exp = 3
    if n and exp and abs(n - exp) > 1:
        return f"文字写 {n} 脚，封装 {name} 是 {exp} 脚"
    if re.match(r"^(R|C|L|D|LED)_\d{4}_", name) and ARRAY_WORDS.search(text or ""):
        return "多元件器件（阵列/排阻），不能用单个片式封装"
    return None


class PackageMapper:
    def __init__(self, tax: Taxonomy | None = None, cfg_path: str | Path = HERE / "config" / "packages.yaml"):
        cfg = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))
        self.rules = [(re.compile(r["pattern"], re.I), r["kicad"], r.get("conf", 0.8), r.get("roots")) for r in cfg["rules"]]
        self.chip_prefix = cfg.get("chip_prefix_by_root", {})
        self.tax = tax

    def map_name(self, pkg: str, root_category: str = "") -> tuple[str, float] | None:
        pkg = (pkg or "").strip()
        if not pkg:
            return None
        for pat, kicad, conf, roots in self.rules:
            if pat.search(pkg):
                if roots and root_category not in roots:
                    continue
                if kicad.startswith("Resistor_SMD:R_") and root_category in self.chip_prefix:
                    kicad = self.chip_prefix[root_category] + kicad.split(":R_")[1]
                return kicad, conf
        return None

    def propose(self, parts: Iterable[Part], package_attr: dict[str, str] | None = None) -> Iterator[Proposal]:
        """package_attr: {part_id: 封装属性值}（来自物料属性表）。没有时退回到从描述里抓。"""
        for p in parts:
            if p.footprint:
                continue
            root = self.tax.path(p.category_id).split(" > ")[0] if (self.tax and p.category_id in self.tax.nodes) else ""
            src, pkg = "attr", (package_attr or {}).get(p.id, "")
            text = f"{p.description} {p.description_en}"
            if not pkg:
                pkg, src = find_package(text), "description"
            hit = self.map_name(pkg, root)
            if not hit or footprint_conflict(hit[0], f"{pkg} {text}"):
                continue
            kicad, conf = hit
            if src == "description":
                conf = round(conf * 0.95, 3)      # 描述里明确写出的封装名可信度较高（已按一级分类门控）
            action = "auto_fill" if conf >= 0.85 else "review"
            yield Proposal(p.id, p.mpn, "footprint", "", kicad.split(":")[-1], kicad, conf, action, f"package-map:{src}",
                           {"package_text": pkg}, ["封装自动映射"] if action == "auto_fill" else ["封装待复核"])
