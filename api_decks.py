"""
牌组（Decks）API 扩展处理模块
实现 docs/decks.md 中标记「未实现」的接口及末尾附录的「牌组扩展接口（v0.8）」：
- POST   /api/decks                                     创建牌组（幂等；attributes.filtered 存在时创建筛选牌组）
- PATCH  /api/decks/<deck_id>                           重命名 / 改父级（decks.rename / decks.reparent）
- DELETE /api/decks/<deck_id>                           删除牌组（cards_too 控制是否连同卡片删除）
- GET    /api/decks/tree                                牌组树（含 new/lrn/rev 计数，支持 ?top_deck_id=）
- GET    /api/decks/<deck_id>/cards                     牌组内卡片列表（分页 + query 叠加过滤）
- POST   /api/decks/<deck_id>/actions/move-cards        批量移动卡片到其他牌组
- POST   /api/decks/<deck_id>/actions/rebuild           重建筛选牌组
- POST   /api/decks/<deck_id>/actions/empty             排空筛选牌组
- POST   /api/decks/<deck_id>/actions/unbury            解除当日搁置
- POST   /api/decks/<deck_id>/actions/extend-limits     临时提高今日上限
- GET    /api/decks/<deck_id>/custom-study-defaults     自定义学习默认参数
- POST   /api/decks/<deck_id>/actions/custom-study      执行自定义学习
- GET    /api/decks/<deck_id>/config                    牌组配置详情
- PUT    /api/decks/<deck_id>/config                    整体替换牌组配置
- PATCH  /api/decks/<deck_id>/config                    切换牌组使用的配置组
- GET    /api/deck-configs                              全部配置组列表
- POST   /api/deck-configs                              克隆配置组
- DELETE /api/deck-configs/<config_id>                  删除配置组

同时覆盖 GET /api/decks 与 GET /api/decks/<deck_id>，属性附加 is_filtered。

路由通过 api.api_route 重新注册（子类继承已实现处理器并补充新方法），
本模块被导入时自动完成注册，无需修改 api.py。
"""

import json
import logging
from dataclasses import asdict
from urllib.parse import urlparse, parse_qs, quote

import anki.utils
from aqt import mw

import api
import schema

try:
    # 筛选牌组创建依赖 pylib 的 Deck.Filtered protobuf 结构
    from anki.decks import FilteredDeckConfig
except Exception:
    FilteredDeckConfig = None

try:
    # 解除搁置 / 自定义学习依赖 pylib 的 scheduler protobuf 结构
    from anki.scheduler.base import CustomStudyRequest, UnburyDeck
except Exception:
    CustomStudyRequest = None
    UnburyDeck = None

logger = logging.getLogger()
logger.info("API decks module loaded")


def _parse_json_body(req_handler):
    """
    读取并解析请求体 JSON

    Returns:
        解析后的对象；解析失败时已通过 req_handler 发送错误响应并返回 None
    """
    content_length = int(req_handler.headers.get("Content-Length", 0))
    if content_length <= 0:
        req_handler.send_error_response(400, "Bad Request", "Empty request body")
        return None

    raw_body = req_handler.rfile.read(content_length).decode("utf-8")
    try:
        return json.loads(raw_body)
    except Exception:
        req_handler.send_error_response(400, "Bad Request", "Invalid JSON body")
        return None


def _parse_int_param(handler, raw_value, param_name):
    """
    解析路径/请求中的整型参数

    Returns:
        int 值；失败时发送 400 并返回 None
    """
    # bool 是 int 的子类（int(True) == 1），需显式拒绝
    if isinstance(raw_value, bool):
        handler.send_error_response(
            400, "Bad Request", f"'{param_name}' must be an integer"
        )
        return None

    try:
        return int(raw_value)
    except (TypeError, ValueError):
        handler.send_error_response(
            400, "Bad Request", f"'{param_name}' must be an integer"
        )
        return None


def _get_deck_by_name(name):
    """按名称获取牌组（兼容新旧 Anki 的 by_name/byName 命名）"""
    decks = mw.col.decks
    if hasattr(decks, "by_name"):
        return decks.by_name(name)
    return decks.byName(name)


def _build_deck_resource(deck_id):
    """
    构建完整牌组资源对象（dict 形式，结构与 api.DeckDetailAPIHandler 一致，
    属性附加 is_filtered 标识是否为筛选牌组）
    """
    # default=False：id 不存在时返回 None 而非回退到默认牌组
    deck = mw.col.decks.get(deck_id, default=False)
    if not deck:
        return None

    # 获取牌组中的卡片数量
    card_ids = mw.col.find_cards(api.deck_query(deck["name"]))

    # 获取牌组中的笔记数量
    note_ids = mw.col.find_notes(api.deck_query(deck["name"]))

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

    # schema.DeckAttributes 暂无 is_filtered 字段，转为 dict 后附加
    resource = asdict(schema.create_deck_resource(deck_id, deck_data))
    resource["attributes"]["is_filtered"] = bool(deck.get("dyn", 0))
    return resource


def _get_deck_or_404(handler, deck_id):
    """
    解析路径中的牌组 id 并获取牌组（不存在时发送 404）

    Returns:
        (deck_id, deck)；失败时已发送错误响应并返回 (None, None)
    """
    deck_id = _parse_int_param(handler, deck_id, "deck_id")
    if deck_id is None:
        return None, None

    # default=False：不存在的 id 返回 None，而非回落到默认牌组
    deck = mw.col.decks.get(deck_id, default=False)
    if not deck:
        handler.send_error_response(
            404, "Not Found", f"Deck with ID {deck_id} not found"
        )
        return None, None

    return deck_id, deck


def _deck_tree_node_dict(node):
    """将 DeckTreeNode 序列化为嵌套 dict（低版本 Anki 无 total_in_deck 时返回 None）"""
    return {
        "deck_id": node.deck_id,
        "name": node.name,
        "new_count": node.new_count,
        "learn_count": node.learn_count,
        "review_count": node.review_count,
        "total_in_deck": getattr(node, "total_in_deck", None),
        "children": [_deck_tree_node_dict(child) for child in node.children],
    }


