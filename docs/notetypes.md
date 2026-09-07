# 笔记类型（NoteTypes）API 设计

规划版本：**v0.3**
AnkiConnect 对照：共 26 个 action。

> AnkiConnect 中的 `model*` 即 Anki 的「笔记类型」（NoteType / Model）。本插件统一使用 `notetypes` 作为资源名。

## 接口一览

| 方法 | 路径 | 说明 | AnkiConnect 对照 | 状态 |
|------|------|------|------------------|------|
| GET | `/api/notetypes` | 笔记类型列表 | `modelNames` / `modelNamesAndIds` | ✅ 已实现 |
| POST | `/api/notetypes` | 创建笔记类型 | `createModel` | 未实现 |
| GET | `/api/notetypes/{id}` | 按 id 查询 | `findModelsById` / `modelNameFromId` | 未实现 |
| GET | `/api/notetypes/byname/{name}` | 按名称查询 | `findModelsByName` | ✅ 已实现 |
| GET | `/api/notetypes/{id}/fields` | 字段列表（含字体/描述） | `modelFieldNames` / `modelFieldDescriptions` / `modelFieldFonts` / `modelFieldsOnTemplates` | 未实现 |
| POST | `/api/notetypes/{id}/fields` | 添加字段 | `modelFieldAdd` | 未实现 |
| PATCH | `/api/notetypes/{id}/fields/{name}` | 重命名/排序/字体/描述 | `modelFieldRename` / `modelFieldReposition` / `modelFieldSetFont` / `modelFieldSetFontSize` / `modelFieldSetDescription` | 未实现 |
| DELETE | `/api/notetypes/{id}/fields/{name}` | 删除字段 | `modelFieldRemove` | 未实现 |
| GET | `/api/notetypes/{id}/templates` | 模板内容 | `modelTemplates` | 未实现 |
| POST | `/api/notetypes/{id}/templates` | 添加模板 | `modelTemplateAdd` | 未实现 |
| PUT | `/api/notetypes/{id}/templates` | 整体更新模板 | `updateModelTemplates` | 未实现 |
| PATCH | `/api/notetypes/{id}/templates/{name}` | 重命名/排序 | `modelTemplateRename` / `modelTemplateReposition` | 未实现 |
| DELETE | `/api/notetypes/{id}/templates/{name}` | 删除模板 | `modelTemplateRemove` | 未实现 |
| GET | `/api/notetypes/{id}/styling` | 样式（CSS） | `modelStyling` | 未实现 |
| PUT | `/api/notetypes/{id}/styling` | 更新样式 | `updateModelStyling` | 未实现 |
| POST | `/api/notetypes/{id}/actions/find-replace` | 模板内查找替换 | `findAndReplaceInModels` | 未实现 |

## 已实现接口（行为保持现状）

`GET /api/notetypes` 返回全部笔记类型（含字段名、模板名）；`GET /api/notetypes/byname/{name}` 按名称查询（支持 URL 编码的中文名）。

## 待实现接口

### `POST /api/notetypes` — 创建笔记类型

```json
{
  "data": {
    "type": "notetypes",
    "attributes": {
      "name": "词汇卡",
      "fields": ["单词", "释义", "例句"],
      "css": ".card { font-family: sans-serif; }",
      "templates": [
        { "name": "正面", "front": "{{单词}}", "back": "{{释义}}<br>{{例句}}" }
      ],
      "cloze": false
    }
  }
}
```

- `cloze: true` 时创建填空题类型（此时 `templates` 中 `front`/`back` 含义同 Anki 填空模板）
- 同名类型已存在时返回 `409`
- 响应 `201`，返回完整笔记类型资源

### `GET /api/notetypes/{id}`

按 id 查询，返回结构与 `byname` 一致（`name` / `fields` / `templates`），另含 `css` 与 `cloze` 标记。

## 字段（Fields）

### `GET /api/notetypes/{id}/fields`

聚合返回字段的完整信息：

```json
{
  "data": [
    {
      "type": "notetype-fields",
      "id": "单词",
      "attributes": {
        "name": "单词",
        "order": 0,
        "font": "Arial",
        "font_size": 20,
        "description": "目标单词",
        "used_in_templates": ["正面"]
      }
    }
  ]
}
```

`used_in_templates` 对应 `modelFieldsOnTemplates`（字段被哪些模板的正反面引用）。

### `POST /api/notetypes/{id}/fields`

```json
{ "data": { "type": "notetype-fields", "attributes": { "name": "音标" } } }
```

新字段追加到末尾；如需指定位置，可在创建后调用 `PATCH .../fields/{name}` 调整 `order`。

### `PATCH /api/notetypes/{id}/fields/{name}`

仅更新出现的属性：

```json
{
  "data": {
    "type": "notetype-fields",
    "attributes": {
      "new_name": "Word",
      "order": 1,
      "font": "Times New Roman",
      "font_size": 24,
      "description": "目标单词"
    }
  }
}
```

