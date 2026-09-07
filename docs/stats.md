# 统计（Stats）API 设计

规划版本：**v0.5**
AnkiConnect 对照：共 3 个 action（另含牌组统计 `getDeckStats`，从 decks 类别归入本文档）。

## 接口一览

| 方法 | 路径 | 说明 | AnkiConnect 对照 |
|------|------|------|------------------|
| GET | `/api/stats/reviews/today` | 今日复习统计 | `getNumCardsReviewedToday` |
| GET | `/api/stats/reviews/by-day` | 按日复习统计 | `getNumCardsReviewedByDay` |
| GET | `/api/stats/collection` | 集合统计报告 | `getCollectionStatsHTML` |
| GET | `/api/stats/decks` | 各牌组统计 | `getDeckStats` |

## 接口设计

### `GET /api/stats/reviews/today`

```json
{
  "data": {
    "type": "stats",
    "id": "reviews-today",
    "attributes": { "date": "2026-09-07", "review_count": 42 }
  }
}
```

### `GET /api/stats/reviews/by-day`

返回有复习记录的每一天：

```json
{
  "data": [
    { "type": "stats", "id": "2026-09-06", "attributes": { "date": "2026-09-06", "review_count": 35 } },
    { "type": "stats", "id": "2026-09-07", "attributes": { "date": "2026-09-07", "review_count": 42 } }
  ],
  "meta": { "days": 2, "total_reviews": 77 }
}
```

### `GET /api/stats/collection`

集合级统计报告（对应 Anki「统计」页的内容）。

| 查询参数 | 说明 |
|----------|------|
| `format` | `html`（默认，与 `getCollectionStatsHTML` 一致）或 `json`（结构化指标，后续版本扩展） |
| `whole_collection` | `true` 时统计整个集合；`false`（默认）统计当前选中牌组 |

`format=html` 时响应 `Content-Type: text/html`，直接返回报告 HTML。

### `GET /api/stats/decks`

按牌组返回统计（对应 `getDeckStats`）：

| 查询参数 | 说明 |
|----------|------|
| `deck_ids` | 逗号分隔的牌组 id，缺省时返回全部牌组 |

```json
{
  "data": [
    {
      "type": "deck-stats",
      "id": "123",
      "attributes": {
        "deck_id": 123,
        "name": "CS",
        "new_count": 10,
        "learn_count": 3,
        "review_count": 25,
        "total_in_deck": 120
      }
    }
  ]
}
```


---

# 附：统计扩展接口（v0.8，pylib 原生能力）

`CollectionStats` 的各类统计图在 pylib 只产出 HTML；本组端点统一支持 `format` 参数：`html`（默认，直接返回报告片段）与 `json`（结构化数据，需调用后端 graphs 服务或 SQL 聚合，实现成本较高，允许分期落地）。

## 接口一览

| 方法 | 路径 | 说明 | pylib 方法 |
|------|------|------|-----------|
| GET | `/api/stats/forecast` | 未来到期预测 | `stats.dueGraph()` |
| GET | `/api/stats/intervals` | 间隔分布 | `stats.ivlGraph()` |
| GET | `/api/stats/hourly` | 分时段记忆保留率 | `stats.hourGraph()` |
| GET | `/api/stats/card-types` | 卡片类型构成（成熟/年轻/未见/挂起） | `stats.cardGraph()` |

## 设计要点

- 公共查询参数：`deck_id`（缺省为整个集合）、`period`（`month` / `year` / `life`，缺省 `month`）
- `format=html` 时 `Content-Type: text/html` 直返（与既有 `/api/stats/collection` 一致）
- `format=json` 时返回结构化序列（如 forecast 返回 `[{ "date": "2026-09-08", "due": 12, "cumulative": 120 }]`）；v0.8 允许先只实现 html，json 标注 `501 Not Implemented` 后续补
