"""
卡片（Cards）API 处理模块

实现 docs/cards.md 中规划的接口，为卡片建立独立资源：
- GET   /api/cards                          卡片列表（分页 + query 搜索 + modified_since 过滤）
- GET   /api/cards/<card_id>                卡片详情
- GET   /api/cards/<card_id>/note           卡片所属笔记
- GET   /api/cards/<card_id>/status         状态聚合（suspended/due/intervals/ease_factor）
- POST  /api/cards/actions/status           批量状态查询
- PATCH /api/cards/<card_id>/schedule       修改调度参数
- POST  /api/cards/actions/suspend          批量挂起
- POST  /api/cards/actions/unsuspend        批量恢复
- POST  /api/cards/actions/forget           批量忘记（重置为新卡）
- POST  /api/cards/actions/relearn          批量重学
- POST  /api/cards/actions/answer           批量作答
- POST  /api/cards/actions/move             批量移入其他牌组
- GET   /api/cards/<card_id>/reviews        复习历史（start / latest 参数）
- POST  /api/cards/<card_id>/reviews        补录复习记录

v0.8 扩展接口（pylib 原生能力）：
- POST  /api/cards/actions/set-flag         设置旗帜（0-7）
- POST  /api/cards/actions/bury             当日搁置
- POST  /api/cards/actions/unbury           解除搁置
- GET   /api/cards/<card_id>/render         渲染卡片正反面 HTML
- GET   /api/cards/<card_id>/memory-state   FSRS 记忆状态（?interval= 附带 fuzz_delta）
- GET   /api/cards/<card_id>/scheduling-states  四个作答按钮的下一间隔预览
- GET   /api/cards/empty-report             空卡片报告
- POST  /api/cards/actions/remove-empty     清理空卡片
- POST  /api/cards/actions/reposition       新卡排序
- 卡片资源 attributes 增加 flag（GET /api/cards 与 GET /api/cards/<card_id>）

路由通过 @api.api_route 注册，本模块被导入时自动完成注册，无需修改 api.py。
静态动作路由（/api/cards/actions/<action>）由 api.get_api_handler 精确匹配
优先命中，不会落入动态路由 /api/cards/<card_id>。

卡片的 type/queue/due 等字段一律返回数据库原值，不做二次封装（项目设计原则）。
"""

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime
from urllib.parse import urlparse, parse_qs, quote_plus

import anki.utils
from anki.errors import NotFoundError
from anki.sound import SoundOrVideoTag, TTSTag
from aqt import mw

import api
import schema

try:
    # forget 动作依赖后端 schedule_cards_as_new 接口（对齐 AnkiConnect forgetCards）
    from anki.scheduler.base import ScheduleCardsAsNew
except Exception:
    ScheduleCardsAsNew = None

try:
    # answer 动作在调度器无 legacy answerCard 时的回退流程需要 CardAnswer
    from anki.scheduler_pb2 import CardAnswer
except Exception:
    CardAnswer = None

logger = logging.getLogger()
logger.info("API cards module loaded")

# revlog 表九列（与 AnkiConnect cardReviews / insertReviews 使用的列一致）
_REVIEW_COLUMNS = "id, cid, usn, ease, ivl, lastIvl, factor, time, type"

# 调度修改中「友好字段名 -> 数据库字段名」的映射
_SCHEDULE_FIELD_MAP = {
    "ease_factor": "factor",
    "interval": "ivl",
    "reviews": "reps",
    "template_index": "ord",
}

# 允许直改的数据库字段（对齐 AnkiConnect setSpecificValueOfCard 的白名单，去除主键 id）
_SCHEDULE_DIRECT_FIELDS = {
    "did",
    "ivl",
    "lapses",
    "left",
    "mod",
    "nid",
    "odid",
    "odue",
    "ord",
    "queue",
    "reps",
    "type",
    "usn",
}


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


def _get_card(handler, card_id):
    """按 id 获取卡片

    id 非法时发送 400、卡片不存在时发送 404，并返回 None。
    """
    cid = _parse_int(card_id)
    if cid is None:
        handler.req_handler.send_error_response(
            400, "Bad Request", f"Invalid card ID: {card_id}"
        )
        return None
    try:
        return mw.col.get_card(cid)
    except NotFoundError:
        handler.req_handler.send_error_response(
            404, "Not Found", f"Card with ID {card_id} not found"
        )
        return None


def _sched_method(snake_name, camel_name):
    """获取调度器方法（兼容新版 snake_case 与旧版 camelCase 命名）"""
    method = getattr(mw.col.sched, snake_name, None)
    if method is None:
        method = getattr(mw.col.sched, camel_name, None)
    return method


def _answer_card(card, ease):
    """作答单张卡片（ease 1-4）

    优先调用 legacy 接口 sched.answerCard(card, ease)：其内部已完成
    get_scheduling_states + build_answer + answer_card，与 AnkiConnect
    answerCards 的用法一致。注意不能直接调 sched.answer_card(card, ease)：
    v3 调度器的 answer_card 接收 protobuf CardAnswer，签名不匹配。

    调度器无 legacy answerCard 时，回退到 v3 的完整流程。
    """
    answer_card_legacy = getattr(mw.col.sched, "answerCard", None)
    if answer_card_legacy is not None:
        answer_card_legacy(card, ease)
        return

    # 回退流程：手动构建 CardAnswer 后提交
    rating = {
        1: CardAnswer.AGAIN,
        2: CardAnswer.HARD,
        3: CardAnswer.GOOD,
        4: CardAnswer.EASY,
    }[ease]
    states = mw.col._backend.get_scheduling_states(card.id)
    mw.col.sched.answer_card(
        mw.col.sched.build_answer(card=card, states=states, rating=rating)
    )


def _build_card_resource(card_id):
    """构建卡片资源对象（与 api.NoteCardsAPIHandler 的结构保持一致）

    在 schema.create_card_resource 的基础上追加 reviews 关系链接。
    """
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
    # v0.8：卡片资源 attributes 增加 flag（card.user_flag()）。
    # schema.CardAttributes 是固定字段的 dataclass，不能在其中加字段
    # （schema.py 由其他模块维护），这里将 attributes 展开为普通 dict 后追加 flag；
    # 序列化（asdict / json.dumps）对 dict 与 dataclass 的处理一致，不破坏既有结构。
    attributes = asdict(resource.attributes)
    attributes["flag"] = card.user_flag()
    resource.attributes = attributes

    resource.relationships["reviews"] = schema.Relationship(
        links=schema.Links(related=f"/api/cards/{card_id}/reviews")
    )
    return resource


