# 笔记（Notes）API 设计

规划版本：**v0.2**（`PATCH /api/notes/{id}/notetype` 归入 v0.3）
AnkiConnect 对照：共 21 个 action。

## 接口一览

| 方法 | 路径 | 说明 | AnkiConnect 对照 | 状态 |
|------|------|------|------------------|------|
| GET | `/api/notes` | 笔记列表（分页，支持搜索） | `findNotes` + `notesInfo` | ✅ 已实现（搜索待补） |
| POST | `/api/notes` | 创建笔记 | `addNote` | ✅ 已实现 |
| GET | `/api/notes/{id}` | 笔记详情 | `notesInfo` | ✅ 已实现 |
| DELETE | `/api/notes/{id}` | 删除笔记 | `deleteNotes` | ✅ 已实现 |
| GET | `/api/notes/{id}/cards` | 笔记的卡片列表 | `cardsInfo`（部分） | ✅ 已实现 |
| PATCH | `/api/notes/{id}` | 修改字段/标签/牌组 | `updateNoteFields` / `updateNote` | 未实现 |
| POST | `/api/notes/batch` | 批量创建笔记 | `addNotes` | 未实现 |
| POST | `/api/notes/validate` | 创建前预检（不写入） | `canAddNote` / `canAddNotes` / `canAddNoteWithErrorDetail` / `canAddNotesWithErrorDetail` | 未实现 |
| GET | `/api/notes?modified_since=` | 按修改时间过滤 | `notesModTime` | 未实现 |
| POST | `/api/notes/actions/remove-empty` | 清理空笔记 | `removeEmptyNotes` | 未实现 |
| PATCH | `/api/notes/{id}/notetype` | 更换笔记类型 | `updateNoteModel` | 未实现（v0.3） |
| GET | `/api/notes/{id}/tags` | 笔记的标签 | `getNoteTags` | 未实现 |
| POST | `/api/notes/{id}/tags` | 添加标签 | `addTags` | 未实现 |
| PUT | `/api/notes/{id}/tags` | 整体替换标签 | `updateNoteTags` / `replaceTags` | 未实现 |
| DELETE | `/api/notes/{id}/tags` | 移除指定标签 | `removeTags` | 未实现 |
| GET | `/api/tags` | 全部标签 | `getTags` | 未实现 |
| POST | `/api/tags/actions/replace` | 全局重命名标签 | `replaceTagsInAllNotes` | 未实现 |
| DELETE | `/api/tags?unused=true` | 清理未使用标签 | `clearUnusedTags` | 未实现 |

## 已实现接口（行为保持现状）

### `GET /api/notes`

分页返回全部笔记。**v0.2 需扩展**：增加 `query` 与 `modified_since` 查询参数。

| 查询参数 | 类型 | 说明 |
|----------|------|------|
| `page` / `limit` | number | 分页，默认 `1` / `20` |
| `query` | string | Anki 搜索语法，如 `deck:CS is:suspended`（对应 `findNotes`） |
| `modified_since` | number | Unix 时间戳，仅返回其后修改的笔记（对应 `notesModTime`） |
| `fields` | string | `summary` 时仅返回 id/guid/tags/modified，默认返回完整字段 |

### `POST /api/notes`

创建笔记，返回 `201` 与完整资源。现有行为不变：

```json
{
  "data": {
    "type": "notes",
    "deck_id": 123,
    "attributes": {
      "note_type": "Basic",
      "fields": { "Front": "Q", "Back": "A" },
      "tags": ["tag1"]
    }
  }
}
```

> 建议 v0.2 顺带增加 `attributes.options.allow_duplicate`（布尔，默认 `false`）：为 `false` 时若首字段重复则返回 `409`，对齐 AnkiConnect `addNote` 的 `options.allowDuplicate`。

## 待实现接口

### `PATCH /api/notes/{id}` — 修改笔记

对应 `updateNoteFields` / `updateNote`。仅更新请求中出现的属性：

```json
{
  "data": {
    "type": "notes",
    "id": "1514547547030",
    "attributes": {
      "fields": { "Front": "新正面" },
      "tags": ["整体替换后的标签列表"],
      "deck_id": 456
    }
  }
}
```

- `fields`：按字段名合并更新，未提及的字段保持不变；非法字段名返回 `400`
- `tags`：出现时**整体替换**（增量增删请用 `/api/notes/{id}/tags` 子资源）
- `deck_id`：出现时将该笔记的全部卡片移入目标牌组（与 `changeDeck` 联动）
- 响应 `200`，返回更新后的完整笔记资源

### `POST /api/notes/batch` — 批量创建

对应 `addNotes`。`data` 为数组，元素结构与单条创建一致：

```json
{
  "data": [
    { "type": "notes", "deck_id": 123, "attributes": { "note_type": "Basic", "fields": {...} } },
    { "type": "notes", "deck_id": 123, "attributes": { "note_type": "Basic", "fields": {...} } }
  ]
}
```

响应 `200`（非全成功时也返回 200，逐项给出结果）：

```json
{
  "data": [
    { "type": "notes", "id": "1514547547030" },
    null
  ],
  "meta": { "total": 2, "created": 1, "failed": 1 }
}
```

失败的元素位置返回 `null` 并在 `errors` 数组中按 `source.pointer` 指明下标。