def _build_card_resource(card_id):
    """构建卡片资源对象（与 api.NoteCardsAPIHandler 的结构保持一致）"""
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

    return schema.create_card_resource(card_id, card_data)


def _create_deck_config_resource(config_id, config):
    """构建配置组资源对象，配置内容原样放入 attributes（new/lapse/rev 等嵌套结构不变）"""
    return schema.Resource(
        type="deck-configs",
        id=str(config_id),
        attributes=dict(config) if config else {},
        links=schema.Links(self=f"/api/deck-configs/{config_id}"),
    )


def _all_config_ids():
    """获取全部配置组 id 列表"""
    return [c["id"] for c in mw.col.decks.all_config()]


@api.api_route("/api/decks")
class DecksAPIHandler(api.DecksAPIHandler):
    """牌组列表处理器 - /api/decks（继承已实现部分，补充创建牌组）"""

    def do_GET(self, **params):
        """处理获取牌组列表请求（属性附带 is_filtered，其余与父类一致）"""
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        resources = []
        for deck_id in mw.col.decks.all_ids():
            resource = _build_deck_resource(deck_id)
            if resource:
                resources.append(resource)

        # 创建文档
        document = schema.DeckCollectionDocument(
            data=resources,
            links=schema.Links(self="/api/decks"),
            meta={"total": len(resources)},
        )

        self.send_json_response(200, document)

    def do_POST(self):
        """
        处理创建牌组请求（对应 AnkiConnect createDeck）
        支持 :: 层级命名（父级不存在时由 Anki 自动创建）；
        同名牌组已存在时幂等返回 200 与现有牌组。

        attributes 含 filtered 块时改为创建筛选牌组
        （sched.add_or_update_filtered_deck），与普通创建互斥：

        body:
        {
            "data": {
                "type": "decks",
                "attributes": {
                    "name": "考前突击",
                    "filtered": {
                        "search": "deck:英语 is:due",
                        "search_2": "",
                        "limit": 100,
                        "order": 0,
                        "reschedule": true
                    }
                }
            }
        }
        """
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        body = _parse_json_body(self.req_handler)
        if body is None:
            return

        # 校验 JSON:API 结构
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            self.send_error_response(400, "Bad Request", "'data' object is required")
            return

        if data.get("type") != "decks":
            self.send_error_response(
                409, "Conflict", "Resource type must be 'decks'"
            )
            return

        attrs = data.get("attributes", {})
        name = attrs.get("name") if isinstance(attrs, dict) else None
        if not name or not str(name).strip():
            self.send_error_response(
                400, "Bad Request", "'attributes.name' is required"
            )
            return

        name = str(name).strip()

        # attributes.filtered 存在时走筛选牌组创建（与普通创建互斥）
        filtered = attrs.get("filtered")
        if filtered is not None:
            self._create_filtered_deck(name, filtered)
            return

        try:
            # 同名牌组已存在：幂等返回 200 与现有牌组
            existing = _get_deck_by_name(name)
            if existing:
                resource = _build_deck_resource(existing["id"])
                document = schema.DeckDocument(
                    data=resource,
                    links=schema.Links(self=f"/api/decks/{existing['id']}"),
                )
                self.send_json_response(200, document)
                return

            # 创建牌组（:: 层级的父级由 Anki 自动创建）
            try:
                new_deck_id = mw.col.decks.id(name)
            except Exception as e:
                # 空段（如 "a::::b"）等非法名称会被后端拒绝，属于客户端错误
                self.send_error_response(
                    400, "Bad Request", f"Invalid deck name '{name}': {str(e)}"
                )
                return
            logger.info(f"Created deck '{name}' with id {new_deck_id}")

            resource = _build_deck_resource(new_deck_id)
            document = schema.DeckDocument(
                data=resource, links=schema.Links(self=f"/api/decks/{new_deck_id}")
            )
            self.send_json_response(201, document)

        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error creating deck: {str(e)}"
            )

    def _create_filtered_deck(self, name, filtered):
        """
        创建/更新筛选牌组（sched.add_or_update_filtered_deck）

        name 同名且为筛选牌组时按新搜索词更新并返回 200；
        name 同名但为普通牌组时返回 409（普通牌组不可转为筛选牌组）。

        filtered 块字段：
        - search / search_2：两条搜索词（search_2 为空时仅一条）
        - limit：取卡上限，默认 100
        - order：排序（Deck.Filtered.SearchTerm.Order，0-9），默认 0
        - reschedule：是否重排程，默认 true
        """
        if not isinstance(filtered, dict):
            self.send_error_response(
                400, "Bad Request", "'attributes.filtered' must be an object"
            )
            return

        if FilteredDeckConfig is None:
            self.send_error_response(
                500,
                "Internal Server Error",
                "pylib FilteredDeckConfig not available in this Anki version",
            )
            return

        search = filtered.get("search", "")
        search_2 = filtered.get("search_2", "")
        if not isinstance(search, str) or not isinstance(search_2, str):
            self.send_error_response(
                400, "Bad Request", "'filtered.search'/'filtered.search_2' must be strings"
            )
            return

        limit = _parse_int_param(self, filtered.get("limit", 100), "filtered.limit")
        if limit is None:
            return
        if limit < 0:
            self.send_error_response(
                400, "Bad Request", "'filtered.limit' must be a non-negative integer"
            )
            return

        order = _parse_int_param(self, filtered.get("order", 0), "filtered.order")
        if order is None:
            return
        if not 0 <= order <= 9:
            self.send_error_response(
                400, "Bad Request", "'filtered.order' must be an integer between 0 and 9"
            )
            return

        reschedule = bool(filtered.get("reschedule", True))

        # 同名普通牌组不可转为筛选牌组
        existing = _get_deck_by_name(name)
        if existing and not existing.get("dyn"):
            self.send_error_response(
                409,
                "Conflict",
                f"Deck '{name}' already exists and is not a filtered deck",
            )
            return

        try:
            # deck_id=0 表示新建；同名筛选牌组存在时加载并更新
            deck_id = existing["id"] if existing else 0
            deck_update = mw.col.sched.get_or_create_filtered_deck(deck_id)
            deck_update.name = name
            deck_update.config.reschedule = reschedule

            terms = [
                FilteredDeckConfig.SearchTerm(
                    search=search, limit=limit, order=order
                )
            ]
            if search_2.strip():
                terms.append(
                    FilteredDeckConfig.SearchTerm(
                        search=search_2, limit=limit, order=order
                    )
                )
            del deck_update.config.search_terms[:]
            deck_update.config.search_terms.extend(terms)

            new_deck_id = mw.col.sched.add_or_update_filtered_deck(deck_update).id
            logger.info(
                f"Added/updated filtered deck '{name}' with id {new_deck_id}"
            )
        except Exception as e:
            self.send_error_response(
                500,
                "Internal Server Error",
                f"Error creating filtered deck: {str(e)}",
            )
            return

        resource = _build_deck_resource(new_deck_id)
        document = schema.DeckDocument(
            data=resource, links=schema.Links(self=f"/api/decks/{new_deck_id}")
        )
        self.send_json_response(200 if existing else 201, document)