def _build_note_data(note):
    """构建笔记资源属性数据（结构与 api.py 中既有实现保持一致）"""
    note_type = mw.col.models.get(note.mid)
    note_data = {
        "guid": note.guid,
        "note_type": note_type["name"] if note_type else "Unknown",
        "tags": note.tags,
        "fields": {},
        "created": note.mod,
        "modified": note.mod,
    }

    for i, field_name in enumerate(
        [field["name"] for field in note_type["flds"]] if note_type else []
    ):
        if i < len(note.fields):
            note_data["fields"][field_name] = note.fields[i]

    return note_data


def _check_resource_identity(handler, data, card_id):
    """校验 JSON:API data 对象的 type 与 id（PATCH 接口共用）

    校验通过返回 True，否则已发送错误响应并返回 False。
    """
    if data.get("type") != "cards":
        handler.req_handler.send_error_response(
            409, "Conflict", "Resource type must be 'cards'"
        )
        return False

    body_id = data.get("id")
    if body_id is not None and str(body_id) != str(card_id):
        handler.req_handler.send_error_response(
            409,
            "Conflict",
            f"Resource ID '{body_id}' does not match path ID '{card_id}'",
        )
        return False
    return True


def _read_card_ids_body(handler):
    """读取批量卡片动作请求体，要求 card_ids 为非空数组

    校验失败时已发送错误响应并返回 None；成功时返回完整请求体。
    """
    body = _read_json_body(handler.req_handler)
    if body is None:
        return None

    card_ids = body.get("card_ids") if isinstance(body, dict) else None
    if not isinstance(card_ids, list) or not card_ids:
        handler.req_handler.send_error_response(
            400, "Bad Request", "'card_ids' must be a non-empty array"
        )
        return None
    return body


def _split_valid_card_ids(card_ids):
    """拆分有效/无效卡片 id（无效指非整数或卡片不存在）

    返回 (valid_ids, skipped_ids)，保持输入顺序。
    """
    valid_ids = []
    skipped = []
    for raw in card_ids:
        cid = _parse_int(raw)
        if cid is None:
            skipped.append(raw)
            continue
        try:
            mw.col.get_card(cid)
            valid_ids.append(cid)
        except Exception:
            skipped.append(raw)
    return valid_ids, skipped


def _action_meta(succeeded, skipped):
    """构建批量动作响应的 meta（成功/失败计数 + 被跳过的 id 列表）"""
    meta = {"succeeded": succeeded, "failed": len(skipped)}
    if skipped:
        meta["skipped_card_ids"] = skipped
    return meta


def _is_card_due(card_id):
    """判断卡片当前是否到期（对齐 AnkiConnect areDue 的单卡逻辑）

    新卡片视为到期；其他卡片优先用 is:due 搜索，
    对间隔超过 20 分钟的学习卡片按最后一次复习时间推算。
    """
    if mw.col.find_cards(f"cid:{card_id} is:new"):
        return True

    rows = mw.col.db.all("select id/1000.0, ivl from revlog where cid = ?", card_id)
    if not rows:
        # 无复习记录（如调度字段被手动修改过），退化为 is:due 搜索
        return bool(mw.col.find_cards(f"cid:{card_id} is:due"))

    date, ivl = rows[-1]
    if ivl >= -1200:
        return bool(mw.col.find_cards(f"cid:{card_id} is:due"))
    return date - ivl <= time.time()


def _card_intervals(card_id):
    """返回卡片的间隔历史（对齐 AnkiConnect getIntervals 的 complete 模式）

    新卡片没有复习记录，返回空列表。
    """
    if mw.col.find_cards(f"cid:{card_id} is:new"):
        return []
    return mw.col.db.list("select ivl from revlog where cid = ?", card_id)


def _build_card_status_resource(card):
    """构建卡片状态聚合资源（对应 suspended / areDue / getIntervals / getEaseFactors）"""
    return {
        "type": "card-status",
        "id": str(card.id),
        "attributes": {
            "suspended": card.queue == -1,
            "due": _is_card_due(card.id),
            "intervals": _card_intervals(card.id),
            "ease_factor": card.factor,
        },
    }


def _build_review_resource(row):
    """把 revlog 行构建为复习记录资源

    row 列为 (_REVIEW_COLUMNS)：id, cid, usn, ease, ivl, lastIvl, factor, time, type。
    revlog.id 为毫秒时间戳，资源 id 使用原值；
    attributes.review_time 按 Unix 秒级时间戳返回（与文档约定一致）。
    """
    rid, cid, usn, ease, ivl, last_ivl, factor, duration, review_type = row
    return {
        "type": "reviews",
        "id": str(rid),
        "attributes": {
            "review_time": int(rid / 1000),
            "card_id": cid,
            "usn": usn,
            "button_pressed": ease,
            "new_interval": ivl,
            "previous_interval": last_ivl,
            "new_factor": factor,
            "review_duration": duration,
            "review_type": review_type,
        },
    }


# 卡片列表 / 详情 / 所属笔记
##########################################################################