### `POST /api/notes/validate` — 创建前预检

对应 `canAddNote(s) / canAddNotesWithErrorDetail`。请求体与 `POST /api/notes`（或 `/batch`）相同，但不写入任何数据：

```json
{
  "data": [
    { "valid": true, "detail": null },
    { "valid": false, "detail": "cannot create note because it is a duplicate" }
  ]
}
```

### `POST /api/notes/actions/remove-empty` — 清理空笔记

对应 `removeEmptyNotes`。无请求体，响应：

```json
{ "data": null, "meta": { "removed": 3 } }
```

### `PATCH /api/notes/{id}/notetype` — 更换笔记类型（v0.3）

对应 `updateNoteModel`。更换类型需要字段映射：

```json
{
  "data": {
    "type": "notes",
    "id": "1514547547030",
    "attributes": {
      "note_type": "Cloze",
      "field_map": { "Front": "Text", "Back": "Extra" }
    }
  }
}
```

### 标签子资源

笔记的标签也可以直接通过 `PATCH /api/notes/{id}` 的 `tags` 整体替换；子资源用于增量操作。

**`POST /api/notes/{id}/tags`** — 增量添加（`addTags`）：

```json
{ "tags": ["新标签", "另一个::层级标签"] }
```

**`DELETE /api/notes/{id}/tags`** — 移除指定标签（`removeTags`），请求体同上。

**`PUT /api/notes/{id}/tags`** — 整体替换（`updateNoteTags`），请求体同上。

**`GET /api/tags`** — 全部标签（`getTags`），返回字符串数组：

```json
{ "data": ["英语", "英语::听力", "计算机"], "meta": { "total": 3 } }
```

**`POST /api/tags/actions/replace`** — 全局重命名（`replaceTagsInAllNotes`）：

```json
{ "old_tag": "英语", "new_tag": "English" }
```

**`DELETE /api/tags?unused=true`** — 清理未使用标签（`clearUnusedTags`），响应 `meta.removed` 给出清理数量。


---

# 附：笔记与标签扩展接口（v0.8，pylib 原生能力）

## 接口一览

| 方法 | 路径 | 说明 | pylib 方法 |
|------|------|------|-----------|
| POST | `/api/notes/actions/find-replace` | 笔记字段内容查找替换 | `col.find_and_replace()` |
| GET | `/api/notes/dupes` | 按字段查找重复笔记 | `col.find_dupes(field, search)` |
| POST | `/api/notes/actions/render-preview` | 不落库渲染预览 | `note.ephemeral_card()` |
| POST | `/api/notes/actions/suspend` | 笔记级挂起 | `sched.suspend_notes()` |
| POST | `/api/notes/actions/unsuspend` | 笔记级恢复 | `sched.unsuspend_cards()`（按笔记取卡） |
| POST | `/api/notes/actions/bury` | 笔记级搁置 | `sched.bury_notes()` |
| GET | `/api/tags/tree` | 标签层级树 | `tags.tree()` |
| POST | `/api/tags/actions/attach` | 批量加标签（多笔记） | `tags.bulk_add()` |
| POST | `/api/tags/actions/detach` | 批量移除标签（多笔记） | `tags.bulk_remove()` |
| POST | `/api/tags/actions/find-replace` | 标签名查找替换（支持正则） | `tags.find_and_replace()` |
| POST | `/api/tags/actions/reparent` | 更改标签父级 | `tags.reparent()` |

## 设计要点

### `POST /api/notes/actions/find-replace` — 字段内容查找替换

与 notetypes.md 的 find-replace（作用于**模板**）不同，本接口作用于**笔记字段内容**：

```json
{
  "note_ids": [1514547547030],
  "search": "旧词", "replacement": "新词",
  "regex": false, "field_name": "正面", "match_case": false
}
```

`note_ids` 缺省时作用于全部笔记（响应 `meta.updated` 为变更笔记数）。

### `GET /api/notes/dupes`

查询参数 `field`（必填，字段名）与 `query`（可选，在结果中进一步过滤的搜索词），返回重复值分组的笔记 id 列表。

### `POST /api/notes/actions/render-preview` — 不落库渲染

```json
{
  "note_type": "问答题",
  "fields": { "正面": "Q", "背面": "A" },
  "template_ord": 0,
  "fill_empty": false
}
```

可选 `custom_template`（`{ "front": "...", "back": "..." }`）与 `css` 覆盖类型默认模板做试渲染。响应结构与 `GET /api/cards/{id}/render` 一致（question/answer/css/av_tags），**不写入任何数据**。

### 笔记级调度

body 均为 `{ "note_ids": [...] }`，内部先取各笔记的卡片再调用对应 sched 方法；响应 `meta` 给出受影响卡片数。

### 标签增强

- `GET /api/tags/tree`：嵌套树 `[{ "name": "英语", "children": [{ "name": "听力", "children": [] }] }]`
- `attach` / `detach`：body `{ "note_ids": [...], "tags": ["a", "b"] }`，`meta.updated` 为变更笔记数
- `find-replace`：body `{ "search": "...", "replacement": "...", "regex": false, "match_case": false, "note_ids": [...]? }`；`replacement` 为空字符串时删除匹配标签
- `reparent`：body `{ "tags": ["英语::听力"], "new_parent": "语言" }`（空字符串表示移到顶层）
