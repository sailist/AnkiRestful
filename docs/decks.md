# 牌组（Decks）API 设计

规划版本：**v0.2**（牌组配置 `deck-configs` 系列可顺延至 v0.4 与卡片一同落地）
AnkiConnect 对照：共 13 个 action。

## 接口一览

| 方法 | 路径 | 说明 | AnkiConnect 对照 | 状态 |
|------|------|------|------------------|------|
| GET | `/api/decks` | 牌组列表 | `deckNames` / `deckNamesAndIds` / `getDecks` | ✅ 已实现 |
| POST | `/api/decks` | 创建牌组 | `createDeck` | 未实现 |
| GET | `/api/decks/{id}` | 牌组详情 | `deckNameFromId`（扩展） | ✅ 已实现 |
| DELETE | `/api/decks/{id}` | 删除牌组 | `deleteDecks` | 未实现 |
| GET | `/api/decks/{id}/notes` | 牌组内笔记 | `findNotes`（deck 过滤） | ✅ 已实现 |
| GET | `/api/decks/{id}/cards` | 牌组内卡片 | `findCards`（deck 过滤） | 未实现 |
| POST | `/api/decks/{id}/actions/move-cards` | 批量移出卡片 | `changeDeck` | 未实现 |
| GET | `/api/decks/{id}/config` | 牌组配置详情 | `getDeckConfig` | 未实现 |
| PUT | `/api/decks/{id}/config` | 保存牌组配置 | `saveDeckConfig` | 未实现 |
| PATCH | `/api/decks/{id}/config` | 切换配置组 | `setDeckConfigId` | 未实现 |
| POST | `/api/deck-configs` | 克隆配置组 | `cloneDeckConfigId` | 未实现 |
| DELETE | `/api/deck-configs/{id}` | 删除配置组 | `removeDeckConfigId` | 未实现 |

## 已实现接口（行为保持现状）

`GET /api/decks` 返回全部牌组及统计（`review_count` / `new_count` / `learning_count` / `total_cards` / `total_notes`）；`GET /api/decks/{id}` 返回单个牌组；`GET /api/decks/{id}/notes` 返回牌组内笔记列表。

> 说明：AnkiConnect 的 `getDecks(cards)` 语义（返回一组卡片各自所属的牌组名）由卡片资源的 `deck_id` / `deck_name` 属性天然覆盖，不再单独提供。

## 待实现接口

### `POST /api/decks` — 创建牌组

```json
{
  "data": {
    "type": "decks",
    "attributes": { "name": "语言::英语" }
  }
}
```

- 支持 `::` 层级命名，父级不存在时自动创建（与 Anki 行为一致）
- 牌组已存在时返回 `200` 与现有牌组（幂等），而非报错
- 响应 `201`，返回完整牌组资源

### `DELETE /api/decks/{id}` — 删除牌组

| 查询参数 | 类型 | 说明 |
|----------|------|------|
| `cards_too` | boolean | 默认 `false`：仅删除空牌组，非空时返回 `409`；`true` 时连同牌组内卡片一并删除（对应 `deleteDecks(cardsToo=true)`） |

响应 `200`，`meta.deleted` 给出删除的牌组名。

### `GET /api/decks/{id}/cards`

返回牌组内卡片列表，支持分页与 `query` 参数（在 deck 过滤基础上叠加），资源结构见 [cards.md](cards.md)。

### `POST /api/decks/{id}/actions/move-cards`

将指定卡片移入**其他**牌组（与 [cards.md](cards.md) 的 `POST /api/cards/actions/move` 等价，二者实现同一逻辑）：

```json
{ "card_ids": [1498938915662], "target_deck_id": 456 }
```

## 牌组配置（Deck Config）

配置组是独立于牌组的资源（一个配置组可被多个牌组共用），因此单独建模为 `/api/deck-configs`。

### `GET /api/decks/{id}/config`

返回该牌组当前使用的完整配置对象（`new` / `lapse` / `rev` 等嵌套结构，与 Anki 数据库对齐，原样返回）。

### `PUT /api/decks/{id}/config`

整体替换该牌组的配置对象（对应 `saveDeckConfig`），请求体为完整配置 JSON，响应 `200` 返回保存后的配置。

### `PATCH /api/decks/{id}/config`