@api.api_route("/api/cards")
class CardsAPIHandler(api.APIHandler):
    """卡片列表处理器 - /api/cards

    对应 AnkiConnect findCards + cardsInfo（分页返回卡片资源），
    modified_since 对应 cardsModTime（仅返回其后修改的卡片）。
    """

    def do_GET(self, **params):
        """处理获取卡片列表请求（支持搜索/时间过滤/分页）"""
        try:
            if not _check_collection(self):
                return

            # 获取查询参数
            query_params = parse_qs(urlparse(self.req_handler.path).query)

            # 分页参数
            page = _parse_int(query_params.get("page", [1])[0])
            limit = _parse_int(query_params.get("limit", [20])[0])
            if page is None or limit is None or page < 1 or limit < 1:
                self.req_handler.send_error_response(
                    400, "Bad Request", "'page' and 'limit' must be positive integers"
                )
                return

            # 搜索参数（Anki 原生搜索语法，对应 findCards）
            search_query = query_params.get("query", [""])[0]

            # 修改时间过滤（对应 cardsModTime，仅返回其后修改的卡片）
            modified_since = None
            raw_modified_since = query_params.get("modified_since", [None])[0]
            if raw_modified_since is not None:
                try:
                    modified_since = int(float(raw_modified_since))
                except (ValueError, TypeError):
                    self.req_handler.send_error_response(
                        400,
                        "Bad Request",
                        "'modified_since' must be a Unix timestamp",
                    )
                    return

            # 执行搜索，非法的搜索语法返回 400
            try:
                card_ids = list(mw.col.find_cards(search_query))
            except Exception as e:
                self.req_handler.send_error_response(
                    400, "Bad Request", f"Invalid search query: {str(e)}"
                )
                return

            # modified_since 过滤（保持搜索结果的原有顺序）
            if modified_since is not None:
                modified_ids = set(
                    mw.col.db.list(
                        "select id from cards where mod > ?", modified_since
                    )
                )
                card_ids = [cid for cid in card_ids if cid in modified_ids]

            total_cards = len(card_ids)
            offset = (page - 1) * limit
            paginated_ids = card_ids[offset : offset + limit]
            resources = []

            for card_id in paginated_ids:
                try:
                    resource = _build_card_resource(card_id)
                except Exception:
                    # 卡片在搜索后被删除等情况，跳过
                    continue
                resources.append(resource)

            # 创建分页链接（保留搜索/过滤参数，便于直接跟随 next/prev）
            extra_query = []
            if search_query:
                extra_query.append("query=" + quote_plus(search_query))
            if modified_since is not None:
                extra_query.append(f"modified_since={modified_since}")
            extra_query = "&".join(extra_query)

            base_url = "/api/cards"

            def page_url(p):
                url = f"{base_url}?page={p}&limit={limit}"
                if extra_query:
                    url += "&" + extra_query
                return url

            # 空结果时页数保底为 1，避免 links.last 生成 page=0 的非法链接
            pages = max(1, (total_cards + limit - 1) // limit)
            links = schema.Links(
                self=page_url(page), first=page_url(1), last=page_url(pages)
            )

            if page > 1:
                links.prev = page_url(page - 1)

            if page < pages:
                links.next = page_url(page + 1)

            # 创建文档
            document = schema.CardCollectionDocument(
                data=resources,
                links=links,
                meta={
                    "pagination": {
                        "page": page,
                        "limit": limit,
                        "total": total_cards,
                        "pages": pages,
                    }
                },
            )

            self.send_json_response(200, document)

        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error getting cards: {str(e)}"
            )


@api.api_route("/api/cards/<card_id>")
class CardDetailAPIHandler(api.APIHandler):
    """卡片详情处理器 - /api/cards/{card_id}（对应 AnkiConnect cardsInfo 的单卡场景）"""

    def do_GET(self, card_id):
        """处理获取单个卡片详情请求"""
        if not _check_collection(self):
            return

        card = _get_card(self, card_id)
        if card is None:
            return

        resource = _build_card_resource(card.id)
        document = schema.JsonApiDocument(
            data=resource, links=schema.Links(self=f"/api/cards/{card_id}")
        )
        self.send_json_response(200, document)


@api.api_route("/api/cards/<card_id>/note")
class CardNoteAPIHandler(api.APIHandler):
    """卡片所属笔记处理器 - /api/cards/{card_id}/note（对应 AnkiConnect cardsToNotes）"""

    def do_GET(self, card_id):
        """处理获取卡片所属笔记请求"""
        if not _check_collection(self):
            return

        card = _get_card(self, card_id)
        if card is None:
            return

        try:
            note = mw.col.get_note(card.nid)
        except Exception:
            self.req_handler.send_error_response(
                404, "Not Found", f"Note with ID {card.nid} not found"
            )
            return

        note_data = _build_note_data(note)
        resource = schema.create_note_resource(note.id, note_data)
        document = schema.NoteDocument(
            data=resource, links=schema.Links(self=f"/api/cards/{card_id}/note")
        )
        self.send_json_response(200, document)


# 状态聚合
##########################################################################


@api.api_route("/api/cards/<card_id>/status")
class CardStatusAPIHandler(api.APIHandler):
    """卡片状态聚合处理器 - /api/cards/{card_id}/status

    一次返回 suspended / due / intervals / ease_factor，
    对应 AnkiConnect suspended / areDue / getIntervals / getEaseFactors。
    """

    def do_GET(self, card_id):
        """处理获取卡片状态请求"""
        if not _check_collection(self):
            return

        card = _get_card(self, card_id)
        if card is None:
            return

        document = {
            "data": _build_card_status_resource(card),
            "links": {"self": f"/api/cards/{card_id}/status"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/cards/actions/status")
class CardsStatusActionAPIHandler(api.APIHandler):
    """批量卡片状态查询处理器 - /api/cards/actions/status

    对应 AnkiConnect areSuspended / areDue / getIntervals / getEaseFactors
    的批量语义：返回数组与输入顺序一一对应，无效 id 的位置为 null。
    """

    def do_POST(self):
        """
        body:
        { "card_ids": [1498938915662, 1498938915663] }
        """
        if not _check_collection(self):
            return

        body = _read_card_ids_body(self)
        if body is None:
            return

        results = []
        succeeded = 0
        for raw in body["card_ids"]:
            cid = _parse_int(raw)
            card = None
            if cid is not None:
                try:
                    card = mw.col.get_card(cid)
                except Exception:
                    card = None

            if card is None:
                results.append(None)
            else:
                results.append(_build_card_status_resource(card))
                succeeded += 1

        document = {
            "data": results,
            "meta": {
                "total": len(results),
                "succeeded": succeeded,
                "failed": len(results) - succeeded,
            },
        }
        self.send_json_response(200, document)


# 调度修改
##########################################################################


@api.api_route("/api/cards/<card_id>/schedule")
class CardScheduleAPIHandler(api.APIHandler):
    """卡片调度修改处理器 - /api/cards/{card_id}/schedule

    对应 AnkiConnect setDueDate / setEaseFactors / setSpecificValueOfCard。
    仅更新请求中出现的字段：due_date 走调度器 set_due_date
    （复习卡为天偏移如 "0"/"1"/"-7"，新卡为序号或 "!" 置顶），
    其余字段直接改写卡片数据库字段后 update_card。
    """

    def do_PATCH(self, card_id):
        """
        body:
        {
            "data": {
                "type": "cards",
                "id": "1498938915662",
                "attributes": {
                    "due_date": "0",
                    "ease_factor": 2600,
                    "interval": 30,
                    "reviews": 12,
                    "lapses": 1
                }
            }
        }
        """
        if not _check_collection(self):
            return

        card = _get_card(self, card_id)
        if card is None:
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'data' object is required"
            )
            return

        if not _check_resource_identity(self, data, card_id):
            return

        attrs = data.get("attributes")
        if not isinstance(attrs, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes' object is required"
            )
            return

        # 先完成全部校验，再统一应用，避免部分生效
        due_date = None
        field_updates = {}
        for key, value in attrs.items():
            if key == "due_date":
                if not isinstance(value, str) or not value:
                    self.req_handler.send_error_response(
                        400,
                        "Bad Request",
                        "'attributes.due_date' must be a non-empty string",
                    )
                    return
                due_date = value
            elif key in _SCHEDULE_FIELD_MAP or key in _SCHEDULE_DIRECT_FIELDS:
                parsed = _parse_int(value)
                if parsed is None:
                    self.req_handler.send_error_response(
                        400,
                        "Bad Request",
                        f"'attributes.{key}' must be an integer",
                    )
                    return
                db_key = _SCHEDULE_FIELD_MAP.get(key, key)
                field_updates[db_key] = parsed
            else:
                self.req_handler.send_error_response(
                    400, "Bad Request", f"Unknown schedule attribute: '{key}'"
                )
                return

        if due_date is None and not field_updates:
            self.req_handler.send_error_response(
                400,
                "Bad Request",
                "Must provide at least one schedulable attribute",
            )
            return

        # 数据库字段直改（对齐 setEaseFactors / setSpecificValueOfCard）
        if field_updates:
            try:
                for db_key, value in field_updates.items():
                    setattr(card, db_key, value)
                mw.col.update_card(card, skip_undo_entry=True)
            except Exception as e:
                self.req_handler.send_error_response(
                    500, "Internal Server Error", f"Error updating card: {str(e)}"
                )
                return

        # 到期日修改（对齐 setDueDate，复习卡按天偏移、新卡按序号）
        if due_date is not None:
            try:
                set_due_date = _sched_method("set_due_date", "setDueDate")
                set_due_date([card.id], due_date, config_key=None)
            except Exception as e:
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    f"Error setting due date: {str(e)}",
                )
                return

        logger.info(
            f"Updated schedule of card {card.id}: "
            f"fields={list(field_updates.keys())}, due_date={due_date}"
        )

        # 重新读取卡片以返回最新状态
        resource = _build_card_resource(card.id)
        document = schema.JsonApiDocument(
            data=resource, links=schema.Links(self=f"/api/cards/{card_id}/schedule")
        )
        self.send_json_response(200, document)


