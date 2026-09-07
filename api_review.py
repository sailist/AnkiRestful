"""
复习与工具（Review & Utils）API 处理模块

实现 docs/review.md 中规划的接口（v0.8，支撑无头复习客户端场景）：
- GET  /api/review/queue          取待学队列（幂等预览，不移出队列）
- POST /api/utils/compare-answer  输入型卡片答案比对
- POST /api/search/build          搜索串构造/校验

路由通过 @api.api_route 注册，本模块被导入时自动完成注册，无需修改 api.py。

说明：
- /api/review/queue 对应 v3 调度器的 get_queued_cards() / describe_next_states()，
  仅 v3 调度器可用；卡片资源结构与 GET /api/cards/<card_id> 一致，
  每个元素的 meta.scheduling_states 给出四个作答按钮的间隔预览。
- compare_answer 的整体 HTML 由 pylib 生成（rslib typeanswer.rs，输出结构固定），
  本模块按该结构拆分为 expected/provided 两个 diff 高亮片段。
- /api/search/build 对应 col.build_search_string()：传 query 为语法校验
  （解析失败即非法），传 nodes 为由结构化节点构造搜索串。
"""

import json
import logging
from dataclasses import asdict
from urllib.parse import urlparse, parse_qs

from aqt import mw

import api
import schema

try:
    # 搜索节点构造依赖 SearchNode protobuf（col.build_search_string 的入参）
    from anki.search_pb2 import SearchNode
except Exception:
    SearchNode = None

logger = logging.getLogger()
logger.info("API review module loaded")


# 通用辅助函数
##########################################################################


def _check_collection(handler):
    """检查 collection 是否可用，不可用时发送 503 并返回 False"""
    if not mw.col:
        handler.req_handler.send_error_response(
            503, "Service Unavailable", "Collection not available"
        )
        return False
    return True


def _read_json_body(req_handler):
    """读取并解析 JSON 请求体

    解析失败时自动发送 400 错误响应并返回 None，调用方应直接 return。
    """
    content_length = int(req_handler.headers.get("Content-Length", 0))
    if content_length <= 0:
        req_handler.send_error_response(400, "Bad Request", "Empty request body")
        return None

    raw_body = req_handler.rfile.read(content_length).decode("utf-8")
    try:
        body = json.loads(raw_body)
    except Exception:
        req_handler.send_error_response(400, "Bad Request", "Invalid JSON body")
        return None

    if body is None:
        req_handler.send_error_response(400, "Bad Request", "Invalid JSON body")
        return None
    return body


def _parse_int(value):
    """将值解析为整数，失败返回 None（布尔值视为非法输入）"""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _sched_method(snake_name, camel_name):
    """获取调度器方法（兼容新版 snake_case 与旧版 camelCase 命名）"""
    method = getattr(mw.col.sched, snake_name, None)
    if method is None:
        method = getattr(mw.col.sched, camel_name, None)
    return method


def _build_card_resource(card):
    """构建卡片资源对象（与 GET /api/cards/<card_id> 的结构保持一致）"""
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

    resource = schema.create_card_resource(card.id, card_data)
    # 与 /api/cards 系列接口一致：attributes 展开为 dict 后追加 flag
    attributes = asdict(resource.attributes)
    attributes["flag"] = card.user_flag()
    resource.attributes = attributes

    resource.relationships["reviews"] = schema.Relationship(
        links=schema.Links(related=f"/api/cards/{card.id}/reviews")
    )
    return resource


# 待学队列
##########################################################################