切换牌组使用的配置组（对应 `setDeckConfigId`）：

```json
{ "data": { "type": "deck-configs", "id": "2" } }
```

### `POST /api/deck-configs` — 克隆配置组

```json
{
  "data": {
    "type": "deck-configs",
    "attributes": {
      "clone_from": 1,
      "name": "英语专用配置"
    }
  }
}
```

响应 `201`，返回新配置组 id 与名称。

### `DELETE /api/deck-configs/{id}`

删除配置组。若仍有牌组引用该配置，返回 `409` 并在 `detail` 中列出引用方；默认配置（id=1）不可删除，返回 `400`。

## 统计

牌组维度的统计（`getDeckStats`）归入统计类别，见 [stats.md](stats.md) 的 `GET /api/stats/decks`。


---

# 附：牌组扩展接口（v0.8，pylib 原生能力）

## 接口一览

| 方法 | 路径 | 说明 | pylib 方法 |
|------|------|------|-----------|
| PATCH | `/api/decks/{id}` | 重命名 / 改父级 | `decks.rename()` / `decks.reparent()` |
| GET | `/api/decks/tree` | 牌组树（含 new/lrn/rev 计数） | `sched.deck_due_tree()` |
| — | `POST /api/decks` 扩展 | 创建筛选牌组 | `sched.add_or_update_filtered_deck()` |
| POST | `/api/decks/{id}/actions/rebuild` | 重建筛选牌组 | `sched.rebuild_filtered_deck()` |
| POST | `/api/decks/{id}/actions/empty` | 排空筛选牌组 | `sched.empty_filtered_deck()` |
| POST | `/api/decks/{id}/actions/unbury` | 解除当日搁置 | `sched.unbury_deck(deck_id, mode)` |
| POST | `/api/decks/{id}/actions/extend-limits` | 临时提高今日上限 | `sched.extend_limits(new, rev)` |
| GET | `/api/decks/{id}/custom-study-defaults` | 自定义学习默认参数 | `sched.custom_study_defaults()` |
| POST | `/api/decks/{id}/actions/custom-study` | 执行自定义学习 | `sched.custom_study(request)` |
| GET | `/api/deck-configs` | 全部配置组列表 | `decks.all_config()` |
| — | `GET /api/decks/{id}` 扩展 | 属性增加 `is_filtered` | `decks.is_filtered()` |

## 设计要点

### `PATCH /api/decks/{id}` — 重命名 / 改父级

```json
{ "data": { "type": "decks", "id": "123", "attributes": { "name": "语言::英语", "parent_id": 0 } } }
```

- `name`：重命名（`decks.rename` 会联动更新子牌组前缀）；两个属性至少提供一个
- `parent_id`：改父级（`0` 表示移到顶层）；`name` 与 `parent_id` 同时提供时先 reparent 后 rename

### `GET /api/decks/tree`

返回嵌套树，节点含 `deck_id`/`name`/`new_count`/`learn_count`/`review_count`/`total_in_deck`/`children`；支持 `?top_deck_id=` 限定子树。

### 筛选牌组（Filtered Deck）

`POST /api/decks` 创建时 `attributes` 增加可选 `filtered` 块（与普通创建互斥）：

```json
{
  "data": {
    "type": "decks",
    "attributes": {
      "name": "考前突击",
      "filtered": {
        "search": "deck:英语 is:due",
        "search_2": "",
        "limit": 100,
        "order": 0,
        "reschedule": true
      }
    }
  }
}
```

`GET /api/decks/{id}` 的 `attributes.is_filtered` 标识是否为筛选牌组；`POST .../actions/rebuild` 按当前搜索词重新填充；`POST .../actions/empty` 排空（卡片回到原牌组）。对普通牌组调用 rebuild/empty 返回 `400`。

### 其他动作

- `unbury`：body `{ "mode": "all" }`（`all` / `user_only` / `sched_only`）
- `extend-limits`：body `{ "new": 10, "rev": 20 }`（今日临时增加的新卡/复习上限）
- `custom-study`：body 结构复杂，先 `GET .../custom-study-defaults` 取默认结构再改后提交

### `GET /api/deck-configs`

返回全部配置组（id/name/完整配置），补齐 deck-configs 资源的列表端点。
