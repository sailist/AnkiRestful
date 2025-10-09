# Anki RESTful API

A modern RESTful API plugin for Anki that allows you to easily access and manipulate Anki's data and functionality through HTTP requests.

## ✨ Core Features

- 🚀 **Standardized API**: Compliant with [JSON:API](https://jsonapi.org/) specification, providing consistent data structures and interface design
- 🎯 **Database Alignment**: Interface names and data structures are fully aligned with Anki's internal database, reducing learning curve
- 🔧 **Lightweight Design**: Only depends on Python standard library, no additional third-party dependencies required
- 🔄 **Dynamic Reload**: Supports runtime reloading of API routes, improving development efficiency
- 📊 **Complete Logging**: Detailed request logging for easy debugging and monitoring

## 🚧 Development Roadmap

See [TODOs.md](TODOs.md)

## 📦 Installation Guide

### Manual Installation

1. **Download Plugin**: Copy the entire plugin folder to Anki's addon directory:
   - **Windows**: `%APPDATA%\Anki2\addons21\`
   - **macOS**: `~/Library/Application Support/Anki2/addons21/`
   - **Linux**: `~/.local/share/Anki2/addons21/`

2. **Start Service**: Restart Anki, and the plugin will automatically start the API server

3. **Verify Installation**: Visit `http://localhost:8102/api/`. If you see the following response, the installation is successful:

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

### Add-on Store Installation

> **Coming Soon**: We are working on submitting the plugin to the official Anki add-on store for one-click installation.

## 🚀 Quick Start

### Get All Decks

Retrieve all your deck information with the following API call:

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


## 🛠️ Development Guide

### Environment Setup

```bash
git clone https://github.com/sailist/AnkiRestful
cd AnkiRestful
```

### Recommended Development Tools

- **API Testing**: We recommend using [Bruno](https://www.usebruno.com/) for API testing. The `bruno/` directory can be imported directly
- **Dynamic Reload**: After modifying code, you can dynamically reload all API routes via the `/restart` endpoint without restarting Anki
- **Log Viewing**: All request logs are saved in the `logs/` directory for easy debugging and troubleshooting

### Project Structure

```
AnkiRestful2/
├── api.py          # Core API implementation
├── schema.py       # Data model definitions
├── config.json     # Configuration file
├── bruno/          # Bruno API test collection
├── logs/           # Log files
└── examples/       # Usage examples
```


## ⚙️ Configuration

Edit the `config.json` file to customize plugin settings:

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

### Configuration Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `server.host` | string | `localhost` | Server listening address |
| `server.port` | number | `8102` | Server port number |
| `server.auto_start` | boolean | `true` | Auto-start API server when Anki launches |
| `api.enable_cors` | boolean | `true` | Enable Cross-Origin Resource Sharing (CORS) support |
| `api.max_connections` | number | `10` | Maximum concurrent connections (reserved configuration) |

## 📋 Version Roadmap

For detailed development plans and feature roadmap, please see [TODOs.md](TODOs.md).

## 🤝 Contributing

We welcome all forms of contributions! Whether it's:

- 🐛 **Bug Reports**: Found a bug or have improvement suggestions
- 💡 **Feature Suggestions**: Propose new feature ideas
- 📝 **Documentation Improvements**: Enhance documentation content
- 🔧 **Code Contributions**: Submit Pull Requests

### How to Contribute

1. Fork this repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

Thanks to all developers and users who have contributed to this project!
