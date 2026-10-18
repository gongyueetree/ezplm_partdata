# ezPLM 物料库数据治理 · 第 0 阶段试点工具包

对 ezplm.cn（云芯易绘）118 万条物料做：缺口分析 → 分类校验/补全 → 从开源 KiCad 工程/库提取符号封装并匹配 → 官网/数据手册链接补全 → 复核与写回。
全部输出都是"建议"（JSONL，带置信度、方法、证据、标签），默认 **不写回**；复核满意后再经 API 写回。

## 0. 推荐工作方式：暂存数据平台（ezPLM 只读）

```
ezPLM（线上，只读）──API 分批拉取──▶ 暂存库 staging.sqlite / Postgres ──▶ 分批处理（分类/封装/符号/URL）
                                           │                                    │
                                           ▼                                    ▼
                                  复核网站（看板 / 物料 / 复核队列 / 回写）◀── 建议 + 自动写入
                                           │
                        全部处理并复核完成后  ▼  一次性生成回写包，再 --apply 写回 ezPLM
```

```bash
pip install duckdb pandas scikit-learn kiutils requests pyyaml sqlalchemy fastapi uvicorn jinja2 python-multipart anthropic
export EZPLM_API_BASE=https://www.ezplm.cn/admin-api  EZPLM_API_TOKEN=...      # 只读即可
export EZPLM_STAGING_DB=sqlite:///staging.sqlite                               # 或 postgresql://...

python -m ezplm_pilot.cli ingest  --source api --batch 500          # 分批拉取；Ctrl-C 后再跑会从上次页码续
python -m ezplm_pilot.cli scan    --dir <KiCad库/工程目录> --kind vendor-official   # 可多次，建符号/封装索引
python -m ezplm_pilot.cli process --batch 200 [--llm] [--urls]     # 分批处理未处理的物料；可反复执行
python -m ezplm_pilot.cli serve   --port 8080                        # 复核网站（EZPLM_WEB_PASSWORD=xxx 可加登录）
python -m ezplm_pilot.cli status                                     # 进度
python -m ezplm_pilot.cli export                                     # 生成回写包 out/writeback.jsonl（不写 ezPLM）
python -m ezplm_pilot.cli export --apply                             # 最后一步：真正写回（需要写接口，见 docs/API_CONTRACT.md）
```

暂存库结构（`ezplm_pilot/staging.py`）：`parts` 每条物料存 `orig_*`（拉取时快照，永不改）和 `new_*`（只有"自动写入"或"复核通过"才写）；
`proposals` 每条建议（字段/建议值/置信度/方法/证据/复核状态）；`categories` 分类树；`sync_state` 拉取游标；`jobs` 每批次日志。
回写包只包含"有新值、没有待复核建议、未回写"的物料，并且只填 ezPLM 里为空的字段。

复核网站四个页面：看板（进度、缺口、建议分布、批次日志）、物料（按缺失字段/标签/是否有新值筛选，原值→新值对照）、
复核队列（按字段/动作/置信度筛选，一键通过/驳回/撤销自动写入，通过即写 new_*）、回写（生成并下载回写包；真正写回只在命令行）。

## 0b. 安装与运行（单步命令，不经暂存库）

```bash
pip install duckdb pandas scikit-learn kiutils requests pyyaml anthropic
cd ezplm_pilot

python -m ezplm_pilot.cli profile                       # 缺口分析 → out/gap_report.html + gap_*.csv
python -m ezplm_pilot.cli classify                      # 分类校验 → out/proposals_category.jsonl
python -m ezplm_pilot.cli classify --llm                # 加 LLM 仲裁（需 ANTHROPIC_API_KEY；只对 10%~20% 的疑难件调用）
python -m ezplm_pilot.cli scan --dir data/sources/espressif-kicad --kind vendor-official   # 建符号/封装索引（可多次追加不同来源）
python -m ezplm_pilot.cli symbols                       # 型号 ↔ 符号匹配 → out/proposals_symbol.jsonl
python -m ezplm_pilot.cli packages                      # 通用封装映射 → out/proposals_footprint.jsonl
python -m ezplm_pilot.cli urls                          # 官网/数据手册 URL（联网验证）→ out/proposals_url.jsonl
python -m ezplm_pilot.cli summary                       # 汇总 → out/summary.html
python -m ezplm_pilot.cli writeback --file out/proposals_category.jsonl            # dry-run
python -m ezplm_pilot.cli writeback --file out/proposals_category.jsonl --apply    # 真写回（需 EZPLM_API_BASE / EZPLM_API_TOKEN）
```