@api.api_route("/api/decks/<deck_id>")
class DeckDetailAPIHandler(api.DeckDetailAPIHandler):
    """单个牌组详情处理器 - /api/decks/{deck_id}（继承已实现部分，补充更新/删除牌组）"""

    def do_GET(self, deck_id):
        """处理获取单个牌组详情请求（属性附带 is_filtered）"""
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        deck_id, deck = _get_deck_or_404(self, deck_id)
        if deck is None:
            return

        resource = _build_deck_resource(deck_id)

        # 创建文档
        document = schema.DeckDocument(
            data=resource, links=schema.Links(self=f"/api/decks/{deck_id}")
        )

        self.send_json_response(200, document)

    def do_PATCH(self, deck_id):
        """
        处理重命名 / 改父级请求（decks.rename / decks.reparent）

        body:
        {
            "data": {
                "type": "decks",
                "id": "123",
                "attributes": { "name": "语言::英语", "parent_id": 0 }
            }
        }

        name 与 parent_id 至少提供一个；同时提供时先 reparent 后 rename；
        parent_id 为 0 表示移到顶层；rename 会联动更新子牌组前缀。
        """
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        deck_id, deck = _get_deck_or_404(self, deck_id)
        if deck is None:
            return

        body = _parse_json_body(self.req_handler)
        if body is None:
            return

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            self.send_error_response(400, "Bad Request", "'data' object is required")
            return

        if data.get("type") != "decks":
            self.send_error_response(
                409, "Conflict", "Resource type must be 'decks'"
            )
            return

        # body 中的 id 存在时必须与路径一致
        if data.get("id") is not None:
            body_id = _parse_int_param(self, data["id"], "data.id")
            if body_id is None:
                return
            if body_id != deck_id:
                self.send_error_response(
                    409,
                    "Conflict",
                    f"'data.id' {body_id} does not match path deck_id {deck_id}",
                )
                return

        attrs = data.get("attributes", {})
        if not isinstance(attrs, dict):
            self.send_error_response(
                400, "Bad Request", "'attributes' object is required"
            )
            return

        name = attrs.get("name")
        parent_id = attrs.get("parent_id")
        if name is None and parent_id is None:
            self.send_error_response(
                400,
                "Bad Request",
                "at least one of 'attributes.name' / 'attributes.parent_id' is required",
            )
            return

        if name is not None:
            name = str(name).strip()
            if not name:
                self.send_error_response(
                    400, "Bad Request", "'attributes.name' must be a non-empty string"
                )
                return

        if parent_id is not None:
            parent_id = _parse_int_param(
                self, parent_id, "attributes.parent_id"
            )
            if parent_id is None:
                return
            if parent_id == deck_id:
                self.send_error_response(
                    400, "Bad Request", "A deck cannot be its own parent"
                )
                return
            # parent_id 为 0 表示移到顶层，否则目标父牌组必须存在
            if parent_id != 0 and not mw.col.decks.get(parent_id, default=False):
                self.send_error_response(
                    404, "Not Found", f"Parent deck with ID {parent_id} not found"
                )
                return
            # 牌组不能挂到自身子孙之下（deck_and_child_ids 含自身与全部子孙）
            if parent_id != 0 and parent_id in mw.col.decks.deck_and_child_ids(deck_id):
                self.send_error_response(
                    400,
                    "Bad Request",
                    f"Deck {deck_id} cannot be moved under its own descendant {parent_id}",
                )
                return

        try:
            # 同时提供时先 reparent 后 rename
            if parent_id is not None:
                mw.col.decks.reparent([deck_id], parent_id)
                logger.info(f"Reparented deck {deck_id} under parent {parent_id}")
            if name is not None:
                mw.col.decks.rename(deck_id, name)
                logger.info(f"Renamed deck {deck_id} to '{name}'")
        except Exception as e:
            # 后端校验失败（重名、非法名称等）属于客户端错误，按异常消息映射
            message = str(e)
            if "already exists" in message.lower():
                self.send_error_response(
                    409, "Conflict", f"Error updating deck: {message}"
                )
            else:
                self.send_error_response(
                    400, "Bad Request", f"Error updating deck: {message}"
                )
            return

        resource = _build_deck_resource(deck_id)
        document = schema.DeckDocument(
            data=resource, links=schema.Links(self=f"/api/decks/{deck_id}")
        )
        self.send_json_response(200, document)

    def do_DELETE(self, deck_id):
        """
        处理删除牌组请求（对应 AnkiConnect deleteDecks）
        查询参数 cards_too：默认 false 时非空牌组返回 409；
        true 时连同牌组内卡片一并删除。
        """
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        deck_id = _parse_int_param(self, deck_id, "deck_id")
        if deck_id is None:
            return

        deck = mw.col.decks.get(deck_id, default=False)
        if not deck:
            self.send_error_response(
                404, "Not Found", f"Deck with ID {deck_id} not found"
            )
            return

        # 默认牌组不可删除（与 Anki GUI 行为一致）
        if deck_id == 1:
            self.send_error_response(
                400, "Bad Request", "Default deck (id=1) cannot be deleted"
            )
            return

        # 解析 cards_too 查询参数
        query_params = parse_qs(urlparse(self.req_handler.path).query)
        cards_too = query_params.get("cards_too", ["false"])[0].lower() in (
            "true",
            "1",
            "yes",
        )

        # deck:name 搜索同时覆盖子牌组，删除父牌组会级联删除子牌组
        card_ids = mw.col.find_cards(api.deck_query(deck["name"]))
        if card_ids and not cards_too:
            self.send_error_response(
                409,
                "Conflict",
                f"Deck '{deck['name']}' is not empty "
                f"({len(card_ids)} cards); pass cards_too=true to delete it "
                "along with its cards",
            )
            return

        deck_name = deck["name"]
        try:
            mw.col.decks.remove([deck_id])
            logger.info(f"Deleted deck '{deck_name}' (id {deck_id})")
        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error deleting deck: {str(e)}"
            )
            return

        document = schema.JsonApiDocument(meta={"deleted": deck_name})
        self.send_json_response(200, document)


