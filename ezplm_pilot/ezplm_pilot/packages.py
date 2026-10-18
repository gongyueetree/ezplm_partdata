"""通用封装映射：器件的封装属性 / 描述中的封装叫法 → KiCad 官方封装名。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from .models import Part, Proposal
from .taxonomy import Taxonomy

HERE = Path(__file__).resolve().parent.parent
PKG_IN_DESC = re.compile(
    r"\b(SOT-?\d{2,3}(?:-\d)?|SOD-?\d{2,3}F?L?|SOIC-?\d{1,2}W?|\d{1,2}-?SOIC|TSSOP-?\d{1,2}|\d{1,2}-?TSSOP|MSOP-?\d{1,2}|SSOP-?\d{1,2}|"
    r"QFN-?\d{1,3}|DFN-?\d{1,2}|LQFP-?\d{2,3}|TQFP-?\d{2,3}|DIP-?\d{1,2}|TO-?\d{2,3}(?:-\d)?|DPAK|D2PAK|SMA|SMB|SMC|SC-?70(?:-\d)?|"
    r"0201|0402|0603|0805|1206|1210|2512)\b", re.I)


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
            if not pkg:
                m = PKG_IN_DESC.search(f"{p.description} {p.description_en}")
                pkg, src = (m.group(1) if m else ""), "description"
            hit = self.map_name(pkg, root)
            if not hit:
                continue
            kicad, conf = hit
            if src == "description":
                conf = round(conf * 0.95, 3)      # 描述里明确写出的封装名可信度较高（已按一级分类门控）
            action = "auto_fill" if conf >= 0.85 else "review"
            yield Proposal(p.id, p.mpn, "footprint", "", kicad.split(":")[-1], kicad, conf, action, f"package-map:{src}",
                           {"package_text": pkg}, ["封装自动映射"] if action == "auto_fill" else ["封装待复核"])
