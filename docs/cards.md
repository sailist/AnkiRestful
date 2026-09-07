# 卡片（Cards）API 设计

规划版本：**v0.4**
AnkiConnect 对照：共 21 个 action。

> 当前仅有 `GET /api/notes/{id}/cards`（已实现，保留）。本文档为卡片建立独立资源，覆盖查询、调度操作与复习历史。

## 接口一览

| 方法 | 路径 | 说明 | AnkiConnect 对照 |
|------|------|------|------------------|
| GET | `/api/cards` | 卡片列表（分页，支持搜索） | `findCards` + `cardsInfo` |
| GET | `/api/cards/{id}` | 卡片详情 | `cardsInfo` |
| GET | `/api/cards/{id}/note` | 卡片所属笔记 | `cardsToNotes` |
| GET | `/api/cards?modified_since=` | 按修改时间过滤 | `cardsModTime` |
| GET | `/api/cards/{id}/status` | 状态聚合查询 | `suspended` / `areSuspended` / `areDue` / `getIntervals` / `getEaseFactors` |
| PATCH | `/api/cards/{id}/schedule` | 修改调度参数 | `setDueDate` / `setEaseFactors` / `setSpecificValueOfCard` |
| POST | `/api/cards/actions/suspend` | 挂起 | `suspend` |
| POST | `/api/cards/actions/unsuspend` | 恢复 | `unsuspend` |
| POST | `/api/cards/actions/forget` | 忘记（重置为新卡） | `forgetCards` |
| POST | `/api/cards/actions/relearn` | 重学 | `relearnCards` |
| POST | `/api/cards/actions/answer` | 作答 | `answerCards` |
| POST | `/api/cards/actions/move` | 移入其他牌组 | `changeDeck` |
| GET | `/api/cards/{id}/reviews` | 复习历史 | `cardReviews` / `getReviewsOfCards` / `getLatestReviewID` |
| POST | `/api/cards/{id}/reviews` | 补录复习记录 | `insertReviews` |

## 资源结构

```json
{
  "type": "cards",
  "id": "1498938915662",
  "attributes": {
    "note_id": 1502298033753,
    "deck_id": 123,
    "deck_name": "CS",
    "template_index": 0,
    "type": 2,
    "queue": 2,
    "due": 440,
    "interval": 30,
    "ease_factor": 2500,
    "reviews": 12,
    "lapses": 1,
    "left": 0,
    "modified": 1725600000
  },
  "relationships": {
    "note": { "links": { "related": "/api/cards/1498938915662/note" } },
    "reviews": { "links": { "related": "/api/cards/1498938915662/reviews" } }
  },
  "links": { "self": "/api/cards/1498938915662" }
}
```

`type` / `queue` 为 Anki 数据库原值（新卡=0、学习中=1/3、复习=2 等），与数据库对齐是本插件的设计原则，不做二次封装。

## 查询接口

### `GET /api/cards`

| 查询参数 | 类型 | 说明 |
|----------|------|------|
| `page` / `limit` | number | 分页 |
| `query` | string | Anki 搜索语法，如 `deck:CS is:due`（对应 `findCards`） |
| `modified_since` | number | Unix 时间戳（对应 `cardsModTime`） |

### `GET /api/cards/{id}/status` — 状态聚合

一次返回常用状态位，避免客户端多次调用：

```json
{
  "data": {
    "type": "card-status",
    "id": "1498938915662",
    "attributes": {
      "suspended": false,
      "due": true,
      "intervals": [1, 10, 30],
      "ease_factor": 2500
    }
  }
}
```

批量场景使用 `POST /api/cards/actions/status`，body 为 `{ "card_ids": [...] }`，返回数组（对应 `areSuspended` / `areDue` / `getIntervals` / `getEaseFactors` 的批量语义）。

## 调度修改

### `PATCH /api/cards/{id}/schedule`

仅更新请求中出现的字段：

```json
{
  "data": {
    "type": "cards",
    "id": "1498938915662",
    "attributes": {
      "due_date": "0",
      "ease_factor": 2600,
      "interval": 30,
      "reviews": 12,
      "lapses": 1
    }
  }
}
```

| 字段 | 类型 | 说明 | AnkiConnect 对照 |
|------|------|------|------------------|
| `due_date` | string | 新到期日。复习卡为天偏移（`"0"`=今天，`"1"`=明天，`"-7"`=一周前）；新卡为序号或 `"!"`（置顶） | `setDueDate` |
| `ease_factor` | number | 简易度（千分比，2500 = 250%） | `setEaseFactors` |
| `interval` / `reviews` / `lapses` 等 | number | 任意数据库字段直改 | `setSpecificValueOfCard` |

响应 `200`，返回更新后的卡片资源。

## 调度动作

动作接口统一支持批量：body 中传 `card_ids` 数组；单卡片等价于只含一个 id。响应 `meta` 给出成功/失败计数。

### `POST /api/cards/actions/suspend` / `unsuspend`

```json
{ "card_ids": [1498938915662, 1498938915663] }
```

```json
{ "data": null, "meta": { "succeeded": 2, "failed": 0 } }
```

### `POST /api/cards/actions/forget`

重置为新卡。可选参数：