@api.api_route("/api/decks/<deck_id>/cards")
class DeckCardsAPIHandler(api.APIHandler):
    """牌组卡片处理器 - /api/decks/{deck_id}/cards"""

    def do_GET(self, deck_id):
        """处理获取牌组内卡片列表请求（分页 + query 在 deck 过滤基础上叠加）"""
        try:
            if not mw.col:
                self.send_error_response(
                    503, "Service Unavailable", "Collection not available"
                )
                return

            deck_id = _parse_int_param(self, deck_id, "deck_id")
            if deck_id is None:
                return

            deck = mw.col.decks.get(deck_id, default=False)
            if not deck:
                self.send_error_response(
                    404, "Not Found", f"Deck with ID {deck_id} not found"
                )
                return

            # 获取查询参数
            query_params = parse_qs(urlparse(self.req_handler.path).query)

            # 分页参数（非法值返回 400 而非 500）
            try:
                page = int(query_params.get("page", [1])[0])
                limit = int(query_params.get("limit", [20])[0])
                if page < 1 or limit < 1:
                    raise ValueError
            except ValueError:
                self.send_error_response(
                    400, "Bad Request", "'page' and 'limit' must be positive integers"
                )
                return
            offset = (page - 1) * limit

            # 在 deck 过滤基础上叠加 query 参数（Anki 原生搜索语法）
            search = api.deck_query(deck["name"])
            extra_query = query_params.get("query", [""])[0].strip()
            if extra_query:
                search = f"{search} {extra_query}"

            # 获取牌组内卡片ID（非法搜索语法返回 400）
            try:
                card_ids = list(mw.col.find_cards(search))
            except Exception as e:
                self.send_error_response(
                    400, "Bad Request", f"Invalid search query: {str(e)}"
                )
                return
            total_cards = len(card_ids)

            # 分页获取卡片
            paginated_ids = card_ids[offset : offset + limit]
            resources = []

            for card_id in paginated_ids:
                resources.append(_build_card_resource(card_id))

            # 创建分页链接（保留 query 参数）
            base_url = f"/api/decks/{deck_id}/cards"
            query_suffix = f"&query={quote(extra_query)}" if extra_query else ""
            total_pages = (total_cards + limit - 1) // limit
            links = schema.Links(
                self=f"{base_url}?page={page}&limit={limit}{query_suffix}",
                first=f"{base_url}?page=1&limit={limit}{query_suffix}",
                last=f"{base_url}?page={total_pages}&limit={limit}{query_suffix}",
            )

            if page > 1:
                links.prev = f"{base_url}?page={page-1}&limit={limit}{query_suffix}"

            if page < total_pages:
                links.next = f"{base_url}?page={page+1}&limit={limit}{query_suffix}"

            # 创建文档
            document = schema.CardCollectionDocument(
                data=resources,
                links=links,
                meta={
                    "deck_id": deck_id,
                    "deck_name": deck["name"],
                    "pagination": {
                        "page": page,
                        "limit": limit,
                        "total": total_cards,
                        "pages": total_pages,
                    },
                },
            )

            self.send_json_response(200, document)

        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error getting deck cards: {str(e)}"
            )


@api.api_route("/api/decks/<deck_id>/actions/move-cards")
class DeckMoveCardsAPIHandler(api.APIHandler):
    """批量移出卡片处理器 - /api/decks/{deck_id}/actions/move-cards"""

    def do_POST(self, deck_id):
        """
        处理批量移动卡片请求（对应 AnkiConnect changeDeck）
        将指定卡片从当前牌组移入目标牌组。

        body:
        { "card_ids": [1498938915662], "target_deck_id": 456 }
        """
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        deck_id = _parse_int_param(self, deck_id, "deck_id")
        if deck_id is None:
            return

        # 路径中的牌组（卡片来源）必须存在
        deck = mw.col.decks.get(deck_id, default=False)
        if not deck:
            self.send_error_response(
                404, "Not Found", f"Deck with ID {deck_id} not found"
            )
            return

        body = _parse_json_body(self.req_handler)
        if body is None:
            return

        if not isinstance(body, dict):
            self.send_error_response(400, "Bad Request", "Request body must be an object")
            return

        card_ids = body.get("card_ids")
        target_deck_id = body.get("target_deck_id")

        if not isinstance(card_ids, list) or not card_ids:
            self.send_error_response(
                400, "Bad Request", "'card_ids' must be a non-empty array"
            )
            return

        if target_deck_id is None:
            self.send_error_response(
                400, "Bad Request", "'target_deck_id' is required"
            )
            return

        target_deck_id = _parse_int_param(self, target_deck_id, "target_deck_id")
        if target_deck_id is None:
            return

        # 校验目标牌组
        target_deck = mw.col.decks.get(target_deck_id, default=False)
        if not target_deck:
            self.send_error_response(
                404, "Not Found", f"Target deck with ID {target_deck_id} not found"
            )
            return

        # 过滤出真实存在的卡片，不存在的卡片跳过并在 meta 中报告
        valid_card_ids = []
        skipped_card_ids = []
        for card_id in card_ids:
            try:
                mw.col.get_card(int(card_id))
                valid_card_ids.append(int(card_id))
            except Exception:
                skipped_card_ids.append(card_id)

        try:
            if valid_card_ids:
                # 现代接口 set_deck：自动处理筛选牌组（odid）与 usn/mod，
                # 取代旧版 remFromDyn + 裸 SQL（v3 调度器已无 remFromDyn）
                mw.col.set_deck(valid_card_ids, target_deck_id)
                logger.info(
                    f"Moved {len(valid_card_ids)} cards from deck {deck_id} "
                    f"to deck {target_deck_id}"
                )
        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error moving cards: {str(e)}"
            )
            return

        document = schema.JsonApiDocument(
            meta={
                "moved": len(valid_card_ids),
                "skipped_card_ids": skipped_card_ids,
                "source_deck_id": deck_id,
                "target_deck_id": target_deck_id,
                "target_deck_name": target_deck["name"],
            }
        )
        self.send_json_response(200, document)