# 调度动作（批量）
##########################################################################


@api.api_route("/api/cards/actions/suspend")
class CardsSuspendAPIHandler(api.APIHandler):
    """批量挂起卡片处理器 - /api/cards/actions/suspend（对应 AnkiConnect suspend）"""

    def do_POST(self):
        """
        body:
        { "card_ids": [1498938915662, 1498938915663] }
        """
        if not _check_collection(self):
            return

        body = _read_card_ids_body(self)
        if body is None:
            return

        valid_ids, skipped = _split_valid_card_ids(body["card_ids"])

        # 过滤掉已是挂起状态的卡片（对齐 AnkiConnect suspend）
        to_suspend = [
            cid for cid in valid_ids if mw.col.get_card(cid).queue != -1
        ]

        if to_suspend:
            try:
                _sched_method("suspend_cards", "suspendCards")(to_suspend)
            except Exception as e:
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    f"Error suspending cards: {str(e)}",
                )
                return

        logger.info(f"Suspended {len(to_suspend)} cards")
        meta = _action_meta(len(to_suspend), skipped)
        unchanged = len(valid_ids) - len(to_suspend)
        if unchanged:
            meta["unchanged"] = unchanged
        self.send_json_response(200, {"data": None, "meta": meta})


@api.api_route("/api/cards/actions/unsuspend")
class CardsUnsuspendAPIHandler(api.APIHandler):
    """批量恢复卡片处理器 - /api/cards/actions/unsuspend（对应 AnkiConnect unsuspend）"""

    def do_POST(self):
        """
        body:
        { "card_ids": [1498938915662, 1498938915663] }
        """
        if not _check_collection(self):
            return

        body = _read_card_ids_body(self)
        if body is None:
            return

        valid_ids, skipped = _split_valid_card_ids(body["card_ids"])

        # 只处理当前处于挂起状态的卡片
        to_unsuspend = [
            cid for cid in valid_ids if mw.col.get_card(cid).queue == -1
        ]

        if to_unsuspend:
            try:
                _sched_method("unsuspend_cards", "unsuspendCards")(to_unsuspend)
            except Exception as e:
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    f"Error unsuspending cards: {str(e)}",
                )
                return

        logger.info(f"Unsuspended {len(to_unsuspend)} cards")
        meta = _action_meta(len(to_unsuspend), skipped)
        unchanged = len(valid_ids) - len(to_unsuspend)
        if unchanged:
            meta["unchanged"] = unchanged
        self.send_json_response(200, {"data": None, "meta": meta})


@api.api_route("/api/cards/actions/forget")
class CardsForgetAPIHandler(api.APIHandler):
    """批量忘记卡片处理器 - /api/cards/actions/forget（对应 AnkiConnect forgetCards）

    可选参数：reset_count（重置复习/遗忘计数，默认 false）、
    clear_due（不恢复原新卡序号，默认 false 即恢复原有位置）。
    """

    def do_POST(self):
        """
        body:
        { "card_ids": [1498938915662], "reset_count": false, "clear_due": true }
        """
        if not _check_collection(self):
            return

        if ScheduleCardsAsNew is None:
            self.req_handler.send_error_response(
                500,
                "Internal Server Error",
                "Current Anki version does not support schedule_cards_as_new",
            )
            return

        body = _read_card_ids_body(self)
        if body is None:
            return

        reset_count = bool(body.get("reset_count", False))
        clear_due = bool(body.get("clear_due", False))

        valid_ids, skipped = _split_valid_card_ids(body["card_ids"])

        if valid_ids:
            try:
                # 对齐 AnkiConnect forgetCards：log=True；
                # clear_due 与 restore_position 语义相反
                request = ScheduleCardsAsNew(
                    card_ids=valid_ids,
                    log=True,
                    restore_position=not clear_due,
                    reset_counts=reset_count,
                    context=None,
                )
                mw.col._backend.schedule_cards_as_new(request)
            except Exception as e:
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    f"Error forgetting cards: {str(e)}",
                )
                return

        logger.info(
            f"Forgot {len(valid_ids)} cards "
            f"(reset_count={reset_count}, clear_due={clear_due})"
        )
        self.send_json_response(
            200, {"data": None, "meta": _action_meta(len(valid_ids), skipped)}
        )


