"""ezPLM 物料库数据治理 · 第 0 阶段试点工具包。

模块一览
- sources     数据接入：CSV / 后台样本 / API 适配器，以及写回（默认 dry-run 产出 JSONL）
- taxonomy    物料分类树（路径、叶子、同义词）
- gap         缺口分析（字段缺失率 × 分类 × 制造商，价值分）
- classify    分类校验：规则 → 近邻投票 → LLM 仲裁 → 符号/封装交叉验证 → 决策
- kicad       KiCad 符号/封装/工程文件解析、型号匹配、QA
- packages    通用封装名 → KiCad 标准封装映射
- urls        官网 / 数据手册 URL 模板生成与存活验证
"""

__version__ = "0.1.0"