@api.api_route("/api/decks/<deck_id>/config")
class DeckConfigAPIHandler(api.APIHandler):
    """牌组配置处理器 - /api/decks/{deck_id}/config"""

    def _get_deck(self, deck_id):
        """获取牌组，失败时发送错误响应并返回 None"""
        deck_id = _parse_int_param(self, deck_id, "deck_id")
        if deck_id is None:
            return None, None

        deck = mw.col.decks.get(deck_id, default=False)
        if not deck:
            self.send_error_response(
                404, "Not Found", f"Deck with ID {deck_id} not found"
            )
            return None, None

        return deck_id, deck

    def do_GET(self, deck_id):
        """处理获取牌组配置请求（对应 AnkiConnect getDeckConfig），配置对象原样返回"""
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        deck_id, deck = self._get_deck(deck_id)
        if deck is None:
            return

        try:
            config = mw.col.decks.config_dict_for_deck_id(deck_id)
        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error getting deck config: {str(e)}"
            )
            return

        if not config:
            self.send_error_response(
                404, "Not Found", f"Deck with ID {deck_id} has no config"
            )
            return

        resource = _create_deck_config_resource(config["id"], config)
        document = schema.JsonApiDocument(
            data=resource,
            links=schema.Links(
                self=f"/api/decks/{deck_id}/config", related=f"/api/decks/{deck_id}"
            ),
        )
        self.send_json_response(200, document)

    def do_PUT(self, deck_id):
        """
        处理整体替换牌组配置请求（对应 AnkiConnect saveDeckConfig）
        请求体为完整配置 JSON（也兼容 JSON:API 包裹的 data.attributes 形式）。
        """
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        deck_id, deck = self._get_deck(deck_id)
        if deck is None:
            return

        conf_id = deck.get("conf")
        if not conf_id:
            self.send_error_response(
                409, "Conflict", f"Deck with ID {deck_id} has no config group"
            )
            return

        body = _parse_json_body(self.req_handler)
        if body is None:
            return

        # 兼容 JSON:API 包裹形式与裸配置 JSON 两种请求体
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            config = body["data"].get("attributes")
        else:
            config = body

        if not isinstance(config, dict):
            self.send_error_response(
                400, "Bad Request", "Request body must be a config object"
            )
            return

        # decks.save() 以 "maxTaken" 键是否存在来区分传入的是配置还是牌组，
        # 缺失时会被当作牌组误写，因此要求提交完整配置对象
        if "maxTaken" not in config:
            self.send_error_response(
                400,
                "Bad Request",
                "Config object is incomplete: 'maxTaken' key is required; "
                "please submit a full config object (see GET for its shape)",
            )
            return

        # 请求体中的 id 必须与牌组当前配置组一致
        if "id" in config:
            try:
                body_conf_id = int(config["id"])
            except (TypeError, ValueError):
                self.send_error_response(
                    400, "Bad Request", "'id' must be an integer"
                )
                return
            if body_conf_id != conf_id:
                self.send_error_response(
                    409,
                    "Conflict",
                    f"Config ID {config['id']} does not match deck's config ID {conf_id}",
                )
                return

        if conf_id not in _all_config_ids():
            self.send_error_response(
                404, "Not Found", f"Deck config with ID {conf_id} not found"
            )
            return

        try:
            # 参考 AnkiConnect saveDeckConfig 的实现
            config["id"] = str(conf_id)
            config["mod"] = anki.utils.int_time()
            config["usn"] = mw.col.usn()
            mw.col.decks.save(config)
            mw.col.decks.update_config(config)
        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error saving deck config: {str(e)}"
            )
            return

        saved_config = mw.col.decks.get_config(conf_id)
        resource = _create_deck_config_resource(conf_id, saved_config)
        document = schema.JsonApiDocument(
            data=resource,
            links=schema.Links(
                self=f"/api/decks/{deck_id}/config", related=f"/api/decks/{deck_id}"
            ),
        )
        self.send_json_response(200, document)

    def do_PATCH(self, deck_id):
        """
        处理切换牌组配置组请求（对应 AnkiConnect setDeckConfigId）

        body:
        { "data": { "type": "deck-configs", "id": "2" } }
        """
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        deck_id, deck = self._get_deck(deck_id)
        if deck is None:
            return

        body = _parse_json_body(self.req_handler)
        if body is None:
            return

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            self.send_error_response(400, "Bad Request", "'data' object is required")
            return

        if data.get("type") != "deck-configs":
            self.send_error_response(
                409, "Conflict", "Resource type must be 'deck-configs'"
            )
            return

        config_id = data.get("id")
        if config_id is None:
            self.send_error_response(400, "Bad Request", "'data.id' is required")
            return

        config_id = _parse_int_param(self, config_id, "data.id")
        if config_id is None:
            return

        # 校验目标配置组存在
        config = mw.col.decks.get_config(config_id)
        if not config:
            self.send_error_response(
                404, "Not Found", f"Deck config with ID {config_id} not found"
            )
            return

        try:
            deck["conf"] = config_id
            mw.col.decks.save(deck)
            logger.info(f"Set deck {deck_id} config to {config_id}")
        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error setting deck config: {str(e)}"
            )
            return

        resource = _create_deck_config_resource(config_id, config)
        document = schema.JsonApiDocument(
            data=resource,
            links=schema.Links(
                self=f"/api/decks/{deck_id}/config", related=f"/api/decks/{deck_id}"
            ),
        )
        self.send_json_response(200, document)