@api.api_route("/api/cards/actions/relearn")
class CardsRelearnAPIHandler(api.APIHandler):
    """批量重学卡片处理器 - /api/cards/actions/relearn（对应 AnkiConnect relearnCards）

    将复习卡放回重学队列（type=3, queue=1，与 AnkiConnect 的 SQL 直改一致）。
    """

    def do_POST(self):
        """
        body:
        { "card_ids": [1498938915662] }
        """
        if not _check_collection(self):
            return

        body = _read_card_ids_body(self)
        if body is None:
            return

        valid_ids, skipped = _split_valid_card_ids(body["card_ids"])

        if valid_ids:
            try:
                scids = anki.utils.ids2str(valid_ids)
                mw.col.db.execute(
                    "update cards set type=3, queue=1 where id in " + scids
                )
            except Exception as e:
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    f"Error relearning cards: {str(e)}",
                )
                return

        logger.info(f"Relearned {len(valid_ids)} cards")
        self.send_json_response(
            200, {"data": None, "meta": _action_meta(len(valid_ids), skipped)}
        )


@api.api_route("/api/cards/actions/answer")
class CardsAnswerAPIHandler(api.APIHandler):
    """批量作答卡片处理器 - /api/cards/actions/answer（对应 AnkiConnect answerCards）

    ease 取值 1-4（Again / Hard / Good / Easy），
    meta.results 逐项给出 true/false（与输入顺序一一对应）。
    """

    def do_POST(self):
        """
        body:
        { "answers": [ { "card_id": 1498938915662, "ease": 3 } ] }
        """
        if not _check_collection(self):
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return

        answers = body.get("answers") if isinstance(body, dict) else None
        if not isinstance(answers, list) or not answers:
            self.req_handler.send_error_response(
                400, "Bad Request", "'answers' must be a non-empty array"
            )
            return

        results = []
        for item in answers:
            ok = False
            if isinstance(item, dict):
                cid = _parse_int(item.get("card_id"))
                ease = _parse_int(item.get("ease"))
                if cid is not None and ease is not None and 1 <= ease <= 4:
                    try:
                        card = mw.col.get_card(cid)
                        card.start_timer()
                        _answer_card(card, ease)
                        ok = True
                    except Exception as e:
                        logger.warning(f"Failed to answer card {cid}: {e}")
                        ok = False
            results.append(ok)

        succeeded = sum(1 for r in results if r)
        logger.info(f"Answered {succeeded}/{len(results)} cards")
        self.send_json_response(
            200,
            {
                "data": None,
                "meta": {
                    "results": results,
                    "succeeded": succeeded,
                    "failed": len(results) - succeeded,
                },
            },
        )


@api.api_route("/api/cards/actions/move")
class CardsMoveAPIHandler(api.APIHandler):
    """批量移动卡片处理器 - /api/cards/actions/move（对应 AnkiConnect changeDeck）"""

    def do_POST(self):
        """
        body:
        { "card_ids": [1498938915662], "deck_id": 456 }
        """
        if not _check_collection(self):
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return

        card_ids = body.get("card_ids") if isinstance(body, dict) else None
        if not isinstance(card_ids, list) or not card_ids:
            self.req_handler.send_error_response(
                400, "Bad Request", "'card_ids' must be a non-empty array"
            )
            return

        deck_id = _parse_int(body.get("deck_id"))
        if deck_id is None:
            self.req_handler.send_error_response(
                400, "Bad Request", "'deck_id' must be an integer"
            )
            return

        # 校验目标牌组
        deck = mw.col.decks.get(deck_id)
        if not deck:
            self.req_handler.send_error_response(
                404, "Not Found", f"Deck with ID {deck_id} not found"
            )
            return

        valid_ids, skipped = _split_valid_card_ids(card_ids)

        if valid_ids:
            try:
                # 现代接口 set_deck 自动处理筛选牌组 odid 与 usn/mod
                # （与 api_notes.py 中移动卡片的用法一致），
                # v3 调度器上不存在 legacy 的 sched.remFromDyn
                mw.col.set_deck(valid_ids, deck_id)
            except Exception as e:
                self.req_handler.send_error_response(
                    500, "Internal Server Error", f"Error moving cards: {str(e)}"
                )
                return

        logger.info(f"Moved {len(valid_ids)} cards to deck {deck_id}")
        meta = _action_meta(len(valid_ids), skipped)
        meta["deck_id"] = deck_id
        meta["deck_name"] = deck["name"]
        self.send_json_response(200, {"data": None, "meta": meta})


# 复习历史
##########################################################################


