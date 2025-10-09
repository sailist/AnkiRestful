[English](README-en.md)

# Anki RESTful API

一个为 Anki 提供现代化 RESTful API 的插件，让您能够通过 HTTP 请求轻松访问和操作 Anki 的数据与功能。

## ✨ 核心特性

- 🚀 **标准化 API**：符合 [JSON:API](https://jsonapi.org/) 规范，提供一致的数据结构和接口设计
- 🎯 **数据库对齐**：接口名称、数据结构与 Anki 内部数据库完全对应，降低学习成本
- 🔧 **轻量级设计**：仅依赖 Python 标准库，无需额外第三方依赖
- 🔄 **动态重载**：支持运行时重载 API 路由，提升开发效率
- 📊 **完整日志**：详细的请求日志记录，便于调试和监控

## 🚧 开发计划

见 [TODOs.md](TODOs.md)

## 📦 安装指南

### 手动安装

1. **下载插件**：将整个插件文件夹复制到 Anki 的插件目录中：
   - **Windows**: `%APPDATA%\Anki2\addons21\`
   - **macOS**: `~/Library/Application Support/Anki2/addons21/`
   - **Linux**: `~/.local/share/Anki2/addons21/`

2. **启动服务**：重启 Anki，插件将自动启动 API 服务器

3. **验证安装**：访问 `http://localhost:8102/api/`，如果返回以下响应，说明安装成功：

```json
{
  ...
  "meta": {
    "message": "Anki Restful API",
    "version": "1.0.4",
    "endpoints": {
      "/api": {
        "href": "/api",
        "methods": [
          "GET"
        ]
      },
      "/api/": {
        "href": "/api/",
        "methods": [
          "GET"
        ]
      },
      "/api/notes": {
        "href": "/api/notes",
        "methods": [
          "GET",
          "POST"
        ]
      },
      ...
      "/api/notetypes/byname/{note_type_name}": {
        "href": "/api/notetypes/byname/<note_type_name>",
        "methods": [
          "GET"
        ]
      }
    }
  },
  "jsonapi": {
    "version": "1.0"
  },
  "links": {
    "self": "/api/",
  },
  ...
}
```

### 插件市场安装

> **即将推出**：我们正在努力将插件提交到 Anki 官方插件市场，届时您将能够一键安装。

## 🚀 快速开始

### 获取所有牌组

通过以下 API 调用获取您的所有牌组信息：

```http
GET /api/decks
```

```json
{
  "data": [
    ...
    {
      "type": "decks",
      "id": "1759971095180",
      "attributes": {
        "name": "AnkiRestful-test",
        "description": "",
        "review_count": 0,
        "new_count": 0,
        "learning_count": 0,
        "total_cards": 1,
        "total_notes": 1,
        "deck_config_id": 1,
        "deck_config_name": "Default",
        "created": 1759971095180,
        "modified": 1759971095
      },
      "relationships": {
        "notes": {
          "data": null,
          "links": {
            "self": "/api/decks/1759971095180/relationships/notes",
            "related": "/api/decks/1759971095180/notes",
            "first": null,
            "last": null,
            "prev": null,
            "next": null
          },
          "meta": null
        }
      },
      "links": {
        "self": "/api/decks/1759971095180",
        "related": "/api/decks/1759971095180/notes",
        "first": null,
        "last": null,
        "prev": null,
        "next": null
      },
      "meta": null
    },
    ...
  ],
  "errors": null,
  "meta": {
    "total": ...
  },
  "links": {
    "self": "/api/decks",
    "related": null,
    "first": null,
    "last": null,
    "prev": null,
    "next": null
  },
  "included": null
}
```


## 🛠️ 开发指南

### 环境搭建

```bash
git clone https://github.com/sailist/AnkiRestful
cd AnkiRestful
```

### 开发工具推荐

- **API 测试**：推荐使用 [Bruno](https://www.usebruno.com/) 进行 API 测试，`bruno/` 目录可直接导入
- **动态重载**：修改代码后可通过 `/restart` 接口动态重载所有 API 路由，无需重启 Anki
- **日志查看**：所有请求日志保存在 `logs/` 目录下，便于调试和问题排查

### 项目结构

```
AnkiRestful2/
├── api.py          # 核心 API 实现
├── schema.py       # 数据模型定义
├── config.json     # 配置文件
├── bruno/          # Bruno API 测试集合
├── logs/           # 日志文件
└── examples/       # 使用示例
```


## ⚙️ 配置说明

编辑 `config.json` 文件来自定义插件设置：

```json
{
  "server": {
    "host": "localhost",
    "port": 8102,
    "auto_start": true
  },
  "api": {
    "enable_cors": true,
    "max_connections": 10
  }
}
```

### 配置参数详解

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `server.host` | string | `localhost` | 服务器监听地址 |
| `server.port` | number | `8102` | 服务器端口号 |
| `server.auto_start` | boolean | `true` | Anki 启动时自动启动 API 服务器 |
| `api.enable_cors` | boolean | `true` | 启用跨域资源共享 (CORS) 支持 |
| `api.max_connections` | number | `10` | 最大并发连接数（预留配置） |

## 📋 版本规划

详细的开发计划和功能路线图请查看 [TODOs.md](TODOs.md)。

## 🤝 贡献指南

我们欢迎所有形式的贡献！无论是：

- 🐛 **问题报告**：发现 bug 或有改进建议
- 💡 **功能建议**：提出新功能想法
- 📝 **文档改进**：完善文档内容
- 🔧 **代码贡献**：提交 Pull Request

### 如何贡献

1. Fork 本仓库
2. 创建您的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交您的更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启一个 Pull Request

## 📄 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情。

## 🙏 致谢

感谢所有为这个项目做出贡献的开发者和用户！