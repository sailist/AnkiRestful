# 媒体（Media）API 设计

规划版本：**v0.5**
AnkiConnect 对照：共 5 个 action。

媒体文件存放于 collection 的 `collection.media` 目录，通过文件名引用（笔记字段中的 `<img src="...">`、`<audio src="...">` 等）。

## 接口一览

| 方法 | 路径 | 说明 | AnkiConnect 对照 |
|------|------|------|------------------|
| POST | `/api/media` | 上传媒体文件 | `storeMediaFile` |
| GET | `/api/media/{filename}` | 下载媒体文件 | `retrieveMediaFile` |
| DELETE | `/api/media/{filename}` | 删除媒体文件 | `deleteMediaFile` |
| GET | `/api/media` | 列出媒体文件 | `getMediaFilesNames` |
| GET | `/api/media/directory` | 媒体目录路径 | `getMediaDirPath` |

## 接口设计

### `POST /api/media` — 上传

支持两种提交方式。

**方式一：JSON + Base64**（与 AnkiConnect 对齐，适合小文件与程序化调用）：

```json
{
  "data": {
    "type": "media",
    "attributes": {
      "filename": "word-audio.mp3",
      "data_base64": "UklGRg..."
    }
  }
}
```

也可以提供 `url`（服务端下载后写入）或 `path`（本地文件路径，跳过 Base64 传输）替代 `data_base64`，三选一。

**方式二：multipart/form-data**（适合大文件）：

```
POST /api/media
Content-Type: multipart/form-data; boundary=----

------
Content-Disposition: form-data; name="file"; filename="photo.png"
Content-Type: image/png

<二进制内容>
------
```

- 同名文件已存在时默认覆盖；加查询参数 `overwrite=false` 时返回 `409`
- 文件名会做安全清理（去除路径分隔符等），与 Anki 行为一致
- 响应 `201`：

```json
{
  "data": { "type": "media", "id": "word-audio.mp3", "attributes": { "filename": "word-audio.mp3", "size": 20480 } },
  "links": { "self": "/api/media/word-audio.mp3" }
}
```

### `GET /api/media/{filename}` — 下载

- 默认返回文件二进制（`Content-Type` 按扩展名推断），可直接用于 `<img>` / `<audio>` 标签
- 加查询参数 `format=base64` 时返回 JSON:API 文档（`attributes.data_base64`），与 AnkiConnect 行为对齐
- 文件不存在返回 `404`

### `DELETE /api/media/{filename}`

删除文件，响应 `200`，`meta.deleted` 为文件名。

> 注意：不检查文件是否仍被笔记引用（与 AnkiConnect 一致）。引用检查属于「检查媒体」功能，不在本期范围。

### `GET /api/media` — 列表

| 查询参数 | 说明 |
|----------|------|
| `pattern` | glob 模式过滤，如 `*.mp3`（对应 `getMediaFilesNames`） |
| `page` / `limit` | 分页 |

```json
{ "data": ["photo.png", "word-audio.mp3"], "meta": { "total": 2 } }
```

### `GET /api/media/directory`

返回媒体目录的绝对路径：

```json
{ "data": { "type": "media-directory", "attributes": { "path": "/Users/xxx/Library/Application Support/Anki2/User 1/collection.media" } } }
```


---

# 附：媒体扩展接口（v0.8，pylib 原生能力）

## 接口一览

| 方法 | 路径 | 说明 | pylib 方法 |
|------|------|------|-----------|
| POST | `/api/media/actions/check` | 检查媒体（未引用/缺失报告） | `media.check()` |
| POST | `/api/media/trash/actions/empty` | 清空回收站 | `media.empty_trash()` |
| POST | `/api/media/trash/actions/restore` | 从回收站还原 | `media.restore_trash()` |
| POST | `/api/media/actions/force-resync` | 强制下次全量媒体同步 | `media.force_resync()` |

## 设计要点

### `POST /api/media/actions/check` — 检查媒体

耗时长，客户端应设置长超时（或配合 `GET /api/system/progress` 轮询）。响应：

```json
{
  "data": {
    "type": "media-check",
    "attributes": {
      "unused": ["old.png"],
      "missing": ["lost.mp3"],
      "missing_media_notes": [1514547547030],
      "report": "共 120 个文件，1 个未使用，1 个缺失",
      "have_trash": true
    }
  }
}
```

### 回收站

注意既有 `DELETE /api/media/{filename}` 是移入回收站（`trash_files`），与 `empty`（彻底清空）/`restore`（还原）形成闭环。两个动作均无请求体，响应 `meta` 给出处理文件数。

### `POST /api/media/actions/force-resync`

删除媒体同步索引，下次 sync 时全量重传（用于媒体同步损坏后的修复）。无请求体，响应 `200`。