数据源三选一（`--source`）：
- `sample`（默认）：`data/sample/` 里从后台抓的 150 条真实物料 + 505 节点分类树，离线就能跑通全流程
- `csv --parts parts.csv --categories cats.tsv|json`：数据库导出。列名见 `ezplm_pilot/sources.py` 的 `CSV_COLUMNS`，缺列自动补空
- `api`：走 REST（契约见 `docs/API_CONTRACT.md`；现有 `/admin-api/partlibs` 与 `/admin-api/admin-categories` 已兼容）

API 能否全自动化：**可以**。读接口现在就有；补上 `PATCH /parts/batch`（只填空、带来源、按 job 可回滚）后，`writeback --apply` 即可闭环，不需要人工导出导入。

## 1. 各模块做什么、在样本上跑出了什么

| 模块 | 做法 | 样本上的结果 |
|---|---|---|
| `gap.py` 缺口分析 | DuckDB 统计缺失率 × 一级分类 × 制造商；每件算"价值分"（一线厂商、有 PDF、有图、有封装/符号、非模板描述加分）决定补全顺序 | 150 条：符号缺 91%，封装缺 31%，PDF 缺 13%，官网链接缺 100%，分类缺 1.3% |
| `classify.py` 分类校验 | ① 规则（制造商+型号前缀、描述关键词，`config/rules.yaml`，60+ 条）→ ② 字符 n-gram TF-IDF 近邻投票（中文免分词）→ ③ 两层不一致或置信度 0.45~0.85 时送 LLM 做"选择题"（只给 ≤8 个候选）→ ④ 符号库前缀交叉验证 → 决策 | 150 条：147 正确；**自动补 1 条**空分类（DMP10H400SE-13 → 单 FET）；**标记 1 条疑似错分**（ISOW7844FDWE 现在在"电源管理-专用"，应为"数字隔离器"，置信 0.99）；1 条进复核（DMP1055UFDB-13 双 MOSFET，规则与近邻分歧）。LLM 只被调用 13 次（9%） |
| `kicad.py` 符号/封装提取 | kiutils 解析 `.kicad_sym` / `.kicad_mod` / `.kicad_sch`（含内嵌符号与元件 MPN 字段）/ `.kicad_pcb`；记录来源、License、commit；型号匹配：精确 → 符号 MPN 字段 → 去包装后缀 → `x` 通配（STM32H503KBUx）→ 前缀；QA：符号引脚数 vs 封装名引脚数 vs 封装焊盘数、符号默认封装 vs 器件封装；来源可信度加权 | 乐鑫官方库 49 符号/54 封装 + Winterbloom 量产产品工程 78 符号/60 封装/388 元件实例，3.6 秒建完索引；ESP32-S3/C3 精确命中（conf 1.0 自动写入）；MCP6004T-I/ST 通过工程里的 MPN 字段命中（0.90）；模组后缀 N8R8、ATSAMD21G18A-AU 前缀命中进复核（0.64~0.70） |
| `packages.py` 通用封装映射 | 封装属性或描述里的 SOT-23/SOD-123/DPAK/0603… → KiCad 官方封装名（`config/packages.yaml`，按一级分类门控，避免"SMA 接口"被当成 SMA 二极管封装） | 从描述抓到的给 0.77~0.81 进复核；从"封装"属性字段来的会 ≥0.85 自动写入（需要 API 返回 attributes） |
| `urls.py` 链接补全 | 按制造商模板（`config/manufacturers.yaml`，20 家）生成候选，支持通用料号 GPN（ISO7740FDBQ→ISO7740F、STM32H503KBU7→stm32h503kb.pdf）和多模板依次尝试；HTTP 验证通过且不是搜索页才写入 | 模板生成已验证；**存活验证在本沙箱被网络策略拦截（厂商站 403），需在你们服务器上跑** |