@api.api_route("/api/cards/<card_id>/reviews")
class CardReviewsAPIHandler(api.APIHandler):
    """卡片复习历史处理器 - /api/cards/{card_id}/reviews

    GET 对应 AnkiConnect cardReviews / getReviewsOfCards / getLatestReviewID；
    POST 对应 insertReviews（补录外部复习数据）。
    """

    def do_GET(self, card_id):
        """处理获取复习历史请求

        查询参数：
        - start=YYYY-MM-DD：仅返回该日之后的记录（对应 cardReviews 的 startTS）
        - latest=true：仅返回最近一条记录的 id（对应 getLatestReviewID）
        """
        if not _check_collection(self):
            return

        card = _get_card(self, card_id)
        if card is None:
            return

        query_params = parse_qs(urlparse(self.req_handler.path).query)
        latest = query_params.get("latest", [""])[0].lower() in ("true", "1")
        start_raw = query_params.get("start", [None])[0]

        # latest=true：仅返回最近一条记录的 id
        if latest:
            latest_id = (
                mw.col.db.scalar(
                    "select max(id) from revlog where cid = ?", card.id
                )
                or 0
            )
            document = {
                "data": {"type": "reviews", "id": str(latest_id)},
                "links": {"self": f"/api/cards/{card_id}/reviews?latest=true"},
                "meta": {"card_id": card.id, "latest_review_id": latest_id},
            }
            self.send_json_response(200, document)
            return

        # start：起始日期（本地时区零点），转换为 revlog.id 的毫秒时间戳
        start_ms = None
        if start_raw:
            try:
                start_ms = int(
                    datetime.strptime(start_raw, "%Y-%m-%d").timestamp() * 1000
                )
            except (ValueError, TypeError):
                self.req_handler.send_error_response(
                    400,
                    "Bad Request",
                    "'start' must be a date in YYYY-MM-DD format",
                )
                return

        if start_ms is not None:
            rows = mw.col.db.all(
                f"select {_REVIEW_COLUMNS} from revlog where cid = ? and id > ?",
                card.id,
                start_ms,
            )
        else:
            rows = mw.col.db.all(
                f"select {_REVIEW_COLUMNS} from revlog where cid = ?", card.id
            )

        resources = [_build_review_resource(row) for row in rows]

        self_link = f"/api/cards/{card_id}/reviews"
        if start_raw:
            self_link += f"?start={start_raw}"

        document = {
            "data": resources,
            "links": {"self": self_link},
            "meta": {"card_id": card.id, "count": len(resources)},
        }
        self.send_json_response(200, document)

    def do_POST(self, card_id):
        """处理补录复习记录请求（对应 AnkiConnect insertReviews）

        全部记录校验通过后一次性写入（任一非法则整体拒绝），
        revlog.id 由 review_time（Unix 秒）乘以 1000 得到，
        usn 缺省取当前集合 usn，其余可选字段缺省为 0。
        """
        if not _check_collection(self):
            return

        card = _get_card(self, card_id)
        if card is None:
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            self.req_handler.send_error_response(
                400, "Bad Request", "'data' must be a non-empty array"
            )
            return

        # 先完成全部校验，再统一写入，避免部分生效
        rows = []
        seen_rids = set()
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                self.req_handler.send_error_response(
                    400, "Bad Request", f"'data' item at index {index} must be an object"
                )
                return

            if item.get("type") != "reviews":
                self.req_handler.send_error_response(
                    409,
                    "Conflict",
                    f"Resource type must be 'reviews' (index {index})",
                )
                return

            attrs = item.get("attributes")
            if not isinstance(attrs, dict):
                self.req_handler.send_error_response(
                    400,
                    "Bad Request",
                    f"'attributes' object is required (index {index})",
                )
                return

            review_time = attrs.get("review_time")
            if isinstance(review_time, bool) or not isinstance(
                review_time, (int, float)
            ):
                self.req_handler.send_error_response(
                    400,
                    "Bad Request",
                    "'attributes.review_time' must be a Unix timestamp in seconds"
                    f" (index {index})",
                )
                return

            button_pressed = _parse_int(attrs.get("button_pressed"))
            if button_pressed is None:
                self.req_handler.send_error_response(
                    400,
                    "Bad Request",
                    f"'attributes.button_pressed' must be an integer (index {index})",
                )
                return

            # 可选字段：缺省为 0（usn 缺省取当前集合 usn）
            optional_ints = {}
            for key in (
                "new_interval",
                "previous_interval",
                "new_factor",
                "review_duration",
                "review_type",
                "usn",
            ):
                value = attrs.get(key)
                if value is None:
                    continue
                parsed = _parse_int(value)
                if parsed is None:
                    self.req_handler.send_error_response(
                        400,
                        "Bad Request",
                        f"'attributes.{key}' must be an integer (index {index})",
                    )
                    return
                optional_ints[key] = parsed

            rid = int(review_time * 1000)
            # revlog.id 为主键（review_time×1000），与已有记录或本批记录
            # 冲突时返回 409，而非放任数据库抛 UNIQUE 约束错误变成 500
            if rid in seen_rids or mw.col.db.scalar(
                "select 1 from revlog where id = ?", rid
            ):
                self.req_handler.send_error_response(
                    409,
                    "Conflict",
                    f"Review with review_time {review_time} already exists"
                    f" (index {index})",
                )
                return
            seen_rids.add(rid)
            rows.append(
                (
                    rid,
                    card.id,
                    optional_ints.get("usn", mw.col.usn()),
                    button_pressed,
                    optional_ints.get("new_interval", 0),
                    optional_ints.get("previous_interval", 0),
                    optional_ints.get("new_factor", 0),
                    optional_ints.get("review_duration", 0),
                    optional_ints.get("review_type", 0),
                )
            )

        try:
            # 逐条参数化写入（DBProxy.execute 在各 Anki 版本均可用，比 executemany 更稳妥）
            for row in rows:
                mw.col.db.execute(
                    "insert into revlog(id,cid,usn,ease,ivl,lastIvl,factor,time,type)"
                    " values (?,?,?,?,?,?,?,?,?)",
                    *row,
                )
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error inserting reviews: {str(e)}"
            )
            return

        logger.info(f"Inserted {len(rows)} reviews for card {card.id}")
        resources = [_build_review_resource(row) for row in rows]
        document = {
            "data": resources,
            "links": {"self": f"/api/cards/{card_id}/reviews"},
            "meta": {"card_id": card.id, "inserted": len(rows)},
        }
        self.send_json_response(201, document)


# 旗帜 / 搁置（v0.8 扩展）
##########################################################################


@api.api_route("/api/cards/actions/set-flag")
class CardsSetFlagAPIHandler(api.APIHandler):
    """批量设置旗帜处理器 - /api/cards/actions/set-flag

    对应 pylib col.set_user_flag_for_cards()；flag 取值 0-7
    （0=无旗，1-7=红橙黄绿蓝粉青）。
    """

    def do_POST(self):
        """
        body:
        { "card_ids": [1498938915662, 1498938915663], "flag": 1 }
        """
        if not _check_collection(self):
            return

        body = _read_card_ids_body(self)
        if body is None:
            return

        flag = _parse_int(body.get("flag"))
        if flag is None or not 0 <= flag <= 7:
            self.req_handler.send_error_response(
                400, "Bad Request", "'flag' must be an integer between 0 and 7"
            )
            return

        valid_ids, skipped = _split_valid_card_ids(body["card_ids"])

        if valid_ids:
            try:
                # 新版方法名为 set_user_flag_for_cards，旧版为 set_user_flag，
                # 两者参数顺序一致（flag 在前）
                set_flag = getattr(mw.col, "set_user_flag_for_cards", None)
                if set_flag is None:
                    set_flag = getattr(mw.col, "set_user_flag")
                set_flag(flag, valid_ids)
            except Exception as e:
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    f"Error setting flag: {str(e)}",
                )
                return

        logger.info(f"Set flag {flag} on {len(valid_ids)} cards")
        meta = _action_meta(len(valid_ids), skipped)
        meta["flag"] = flag
        self.send_json_response(200, {"data": None, "meta": meta})