### `DELETE /api/notetypes/{id}/fields/{name}`

删除字段。若字段仍被模板引用，返回 `409` 并列出引用它的模板。

## 模板（Templates）

### `GET /api/notetypes/{id}/templates`

```json
{
  "data": [
    {
      "type": "notetype-templates",
      "id": "正面",
      "attributes": { "name": "正面", "order": 0, "front": "{{单词}}", "back": "{{释义}}" }
    }
  ]
}
```

### `POST /api/notetypes/{id}/templates`

```json
{
  "data": {
    "type": "notetype-templates",
    "attributes": { "name": "反面", "front": "{{释义}}", "back": "{{单词}}" }
  }
}
```

### `PUT /api/notetypes/{id}/templates`

整体更新多个模板的内容（对应 `updateModelTemplates`，按名称匹配，仅更新出现的模板）：

```json
{
  "data": [
    { "type": "notetype-templates", "id": "正面", "attributes": { "front": "...", "back": "..." } }
  ]
}
```

### `PATCH /api/notetypes/{id}/templates/{name}`

```json
{ "data": { "type": "notetype-templates", "attributes": { "new_name": "逆向", "order": 1 } } }
```

### `DELETE /api/notetypes/{id}/templates/{name}`

删除模板。笔记类型至少保留一个模板，删除最后一个时返回 `400`。

## 样式（Styling）

### `GET /api/notetypes/{id}/styling`

```json
{ "data": { "type": "notetype-styling", "id": "1502298033753", "attributes": { "css": ".card { ... }" } } }
```

### `PUT /api/notetypes/{id}/styling`

整体替换 CSS：

```json
{ "data": { "type": "notetype-styling", "attributes": { "css": ".card { font-size: 22px; }" } } }
```

## 查找替换

### `POST /api/notetypes/{id}/actions/find-replace`

在模板正反面内容中查找替换（对应 `findAndReplaceInModels`）：

```json
{
  "find": "{{FrontSide}}",
  "replace": "",
  "front": true,
  "back": true
}
```

响应 `meta.replaced` 给出替换的模板数量。


---

# 附：笔记类型扩展接口（v0.7/v0.8，pylib 原生能力）

## 接口一览

| 方法 | 路径 | 说明 | pylib 方法 | 规划 |
|------|------|------|-----------|------|
| DELETE | `/api/notetypes/{id}` | 删除笔记类型 | `models.remove(id)` | v0.7 |
| POST | `/api/notetypes/{id}/actions/copy` | 克隆笔记类型 | `models.copy(model)` | v0.8 |
| GET | `/api/notetypes/stock` | 内置类型蓝本列表 | `stdmodels.get_stock_notetypes(col)` | v0.8 |
| — | `POST /api/notetypes` 扩展 | 支持 `stock_kind` 按蓝本创建 | backend `get_stock_notetype_legacy` | v0.8 |
| — | `GET /api/notetypes` 扩展 | 列表项增加 `use_count` | `models.all_use_counts()` | v0.8 |
| — | `GET /api/notetypes/{id}` 扩展 | 详情增加 `use_count`/`sort_field` | `models.use_count` / `sort_idx` | v0.8 |
| — | `GET .../templates` 扩展 | 每项增加 `use_count` | `models.template_use_count(ntid, ord)` | v0.8 |
| PATCH | `/api/notetypes/{id}` | 修改 `sort_field`（排序/查重依据字段） | `models.set_sort_index(model, idx)` | v0.8 |

## 设计要点

### `DELETE /api/notetypes/{id}` — 删除笔记类型（v0.7）

删除会连同该类型的全部笔记与卡片（pylib `models.remove` 语义）。

| 查询参数 | 说明 |
|----------|------|
| `force` | 默认 `false`：类型仍被笔记使用时返回 `409`（detail 含 `use_count`）；`true` 时强制删除 |

响应 `200`，`meta` 给出 `{ "deleted": "类型名", "removed_notes": 12 }`。

### `POST /api/notetypes/{id}/actions/copy` — 克隆（v0.8）

body 可选 `{ "name": "新名称" }`（缺省时 pylib 自动加后缀），响应 `201` 返回新类型资源。

### 内置蓝本（v0.8）

`GET /api/notetypes/stock` 返回 6 种内置类型的 `stock_kind` 与名称（Basic、Basic+reversed、optional-reversed、typing、Cloze、Image Occlusion）。`POST /api/notetypes` 的 `attributes` 增加可选 `stock_kind`：提供时按蓝本创建（cloze/图片遮挡手工构造极易出错，应优先用蓝本），与 `fields/templates/css` 互斥。

### 排序字段（v0.8）

`PATCH /api/notetypes/{id}` body `{ "data": { "type": "notetypes", "attributes": { "sort_field": "单词" } } }`（字段名或下标均可）；`GET` 详情时 `attributes.sort_field` 透出当前排序字段名。
