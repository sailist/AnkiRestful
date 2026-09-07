# 系统（System）API 设计

规划版本：**v0.6**
AnkiConnect 对照：共 11 个 action。

系统类接口操作的是集合（Collection）、配置档案（Profile）与插件本身，不属于任何业务资源，统一挂在 `/api/system/` 下。

## 接口一览

| 方法 | 路径 | 说明 | AnkiConnect 对照 |
|------|------|------|------------------|
| GET | `/api/system/version` | 插件与 Anki 版本 | `version` |
| GET | `/api/system/capabilities` | 已注册接口清单 | `apiReflect` |
| POST | `/api/system/actions/sync` | 触发同步 | `sync` |
| POST | `/api/system/actions/multi` | 批量调用 | `multi` |
| POST | `/api/system/actions/reload` | 重载集合 | `reloadCollection` |
| GET | `/api/system/profiles` | 配置档案列表 | `getProfiles` |
| GET | `/api/system/profiles/active` | 当前配置档案 | `getActiveProfile` |
| POST | `/api/system/profiles/active` | 切换配置档案 | `loadProfile` |
| POST | `/api/system/actions/export` | 导出 .apkg | `exportPackage` |
| POST | `/api/system/actions/import` | 导入 .apkg | `importPackage` |
| — | — | 权限申请 | `requestPermission`（不适用，见下文） |

> 已有的 `GET /restart`（热重载 API 路由）保持现状，属于系统类能力，后续可迁移为 `POST /api/system/actions/restart` 并保留旧路径兼容。

## 接口设计

### `GET /api/system/version`

```json
{
  "data": {
    "type": "system",
    "id": "version",
    "attributes": { "plugin_version": "1.0.4", "anki_version": "25.09" }
  }
}
```

### `GET /api/system/capabilities`

返回当前已注册的全部路由及方法（即 `GET /api/` 的 `meta.endpoints`，此处同时给出对应的 AnkiConnect action 名，便于兼容性自查）：

```json
{
  "data": {
    "type": "capabilities",
    "attributes": {
      "routes": { "/api/notes": ["GET", "POST"], "...": [] },
      "ankiconnect_equivalents": ["addNote", "findNotes", "..."]
    }
  }
}
```

### `POST /api/system/actions/sync`

触发 AnkiWeb 同步。无请求体；同步完成后响应 `200`，失败时返回 `502` 与错误详情。同步期间其他请求可能阻塞，客户端应设置较长超时。

### `POST /api/system/actions/multi`

一次请求内顺序执行多个接口调用（对应 `multi`）：

```json
{
  "requests": [
    { "method": "GET", "path": "/api/decks" },
    { "method": "POST", "path": "/api/notes", "body": { "data": { "...": "..." } } }
  ]
}
```

响应为数组，与请求一一对应：

```json
{
  "data": [
    { "status": 200, "body": { "...": "..." } },
    { "status": 201, "body": { "...": "..." } }
  ]
}
```

任一请求失败不影响其他请求执行（与 AnkiConnect `multi` 一致，不做事务回滚）。

### `POST /api/system/actions/reload`

丢弃内存中的集合状态并从磁盘重新加载（对应 `reloadCollection`）。无请求体，响应 `200`。

### 配置档案（Profiles）

**`GET /api/system/profiles`**：

```json
{ "data": ["User 1", "测试账户"], "meta": { "total": 2 } }
```

**`GET /api/system/profiles/active`**：

```json
{ "data": { "type": "profiles", "id": "User 1", "attributes": { "name": "User 1" } } }
```

**`POST /api/system/profiles/active`** — 切换配置档案：

```json
{ "data": { "type": "profiles", "attributes": { "name": "测试账户" } } }
```

切换后集合会重新加载，响应 `200` 返回新的活动档案；名称不存在时返回 `404`。

### 导入导出

**`POST /api/system/actions/export`** — 导出牌组为 .apkg：

```json
{
  "deck": "CS",
  "path": "/tmp/CS.apkg",
  "include_sched": true
}
```

响应 `200`，`meta.path` 为生成的文件路径。

**`POST /api/system/actions/import`** — 导入 .apkg：

```json
{ "path": "/tmp/CS.apkg" }
```

响应 `200` 表示导入完成。导入会修改集合，建议先调用 `/api/system/actions/sync` 或手动备份。

