# GUI 类接口（暂不规划）

AnkiConnect 对照：共 22 个 action。

## 为什么不规划

这类接口操作的是 Anki 的 Qt 主窗口（打开浏览器、切换界面、驱动复习流程、播放音频等），共同特点是：

1. **依赖 GUI 状态**：窗口不存在、被最小化、或用户正在交互时行为不确定；
2. **与本插件定位冲突**：AnkiRestful 的定位是「无头数据 API」——让外部工具（脚本、Obsidian 插件、CI）程序化地读写 Anki 数据，而不是远程遥控 Anki 界面；
3. **可替代**：大部分 GUI 动作都有对应的数据层接口（如「添加卡片」用 `POST /api/notes`，「查找笔记」用 `GET /api/notes?query=`）。

如未来确有远程驱动复习流程等需求，再单独立项评估。

## 完整清单

| AnkiConnect action | 作用 | 数据层替代方案 |
|--------------------|------|----------------|
| `guiBrowse` | 打开卡片浏览器并搜索 | `GET /api/notes?query=` |
| `guiSelectedNotes` | 浏览器中选中的笔记 | — |
| `guiAddCards` | 打开「添加」窗口 | `POST /api/notes` |
| `guiAddNoteSetData` | 向「添加」窗口预填内容 | `POST /api/notes` |
| `guiEditNote` | 打开笔记编辑窗口 | `PATCH /api/notes/{id}` |
| `guiCurrentCard` | 复习中的当前卡片 | `GET /api/cards?query=is:due` |
| `guiStartCardTimer` | 启动卡片计时器 | — |
| `guiShowQuestion` | 显示问题面 | — |
| `guiShowAnswer` | 显示答案面 | — |
| `guiAnswerCard` | 对当前卡片作答 | `POST /api/cards/actions/answer` |
| `guiUndo` | 撤销 | — |
| `guiDeckOverview` | 打开牌组概览页 | `GET /api/decks/{id}` |
| `guiDeckBrowser` | 打开牌组列表页 | `GET /api/decks` |
| `guiDeckReview` | 开始复习指定牌组 | — |
| `guiReviewActive` | 是否处于复习界面 | — |
| `guiCheckDatabase` | 检查数据库 | — |
| `guiExitAnki` | 退出 Anki | — |
| `guiSelectCard` | 浏览器中选中卡片 | — |
| `guiSelectNote` | 浏览器中选中笔记 | — |
| `guiImportFile` | 弹出导入文件窗口 | `POST /api/system/actions/import` |
| `guiPlayAudio` | 播放笔记中的音频 | `GET /api/media/{filename}` |
| `openNewWindow` | 打开新窗口 | — |

> 注：`guiCheckDatabase`（数据库检查）是唯一一个没有数据层替代、且对自动化有实际价值的接口，可考虑在 v0.6 以 `POST /api/system/actions/check-database` 形式提供——它不依赖窗口显示，只是在 GUI 线程执行检查。