@api.api_route("/api/deck-configs")
class DeckConfigsAPIHandler(api.APIHandler):
    """配置组列表处理器 - /api/deck-configs"""

    def do_GET(self):
        """处理获取全部配置组列表请求（decks.all_config），配置内容原样放入 attributes"""
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        try:
            configs = mw.col.decks.all_config()
        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error getting deck configs: {str(e)}"
            )
            return

        resources = [
            _create_deck_config_resource(config["id"], config) for config in configs
        ]
        document = schema.JsonApiDocument(
            data=resources,
            links=schema.Links(self="/api/deck-configs"),
            meta={"total": len(resources)},
        )
        self.send_json_response(200, document)

    def do_POST(self):
        """
        处理克隆配置组请求（对应 AnkiConnect cloneDeckConfigId）

        body:
        {
            "data": {
                "type": "deck-configs",
                "attributes": { "clone_from": 1, "name": "英语专用配置" }
            }
        }
        """
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        body = _parse_json_body(self.req_handler)
        if body is None:
            return

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            self.send_error_response(400, "Bad Request", "'data' object is required")
            return

        if data.get("type") != "deck-configs":
            self.send_error_response(
                409, "Conflict", "Resource type must be 'deck-configs'"
            )
            return

        attrs = data.get("attributes", {})
        if not isinstance(attrs, dict):
            self.send_error_response(
                400, "Bad Request", "'attributes' object is required"
            )
            return

        name = attrs.get("name")
        if not name or not str(name).strip():
            self.send_error_response(
                400, "Bad Request", "'attributes.name' is required"
            )
            return

        clone_from = attrs.get("clone_from", 1)
        clone_from = _parse_int_param(self, clone_from, "attributes.clone_from")
        if clone_from is None:
            return

        # 校验被克隆的配置组存在
        source_config = mw.col.decks.get_config(clone_from)
        if not source_config:
            self.send_error_response(
                404, "Not Found", f"Deck config with ID {clone_from} not found"
            )
            return

        try:
            new_config_id = mw.col.decks.add_config_returning_id(
                str(name).strip(), source_config
            )
            logger.info(
                f"Cloned deck config {clone_from} to {new_config_id} ('{name}')"
            )
        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error cloning deck config: {str(e)}"
            )
            return

        new_config = mw.col.decks.get_config(new_config_id)
        resource = _create_deck_config_resource(new_config_id, new_config)
        document = schema.JsonApiDocument(
            data=resource,
            links=schema.Links(self=f"/api/deck-configs/{new_config_id}"),
        )
        self.send_json_response(201, document)


@api.api_route("/api/deck-configs/<config_id>")
class DeckConfigDetailAPIHandler(api.APIHandler):
    """单个配置组处理器 - /api/deck-configs/{config_id}"""

    def do_DELETE(self, config_id):
        """
        处理删除配置组请求（对应 AnkiConnect removeDeckConfigId）
        默认配置（id=1）不可删除返回 400；仍被牌组引用时返回 409 并列出引用方。
        """
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        config_id = _parse_int_param(self, config_id, "config_id")
        if config_id is None:
            return

        # 默认配置不可删除
        if config_id == 1:
            self.send_error_response(
                400, "Bad Request", "Default deck config (id=1) cannot be deleted"
            )
            return

        config = mw.col.decks.get_config(config_id)
        if not config:
            self.send_error_response(
                404, "Not Found", f"Deck config with ID {config_id} not found"
            )
            return

        # 检查是否仍有牌组引用该配置组
        referencing_decks = []
        for deck_id in mw.col.decks.all_ids():
            deck = mw.col.decks.get(deck_id)
            # 旧版牌组的 conf 可能是字符串，统一按字符串比较
            if deck and str(deck.get("conf")) == str(config_id):
                referencing_decks.append(deck["name"])

        if referencing_decks:
            self.send_error_response(
                409,
                "Conflict",
                "Deck config is still in use by decks: "
                + ", ".join(referencing_decks),
            )
            return

        try:
            mw.col.decks.remove_config(config_id)
            logger.info(f"Deleted deck config {config_id} ('{config.get('name', '')}')")
        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error deleting deck config: {str(e)}"
            )
            return

        document = schema.JsonApiDocument(meta={"deleted": config_id})
        self.send_json_response(200, document)


@api.api_route("/api/decks/tree")
class DeckTreeAPIHandler(api.APIHandler):
    """牌组树处理器 - /api/decks/tree（静态路由，优先于 /api/decks/<deck_id> 匹配）"""

    def do_GET(self):
        """
        处理获取牌组树请求（sched.deck_due_tree）

        返回嵌套树，节点含 deck_id/name/new_count/learn_count/review_count/
        total_in_deck/children；?top_deck_id= 限定子树，不存在时返回 404。
        """
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        query_params = parse_qs(urlparse(self.req_handler.path).query)
        top_deck_id = query_params.get("top_deck_id", [None])[0]

        self_link = "/api/decks/tree"
        try:
            if top_deck_id is not None:
                top_deck_id = _parse_int_param(self, top_deck_id, "top_deck_id")
                if top_deck_id is None:
                    return
                # 指定 top_deck_id 时找不到子树返回 None
                tree = mw.col.sched.deck_due_tree(top_deck_id)
                if tree is None:
                    self.send_error_response(
                        404, "Not Found", f"Deck with ID {top_deck_id} not found"
                    )
                    return
                self_link = f"/api/decks/tree?top_deck_id={top_deck_id}"
            else:
                tree = mw.col.sched.deck_due_tree()
        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error getting deck tree: {str(e)}"
            )
            return

        node = _deck_tree_node_dict(tree)
        document = {
            "data": {
                "type": "deck-tree",
                "id": str(node["deck_id"]),
                "attributes": node,
            },
            "links": {"self": self_link},
        }
        self.send_json_response(200, document)