@api.api_route("/api/review/queue")
class ReviewQueueAPIHandler(api.APIHandler):
    """待学队列处理器 - /api/review/queue

    对应 v3 调度器 sched.get_queued_cards()：幂等预览待学卡片，
    不移出队列、不改变调度状态（作答走 POST /api/cards/actions/answer，
    渲染走 GET /api/cards/{id}/render）。仅 v3 调度器可用
    （v1/v2 的 DummyScheduler 无此能力）。
    """

    def do_GET(self):
        """处理获取待学队列请求

        查询参数：
        - limit：返回卡片数，默认 1
        - intraday_learning_only：true 时仅返回当日学习队列中的卡
        """
        if not _check_collection(self):
            return

        get_queued_cards = _sched_method("get_queued_cards", "getQueuedCards")
        describe = _sched_method("describe_next_states", "describeNextStates")
        if get_queued_cards is None or describe is None:
            self.req_handler.send_error_response(
                400, "Bad Request", "review queue requires the v3 scheduler"
            )
            return

        query_params = parse_qs(urlparse(self.req_handler.path).query)

        limit = _parse_int(query_params.get("limit", [1])[0])
        if limit is None or limit < 1:
            self.req_handler.send_error_response(
                400, "Bad Request", "'limit' must be a positive integer"
            )
            return

        intraday_only = query_params.get("intraday_learning_only", [""])[0]
        intraday_only = intraday_only.strip().lower() in ("true", "1", "yes")

        try:
            queued = get_queued_cards(
                fetch_limit=limit, intraday_learning_only=intraday_only
            )
        except Exception as e:
            self.req_handler.send_error_response(
                500,
                "Internal Server Error",
                f"Error getting queued cards: {str(e)}",
            )
            return

        resources = []
        for queued_card in queued.cards:
            try:
                card = mw.col.get_card(queued_card.card.id)
            except Exception:
                # 卡片在取队列后被删除等情况，跳过
                logger.warning(
                    f"Queued card {queued_card.card.id} not found, skipped"
                )
                continue

            resource = _build_card_resource(card)

            try:
                texts = list(describe(queued_card.states))
            except Exception as e:
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    f"Error describing next states: {str(e)}",
                )
                return

            # 按钮顺序与 SchedulingStates 的 again/hard/good/easy 一致，
            # rating 1-4 依次对应四个作答按钮
            resource.meta = {
                "scheduling_states": [
                    {"rating": rating, "interval_text": interval_text}
                    for rating, interval_text in enumerate(texts, start=1)
                ]
            }
            resources.append(resource)

        remaining = (
            queued.new_count + queued.learning_count + queued.review_count
        )

        self_link = f"/api/review/queue?limit={limit}"
        if intraday_only:
            self_link += "&intraday_learning_only=true"

        document = schema.CardCollectionDocument(
            data=resources,
            links=schema.Links(self=self_link),
            meta={"remaining": remaining},
        )
        self.send_json_response(200, document)


# 答案比对
##########################################################################


def _split_comparison_html(output):
    """将 compare_answer 返回的整体 HTML 拆分为期望/输入两个高亮片段

    pylib（rslib typeanswer.rs）的输出结构固定：
    - 完全一致: <code id=typeans><span class=typeGood>...</span></code>
    - 存在差异: <code id=typeans>{输入片段}<br><span id=typearrow>&darr;</span><br>{期望片段}</code>
    - 输入为空: <code id=typeans>{期望文本}</code>

    返回 (correct, expected_html, provided_html)。
    """
    inner = output
    prefix = "<code id=typeans>"
    suffix = "</code>"
    if inner.startswith(prefix) and inner.endswith(suffix):
        inner = inner[len(prefix) : len(inner) - len(suffix)]

    separator = "<br><span id=typearrow>&darr;</span><br>"
    if separator in inner:
        provided_html, expected_html = inner.split(separator, 1)
        return False, expected_html, provided_html

    # 折叠输出：含 typeGood 高亮为完全一致；输入为空时仅为期望文本，视为不一致
    correct = "<span class=typeGood>" in inner
    if correct:
        return True, inner, inner
    return False, inner, ""


@api.api_route("/api/utils/compare-answer")
class UtilsCompareAnswerAPIHandler(api.APIHandler):
    """答案比对处理器 - /api/utils/compare-answer

    对应 pylib col.compare_answer()，用于「输入答案」型卡片的无头判分：
    correct 为完全匹配（大小写/组合字符按 pylib 规则处理），
    expected_html / provided_html 为带 diff 高亮的对照片段，客户端可直接展示。
    """

    def do_POST(self):
        """
        body:
        { "expected": "Mitochondria", "provided": "mitochondria", "combining": true }
        combining 可选，默认 true（与 pylib 及 Anki 复习界面默认值一致）。
        """
        if not _check_collection(self):
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return

        if not isinstance(body, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "Request body must be a JSON object"
            )
            return

        expected = body.get("expected")
        provided = body.get("provided")
        if not isinstance(expected, str) or not isinstance(provided, str):
            self.req_handler.send_error_response(
                400, "Bad Request", "'expected' and 'provided' must be strings"
            )
            return

        combining = body.get("combining", True)
        if not isinstance(combining, bool):
            self.req_handler.send_error_response(
                400, "Bad Request", "'combining' must be a boolean"
            )
            return

        try:
            output = mw.col.compare_answer(expected, provided, combining)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error comparing answer: {str(e)}"
            )
            return

        correct, expected_html, provided_html = _split_comparison_html(output)

        resource = {
            "type": "answer-comparison",
            "attributes": {
                "correct": correct,
                "expected_html": expected_html,
                "provided_html": provided_html,
            },
        }
        self.send_json_response(200, {"data": resource})


