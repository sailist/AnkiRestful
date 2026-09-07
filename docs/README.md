# AnkiRestful API 设计文档

本目录是 AnkiRestful 插件全部接口的 RESTful 设计规范，对标 [AnkiConnect](https://git.sr.ht/~foosoft/anki-connect) 的 122 个 action，按类别拆分为 8 个文档。

## 分类索引

| 文档 | 类别 | AnkiConnect 接口数 | 规划版本 |
|------|------|-------------------:|----------|
| [notes.md](notes.md) | 笔记（含标签） | 21 | v0.2 |
| [decks.md](decks.md) | 牌组（含牌组配置） | 13 | v0.2 |
| [notetypes.md](notetypes.md) | 笔记类型（模板/字段/样式） | 26 | v0.3 |
| [cards.md](cards.md) | 卡片（调度/复习历史） | 21 | v0.4 |
| [media.md](media.md) | 媒体文件 | 5 | v0.5 |
| [stats.md](stats.md) | 统计 | 3 | v0.5 |
| [system.md](system.md) | 系统/集合/配置档案/导入导出 | 11 | v0.6 |
| [review.md](review.md) | 复习队列与工具（无头复习客户端） | 3（pylib 原生） | v0.8 |
| [gui.md](gui.md) | GUI 操作（暂不规划） | 22 | — |

## 通用约定

### Base URL

```
http://localhost:8102
```

主机与端口由插件 `config.json` 的 `server.host` / `server.port` 决定。

### 数据格式

- 请求与响应均为 JSON，遵循 [JSON:API](https://jsonapi.org/) 规范
- 响应 `Content-Type: application/vnd.api+json`
- 资源对象结构：`{ "type": "<资源类型>", "id": "<id>", "attributes": {...}, "relationships": {...}, "links": {...} }`
- 每个接口表格中的「AnkiConnect 对照」列给出对应的 AnkiConnect action 名，便于迁移

### 方法语义

| 方法 | 语义 |
|------|------|
| `GET` | 查询，无副作用 |
| `POST` | 创建资源；或执行动作（见下文「动作接口」） |
| `PATCH` | 部分更新资源的指定属性 |
| `PUT` | 整体替换资源（或子资源） |
| `DELETE` | 删除资源 |

### 动作接口

无法归入 CRUD 的操作（挂起卡片、触发同步等）统一使用动作路径：

```
POST /api/<资源>/<id>/actions/<action>     # 单个对象
POST /api/<资源>/actions/<action>          # 批量对象，body 中传 id 数组
```

响应为 JSON:API 文档，`meta` 中携带动作结果。

### 分页

列表接口统一使用 `page` / `limit` 查询参数（默认 `page=1, limit=20`）：

- `meta.pagination`：`{ "page", "limit", "total", "pages" }`
- `links`：`self` / `first` / `last` / `prev` / `next`

### 搜索

支持搜索的列表接口使用 `query` 查询参数，值为 **Anki 原生搜索语法**（如 `deck:CS tag:英语 is:due`），与 Anki 浏览器中的搜索框行为一致。

### 错误格式

错误响应为单个错误对象（与既有实现一致）：

```json
{
  "id": null,
  "status": 404,
  "title": "Not Found",
  "detail": "Note with ID 123 not found"
}
```

| 状态码 | 使用场景 |
|--------|----------|
| 400 | 请求体缺失、JSON 非法、参数校验失败 |
| 401 | 启用了 API Key 认证但请求未携带有效凭据 |
| 404 | 路由不存在、目标资源不存在、或端点不支持该 HTTP 方法 |
| 409 | 资源冲突（如 `data.type` 与端点不符、重复、违反约束） |
| 500 | 服务器内部错误（detail 只含错误消息，不含堆栈） |
| 503 | Collection 未加载（如 Anki 尚在 profile 选择界面） |

### 安全

插件默认仅监听 `localhost`（`server.host`），但**默认不要求认证**，且 CORS 默认 `*`——这意味着本机任意进程（含浏览器中的任意网页）都能调用全部接口。如需收紧，编辑插件目录下的 `config.json`：

```json
"security": {
  "api_key": "在这里填一个随机字符串",
  "allowed_origins": ["app://obsidian.md"]
}
```

- `api_key`：非空时启用认证，所有请求（含 `/restart`）必须携带请求头 `Authorization: Bearer <api_key>`，否则返回 `401`。默认为空（不启用，保持向后兼容）
- `allowed_origins`：CORS 白名单。包含 `"*"` 时允许任意来源（默认）；否则仅回显列表内的请求 Origin，其余来源不发 CORS 头

建议：如果把 AnkiRestful 暴露给浏览器端应用使用，务必配置 `api_key` 并收紧 `allowed_origins`。

### 时间戳

除特别说明外，时间字段均为 **Unix 秒级时间戳**。卡片 `due` 字段的含义随卡片状态变化（新卡片为序号，复习卡片为「相对 collection 创建日的天数」），与 Anki 数据库保持一致。
