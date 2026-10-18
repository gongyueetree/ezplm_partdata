# ezPLM 物料库数据治理 · API 契约（给后端同事）

采用"暂存平台"方式后，第一阶段**只需要读接口**（现有的已够用，补上缺失字段过滤与游标分页更好）；
写回接口（第 3 节）等全部数据处理并复核完成后再上线即可。

脚本需要两类接口：**批量读**和**批量写回**。读接口现有后台的 `/admin-api/partlibs`、`/admin-api/admin-categories` 已经能用（脚本已兼容），
但做全量扫描时缺少"按缺失字段过滤"和"游标分页"，建议按下面补齐。所有接口走 `Authorization: Bearer <token>`，
返回体统一 `{"success": true, "data": ...}`；建议给脚本单独发一个长期有效、仅限这几个接口的 token。

## 1. 读：按条件分页拉物料

```
GET /parts?cursor=<opaque>&pageSize=500&missing=symbol,footprint,category&manufacturerId=&categoryId=&updatedAfter=
```

- `missing`：逗号分隔，取值 `category | symbol | footprint | pdf | url | image`，表示只返回这些字段为空的器件（OR 关系；AND 可用 `missingAll`）
- `cursor`：游标分页（按 id 排序即可），比 `current/pageSize` 翻页稳定得多——110 万条用 offset 翻页到后面会非常慢
- 返回每条至少包含：

```json
{"id": "...", "mpn": "...", "manufacturer": {"id": "...", "name": "..."},
 "category": {"id": "...", "name": "..."} | null,
 "symbol": {"id": "...", "name": "STM32H503KBUx", "category": {"name": "MCU_ST_STM32H5"}} | null,
 "footprint": {"id": "...", "name": "QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm", "category": {"name": "Package_DFN_QFN"}} | null,
 "hasPdf": "t", "image": "...|null", "url": "...|null",
 "description": "...", "descriptionEn": "...",
 "attributes": {"封装": "SOT-23-6", "...": "..."},      ← 物料属性里的"封装/Package"字段对封装映射非常重要
 "notes": "...", "status": "normal", "updatedAt": "..."}
```

响应：`{"data": {"items": [...], "nextCursor": "..."|null, "total": 1185350}}`

## 2. 读：分类树 / 符号库 / 封装库

```
GET /admin-categories?taxonomy=partlib|symbol|footprint     （现有，保持）
GET /symbols?name=<exact>&page...      → [{id, name, category, rootSymbol}]
GET /footprints?name=<exact>&page...   → [{id, name, category, has3d, hasPdf}]
```

脚本写回符号/封装时需要把"名字"换成库里的 id；若库里没有，需要先创建（见 4）。

## 3. 写：批量更新物料字段 + 打标签 + 记录来源

```
PATCH /parts/batch
{"updates": [
  {"id": "<part id>",
   "set": {"categoryId": "...", "symbolId": "...", "footprintId": "...", "url": "...", "datasheetUrl": "..."},
   "tags": ["自动分类"],                      ← 追加标签，不覆盖已有标签
   "provenance": {"method": "knn+rule", "confidence": 0.93, "evidence": {...}, "job": "2026-10-18-classify"}}
]}
```

- 一次最多 500 条；返回每条的 `ok/err`
- **只允许"填空"**：`set` 里某字段原值非空时服务端拒绝（返回 err=not_empty），除非 `"overwrite": true`。这是防止脚本误改已有数据的最后一道闸
- `provenance` 建议落到一张独立的 `part_field_provenance` 表（part_id, field, old, new, method, confidence, evidence_json, job, created_at），
  这样随时可以按 job 回滚：`POST /jobs/{job}/rollback`
- 标签建议固定这几个：`自动分类`、`分类疑似错误`、`分类待复核`、`符号自动匹配`、`符号待复核`、`封装自动映射`、`封装待复核`、`链接自动补全`、`待分类`

## 4. 写：创建符号 / 封装（从开源工程提取后入库）

```
POST /symbols     multipart: file=<.kicad_sym 单符号>, name, category, source, license, sourceCommit
POST /footprints  multipart: file=<.kicad_mod>, name, category, source, license, model3d(optional .step/.wrl)
```

返回 `{id}`。`source/license/sourceCommit` 请务必存下来（合规 + 复核时要看出处）。

## 5. 复核队列（可选，但强烈建议）

```
GET  /review?field=category&status=pending&page...   → 待复核列表（脚本写回时 action=review/flag_mismatch 的条目）
POST /review/{id}/approve | /reject                   → 人工处理；approve 时执行 set，并把结果记为训练样本
```

有了这个，后台加一个复核页（左：器件信息 + 数据手册链接；右：建议值 / 渲染出来的符号封装图 / 来源 / 置信度；一键通过或驳回）即可。

## 6. 速率与幂等

- 读接口 ≥ 50 req/s 不限流即可（110 万条 / 500 每页 ≈ 2400 次请求）
- 写接口按 `job + part_id + field` 幂等，重复提交不重复记录