# 搜索串构造/校验
##########################################################################

# SearchNode 各枚举的「名称 -> proto 枚举值」映射
# （枚举值见 proto/anki/search.proto，proto3 枚举值固定）
_SEARCH_FLAG_VALUES = {
    "none": 0,
    "any": 1,
    "red": 2,
    "orange": 3,
    "green": 4,
    "blue": 5,
    "pink": 6,
    "turquoise": 7,
    "purple": 8,
}
_SEARCH_CARD_STATE_VALUES = {
    "new": 0,
    "learn": 1,
    "review": 2,
    "due": 3,
    "suspended": 4,
    "buried": 5,
}
_SEARCH_RATING_VALUES = {
    "any": 0,
    "again": 1,
    "hard": 2,
    "good": 3,
    "easy": 4,
    "reschedule": 5,
    "rescheduled": 5,
}
_SEARCH_FIELD_MODE_VALUES = {"normal": 0, "regex": 1, "nocombining": 2}
_SEARCH_JOINER_VALUES = {"AND": 0, "OR": 1}

# 值为字符串的过滤键（query 为 parsable_text 的别名）
_SEARCH_STRING_FIELDS = (
    "deck",
    "tag",
    "note",
    "field_name",
    "literal_text",
    "parsable_text",
    "query",
)
# 值为整数的过滤键
_SEARCH_INT_FIELDS = ("nid", "due_in_days", "due_on_day")
# 值为非负整数的过滤键
_SEARCH_UINT_FIELDS = (
    "template",
    "added_in_days",
    "edited_in_days",
    "introduced_in_days",
)


def _enum_value(value, table, field_desc):
    """解析枚举值（支持 proto 整数值或名称），失败抛出 ValueError"""
    parsed = _parse_int(value)
    if parsed is not None:
        if parsed in table.values():
            return parsed
    elif isinstance(value, str):
        enum_value = table.get(value.strip().lower())
        if enum_value is not None:
            return enum_value
    raise ValueError(f"{field_desc} must be one of: {', '.join(sorted(table))}")


def _parse_joiner(value, field_desc="'joiner'"):
    """解析搜索连接词（AND/OR，大小写不敏感），失败抛出 ValueError"""
    if isinstance(value, str):
        joiner = value.strip().upper()
        if joiner in _SEARCH_JOINER_VALUES:
            return joiner
    raise ValueError(f"{field_desc} must be 'AND' or 'OR'")


def _build_search_node(node):
    """将节点对象转换为 SearchNode（negated/group 递归）

    每个节点必须且只能包含一个过滤键；非法输入抛出 ValueError。
    支持的过滤键：
    - 字符串：deck / tag / note / field_name / literal_text / parsable_text（别名 query）
    - 整数：nid / due_in_days / due_on_day
    - 非负整数：template / added_in_days / edited_in_days / introduced_in_days
    - 枚举：flag / card_state（proto 整数值或名称）
    - 对象：rated / dupe / field / nids / negated / group
    """
    if not isinstance(node, dict):
        raise ValueError("each search node must be an object")
    if len(node) != 1:
        raise ValueError("each search node must have exactly one filter")

    key, value = next(iter(node.items()))

    if key in _SEARCH_STRING_FIELDS:
        if not isinstance(value, str):
            raise ValueError(f"'{key}' filter must be a string")
        if key == "query":
            key = "parsable_text"
        return SearchNode(**{key: value})

    if key in _SEARCH_INT_FIELDS or key in _SEARCH_UINT_FIELDS:
        parsed = _parse_int(value)
        if parsed is None:
            raise ValueError(f"'{key}' filter must be an integer")
        if key in _SEARCH_UINT_FIELDS and parsed < 0:
            raise ValueError(f"'{key}' filter must be a non-negative integer")
        return SearchNode(**{key: parsed})

    if key == "flag":
        return SearchNode(flag=_enum_value(value, _SEARCH_FLAG_VALUES, "'flag'"))

    if key == "card_state":
        return SearchNode(
            card_state=_enum_value(value, _SEARCH_CARD_STATE_VALUES, "'card_state'")
        )

    if key == "rated":
        if not isinstance(value, dict):
            raise ValueError("'rated' filter must be an object")
        days = _parse_int(value.get("days"))
        if days is None or days < 0:
            raise ValueError("'rated.days' must be a non-negative integer")
        rating = _enum_value(
            value.get("rating", "any"), _SEARCH_RATING_VALUES, "'rated.rating'"
        )
        return SearchNode(rated=SearchNode.Rated(days=days, rating=rating))

    if key == "nids":
        if not isinstance(value, list) or not value:
            raise ValueError("'nids' filter must be a non-empty array")
        ids = []
        for raw in value:
            parsed = _parse_int(raw)
            if parsed is None:
                raise ValueError("'nids' filter must contain only integers")
            ids.append(parsed)
        return SearchNode(nids=SearchNode.IdList(ids=ids))

    if key == "dupe":
        if not isinstance(value, dict):
            raise ValueError("'dupe' filter must be an object")
        notetype_id = _parse_int(value.get("notetype_id"))
        if notetype_id is None:
            raise ValueError("'dupe.notetype_id' must be an integer")
        first_field = value.get("first_field", "")
        if not isinstance(first_field, str):
            raise ValueError("'dupe.first_field' must be a string")
        return SearchNode(
            dupe=SearchNode.Dupe(notetype_id=notetype_id, first_field=first_field)
        )

    if key == "field":
        if not isinstance(value, dict):
            raise ValueError("'field' filter must be an object")
        field_name = value.get("field_name")
        text = value.get("text")
        if not isinstance(field_name, str) or not isinstance(text, str):
            raise ValueError("'field.field_name' and 'field.text' must be strings")
        mode = _enum_value(
            value.get("mode", "normal"), _SEARCH_FIELD_MODE_VALUES, "'field.mode'"
        )
        return SearchNode(
            field=SearchNode.Field(field_name=field_name, text=text, mode=mode)
        )

    if key == "negated":
        return SearchNode(negated=_build_search_node(value))

    if key == "group":
        if not isinstance(value, dict):
            raise ValueError("'group' filter must be an object")
        sub_nodes = value.get("nodes")
        if not isinstance(sub_nodes, list) or not sub_nodes:
            raise ValueError("'group.nodes' must be a non-empty array")
        joiner = _parse_joiner(value.get("joiner", "AND"), "'group.joiner'")
        return SearchNode(
            group=SearchNode.Group(
                nodes=[_build_search_node(sub) for sub in sub_nodes],
                joiner=_SEARCH_JOINER_VALUES[joiner],
            )
        )

    raise ValueError(f"unknown search node filter: '{key}'")


