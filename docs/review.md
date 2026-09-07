# 复习与工具（Review & Utils）API 设计

规划版本：**v0.8**（pylib 原生能力，AnkiConnect 未覆盖）

本类别的目标：支撑「无头复习客户端」场景——不打开 Anki 界面，完全通过 API 取卡、预览间隔、作答、判分。

## 接口一览

| 方法 | 路径 | 说明 | pylib 方法 |
|------|------|------|-----------|
| GET | `/api/review/queue` | 取待学队列（幂等预览，不移出队列） | v3 `sched.get_queued_cards()` |
| POST | `/api/utils/compare-answer` | 输入型卡片答案比对 | `col.compare_answer()` |
| POST | `/api/search/build` | 搜索串构造/校验 | `col.build_search_string()` |

## 设计要点

### `GET /api/review/queue` — 待学队列

| 查询参数 | 说明 |
|----------|------|
| `limit` | 返回卡片数，默认 1 |
| `intraday_learning_only` | `true` 时仅返回当日学习队列中的卡 |

响应（卡片字段与 cards.md 的资源结构一致，附带各按钮间隔预览）：

```json
{
  "data": [
    {
      "type": "cards",
      "id": "1498938915662",
      "attributes": { "...": "同卡片资源" },
      "meta": { "scheduling_states": [
        { "rating": 1, "interval_text": "<1m" },
        { "rating": 2, "interval_text": "<6m" },
        { "rating": 3, "interval_text": "<10m" },
        { "rating": 4, "interval_text": "4d" }
      ] }
    }
  ],
  "meta": { "remaining": 42 }
}
```

本接口是**幂等预览**：不改变队列状态。作答走 `POST /api/cards/actions/answer`；渲染卡片走 `GET /api/cards/{id}/render`；按钮间隔的单独查询走 `GET /api/cards/{id}/scheduling-states`。

### `POST /api/utils/compare-answer` — 答案比对

用于「输入答案」型卡片的无头判分：

```json
{ "expected": "Mitochondria", "provided": "mitochondria" }
```

```json
{
  "data": {
    "type": "answer-comparison",
    "attributes": { "correct": true, "expected_html": "...", "provided_html": "..." }
  }
}
```

`correct` 为完全匹配（大小写/组合字符按 pylib 规则处理）；两个 html 字段为带 diff 高亮的对照片段，客户端可直接展示。

### `POST /api/search/build` — 搜索串构造/校验

两种用法：

1. **校验**：body `{ "query": "deck:CS is:due" }` → 合法返回 `{ "valid": true, "normalized": "..." }`，非法返回 `400` 与错误详情
2. **构造**：body `{ "nodes": [{ "deck": "CS" }, { "tag": "英语" }], "joiner": "AND" }` → 返回拼接好的搜索串

客户端可在调用 `GET /api/notes?query=` / `GET /api/cards?query=` 前先用本接口预检，避免搜索语法错误变成 500。
