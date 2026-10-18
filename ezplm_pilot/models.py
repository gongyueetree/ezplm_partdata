from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Part:
    """与后台 /admin-api/partlibs 返回的字段一一对应（扁平化）。"""

    id: str
    mpn: str
    manufacturer: str = ""
    manufacturer_id: str = ""
    category: str = ""            # 分类名（叶子）
    category_id: str = ""
    symbol: str = ""              # KiCad 符号名
    symbol_lib: str = ""          # 符号所属库/分类（后台"符号分类"，如 Transistor_FET）
    footprint: str = ""           # KiCad 封装名
    footprint_lib: str = ""       # 封装所属库/分类（如 Package_TO_SOT_SMD）
    has_pdf: bool = False
    has_image: bool = False
    url: str = ""                 # 官网链接
    description: str = ""
    description_en: str = ""
    status: str = "normal"

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def text(self) -> str:
        """用于分类的文本：制造商 + 型号 + 中英文描述。"""
        return " ".join(x for x in (self.manufacturer, self.mpn, self.description, self.description_en) if x)


@dataclass
class Proposal:
    """一条写回建议。所有模块的输出都统一成这个结构，方便汇总、复核与写回。"""

    part_id: str
    mpn: str
    field: str                      # category_id / symbol / footprint / url / datasheet
    current: str
    proposed: str
    proposed_label: str = ""        # 人可读（如分类名）
    confidence: float = 0.0
    action: str = "review"          # auto_fill | flag_mismatch | review | ok | skip
    method: str = ""                # rule / knn / llm / kicad-official / url-template ...
    evidence: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