### 关于 `requestPermission`

AnkiConnect 的 `requestPermission` 用于向用户弹窗申请 API 访问权限（基于调用方 origin 的白名单机制）。本插件当前设计为仅监听 localhost、面向本地可信调用方，**不实现该接口**；如未来引入 API Key 或 origin 白名单机制，再补充对应端点。


---

# 附：系统扩展接口（v0.7，pylib 原生能力）

以下接口来自 Anki pylib 自身能力（非 AnkiConnect 对齐项），全部为纯后端调用、无 GUI 依赖。

## 接口一览

| 方法 | 路径 | 说明 | pylib 方法 |
|------|------|------|-----------|
| GET | `/api/system/undo-status` | 撤销/重做可用性与操作名 | `col.undo_status()` |
| POST | `/api/system/actions/undo` | 撤销 | `col.undo()` |
| POST | `/api/system/actions/redo` | 重做 | `col.redo()` |
| GET | `/api/system/config` | 全部集合配置 | `col.all_config()` |
| GET | `/api/system/config/{key}` | 读取单个配置键 | `col.get_config(key)` |
| PUT | `/api/system/config/{key}` | 写入配置键（任意 JSON） | `col.set_config(key, val)` |
| DELETE | `/api/system/config/{key}` | 删除配置键 | `col.remove_config(key)` |
| GET | `/api/system/preferences` | 全局偏好（proto 转 JSON） | `col.get_preferences()` |
| PATCH | `/api/system/preferences` | 合并式更新偏好 | `col.set_preferences(prefs)` |
| POST | `/api/system/actions/backup` | 立即创建备份 | `col.create_backup(force, wait_for_completion)` |
| GET | `/api/system/progress` | 后端长任务进度 | `col.latest_progress()` |
| POST | `/api/system/actions/abort` | 中止当前后端操作 | `col.set_wants_abort()` |
| GET | `/api/system/sync/status` | 同步状态（是否需要同步/全量） | `col.sync_status(auth)` |
| GET | `/api/system/sync/media-status` | 媒体同步状态 | `col.media_sync_status()` |
| POST | `/api/system/actions/full-upload` | 全量上传（危险，需 confirm） | `col.full_upload_or_download(auth, usn, True)` |
| POST | `/api/system/actions/full-download` | 全量下载（危险，需 confirm） | `col.full_upload_or_download(auth, usn, False)` |
| POST | `/api/system/actions/optimize` | 数据库 vacuum+analyze | `col.optimize()` |
| GET | `/api/system/collection` | 集合元信息 | crt/mod/note_count/card_count/sched_ver/v3_scheduler/is_empty |

## 设计要点

### 撤销/重做

`GET /api/system/undo-status` 响应：

```json
{
  "data": {
    "type": "undo-status",
    "attributes": {
      "undo_available": true, "redo_available": false,
      "undo_label": "添加笔记", "redo_label": ""
    }
  }
}
```

`POST .../undo|redo` 无可撤销/重做内容时返回 `409`；成功后返回新的 undo-status。

### 配置与偏好

- `PUT /api/system/config/{key}` 请求体为任意 JSON 值（原样写入）；`GET` 键不存在返回 `404`
- `PATCH /api/system/preferences` 只合并请求中出现的字段；proto 与 JSON 的转换注意枚举与嵌套结构

### 备份与长任务

- `POST .../backup` body `{ "force": true, "wait": true }`，响应 `meta.path` 为备份文件路径；`wait=false` 时立即返回，客户端用 `GET /api/system/progress` 轮询
- `GET /api/system/progress` 无任务进行中时返回 `data: null`

### 同步增强

- `sync/status`、`media-status`、`full-upload`、`full-download` 需要同步凭据，用 `mw.pm.sync_auth()` 获取；未登录 AnkiWeb 时返回 `401`
- `full-upload` / `full-download` 必须带 body `{ "confirm": true }`，否则返回 `400`；执行后集合需要 reload（实现内自动调用）

### 集合元信息

`GET /api/system/collection` 响应：

```json
{
  "data": {
    "type": "collection",
    "attributes": {
      "created": 1725600000, "modified": 1725700000,
      "note_count": 120, "card_count": 150,
      "scheduler_version": 3, "v3_scheduler": true, "is_empty": false
    }
  }
}
```