决策三档（所有模块统一）：`auto_fill`（原值为空且置信 ≥0.85，写入并打"自动…"标签）/ `flag_mismatch`（原值非空但与推断冲突，不改，打"分类疑似错误"）/ `review`（进复核队列）。阈值在 `classify.Policy` 和各命令的 `--auto-min` 可调。

## 2. 接其它 EDA 格式

| 来源 | 转换 | 备注 |
|---|---|---|
| KiCad 官方库 kicad-symbols / kicad-footprints / kicad-packages3D | 直接 `scan --kind kicad-official` | GitLab 在本沙箱被拦；你们服务器直接 `git clone --depth 1`。CC-BY-SA 4.0 + 例外条款 |
| 乐鑫 espressif/kicad-libraries、Raspberry Pi、ST/TI/ADI 评估板工程 | 直接 scan，`--kind vendor-official` | 已验证乐鑫库 |
| 立创 EasyEDA / 嘉立创开源广场 | `easyeda2kicad --lcsc_id C123456 --symbol --footprint --3d` 按立创编号拉取 → scan | 国产器件覆盖最全；注意立创库条款，保留出处 |
| Altium `.SchLib/.PcbLib` | KiCad 8/9 GUI「导入非 KiCad 库」或 `altium2kicad` → 得到 .kicad_sym/.pretty → scan | 导入器可用 xdotool/脚本批量驱动 |
| Eagle `.lbr` | KiCad 自带 Eagle 导入器 | SparkFun/Adafruit 大量 Eagle 库 |
| GitHub 上的 `.kicad_sch/.kicad_pcb` 工程 | 代码搜索 `extension:kicad_sch`，按 star/是否有生产文件打分 → `scan --kind oshw-product|community` | 已验证 Winterbloom（量产产品） |

## 3. 已知限制 / 下一步

- `kiutils` 对 KiCad 9 新格式个别封装文件解析失败（已加正则兜底数焊盘）；正式跑建议先 `kicad-cli fp upgrade` / `sym upgrade` 统一版本，或改用 `kicad-cli` + sexp 解析
- 近邻层在 150 条小样本上会被"邻居都是同一制造商"带偏；全量 118 万条时请只用**可信子集**（有 PDF、有符号/封装、规格描述非模板化的）做近邻库，并对每个分类做采样上限（如 2000 条）控制内存
- 规则表只覆盖了样本里出现的分类；跑全量前先用 `gap_by_category.csv` 的前 100 个大类补规则（每类 2~5 条即可）
- 封装映射要真正发挥作用，API 需返回物料属性里的"封装"字段（或在导出 CSV 里加 `package` 列，`packages --package-attr` 读取）
- URL 验证和 LLM 调用需要在有外网的机器上跑；全量 118 万条 URL 验证建议限速并缓存（每制造商一个会话，≤5 req/s）
- 分类正确性的"金标准"：建议从复核队列里每天抽 200 条人工判定，持续回填 `rules.yaml` 与近邻库，准确率会越跑越高

## 4. 目录

```
ezplm_pilot/        代码（sources / taxonomy / gap / classify / kicad / packages / urls / staging / pipeline / web / cli）
config/             rules.yaml 分类规则；packages.yaml 封装映射；manufacturers.yaml URL 模板
data/sample/        后台真实样本（150 条 + 分类树）与符号匹配演示样本
data/sources/       测试用的开源库/工程（乐鑫官方库、Winterbloom 工程、KiCad 官方封装 3 个）
docs/API_CONTRACT.md 给后端同事的接口契约
out/                运行产物：gap_report.html、proposals_*.jsonl、summary.html、libindex.sqlite
```
