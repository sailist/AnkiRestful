"""
API处理模块
包含所有API端点的处理方法
按照 JSON:API 规范实现
"""

import json
from dataclasses import asdict
from aqt import mw
import schema
import logging
import sys
from anki.notes import Note
from anki.errors import NotFoundError

api_version = "1.1.0"

# 全局路由注册表
API_ROUTES = {}

logger = logging.getLogger()
logger.info("API module loaded")


def deck_query(name):
    """构造牌组搜索串（双引号包裹，支持含空格/特殊字符的牌组名）"""
    return 'deck:"' + name.replace('"', '\\"') + '"'


def api_route(path):
    """
    API路由装饰器，用于自动注册API处理器

    Args:
        path (str): API路径，可以包含参数，如 "/api/notes/<note_id>"

    Returns:
        装饰器函数
    """

    def decorator(handler_class):
        # 注册路由
        API_ROUTES[path] = handler_class
        return handler_class

    return decorator


class APIHandler:
    """API处理器基类"""

    def __init__(self, req_handler, method):
        self.req_handler = req_handler
        self.method = method
        self.send_json_response = self.req_handler.send_json_response
        self.send_error_response = self.req_handler.send_error_response


@api_route("/api/")
@api_route("/api")
class RootAPIHandler(APIHandler):
    """根路径处理器 - /api/"""

    def do_GET(self):
        # 创建链接集合
        links = schema.Links(self="/api/", next=None)

        # 添加所有端点链接
        endpoints = {}
        for route_path, handler_class in API_ROUTES.items():
            methods = [
                i
                for i in ["GET", "POST", "PUT", "PATCH", "DELETE"]
                if getattr(handler_class, f"do_{i}", None)
            ]
            endpoints[route_path.replace("<", "{").replace(">", "}")] = {
                "href": route_path,
                "methods": methods,
            }

        # 创建根文档
        document = schema.RootDocument(
            meta={
                "message": "Anki Restful API",
                "version": api_version,
                "endpoints": endpoints,
            },
            links=links,
        )

        self.send_json_response(200, document)