@api.api_route("/api/cards/actions/bury")
class CardsBuryAPIHandler(api.APIHandler):
    """批量搁置卡片处理器 - /api/cards/actions/bury

    对应 pylib sched.bury_cards()（manual=True，即用户手动搁置）。
    搁置仅当日有效，次日自动回到队列，与 suspend（长期挂起）语义不同。
    """

    def do_POST(self):
        """
        body:
        { "card_ids": [1498938915662, 1498938915663] }
        """
        if not _check_collection(self):
            return

        body = _read_card_ids_body(self)
        if body is None:
            return

        valid_ids, skipped = _split_valid_card_ids(body["card_ids"])

        if valid_ids:
            try:
                _sched_method("bury_cards", "buryCards")(valid_ids)
            except Exception as e:
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    f"Error burying cards: {str(e)}",
                )
                return

        logger.info(f"Buried {len(valid_ids)} cards")
        self.send_json_response(
            200, {"data": None, "meta": _action_meta(len(valid_ids), skipped)}
        )


@api.api_route("/api/cards/actions/unbury")
class CardsUnburyAPIHandler(api.APIHandler):
    """批量解除搁置处理器 - /api/cards/actions/unbury

    对应 pylib sched.unbury_cards()（底层为 restore_buried_and_suspended_cards，
    与 pylib 行为一致：被挂起的卡片也会一并恢复）。
    """

    def do_POST(self):
        """
        body:
        { "card_ids": [1498938915662, 1498938915663] }
        """
        if not _check_collection(self):
            return

        body = _read_card_ids_body(self)
        if body is None:
            return

        valid_ids, skipped = _split_valid_card_ids(body["card_ids"])

        if valid_ids:
            try:
                _sched_method("unbury_cards", "unburyCards")(valid_ids)
            except Exception as e:
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    f"Error unburying cards: {str(e)}",
                )
                return

        logger.info(f"Unburied {len(valid_ids)} cards")
        self.send_json_response(
            200, {"data": None, "meta": _action_meta(len(valid_ids), skipped)}
        )


# 渲染 / 记忆状态 / 间隔预览（v0.8 扩展）
##########################################################################


def _serialize_av_tag(tag):
    """将渲染输出的 AVTag 序列化为 JSON 友好的 dict

    card.render_output() 返回的 av_tags 是 anki.sound 的原生 dataclass
    （SoundOrVideoTag / TTSTag），并非 protobuf oneof，没有 WhichOneof 方法，
    需按类型分别处理；字段不存在时用 getattr 兜底。
    """
    if isinstance(tag, SoundOrVideoTag):
        return {"type": "sound_or_video", "filename": getattr(tag, "filename", "")}
    if isinstance(tag, TTSTag):
        return {
            "type": "tts",
            "field_text": getattr(tag, "field_text", ""),
            "lang": getattr(tag, "lang", ""),
            "voices": list(getattr(tag, "voices", None) or []),
            "speed": getattr(tag, "speed", 1.0),
            "other_args": list(getattr(tag, "other_args", None) or []),
        }
    return {"type": "unknown"}


