# 开源硬件工作区 BOM 导出格式（给后端同事）

用途：暂存数据平台用开源硬件工作区（open-projects，约 14,000 个 KiCad 工程）里**已经解析好的 BOM 行**，
按型号去匹配 ezPLM 的 118 万条物料，从中统计"这个型号在开源设计里用的是哪个封装/符号"，补充元器件库。全程脚本，不用大模型。

请导出 **一个 CSV 文件**，UTF-8（带不带 BOM 头都行），逗号分隔，第一行是表头，**每个 BOM 行（`boms.items`）一行**。
项目和工程文件的信息在每一行里重复即可（约 40 万行，几十 MB，不用拆表）。

## 列

| 列名 | 来源 | 说明 |
|---|---|---|
| `project_id` | projects.id | |
| `project_name` | projects.name | |
| `project_category` | projects.category.name | 可空 |
| `bom_id` | boms.id | |
| `bom_no` | boms.bomNo | 如 BOM013860 |
| `bom_version` | boms.version | |
| `bom_notes` | boms.notes | 原样导出（含 GitHub 仓库、提交号、目录、许可证），脚本会自己解析 |
| `eda_tool` | boms.designFile.edaTool | kicad / altium … |
| `parse_status` | boms.designFile.parseStatus | succeeded / failed … |
| `zip_file_id` | boms.designFile.file.id | 工程 zip 的文件 ID |
| `zip_url` | boms.designFile.file.url | 见下方"zip 链接" |
| `item_id` | items.id | |
| `value` | items.value | |
| `mpn` | items.mpn | 可空 |
| `manufacturer` | items.manufacturer | 可空 |
| `footprint` | items.footprint | 封装名（不带库名也可以） |
| `designators` | items.designators | 逗号分隔的位号，如 `R1,R2` |
| `quantity` | items.quantity | |
| `part_id` | items.partId | 已关联 ezPLM 物料时填，可空 |

只需要开源硬件工作区（slug = `open-projects`）的数据；`parse_status` 不是 succeeded 的工程文件也请导出（脚本会跳过它们的 BOM 行，但会记录下来）。

## zip 链接

页面上的文件链接是带过期时间的签名链接。第二阶段（从原理图里提取符号）需要下载这些 zip，两种方式任选：

1. **推荐**：`zip_url` 填一个**有效期 ≥ 14 天**的签名下载链接（导出时现签）；
2. 或者 `zip_url` 留空，另外把这些 zip 打包到一个可下载的位置（对象存储目录 / 网盘），文件名用 `zip_file_id.zip`。

第一阶段（按型号匹配封装）只用 BOM 行，不需要 zip。

## 示例（两行）

```csv
project_id,project_name,project_category,bom_id,bom_no,bom_version,bom_notes,eda_tool,parse_status,zip_file_id,zip_url,item_id,value,mpn,manufacturer,footprint,designators,quantity,part_id
01a15517-2979-...,z-fighter - USB串口及可控供电模块,接口转换及调试器,01a15517-378a-...,BOM013859,zfighter-usb-serial-power-e037d5c,"来源：https://github.com/tangent3D/Z-FIGHTER；…；许可：MIT，…",kicad,succeeded,01a15517-35ca-...,https://…/engineering.zip?e=…,01a15517-44a3-...,TPS2061CDBVT,,,SOT-23-5,U2,1,
01a15517-811c-...,clandestine_circuitry - TL074四通道混音电路板,模拟信号处理链路,01a15517-9281-...,BOM013860,clandestine-mixer4-2c14b6f,"来源：https://github.com/mort13/clandestine_circuitry；…；许可：GPL-3.0，…",kicad,succeeded,01a15517-90be-...,https://…/engineering.zip?e=…,01a15517-a644-...,TL074,,,tl74,U1,1,
```

导出后放在本机，运行：

```bash
python -m ezplm_pilot.cli oshw-load  --csv <导出的 CSV>      # 导入暂存库（DATABASE_URL 指向平台 Postgres）
python -m ezplm_pilot.cli oshw-match                          # 按型号匹配 → 生成封装建议
```