@api_route("/api/notes")
class NotesAPIHandler(APIHandler):
    """笔记列表处理器 - /api/notes"""

    def do_POST(self):
        """
        body:
        {
            "data": {
                "type": "notes",
                "deck_id": 123,
                "attributes": {
                    "note_type": "Basic",
                    "fields": { "Front": "Q", "Back": "A" },
                    "tags": ["tag1", "tag2"],
                }
            }
        }
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        # 读取并解析请求体
        content_length = int(self.req_handler.headers.get("Content-Length", 0))
        if content_length <= 0:
            self.req_handler.send_error_response(
                400, "Bad Request", "Empty request body"
            )
            return

        raw_body = self.req_handler.rfile.read(content_length).decode("utf-8")
        try:
            body = json.loads(raw_body)
        except Exception:
            self.req_handler.send_error_response(
                400, "Bad Request", "Invalid JSON body"
            )
            return

        # 校验 JSON:API 结构
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'data' object is required"
            )
            return

        if data.get("type") != "notes":
            self.req_handler.send_error_response(
                409, "Conflict", "Resource type must be 'notes'"
            )
            return

        # 读取必要参数
        deck_id = data.get("deck_id")
        attrs = data.get("attributes", {})
        note_type_name = attrs.get("note_type")
        fields_map = attrs.get("fields", {})
        tags = attrs.get("tags", [])

        if deck_id is None:
            self.req_handler.send_error_response(
                400, "Bad Request", "'deck_id' is required"
            )
            return

        # 校验牌组（default=False：不存在的 id 返回 None 而非默认牌组）
        deck = mw.col.decks.get(deck_id, default=False)
        if not deck:
            self.req_handler.send_error_response(
                404, "Not Found", f"Deck with ID {deck_id} not found"
            )
            return

        logger.info(f"Deck: {deck}")

        # 校验并获取笔记类型
        if not note_type_name:
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes.note_type' is required"
            )
            return

        logger.info(f"Note type name: {note_type_name}")

        model = mw.col.models.byName(note_type_name)

        logger.info(f"Model: {model}")
        if not model:
            self.req_handler.send_error_response(
                404, "Not Found", f"Note type '{note_type_name}' not found"
            )
            return

        # 创建新笔记并填充字段
        note = Note(mw.col, model)
        # (model)
        logger.info(f"Note.mid: {note.mid}")

        # 设置字段（按字段名赋值，忽略多余字段）
        if isinstance(fields_map, dict):
            for field_name, field_value in fields_map.items():
                try:
                    note[field_name] = (
                        str(field_value) if field_value is not None else ""
                    )
                except Exception:
                    # 忽略不存在的字段名
                    pass

        # 设置标签
        if isinstance(tags, list):
            note.tags = [str(t) for t in tags]

        # 写入集合并创建卡片
        mw.col.add_note(note, deck_id)
        new_note_id = note.id

        # 构建返回资源
        note_type = mw.col.models.get(note.mid)
        note_data = {
            "guid": note.guid,
            "note_type": note_type["name"] if note_type else note_type_name,
            "tags": note.tags,
            "fields": {},
            "created": note.mod,
            "modified": note.mod,
        }

        # 回填已设置的字段
        for f in note_type["flds"] if note_type else []:
            fname = f.get("name")
            if fname is not None:
                try:
                    note_data["fields"][fname] = note[fname]
                except Exception:
                    pass

        resource = schema.create_note_resource(new_note_id, note_data)
        document = schema.NoteDocument(
            data=resource, links=schema.Links(self=f"/api/notes/{new_note_id}")
        )

        self.send_json_response(201, document)

    def do_GET(self, **params):
        """处理获取笔记列表请求"""
        try:
            if not mw.col:
                self.req_handler.send_error_response(
                    503, "Service Unavailable", "Collection not available"
                )
                return

            # 获取查询参数
            from urllib.parse import urlparse, parse_qs

            query_params = parse_qs(urlparse(self.req_handler.path).query)

            # 分页参数（page/limit 必须为 >=1 的整数）
            try:
                page = int(query_params.get("page", [1])[0])
                limit = int(query_params.get("limit", [20])[0])
                if page < 1 or limit < 1:
                    raise ValueError
            except ValueError:
                self.req_handler.send_error_response(
                    400, "Bad Request", "Invalid pagination parameters"
                )
                return
            offset = (page - 1) * limit

            # 获取所有笔记ID
            note_ids = list(mw.col.find_notes(""))
            total_notes = len(note_ids)

            # 分页获取笔记
            paginated_ids = note_ids[offset : offset + limit]
            resources = []

            for note_id in paginated_ids:
                note = mw.col.get_note(note_id)
                note_type = mw.col.models.get(note.mid)

                # 构建笔记数据
                note_data = {
                    "guid": note.guid,
                    "note_type": note_type["name"] if note_type else "Unknown",
                    "tags": note.tags,
                    "fields": {},
                    "created": note.mod,
                    "modified": note.mod,
                }

                # 添加字段数据
                for i, field_name in enumerate(
                    [field["name"] for field in note_type["flds"]] if note_type else []
                ):
                    if i < len(note.fields):
                        note_data["fields"][field_name] = note.fields[i]

                # 创建资源对象
                resource = schema.create_note_resource(note_id, note_data)
                resources.append(resource)

            # 创建分页链接（pages 至少为 1，避免空结果时 last 生成 page=0 的非法链接）
            pages = max(1, (total_notes + limit - 1) // limit)
            base_url = "/api/notes"
            links = schema.Links(
                self=f"{base_url}?page={page}&limit={limit}",
                first=f"{base_url}?page=1&limit={limit}",
                last=f"{base_url}?page={pages}&limit={limit}",
            )

            if page > 1:
                links.prev = f"{base_url}?page={page-1}&limit={limit}"

            if page < pages:
                links.next = f"{base_url}?page={page+1}&limit={limit}"

            # 创建文档
            document = schema.NoteCollectionDocument(
                data=resources,
                links=links,
                meta={
                    "pagination": {
                        "page": page,
                        "limit": limit,
                        "total": total_notes,
                        "pages": pages,
                    }
                },
            )

            self.send_json_response(200, document)

        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error getting notes: {str(e)}"
            )


@api_route("/api/notes/<note_id>")
class NoteDetailAPIHandler(APIHandler):
    """单个笔记详情处理器 - /api/notes/{note_id}"""

    def do_DELETE(self, note_id):
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return
        
        note = mw.col.get_note(int(note_id))
        if not note:
            self.req_handler.send_error_response(
                404, "Not Found", f"Note with ID {note_id} not found"
            )
            return

        mw.col.remove_notes([note.id])

        note_type = mw.col.models.get(note.mid)

        note_data = {
            "guid": note.guid,
            "note_type": note_type["name"] if note_type else "Unknown",
            "tags": note.tags,
            "fields": {},
            "created": note.mod,
            "modified": note.mod,
        }

        # 添加字段数据
        for i, field_name in enumerate(
            [field["name"] for field in note_type["flds"]] if note_type else []
        ):
            if i < len(note.fields):
                note_data["fields"][field_name] = note.fields[i]

        # 创建资源对象
        resource = schema.create_note_resource(int(note_id), note_data)

        # 添加关系
        resource.relationships = {
            "cards": schema.Relationship(
                links=schema.Links(
                    self=f"/api/notes/{note_id}/relationships/cards",
                    related=f"/api/notes/{note_id}/cards",
                )
            )
        }

        # 创建文档
        document = schema.NoteDocument(
            data=resource, links=schema.Links(self=f"/api/notes/{note_id}")
        )

        self.send_json_response(200, document)

    def do_GET(self, note_id):
        """处理获取单个笔记详情请求"""
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        note = mw.col.get_note(int(note_id))
        if not note:
            self.req_handler.send_error_response(
                404, "Not Found", f"Note with ID {note_id} not found"
            )
            return

        note_type = mw.col.models.get(note.mid)

        # 构建笔记数据
        note_data = {
            "guid": note.guid,
            "note_type": note_type["name"] if note_type else "Unknown",
            "tags": note.tags,
            "fields": {},
            "created": note.mod,
            "modified": note.mod,
        }

        # 添加字段数据
        for i, field_name in enumerate(
            [field["name"] for field in note_type["flds"]] if note_type else []
        ):
            if i < len(note.fields):
                note_data["fields"][field_name] = note.fields[i]

        # 创建资源对象
        resource = schema.create_note_resource(int(note_id), note_data)

        # 添加关系
        resource.relationships = {
            "cards": schema.Relationship(
                links=schema.Links(
                    self=f"/api/notes/{note_id}/relationships/cards",
                    related=f"/api/notes/{note_id}/cards",
                )
            )
        }

        # 创建文档
        document = schema.NoteDocument(
            data=resource, links=schema.Links(self=f"/api/notes/{note_id}")
        )

        self.send_json_response(200, document)


@api_route("/api/notes/<note_id>/cards")
class NoteCardsAPIHandler(APIHandler):
    """笔记卡片处理器 - /api/notes/{note_id}/cards"""

    def do_GET(self, note_id):
        """处理获取笔记卡片请求"""
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        # get_note 对不存在的 id 抛 NotFoundError，而非返回 None
        try:
            note = mw.col.get_note(int(note_id))
        except NotFoundError:
            self.req_handler.send_error_response(
                404, "Not Found", f"Note with ID {note_id} not found"
            )
            return

        # 获取该笔记的所有卡片
        card_ids = mw.col.find_cards(f"nid:{note_id}")
        resources = []

        for card_id in card_ids:
            card = mw.col.get_card(card_id)
            deck = mw.col.decks.get(card.did)

            card_data = {
                "note_id": card.nid,
                "deck_id": card.did,
                "deck_name": deck["name"] if deck else "Unknown",
                "template_index": card.ord,
                "type": card.type,
                "queue": card.queue,
                "interval": card.ivl,
                "ease_factor": card.factor,
                "reviews": card.reps,
                "lapses": card.lapses,
                "due": card.due,
            }

            resource = schema.create_card_resource(card_id, card_data)
            resources.append(resource)

        # 创建文档
        document = schema.CardCollectionDocument(
            data=resources,
            links=schema.Links(self=f"/api/notes/{note_id}/cards"),
            meta={"note_id": int(note_id), "count": len(resources)},
        )

        self.send_json_response(200, document)


@api_route("/api/decks")
class DecksAPIHandler(APIHandler):
    """牌组列表处理器 - /api/decks"""

    def do_GET(self, **params):
        """处理获取牌组列表请求"""
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        resources = []
        for deck_id in mw.col.decks.all_ids():
            deck = mw.col.decks.get(deck_id)

            # 获取牌组中的卡片数量
            card_ids = mw.col.find_cards(deck_query(deck["name"]))

            # 获取牌组中的笔记数量
            note_ids = mw.col.find_notes(deck_query(deck["name"]))

            # 获取牌组配置名称
            deck_config_name = ""
            if deck.get("conf"):
                deck_config = mw.col.decks.get_config(deck["conf"])
                if deck_config:
                    deck_config_name = deck_config.get("name", "")

            deck_data = {
                "name": deck["name"],
                "description": deck.get("desc", ""),
                "review_count": deck.get("rev", 0),
                "new_count": deck.get("new", 0),
                "learning_count": deck.get("lrn", 0),
                "total_cards": len(card_ids),
                "total_notes": len(note_ids),
                "deck_config_id": deck.get("conf", 0),
                "deck_config_name": deck_config_name,
                "created": deck.get("id", 0),
                "modified": deck.get("mod", 0),
            }

            resource = schema.create_deck_resource(deck_id, deck_data)
            resources.append(resource)

        # 创建文档
        document = schema.DeckCollectionDocument(
            data=resources,
            links=schema.Links(self="/api/decks"),
            meta={"total": len(resources)},
        )

        self.send_json_response(200, document)


@api_route("/api/decks/<deck_id>")
class DeckDetailAPIHandler(APIHandler):
    """单个牌组详情处理器 - /api/decks/{deck_id}"""

    def do_GET(self, deck_id):
        """处理获取单个牌组详情请求"""
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        deck = mw.col.decks.get(int(deck_id))
        if not deck:
            self.req_handler.send_error_response(
                404, "Not Found", f"Deck with ID {deck_id} not found"
            )
            return

        # 获取牌组中的卡片数量
        card_ids = mw.col.find_cards(deck_query(deck["name"]))

        # 获取牌组中的笔记数量
        note_ids = mw.col.find_notes(deck_query(deck["name"]))

        # 获取牌组配置名称
        deck_config_name = ""
        if deck.get("conf"):
            deck_config = mw.col.decks.get_config(deck["conf"])
            if deck_config:
                deck_config_name = deck_config.get("name", "")

        # 构建牌组数据
        deck_data = {
            "name": deck["name"],
            "description": deck.get("desc", ""),
            "review_count": deck.get("rev", 0),
            "new_count": deck.get("new", 0),
            "learning_count": deck.get("lrn", 0),
            "total_cards": len(card_ids),
            "total_notes": len(note_ids),
            "deck_config_id": deck.get("conf", 0),
            "deck_config_name": deck_config_name,
            "created": deck.get("id", 0),
            "modified": deck.get("mod", 0),
        }

        resource = schema.create_deck_resource(int(deck_id), deck_data)

        # 创建文档
        document = schema.DeckDocument(
            data=resource, links=schema.Links(self=f"/api/decks/{deck_id}")
        )

        self.send_json_response(200, document)


@api_route("/api/decks/<deck_id>/notes")
class DeckNotesAPIHandler(APIHandler):
    """牌组笔记处理器 - /api/decks/{deck_id}/notes"""

    def do_GET(self, deck_id):
        """处理获取牌组笔记请求"""
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        # default=False：不存在的 id 返回 None 而非默认牌组
        deck = mw.col.decks.get(int(deck_id), default=False)
        if not deck:
            self.req_handler.send_error_response(
                404, "Not Found", f"Deck with ID {deck_id} not found"
            )
            return

        # 获取该牌组中的所有笔记ID
        note_ids = mw.col.find_notes(deck_query(deck["name"]))
        resources = []

        for note_id in note_ids:
            note = mw.col.get_note(note_id)
            note_type = mw.col.models.get(note.mid)

            # 构建笔记数据
            note_data = {
                "guid": note.guid,
                "note_type": note_type["name"] if note_type else "Unknown",
                "tags": note.tags,
                "fields": {},
                "created": note.mod,
                "modified": note.mod,
            }

            # 添加字段数据
            for i, field_name in enumerate(
                [field["name"] for field in note_type["flds"]] if note_type else []
            ):
                if i < len(note.fields):
                    note_data["fields"][field_name] = note.fields[i]

            resource = schema.create_note_resource(note_id, note_data)
            resources.append(resource)

        # 创建文档
        document = schema.NoteCollectionDocument(
            data=resources,
            links=schema.Links(
                self=f"/api/decks/{deck_id}/notes", related=f"/api/decks/{deck_id}"
            ),
            meta={
                "deck_id": int(deck_id),
                "deck_name": deck["name"],
                "count": len(resources),
            },
        )

        self.send_json_response(200, document)


@api_route("/api/notetypes")
class NoteTypesAPIHandler(APIHandler):
    """笔记类型处理器 - /api/notetypes"""

    def do_GET(self, **params):
        """处理获取笔记类型列表请求"""
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        resources = []
        for model in mw.col.models.all():
            note_type_data = {
                "name": model["name"],
                "fields": [field["name"] for field in model["flds"]],
                "templates": [template["name"] for template in model["tmpls"]],
            }

            resource = schema.create_note_type_resource(model["id"], note_type_data)
            resources.append(resource)

        # 创建文档
        document = schema.NoteTypeCollectionDocument(
            data=resources,
            links=schema.Links(self="/api/notetypes"),
            meta={"total": len(resources)},
        )

        self.send_json_response(200, document)


@api_route("/api/notetypes/byname/<note_type_name>")
class NoteTypeByNameAPIHandler(APIHandler):
    """笔记类型处理器 - /api/notetypes/byname/{note_type_name}"""

    def do_GET(self, note_type_name):
        """处理获取笔记类型请求"""
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        # 处理可能的 URL 编码
        try:
            from urllib.parse import unquote

            decoded_name = unquote(note_type_name)
        except Exception:
            decoded_name = note_type_name

        model = mw.col.models.byName(decoded_name)
        if not model:
            self.send_error_response(
                404, "Not Found", f"Note type '{decoded_name}' not found"
            )
            return

        note_type_data = {
            "name": model["name"],
            "fields": [field["name"] for field in model["flds"]],
            "templates": [template["name"] for template in model["tmpls"]],
        }

        resource = schema.create_note_type_resource(model["id"], note_type_data)

        document = schema.JsonApiDocument(
            data=resource,
            links=schema.Links(self=f"/api/notetypes/byname/{note_type_name}"),
        )

        self.send_json_response(200, document)


class NoteListsAPIHandler(APIHandler):
    """笔记列表处理器 - /api/get_note_lists (保留以保持向后兼容)"""

    def do_GET(self, **params):
        """处理获取笔记列表请求"""
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        # Get all deck names
        decks = []
        for deck_id in mw.col.decks.all_ids():
            deck = mw.col.decks.get(deck_id)
            decks.append(
                {
                    "id": deck_id,
                    "name": deck["name"],
                    "review_count": deck.get("rev", 0),
                    "new_count": deck.get("new", 0),
                    "learning_count": deck.get("lrn", 0),
                }
            )

        # Get note types
        note_types = []
        for model in mw.col.models.all():
            note_types.append(
                {
                    "id": model["id"],
                    "name": model["name"],
                    "fields": [field["name"] for field in model["flds"]],
                    "templates": [template["name"] for template in model["tmpls"]],
                }
            )

        response = {
            "data": {
                "decks": decks,
                "note_types": note_types,
                "total_notes": mw.col.note_count(),
            }
        }

        self.send_json_response(200, response)


def get_api_handler(path, req_handler, method):
    """根据路径获取对应的API处理器"""
    import re

    # 首先尝试精确匹配静态路由
    if path in API_ROUTES:
        handler_class = API_ROUTES[path]
        return handler_class(req_handler, method), {}

    # 尝试匹配动态路由
    for route_path, handler_class in API_ROUTES.items():
        # 将路由路径中的 <param> 替换为正则表达式捕获组
        pattern = re.sub(r"<(\w+)>", r"([^/]+)", route_path)
        pattern = f"^{pattern}$"

        match = re.match(pattern, path)
        if match:
            # 提取参数名
            param_names = re.findall(r"<(\w+)>", route_path)
            # 构建参数字典
            params = {}
            for i, param_name in enumerate(param_names):
                params[param_name] = match.group(i + 1)

            return handler_class(req_handler, method), params

    # 如果没有匹配的路由，返回 None
    return None, {}


def register_api_route(path, handler_class):
    """注册新的API路由"""
    API_ROUTES[path] = handler_class


def unregister_api_route(path):
    """注销API路由"""
    if path in API_ROUTES:
        del API_ROUTES[path]


def get_all_routes():
    """获取所有注册的路由"""
    return list(API_ROUTES.keys())


def reload_api_handlers():
    """重新加载API处理器（用于热重载）"""
    global API_ROUTES

    return f"API handlers reloaded. Registered routes: {get_all_routes()}"


# 加载各分类 handler 子模块（import 时通过 @api_route 自动注册路由；
# 子类会覆盖同路径的父类注册，/restart 热重载时会一并重新导入）。
# 逐个 try/except：任一子模块导入失败只损失该模块的路由，
# 不影响其余模块与整个 API 服务的可用性。
for _submodule in (
    "api_notes",
    "api_decks",
    "api_notetypes",
    "api_cards",
    "api_media",
    "api_stats",
    "api_system",
    "api_review",
):
    try:
        __import__(_submodule)
    except Exception:
        logger.error(f"Failed to import API submodule: {_submodule}", exc_info=True)