@api.api_route("/api/cards/<card_id>/render")
class CardRenderAPIHandler(api.APIHandler):
    """卡片渲染处理器 - /api/cards/{card_id}/render

    对应 pylib card.render_output()，返回正反面 HTML、CSS 与 AV 标签，
    供无头复习客户端直接展示。
    """

    def do_GET(self, card_id):
        """处理获取卡片渲染结果请求"""
        if not _check_collection(self):
            return

        card = _get_card(self, card_id)
        if card is None:
            return

        try:
            output = card.render_output()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error rendering card: {str(e)}"
            )
            return

        resource = {
            "type": "card-render",
            "id": str(card.id),
            "attributes": {
                "question": output.question_text,
                "answer": output.answer_text,
                "css": output.css,
                "question_av_tags": [
                    _serialize_av_tag(tag) for tag in output.question_av_tags
                ],
                "answer_av_tags": [
                    _serialize_av_tag(tag) for tag in output.answer_av_tags
                ],
            },
        }
        document = {
            "data": resource,
            "links": {"self": f"/api/cards/{card_id}/render"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/cards/<card_id>/memory-state")
class CardMemoryStateAPIHandler(api.APIHandler):
    """卡片 FSRS 记忆状态处理器 - /api/cards/{card_id}/memory-state

    stability / difficulty 对应 pylib col.compute_memory_state()；
    retrievability 取自 col.card_stats_data() 的 fsrs_retrievability
    （compute_memory_state 不返回该值，与 Anki 卡片信息对话框同源）。
    非 FSRS 调度下字段可能为空（原样返回 null）。
    """

    def do_GET(self, card_id):
        """处理获取记忆状态请求

        查询参数：
        - interval=<天数>：附带 fuzz_delta（v3 调度下该间隔的 fuzz 天数偏移）
        """
        if not _check_collection(self):
            return

        card = _get_card(self, card_id)
        if card is None:
            return

        query_params = parse_qs(urlparse(self.req_handler.path).query)
        interval = None
        raw_interval = query_params.get("interval", [None])[0]
        if raw_interval is not None:
            interval = _parse_int(raw_interval)
            if interval is None or interval < 0:
                self.req_handler.send_error_response(
                    400,
                    "Bad Request",
                    "'interval' must be a non-negative integer",
                )
                return

        try:
            state = mw.col.compute_memory_state(card.id)
        except Exception as e:
            self.req_handler.send_error_response(
                500,
                "Internal Server Error",
                f"Error computing memory state: {str(e)}",
            )
            return

        # retrievability 获取失败不阻断主流程，退化为 null
        retrievability = None
        try:
            stats = mw.col.card_stats_data(card.id)
            if stats.HasField("fsrs_retrievability"):
                retrievability = stats.fsrs_retrievability
        except Exception:
            logger.warning(f"Failed to get retrievability of card {card.id}")

        attributes = {
            "stability": state.stability,
            "difficulty": state.difficulty,
            "retrievability": retrievability,
        }

        if interval is not None:
            try:
                attributes["fuzz_delta"] = mw.col.fuzz_delta(card.id, interval)
            except Exception as e:
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    f"Error computing fuzz delta: {str(e)}",
                )
                return

        resource = {
            "type": "memory-state",
            "id": str(card.id),
            "attributes": attributes,
        }
        document = {
            "data": resource,
            "links": {"self": f"/api/cards/{card_id}/memory-state"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/cards/<card_id>/scheduling-states")
class CardSchedulingStatesAPIHandler(api.APIHandler):
    """作答按钮间隔预览处理器 - /api/cards/{card_id}/scheduling-states

    对应 v3 调度器的 describe_next_states()，返回四个按钮
    （again/hard/good/easy）的 label 与 interval_text，
    供外部复习客户端展示；作答仍走 POST /api/cards/actions/answer。
    仅 v3 调度器可用（v1/v2 的 DummyScheduler 无此能力）。
    """

    # 按钮顺序与 SchedulingStates 的 again/hard/good/easy 一致；
    # label 使用 Anki 界面按钮的英文标准名（不做本地化）
    _BUTTONS = (
        ("again", "Again"),
        ("hard", "Hard"),
        ("good", "Good"),
        ("easy", "Easy"),
    )

    def do_GET(self, card_id):
        """处理获取作答按钮间隔预览请求"""
        if not _check_collection(self):
            return

        card = _get_card(self, card_id)
        if card is None:
            return

        describe = getattr(mw.col.sched, "describe_next_states", None)
        if describe is None:
            self.req_handler.send_error_response(
                400,
                "Bad Request",
                "scheduling-states requires the v3 scheduler",
            )
            return

        try:
            states = mw.col._backend.get_scheduling_states(card.id)
            texts = list(describe(states))
        except Exception as e:
            self.req_handler.send_error_response(
                500,
                "Internal Server Error",
                f"Error describing next states: {str(e)}",
            )
            return

        attributes = {}
        for (key, label), interval_text in zip(self._BUTTONS, texts):
            attributes[key] = {"label": label, "interval_text": interval_text}

        resource = {
            "type": "scheduling-states",
            "id": str(card.id),
            "attributes": attributes,
        }
        document = {
            "data": resource,
            "links": {"self": f"/api/cards/{card_id}/scheduling-states"},
        }
        self.send_json_response(200, document)


# 空卡片（v0.8 扩展）
##########################################################################


@api.api_route("/api/cards/empty-report")
class CardsEmptyReportAPIHandler(api.APIHandler):
    """空卡片报告处理器 - /api/cards/empty-report

    对应 pylib col.get_empty_cards()，返回 EmptyCardsReport：
    report 为人类可读文本（含空卡片原因），notes 为结构化明细
    （note_id / card_ids / will_delete_note）。
    """

    def do_GET(self):
        """处理获取空卡片报告请求"""
        if not _check_collection(self):
            return

        try:
            report = mw.col.get_empty_cards()
        except Exception as e:
            self.req_handler.send_error_response(
                500,
                "Internal Server Error",
                f"Error getting empty cards report: {str(e)}",
            )
            return

        notes = []
        empty_card_count = 0
        for note in report.notes:
            card_ids = list(note.card_ids)
            empty_card_count += len(card_ids)
            notes.append(
                {
                    "note_id": note.note_id,
                    "card_ids": card_ids,
                    "will_delete_note": note.will_delete_note,
                }
            )

        resource = {
            "type": "empty-cards-report",
            "id": "empty-report",
            "attributes": {
                "report": report.report,
                "notes": notes,
                "note_count": len(notes),
                "empty_card_count": empty_card_count,
            },
        }
        document = {
            "data": resource,
            "links": {"self": "/api/cards/empty-report"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/cards/actions/remove-empty")
class CardsRemoveEmptyAPIHandler(api.APIHandler):
    """清理空卡片处理器 - /api/cards/actions/remove-empty

    重新生成 EmptyCardsReport 并逐个删除其中的空卡片
    （col.remove_cards_and_orphaned_notes，与 Anki 空卡片对话框一致；
    全部卡片为空的笔记会一并删除）。
    """

    def do_POST(self):
        """处理清理空卡片请求（无需请求体），meta.removed 给出删除数量"""
        if not _check_collection(self):
            return

        try:
            report = mw.col.get_empty_cards()
        except Exception as e:
            self.req_handler.send_error_response(
                500,
                "Internal Server Error",
                f"Error getting empty cards report: {str(e)}",
            )
            return

        to_delete = []
        for note in report.notes:
            to_delete.extend(note.card_ids)

        if to_delete:
            try:
                mw.col.remove_cards_and_orphaned_notes(to_delete)
            except Exception as e:
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    f"Error removing empty cards: {str(e)}",
                )
                return

        logger.info(f"Removed {len(to_delete)} empty cards")
        self.send_json_response(
            200, {"data": None, "meta": {"removed": len(to_delete)}}
        )


# 新卡排序（v0.8 扩展）
##########################################################################


@api.api_route("/api/cards/actions/reposition")
class CardsRepositionAPIHandler(api.APIHandler):
    """新卡排序处理器 - /api/cards/actions/reposition

    对应 pylib sched.reposition_new_cards()。后四项参数可选，
    缺省值与 Anki 排序对话框初始值一致：
    starting_from=1、step_size=1、randomize=false、shift_existing=true。
    """

    def do_POST(self):
        """
        body:
        {
            "card_ids": [1498938915662],
            "starting_from": 1,
            "step_size": 1,
            "randomize": false,
            "shift_existing": true
        }
        """
        if not _check_collection(self):
            return

        body = _read_card_ids_body(self)
        if body is None:
            return

        starting_from = _parse_int(body.get("starting_from", 1))
        step_size = _parse_int(body.get("step_size", 1))
        if starting_from is None or starting_from < 0:
            self.req_handler.send_error_response(
                400,
                "Bad Request",
                "'starting_from' must be a non-negative integer",
            )
            return
        if step_size is None or step_size < 1:
            self.req_handler.send_error_response(
                400, "Bad Request", "'step_size' must be a positive integer"
            )
            return

        randomize = bool(body.get("randomize", False))
        shift_existing = bool(body.get("shift_existing", True))

        valid_ids, skipped = _split_valid_card_ids(body["card_ids"])

        if valid_ids:
            try:
                _sched_method("reposition_new_cards", "repositionNewCards")(
                    valid_ids, starting_from, step_size, randomize, shift_existing
                )
            except Exception as e:
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    f"Error repositioning cards: {str(e)}",
                )
                return

        logger.info(
            f"Repositioned {len(valid_ids)} cards "
            f"(starting_from={starting_from}, step_size={step_size}, "
            f"randomize={randomize}, shift_existing={shift_existing})"
        )
        meta = _action_meta(len(valid_ids), skipped)
        meta["starting_from"] = starting_from
        meta["step_size"] = step_size
        meta["randomize"] = randomize
        meta["shift_existing"] = shift_existing
        self.send_json_response(200, {"data": None, "meta": meta})