@api.api_route("/api/search/build")
class SearchBuildAPIHandler(api.APIHandler):
    """搜索串构造/校验处理器 - /api/search/build

    对应 pylib col.build_search_string()，两种用法（query 与 nodes 互斥）：
    - 校验：body {"query": "deck:CS is:due"}，合法返回 valid+normalized，
      非法返回 400 与错误详情；
    - 构造：body {"nodes": [{"deck": "CS"}, {"tag": "英语"}], "joiner": "AND"}，
      返回拼接好的搜索串。
    客户端可在调用 GET /api/notes?query= / GET /api/cards?query= 前先用本接口预检。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        if SearchNode is None:
            self.req_handler.send_error_response(
                500,
                "Internal Server Error",
                "Current Anki version does not support search node building",
            )
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return

        if not isinstance(body, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "Request body must be a JSON object"
            )
            return

        has_query = "query" in body
        has_nodes = "nodes" in body
        if has_query == has_nodes:
            self.req_handler.send_error_response(
                400, "Bad Request", "Provide exactly one of 'query' or 'nodes'"
            )
            return

        # 用法一：校验搜索语法（build_search_string 解析失败即非法）
        if has_query:
            query = body["query"]
            if not isinstance(query, str):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'query' must be a string"
                )
                return
            try:
                normalized = mw.col.build_search_string(query)
            except Exception as e:
                self.req_handler.send_error_response(
                    400, "Bad Request", f"Invalid search query: {str(e)}"
                )
                return
            resource = {
                "type": "search-query",
                "attributes": {"valid": True, "normalized": normalized},
            }
            self.send_json_response(200, {"data": resource})
            return

        # 用法二：由结构化节点构造搜索串
        nodes = body["nodes"]
        if not isinstance(nodes, list) or not nodes:
            self.req_handler.send_error_response(
                400, "Bad Request", "'nodes' must be a non-empty array"
            )
            return

        try:
            joiner = _parse_joiner(body.get("joiner", "AND"))
            search_nodes = [_build_search_node(node) for node in nodes]
        except ValueError as e:
            self.req_handler.send_error_response(400, "Bad Request", str(e))
            return

        try:
            built = mw.col.build_search_string(*search_nodes, joiner=joiner)
        except Exception as e:
            self.req_handler.send_error_response(
                400, "Bad Request", f"Invalid search nodes: {str(e)}"
            )
            return

        resource = {
            "type": "search-query",
            "attributes": {"query": built},
        }
        self.send_json_response(200, {"data": resource})