@api.api_route("/api/decks/<deck_id>/actions/rebuild")
class DeckRebuildAPIHandler(api.APIHandler):
    """重建筛选牌组处理器 - /api/decks/{deck_id}/actions/rebuild"""

    def do_POST(self, deck_id):
        """按当前搜索词重新填充筛选牌组（sched.rebuild_filtered_deck）；普通牌组返回 400"""
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        deck_id, deck = _get_deck_or_404(self, deck_id)
        if deck is None:
            return

        if not deck.get("dyn"):
            self.send_error_response(
                400,
                "Bad Request",
                f"Deck '{deck['name']}' is not a filtered deck",
            )
            return

        try:
            result = mw.col.sched.rebuild_filtered_deck(deck_id)
            logger.info(f"Rebuilt filtered deck {deck_id} ('{deck['name']}')")
        except Exception as e:
            self.send_error_response(
                500,
                "Internal Server Error",
                f"Error rebuilding filtered deck: {str(e)}",
            )
            return

        document = schema.JsonApiDocument(
            meta={"rebuilt_deck_id": deck_id, "card_count": result.count}
        )
        self.send_json_response(200, document)


@api.api_route("/api/decks/<deck_id>/actions/empty")
class DeckEmptyAPIHandler(api.APIHandler):
    """排空筛选牌组处理器 - /api/decks/{deck_id}/actions/empty"""

    def do_POST(self, deck_id):
        """排空筛选牌组，卡片回到原牌组（sched.empty_filtered_deck）；普通牌组返回 400"""
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        deck_id, deck = _get_deck_or_404(self, deck_id)
        if deck is None:
            return

        if not deck.get("dyn"):
            self.send_error_response(
                400,
                "Bad Request",
                f"Deck '{deck['name']}' is not a filtered deck",
            )
            return

        try:
            mw.col.sched.empty_filtered_deck(deck_id)
            logger.info(f"Emptied filtered deck {deck_id} ('{deck['name']}')")
        except Exception as e:
            self.send_error_response(
                500,
                "Internal Server Error",
                f"Error emptying filtered deck: {str(e)}",
            )
            return

        document = schema.JsonApiDocument(meta={"emptied_deck_id": deck_id})
        self.send_json_response(200, document)


@api.api_route("/api/decks/<deck_id>/actions/unbury")
class DeckUnburyAPIHandler(api.APIHandler):
    """解除当日搁置处理器 - /api/decks/{deck_id}/actions/unbury"""

    def do_POST(self, deck_id):
        """
        解除牌组当日搁置的卡片（sched.unbury_deck）

        body（可省略）: { "mode": "all" }
        mode 取值 all / user_only / sched_only，默认 all。
        """
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        if UnburyDeck is None:
            self.send_error_response(
                500,
                "Internal Server Error",
                "pylib UnburyDeck not available in this Anki version",
            )
            return

        deck_id, deck = _get_deck_or_404(self, deck_id)
        if deck is None:
            return

        # 请求体可省略，省略时按默认 mode=all 处理
        content_length = int(self.req_handler.headers.get("Content-Length", 0))
        if content_length > 0:
            body = _parse_json_body(self.req_handler)
            if body is None:
                return
            if not isinstance(body, dict):
                self.send_error_response(
                    400, "Bad Request", "Request body must be an object"
                )
                return
        else:
            body = {}

        mode_name = body.get("mode", "all")
        modes = {
            "all": UnburyDeck.ALL,
            "user_only": UnburyDeck.USER_ONLY,
            "sched_only": UnburyDeck.SCHED_ONLY,
        }
        if mode_name not in modes:
            self.send_error_response(
                400,
                "Bad Request",
                "'mode' must be one of: all, user_only, sched_only",
            )
            return

        try:
            mw.col.sched.unbury_deck(deck_id, mode=modes[mode_name])
            logger.info(f"Unburied deck {deck_id} (mode={mode_name})")
        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error unburying deck: {str(e)}"
            )
            return

        document = schema.JsonApiDocument(
            meta={"unburied_deck_id": deck_id, "mode": mode_name}
        )
        self.send_json_response(200, document)


@api.api_route("/api/decks/<deck_id>/actions/extend-limits")
class DeckExtendLimitsAPIHandler(api.APIHandler):
    """临时提高今日上限处理器 - /api/decks/{deck_id}/actions/extend-limits"""

    def do_POST(self, deck_id):
        """
        临时提高今日新卡/复习上限（后端 extend_limits）

        body: { "new": 10, "rev": 20 }

        注意：pylib 的 sched.extend_limits() 只作用于当前选中牌组，
        这里直接调用后端接口以作用于路径指定的牌组。
        """
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        deck_id, deck = _get_deck_or_404(self, deck_id)
        if deck is None:
            return

        body = _parse_json_body(self.req_handler)
        if body is None:
            return

        if not isinstance(body, dict):
            self.send_error_response(400, "Bad Request", "Request body must be an object")
            return

        if "new" not in body or "rev" not in body:
            self.send_error_response(
                400, "Bad Request", "'new' and 'rev' are required"
            )
            return

        new = _parse_int_param(self, body["new"], "new")
        if new is None:
            return
        rev = _parse_int_param(self, body["rev"], "rev")
        if rev is None:
            return

        # 契约是「临时提高今日上限」，负数无意义
        if new < 0 or rev < 0:
            self.send_error_response(
                400, "Bad Request", "'new' and 'rev' must be non-negative integers"
            )
            return

        # 版本脆弱点：直接调用私有接口 mw.col._backend（pylib 对其有弃用警告），
        # 调用前探测其是否存在，不存在说明当前 Anki 版本不支持该能力
        backend = getattr(mw.col, "_backend", None)
        if backend is None or not hasattr(backend, "extend_limits"):
            self.send_error_response(
                500,
                "Internal Server Error",
                "Current Anki version does not support extend_limits "
                "(collection._backend unavailable)",
            )
            return

        try:
            backend.extend_limits(
                deck_id=deck_id, new_delta=new, review_delta=rev
            )
            logger.info(f"Extended limits of deck {deck_id}: new+{new}, rev+{rev}")
        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error extending limits: {str(e)}"
            )
            return

        document = schema.JsonApiDocument(
            meta={"deck_id": deck_id, "new": new, "rev": rev}
        )
        self.send_json_response(200, document)