```json
{ "card_ids": [1498938915662], "reset_count": false, "clear_due": true }
```

### `POST /api/cards/actions/relearn`

将复习卡放回重学队列，body 仅 `card_ids`。

### `POST /api/cards/actions/answer`

对应 `answerCards`（无需 GUI 的作答）：

```json
{
  "answers": [
    { "card_id": 1498938915662, "ease": 3 }
  ]
}
```

`ease` 取值 1–4（Again / Hard / Good / Easy）。响应 `meta.results` 逐项给出 `true/false`。

### `POST /api/cards/actions/move`

对应 `changeDeck`：

```json
{ "card_ids": [1498938915662], "deck_id": 456 }
```

## 复习历史

### `GET /api/cards/{id}/reviews`

```json
{
  "data": [
    {
      "type": "reviews",
      "id": "1725600000000",
      "attributes": {
        "review_time": 1725600000,
        "card_id": 1498938915662,
        "usn": -1,
        "button_pressed": 3,
        "new_interval": 30,
        "previous_interval": 15,
        "new_factor": 2500,
        "review_duration": 8500,
        "review_type": 0
      }
    }
  ],
  "links": { "self": "/api/cards/1498938915662/reviews" },
  "meta": { "count": 12 }
}
```

| 查询参数 | 说明 |
|----------|------|
| `start` | 起始日期（`YYYY-MM-DD`），仅返回该日之后的记录（对应 `cardReviews` 的 startTS） |
| `latest` | `true` 时仅返回最近一条记录的 id（对应 `getLatestReviewID`） |

### `POST /api/cards/{id}/reviews` — 补录复习记录

对应 `insertReviews`（用于导入外部复习数据）：

```json
{
  "data": [
    {
      "type": "reviews",
      "attributes": {
        "review_time": 1725600000,
        "button_pressed": 3,
        "new_interval": 30,
        "previous_interval": 15,
        "new_factor": 2500,
        "review_duration": 8500,
        "review_type": 0
      }
    }
  ]
}
```

响应 `201`，`meta.inserted` 给出写入条数。


---

# 附：卡片扩展接口（v0.8，pylib 原生能力）

## 接口一览

| 方法 | 路径 | 说明 | pylib 方法 |
|------|------|------|-----------|
| POST | `/api/cards/actions/set-flag` | 设置旗帜（0-7） | `col.set_user_flag_for_cards()` |
| POST | `/api/cards/actions/bury` | 当日搁置 | `sched.bury_cards()` |
| POST | `/api/cards/actions/unbury` | 解除搁置 | `sched.unbury_cards()` |
| GET | `/api/cards/{id}/render` | 渲染卡片正反面 HTML | `card.render_output()` |
| GET | `/api/cards/{id}/memory-state` | FSRS 记忆状态 | `col.compute_memory_state()` |
| GET | `/api/cards/{id}/scheduling-states` | 四个作答按钮的下一间隔预览 | v3 `sched` 的 `describe_next_states` / `nextIvlStr` |
| GET | `/api/cards/empty-report` | 空卡片报告 | `col.get_empty_cards()` |
| POST | `/api/cards/actions/remove-empty` | 清理空卡片 | report 后逐个删除 |
| POST | `/api/cards/actions/reposition` | 新卡排序 | `sched.reposition_new_cards()` |
| — | `GET /api/cards/{id}` 扩展 | 属性增加 `flag` | `card.user_flag()` |

## 设计要点

### 旗帜

`POST /api/cards/actions/set-flag` body `{ "card_ids": [...], "flag": 1 }`（0=无旗，1-7=红橙黄绿蓝粉青）；卡片资源 `attributes.flag` 透出当前旗帜。

### 搁置（bury）

`bury` / `unbury` body 均为 `{ "card_ids": [...] }`；`bury` 仅当日有效（次日自动回到队列），与 `suspend`（长期挂起）语义不同。牌组级解除见 decks.md 的 `unbury`。

### `GET /api/cards/{id}/render` — 渲染预览

```json
{
  "data": {
    "type": "card-render",
    "id": "1498938915662",
    "attributes": {
      "question": "<div>...</div>", "answer": "<div>...</div>", "css": ".card { ... }",
      "question_av_tags": [], "answer_av_tags": []
    }
  }
}
```

### `GET /api/cards/{id}/memory-state`

返回 `{ "stability": 12.3, "difficulty": 5.1, "retrievability": 0.9 }`；非 FSRS 调度下字段可能为空（原样返回）。查询参数 `?interval=30` 时附带 `fuzz_delta`（v3 fuzz 天数）。

### `GET /api/cards/{id}/scheduling-states`

返回四个按钮（again/hard/good/easy）的 `{ "label": "...", "interval_text": "<10m" }`，供外部复习客户端展示，作答仍走 `POST /api/cards/actions/answer`。

### 空卡片

`GET /api/cards/empty-report` 返回 EmptyCardsReport（空卡片 id 及原因）；`POST /api/cards/actions/remove-empty` 删除这些空卡片，响应 `meta.removed`。

### `POST /api/cards/actions/reposition`

body `{ "card_ids": [...], "starting_from": 1, "step_size": 1, "randomize": false, "shift_existing": true }`（后四项均可选，取 pylib 默认值）。
