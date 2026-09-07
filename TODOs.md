# AnkiRestful 开发计划

## 接口总览

对标 [AnkiConnect](https://git.sr.ht/~foosoft/anki-connect) 的全部 122 个 action，按 8 个类别拆分。
每个接口的 RESTful 设计详见 [docs/](docs/README.md)。

| 类别 | AnkiConnect 接口数 | 当前状态 | 设计文档 | 规划版本 |
|------|-------------------:|----------|----------|----------|
| 笔记 Notes | 21 | 部分（5 个 REST 接口已实现） | [docs/notes.md](docs/notes.md) | v0.2 |
| 牌组 Decks | 13 | 部分（3 个只读接口已实现） | [docs/decks.md](docs/decks.md) | v0.2 |
| 笔记类型 NoteTypes | 26 | 部分（2 个只读接口已实现） | [docs/notetypes.md](docs/notetypes.md) | v0.3 |
| 卡片 Cards | 21 | 未开始（仅笔记下挂了只读列表） | [docs/cards.md](docs/cards.md) | v0.4 |
| 媒体 Media | 5 | 未开始 | [docs/media.md](docs/media.md) | v0.5 |
| 统计 Stats | 3 | 未开始 | [docs/stats.md](docs/stats.md) | v0.5 |
| 系统 System | 11 | 未开始 | [docs/system.md](docs/system.md) | v0.6 |
| GUI | 22 | 暂不规划 | [docs/gui.md](docs/gui.md) | — |

## v0.1 ✅

- [x] handle 支持 do_get/do_post 等方法，不需要在 endpoints 中注册
- [x] 支持创建 note

## v0.2 — 笔记写操作 + 牌组管理

- [ ] `PATCH /api/notes/{id}` 修改笔记字段（updateNoteFields / updateNote）
- [ ] `GET /api/notes?query=` 支持 Anki 搜索语法过滤（findNotes）
- [ ] `POST /api/notes/batch` 批量创建、`POST /api/notes/validate` 创建前预检（addNotes / canAddNotes）
- [ ] 标签管理：`GET/POST/PUT/DELETE /api/notes/{id}/tags`、`GET/DELETE /api/tags`（addTags / removeTags / getTags / getNoteTags / updateNoteTags / replaceTags / clearUnusedTags）
- [ ] `POST /api/decks` 创建牌组（createDeck）
- [ ] `DELETE /api/decks/{id}` 删除牌组（deleteDecks）

## v0.3 — 笔记类型写操作

- [ ] `POST /api/notetypes` 创建笔记类型（createModel）
- [ ] 模板管理：`GET/POST/PUT /api/notetypes/{id}/templates`、`PATCH/DELETE /api/notetypes/{id}/templates/{name}`（modelTemplates / updateModelTemplates / modelTemplateAdd / Remove / Rename / Reposition）
- [ ] 字段管理：`GET/POST /api/notetypes/{id}/fields`、`PATCH/DELETE /api/notetypes/{id}/fields/{name}`（modelFieldNames / Add / Remove / Rename / Reposition / SetFont / SetFontSize / SetDescription）
- [ ] 样式：`GET/PUT /api/notetypes/{id}/styling`（modelStyling / updateModelStyling）
- [ ] `POST /api/notetypes/{id}/actions/find-replace` 模板内查找替换（findAndReplaceInModels）
- [ ] `PATCH /api/notes/{id}/notetype` 更换笔记的笔记类型（updateNoteModel）

## v0.4 — 卡片资源

- [ ] `GET /api/cards`、`GET /api/cards/{id}` 卡片查询（findCards / cardsInfo / cardsToNotes / cardsModTime）
- [ ] 调度动作：`POST /api/cards/actions/{suspend|unsuspend|forget|relearn|answer|move}`（suspend / unsuspend / forgetCards / relearnCards / answerCards / changeDeck）
- [ ] `PATCH /api/cards/{id}/schedule` 修改调度参数（setDueDate / setEaseFactors / setSpecificValueOfCard）
- [ ] `GET /api/cards/{id}/status` 状态查询（suspended / areDue / areSuspended / getIntervals / getEaseFactors）
- [ ] `GET/POST /api/cards/{id}/reviews` 复习历史（cardReviews / getReviewsOfCards / getLatestReviewID / insertReviews）

## v0.5 — 媒体与统计

- [ ] `POST/GET/DELETE /api/media`、`GET /api/media?pattern=`（storeMediaFile / retrieveMediaFile / deleteMediaFile / getMediaFilesNames / getMediaDirPath）
- [ ] `GET /api/stats/reviews/today`、`/api/stats/reviews/by-day`、`/api/stats/collection`、`/api/stats/decks`（getNumCardsReviewedToday / getNumCardsReviewedByDay / getCollectionStatsHTML / getDeckStats）

## v0.6 — 系统与集合

- [ ] `GET /api/system/version`、`GET /api/system/capabilities`（version / apiReflect）
- [ ] `POST /api/system/sync`、`POST /api/system/multi`、`POST /api/system/reload`（sync / multi / reloadCollection）
- [ ] 配置档案：`GET/POST /api/system/profiles`、`GET /api/system/profiles/active`（getProfiles / getActiveProfile / loadProfile）
- [ ] 导入导出：`POST /api/system/export`、`POST /api/system/import`（exportPackage / importPackage）

## v0.7 — 安全操作包（pylib 原生能力，AnkiConnect 未覆盖）

- [ ] 撤销/重做：`GET /api/system/undo-status`、`POST /api/system/actions/undo`、`POST /api/system/actions/redo`（col.undo_status / undo / redo）
- [ ] 集合配置：`GET /api/system/config`、`GET/PUT/DELETE /api/system/config/{key}`（col.all_config / get_config / set_config / remove_config）
- [ ] 全局偏好：`GET/PATCH /api/system/preferences`（col.get_preferences / set_preferences）
- [ ] 备份：`POST /api/system/actions/backup`（col.create_backup）
- [ ] 长任务配套：`GET /api/system/progress`、`POST /api/system/actions/abort`（col.latest_progress / set_wants_abort）
- [ ] 同步增强：`GET /api/system/sync/status`、`GET /api/system/sync/media-status`、`POST /api/system/actions/full-upload`、`POST /api/system/actions/full-download`
- [ ] `POST /api/system/actions/optimize` 数据库优化（col.optimize）
- [ ] `GET /api/system/collection` 集合元信息（crt/mod/note_count/card_count/sched_ver 等）
- [ ] `DELETE /api/notetypes/{id}` 删除笔记类型（models.remove，use_count 预检 + force 参数）

## v0.8 — 能力扩展包（pylib 原生能力）

- [ ] 渲染预览：`GET /api/cards/{id}/render`、`POST /api/notes/actions/render-preview`（Card.render_output / Note.ephemeral_card）
- [ ] 卡片增强：`POST /api/cards/actions/set-flag`、`bury`、`unbury`、`GET /api/cards/{id}/memory-state`、`GET /api/cards/{id}/scheduling-states`、`GET /api/cards/empty-report`、`POST /api/cards/actions/remove-empty`、`POST /api/cards/actions/reposition`
- [ ] 笔记增强：`POST /api/notes/actions/find-replace`（col.find_and_replace）、`GET /api/notes/dupes`、`POST /api/notes/actions/suspend`、`unsuspend`、`bury`
- [ ] 标签增强：`GET /api/tags/tree`、`POST /api/tags/actions/attach`、`detach`、`find-replace`、`reparent`
- [ ] 牌组增强：`PATCH /api/decks/{id}` 重命名/改父级、`GET /api/decks/tree`、筛选牌组（POST /api/decks 扩展 filtered + `actions/rebuild`、`actions/empty`）、`POST /api/decks/{id}/actions/unbury`、`extend-limits`、`custom-study`、`GET /api/deck-configs` 列表
- [ ] 媒体增强：`POST /api/media/actions/check`、`POST /api/media/trash/actions/empty`、`restore`、`POST /api/media/actions/force-resync`
- [ ] 笔记类型增强：`POST /api/notetypes/{id}/actions/copy`、`GET /api/notetypes/stock`、use_count / template_use_count / sort_field 透出
- [ ] 复习与工具：`GET /api/review/queue`、`POST /api/utils/compare-answer`、`POST /api/search/build`
- [ ] 统计结构化：`GET /api/stats/forecast`、`intervals`、`hourly`、`card-types`
- [ ] 导出扩展：`POST /api/system/actions/export` 支持 `format: colpkg | csv`

## v1.0

- [ ] 上传至插件市场，支持一键安装
- [ ] 对齐 AnkiConnect 的全部 API（GUI 类除外）
- [ ] 与 Obsidian 制卡插件集成
- [ ] 完全兼容 AnkiConnect 插件功能

## 暂不规划

- [ ] GUI 类接口（22 个）：依赖 Anki 主窗口状态，与本插件「无头数据 API」的定位冲突。完整清单见 [docs/gui.md](docs/gui.md)，如未来确有需要再单独立项。