@api.api_route("/api/decks/<deck_id>/custom-study-defaults")
class DeckCustomStudyDefaultsAPIHandler(api.APIHandler):
    """自定义学习默认参数处理器 - /api/decks/{deck_id}/custom-study-defaults"""

    def do_GET(self, deck_id):
        """返回自定义学习默认参数（sched.custom_study_defaults），供修改后提交 custom-study"""
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        deck_id, deck = _get_deck_or_404(self, deck_id)
        if deck is None:
            return

        try:
            defaults = mw.col.sched.custom_study_defaults(deck_id)
        except Exception as e:
            self.send_error_response(
                500,
                "Internal Server Error",
                f"Error getting custom study defaults: {str(e)}",
            )
            return

        attributes = {
            "tags": [
                {"name": tag.name, "include": tag.include, "exclude": tag.exclude}
                for tag in defaults.tags
            ],
            "extend_new": defaults.extend_new,
            "extend_review": defaults.extend_review,
            "available_new": defaults.available_new,
            "available_review": defaults.available_review,
            "available_new_in_children": defaults.available_new_in_children,
            "available_review_in_children": defaults.available_review_in_children,
        }
        document = {
            "data": {
                "type": "custom-study-defaults",
                "id": str(deck_id),
                "attributes": attributes,
            },
            "links": {"self": f"/api/decks/{deck_id}/custom-study-defaults"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/decks/<deck_id>/actions/custom-study")
class DeckCustomStudyAPIHandler(api.APIHandler):
    """自定义学习处理器 - /api/decks/{deck_id}/actions/custom-study"""

    # CustomStudyRequest oneof value 的可选字段
    _STUDY_FIELDS = (
        "new_limit_delta",
        "review_limit_delta",
        "forgot_days",
        "review_ahead_days",
        "preview_days",
        "cram",
    )

    def do_POST(self, deck_id):
        """
        执行自定义学习（sched.custom_study）

        body 为 CustomStudyRequest 的 JSON 形式（deck_id 由路径提供，
        body 中提供时必须一致），以下字段恰好提供一个：
        - new_limit_delta / review_limit_delta：提高新卡/复习上限
        - forgot_days：重复最近 x 天遗忘的卡片
        - review_ahead_days：提前复习未来 x 天到期的卡片
        - preview_days：预览最近 x 天添加的新卡
        - cram：{ "kind": 0-3, "card_limit": 100,
                  "tags_to_include": [], "tags_to_exclude": [] }
          （kind：0=到期顺序 1=新卡添加顺序 2=随机复习 3=全部随机不重排程）
        """
        if not mw.col:
            self.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        if CustomStudyRequest is None:
            self.send_error_response(
                500,
                "Internal Server Error",
                "pylib CustomStudyRequest not available in this Anki version",
            )
            return

        deck_id, deck = _get_deck_or_404(self, deck_id)
        if deck is None:
            return

        body = _parse_json_body(self.req_handler)
        if body is None:
            return

        if not isinstance(body, dict):
            self.send_error_response(400, "Bad Request", "Request body must be an object")
            return

        # body 中的 deck_id 存在时必须与路径一致
        if body.get("deck_id") is not None:
            body_deck_id = _parse_int_param(self, body["deck_id"], "deck_id")
            if body_deck_id is None:
                return
            if body_deck_id != deck_id:
                self.send_error_response(
                    400,
                    "Bad Request",
                    f"'deck_id' {body_deck_id} does not match path deck_id {deck_id}",
                )
                return

        provided = [f for f in self._STUDY_FIELDS if body.get(f) is not None]
        if len(provided) != 1:
            self.send_error_response(
                400,
                "Bad Request",
                "exactly one of {} must be provided".format(
                    ", ".join(self._STUDY_FIELDS)
                ),
            )
            return

        field = provided[0]
        request = CustomStudyRequest()
        request.deck_id = deck_id

        if field == "cram":
            params = self._build_cram(body["cram"], request)
            if params is None:
                return
        else:
            value = _parse_int_param(self, body[field], field)
            if value is None:
                return
            # forgot_days / review_ahead_days / preview_days 为无符号整数
            if field != "new_limit_delta" and field != "review_limit_delta":
                if value < 0:
                    self.send_error_response(
                        400,
                        "Bad Request",
                        f"'{field}' must be a non-negative integer",
                    )
                    return
            setattr(request, field, value)
            params = value

        try:
            mw.col.sched.custom_study(request)
            logger.info(f"Custom study on deck {deck_id}: {field}={params}")
        except Exception as e:
            self.send_error_response(
                500, "Internal Server Error", f"Error running custom study: {str(e)}"
            )
            return

        document = schema.JsonApiDocument(
            meta={"deck_id": deck_id, "mode": field, "params": params}
        )
        self.send_json_response(200, document)

    def _build_cram(self, cram, request):
        """
        校验并填充 CustomStudyRequest 的 cram 块

        Returns:
            用于 meta 的回显参数 dict；校验失败时已发送错误响应并返回 None
        """
        if not isinstance(cram, dict):
            self.send_error_response(
                400, "Bad Request", "'cram' must be an object"
            )
            return None

        kind = _parse_int_param(self, cram.get("kind", 0), "cram.kind")
        if kind is None:
            return None
        if not 0 <= kind <= 3:
            self.send_error_response(
                400, "Bad Request", "'cram.kind' must be an integer between 0 and 3"
            )
            return None

        card_limit = _parse_int_param(
            self, cram.get("card_limit", 100), "cram.card_limit"
        )
        if card_limit is None:
            return None
        if card_limit < 1:
            self.send_error_response(
                400, "Bad Request", "'cram.card_limit' must be a positive integer"
            )
            return None

        tags_to_include = cram.get("tags_to_include", [])
        tags_to_exclude = cram.get("tags_to_exclude", [])
        if not isinstance(tags_to_include, list) or not isinstance(
            tags_to_exclude, list
        ):
            self.send_error_response(
                400,
                "Bad Request",
                "'cram.tags_to_include'/'cram.tags_to_exclude' must be arrays",
            )
            return None

        tags_to_include = [str(t) for t in tags_to_include]
        tags_to_exclude = [str(t) for t in tags_to_exclude]

        request.cram.kind = kind
        request.cram.card_limit = card_limit
        request.cram.tags_to_include.extend(tags_to_include)
        request.cram.tags_to_exclude.extend(tags_to_exclude)

        return {
            "kind": kind,
            "card_limit": card_limit,
            "tags_to_include": tags_to_include,
            "tags_to_exclude": tags_to_exclude,
        }
