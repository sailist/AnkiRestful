"""
笔记（Notes）API 处理模块

实现 docs/notes.md 中规划的待实现接口：
- GET   /api/notes                       扩展：query / modified_since / fields=summary
- PATCH /api/notes/<note_id>             修改字段/标签/牌组
- POST  /api/notes/batch                 批量创建
- POST  /api/notes/validate              创建前预检（不写入）
- POST  /api/notes/actions/remove-empty  清理空笔记
- PATCH /api/notes/<note_id>/notetype    更换笔记类型
- GET/POST/PUT/DELETE /api/notes/<note_id>/tags  标签子资源
- GET/DELETE /api/tags                   全部标签 / 清理未使用标签
- POST  /api/tags/actions/replace        全局重命名标签

v0.8 追加（docs/notes.md 附录「笔记与标签扩展接口」，pylib 原生能力）：
- POST /api/notes/actions/find-replace   字段内容查找替换
- GET  /api/notes/dupes                  按字段查找重复笔记
- POST /api/notes/actions/render-preview 不落库渲染预览
- POST /api/notes/actions/suspend        笔记级挂起
- POST /api/notes/actions/unsuspend      笔记级恢复
- POST /api/notes/actions/bury           笔记级搁置
- GET  /api/tags/tree                    标签层级树
- POST /api/tags/actions/attach          批量加标签（多笔记）
- POST /api/tags/actions/detach          批量移除标签（多笔记）
- POST /api/tags/actions/find-replace    标签名查找替换（支持正则）
- POST /api/tags/actions/reparent        更改标签父级

路由通过 @api.api_route 注册；/api/notes 与 /api/notes/<note_id> 以
子类重新注册的方式覆盖 api.py 中的父类处理器，未覆盖的方法
（do_POST / do_GET / do_DELETE）沿用父类实现。

注意：本模块必须在 api 模块之后导入，重新注册才能生效。
"""

import copy
import json
import logging
import re
from urllib.parse import urlparse, parse_qs, quote_plus

from aqt import mw
from anki.notes import Note
from anki.sound import SoundOrVideoTag, TTSTag
from anki.utils import strip_html

import api
import schema

logger = logging.getLogger()
logger.info("API notes module loaded")

# 批量接口逐项错误的标题映射
_STATUS_TITLES = {
    400: "Bad Request",
    404: "Not Found",
    409: "Conflict",
    500: "Internal Server Error",
    503: "Service Unavailable",
}

# 笔记字段检查状态对应的错误信息（anki.notes NoteFieldsCheckResponse.State：
# NORMAL=0 EMPTY=1 DUPLICATE=2 MISSING_CLOZE=3 NOTETYPE_NOT_CLOZE=4 FIELD_NOT_CLOZE=5）
_FIELDS_CHECK_MESSAGES = {
    1: "cannot create note because it is empty",
    2: "cannot create note because it is a duplicate",
    3: "cannot create note because it has no cloze deletions",
    4: "cannot create note because the note type is not a cloze type",
    5: "cannot create note because the field is not a cloze field",
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


def _get_note(handler, note_id):
    """按 id 获取笔记

    id 非法时发送 400、笔记不存在时发送 404，并返回 None。
    """
    nid = _parse_int(note_id)
    if nid is None:
        handler.req_handler.send_error_response(
            400, "Bad Request", f"Invalid note ID: {note_id}"
        )
        return None
    try:
        return mw.col.get_note(nid)
    except Exception:
        handler.req_handler.send_error_response(
            404, "Not Found", f"Note with ID {note_id} not found"
        )
        return None


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


def _send_note_document(handler, note_id, status_code=200):
    """以单个笔记文档响应（结构对齐 NoteDetailAPIHandler.do_GET）"""
    note = mw.col.get_note(int(note_id))
    note_data = _build_note_data(note)

    resource = schema.create_note_resource(int(note_id), note_data)
    resource.relationships = {
        "cards": schema.Relationship(
            links=schema.Links(
                self=f"/api/notes/{note_id}/relationships/cards",
                related=f"/api/notes/{note_id}/cards",
            )
        )
    }

    document = schema.NoteDocument(
        data=resource, links=schema.Links(self=f"/api/notes/{note_id}")
    )
    handler.send_json_response(status_code, document)


def _check_resource_identity(handler, data, note_id):
    """校验 JSON:API data 对象的 type 与 id（PATCH 类接口共用）

    校验通过返回 True，否则已发送错误响应并返回 False。
    """
    if data.get("type") != "notes":
        handler.req_handler.send_error_response(
            409, "Conflict", "Resource type must be 'notes'"
        )
        return False

    body_id = data.get("id")
    if body_id is not None and str(body_id) != str(note_id):
        handler.req_handler.send_error_response(
            409,
            "Conflict",
            f"Resource ID '{body_id}' does not match path ID '{note_id}'",
        )
        return False
    return True


def _is_blank_field(text):
    """判断字段内容是否为空（去除 HTML 标签与空白字符后）"""
    if not text:
        return True
    try:
        stripped = strip_html(text)
    except Exception:
        # 兜底：使用正则去除 HTML 标签
        stripped = re.sub(r"<[^>]*>", "", text).replace("&nbsp;", " ")
    return not stripped.strip()


def _prepare_note(item):
    """校验并构建待创建的 Note 对象（不写入集合）

    供批量创建与创建前预检共用。字段填充方式与单条创建保持一致
    （按字段名赋值、忽略不存在的字段名），重复/空笔记检测对齐
    AnkiConnect createNote 的 dupeOrEmpty 逻辑，支持
    attributes.options.allow_duplicate（默认 false）。

    返回 (note, deck_id, error_detail, error_status)：
    校验通过时 note 为待写入的 Note 对象；否则 note 为 None。
    """
    if not isinstance(item, dict):
        return None, None, "'data' item must be an object", 400

    if item.get("type") != "notes":
        return None, None, "Resource type must be 'notes'", 409

    attrs = item.get("attributes")
    if not isinstance(attrs, dict):
        return None, None, "'attributes' object is required", 400

    deck_id = item.get("deck_id")
    note_type_name = attrs.get("note_type")
    fields_map = attrs.get("fields", {})
    tags = attrs.get("tags", [])
    options = attrs.get("options", {})

    # 校验牌组
    if deck_id is None:
        return None, None, "'deck_id' is required", 400
    did = _parse_int(deck_id)
    if did is None:
        return None, None, "'deck_id' must be an integer", 400
    deck = mw.col.decks.get(did, default=False) if did else None
    if not deck:
        return None, None, f"Deck with ID {deck_id} not found", 404

    # 校验并获取笔记类型
    if not note_type_name:
        return None, None, "'attributes.note_type' is required", 400
    model = mw.col.models.byName(note_type_name)
    if not model:
        return None, None, f"Note type '{note_type_name}' not found", 404

    # 创建新笔记并填充字段（不写入集合）
    note = Note(mw.col, model)
    if isinstance(fields_map, dict):
        for field_name, field_value in fields_map.items():
            try:
                note[field_name] = str(field_value) if field_value is not None else ""
            except Exception:
                # 忽略不存在的字段名
                pass

    # 设置标签
    if isinstance(tags, list):
        note.tags = [str(t) for t in tags]

    # 重复/空笔记检测（对齐 AnkiConnect createNote）
    allow_duplicate = bool(options.get("allow_duplicate")) if isinstance(options, dict) else False
    state = int(note.dupeOrEmpty() or 0)
    if state == 1:
        return None, None, _FIELDS_CHECK_MESSAGES[1], 400
    if state == 2 and not allow_duplicate:
        return None, None, _FIELDS_CHECK_MESSAGES[2], 400
    if state >= 3:
        detail = _FIELDS_CHECK_MESSAGES.get(
            state, "cannot create note for unknown reason"
        )
        return None, None, detail, 400

    return note, did, None, None


# 笔记列表 / 详情（子类重新注册，覆盖 api.py 中的父类）
##########################################################################


@api.api_route("/api/notes")
class NotesAPIHandlerV2(api.NotesAPIHandler):
    """笔记列表处理器（v0.2 扩展）- /api/notes

    继承 api.NotesAPIHandler 并重新注册路由：do_GET 增加
    query（Anki 搜索语法）、modified_since（修改时间过滤）与
    fields=summary（轻量模式）查询参数；do_POST 沿用父类实现。
    """

    def do_GET(self, **params):
        """处理获取笔记列表请求（支持搜索/时间过滤/轻量模式）"""
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

            # 搜索参数（Anki 原生搜索语法，对应 findNotes）
            search_query = query_params.get("query", [""])[0]

            # 修改时间过滤（对应 notesModTime，仅返回其后修改的笔记）
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

            # 轻量模式：仅返回 id/guid/tags/modified
            fields_mode = query_params.get("fields", [None])[0]
            summary = fields_mode == "summary"
            if fields_mode is not None and not summary:
                self.req_handler.send_error_response(
                    400, "Bad Request", "'fields' only supports 'summary'"
                )
                return

            # 执行搜索，非法的搜索语法返回 400
            try:
                note_ids = list(mw.col.find_notes(search_query))
            except Exception as e:
                self.req_handler.send_error_response(
                    400, "Bad Request", f"Invalid search query: {str(e)}"
                )
                return

            # modified_since 过滤（保持搜索结果的原有顺序）
            if modified_since is not None:
                modified_ids = set(
                    mw.col.db.list(
                        "select id from notes where mod > ?", modified_since
                    )
                )
                note_ids = [nid for nid in note_ids if nid in modified_ids]

            total_notes = len(note_ids)
            offset = (page - 1) * limit
            paginated_ids = note_ids[offset : offset + limit]
            resources = []

            for note_id in paginated_ids:
                try:
                    note = mw.col.get_note(note_id)
                except Exception:
                    # 笔记在搜索后被删除等情况，跳过
                    continue

                if summary:
                    resources.append(
                        {
                            "type": "notes",
                            "id": str(note_id),
                            "attributes": {
                                "guid": note.guid,
                                "tags": note.tags,
                                "modified": note.mod,
                            },
                        }
                    )
                else:
                    note_data = _build_note_data(note)
                    resource = schema.create_note_resource(note_id, note_data)
                    resources.append(resource)

            # 创建分页链接（保留搜索/过滤参数，便于直接跟随 next/prev）
            extra_query = []
            if search_query:
                extra_query.append("query=" + quote_plus(search_query))
            if modified_since is not None:
                extra_query.append(f"modified_since={modified_since}")
            if summary:
                extra_query.append("fields=summary")
            extra_query = "&".join(extra_query)

            base_url = "/api/notes"

            def page_url(p):
                url = f"{base_url}?page={p}&limit={limit}"
                if extra_query:
                    url += "&" + extra_query
                return url

            pages = (total_notes + limit - 1) // limit
            links = schema.Links(
                self=page_url(page), first=page_url(1), last=page_url(pages)
            )

            if page > 1:
                links.prev = page_url(page - 1)

            if page < pages:
                links.next = page_url(page + 1)

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


@api.api_route("/api/notes/<note_id>")
class NoteDetailAPIHandlerV2(api.NoteDetailAPIHandler):
    """单个笔记详情处理器（v0.2 扩展）- /api/notes/{note_id}

    继承 api.NoteDetailAPIHandler 并重新注册路由，新增 do_PATCH：
    fields 按名合并更新、tags 整体替换、deck_id 移动全部卡片
    （对应 AnkiConnect updateNoteFields / updateNote / changeDeck）。
    do_GET / do_DELETE 重写父类实现：父类用 get_note 后判空做 404，
    但 get_note 对不存在 id 是抛异常而非返回 None，此处修正为正确的 404。
    """

    def do_GET(self, note_id):
        """处理获取单个笔记详情请求"""
        if not _check_collection(self):
            return
        note = _get_note(self, note_id)
        if note is None:
            return
        _send_note_document(self, note.id)

    def do_DELETE(self, note_id):
        """处理删除笔记请求"""
        if not _check_collection(self):
            return
        note = _get_note(self, note_id)
        if note is None:
            return
        # 先取好资源数据再删除（删除后 get_note 会抛异常）
        note_data = _build_note_data(note)
        nid = note.id
        mw.col.remove_notes([nid])

        resource = schema.create_note_resource(nid, note_data)
        resource.relationships = {
            "cards": schema.Relationship(
                links=schema.Links(
                    self=f"/api/notes/{nid}/relationships/cards",
                    related=f"/api/notes/{nid}/cards",
                )
            )
        }
        document = schema.NoteDocument(
            data=resource, links=schema.Links(self=f"/api/notes/{nid}")
        )
        self.send_json_response(200, document)

    def do_PATCH(self, note_id):
        """处理部分更新笔记请求"""
        if not _check_collection(self):
            return

        note = _get_note(self, note_id)
        if note is None:
            return

        # 读取并解析请求体
        body = _read_json_body(self.req_handler)
        if body is None:
            return

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'data' object is required"
            )
            return

        if not _check_resource_identity(self, data, note_id):
            return

        attrs = data.get("attributes")
        if not isinstance(attrs, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes' object is required"
            )
            return

        fields_map = attrs.get("fields")
        tags = attrs.get("tags")
        deck_id = attrs.get("deck_id")

        if fields_map is None and tags is None and deck_id is None:
            self.req_handler.send_error_response(
                400,
                "Bad Request",
                "Must provide at least one of 'fields', 'tags' or 'deck_id'",
            )
            return

        # 先完成全部校验，再统一应用，避免部分生效
        if fields_map is not None:
            if not isinstance(fields_map, dict):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'attributes.fields' must be an object"
                )
                return
            for field_name in fields_map:
                if field_name not in note:
                    self.req_handler.send_error_response(
                        400, "Bad Request", f"Unknown field name: '{field_name}'"
                    )
                    return

        if tags is not None:
            if not isinstance(tags, list) or not all(
                isinstance(t, str) for t in tags
            ):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'attributes.tags' must be a list of strings"
                )
                return

        did = None
        if deck_id is not None:
            did = _parse_int(deck_id)
            if did is None:
                self.req_handler.send_error_response(
                    400, "Bad Request", "'attributes.deck_id' must be an integer"
                )
                return
            deck = mw.col.decks.get(did, default=False) if did else None
            if not deck:
                self.req_handler.send_error_response(
                    404, "Not Found", f"Deck with ID {deck_id} not found"
                )
                return

        # fields：按字段名合并更新，未提及的字段保持不变
        updated = False
        if fields_map is not None:
            for field_name, field_value in fields_map.items():
                note[field_name] = (
                    str(field_value) if field_value is not None else ""
                )
            updated = True

        # tags：出现时整体替换（增量增删请用 /api/notes/{id}/tags 子资源）
        if tags is not None:
            note.tags = list(tags)
            updated = True

        if updated:
            mw.col.update_note(note, skip_undo_entry=True)

        # deck_id：将该笔记的全部卡片移入目标牌组（与 changeDeck 联动）
        if did is not None:
            card_ids = list(note.card_ids())
            if card_ids:
                mw.col.set_deck(card_ids, did)
                logger.info(
                    f"Moved {len(card_ids)} cards of note {note.id} to deck {did}"
                )

        _send_note_document(self, note.id)


# 批量创建 / 创建前预检 / 清理空笔记
##########################################################################


@api.api_route("/api/notes/batch")
class NoteBatchAPIHandler(api.APIHandler):
    """批量创建笔记处理器 - /api/notes/batch（对应 AnkiConnect addNotes）

    逐项处理：成功的位置返回资源标识符，失败的位置返回 null，
    并在 errors 数组中按 source.pointer 指明下标。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            self.req_handler.send_error_response(
                400, "Bad Request", "'data' must be an array"
            )
            return

        results = []
        errors = []
        created = 0

        for index, item in enumerate(data):
            note, did, error_detail, error_status = _prepare_note(item)

            # 校验通过则写入集合并创建卡片
            if note is not None:
                try:
                    mw.col.add_note(note, did)
                    # 对齐 AnkiConnect addNote：没有产出任何卡片视为失败
                    # （如条件模板对所有字段组合都不产出卡片），清理已写入的笔记
                    if not note.card_ids():
                        mw.col.remove_notes([note.id])
                        error_detail = (
                            "The field values you have provided would make "
                            "an empty question on all cards"
                        )
                        error_status = 400
                        note = None
                except Exception as e:
                    error_detail = str(e)
                    error_status = 400
                    note = None

            if note is not None:
                results.append({"type": "notes", "id": str(note.id)})
                created += 1
            else:
                results.append(None)
                errors.append(
                    {
                        "status": error_status,
                        "title": _STATUS_TITLES.get(error_status, "Error"),
                        "detail": error_detail,
                        "source": {"pointer": f"/data/{index}"},
                    }
                )

        document = {
            "data": results,
            "meta": {
                "total": len(data),
                "created": created,
                "failed": len(data) - created,
            },
        }
        if errors:
            document["errors"] = errors

        logger.info(
            f"Batch create notes: total={len(data)} created={created} "
            f"failed={len(data) - created}"
        )
        self.send_json_response(200, document)


@api.api_route("/api/notes/validate")
class NoteValidateAPIHandler(api.APIHandler):
    """创建前预检处理器 - /api/notes/validate

    对应 AnkiConnect canAddNote / canAddNotes / canAddNoteWithErrorDetail /
    canAddNotesWithErrorDetail。请求体与 POST /api/notes（单条）或
    /api/notes/batch（数组）相同，但不写入任何数据。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return

        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            items = [data]
        elif isinstance(data, list):
            items = data
        else:
            self.req_handler.send_error_response(
                400, "Bad Request", "'data' must be an object or an array"
            )
            return

        results = []
        for item in items:
            note, _did, error_detail, _error_status = _prepare_note(item)
            results.append({"valid": note is not None, "detail": error_detail})

        valid_count = sum(1 for r in results if r["valid"])
        document = {
            "data": results,
            "meta": {
                "total": len(results),
                "valid": valid_count,
                "invalid": len(results) - valid_count,
            },
        }
        self.send_json_response(200, document)


@api.api_route("/api/notes/actions/remove-empty")
class NoteRemoveEmptyAPIHandler(api.APIHandler):
    """清理空笔记处理器 - /api/notes/actions/remove-empty

    对应 AnkiConnect removeEmptyNotes。空笔记定义：所有字段在去除
    HTML 标签与空白字符后均为空的笔记。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        empty_ids = []
        for nid in mw.col.db.list("select id from notes"):
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            if all(_is_blank_field(field) for field in note.fields):
                empty_ids.append(nid)

        if empty_ids:
            mw.col.remove_notes(empty_ids)

        logger.info(f"Removed {len(empty_ids)} empty notes")
        self.send_json_response(
            200, {"data": None, "meta": {"removed": len(empty_ids)}}
        )


# 更换笔记类型
##########################################################################


@api.api_route("/api/notes/<note_id>/notetype")
class NoteNotetypeAPIHandler(api.APIHandler):
    """更换笔记类型处理器 - /api/notes/{note_id}/notetype

    对应 AnkiConnect updateNoteModel。attributes.field_map 为
    「旧字段名 -> 新字段名」的映射；缺省时按同名字段自动映射。
    标签保持不变；卡片不做模板重映射（与 updateNoteModel 行为一致）。
    """

    def do_PATCH(self, note_id):
        if not _check_collection(self):
            return

        note = _get_note(self, note_id)
        if note is None:
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

        if not _check_resource_identity(self, data, note_id):
            return

        attrs = data.get("attributes")
        if not isinstance(attrs, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes' object is required"
            )
            return

        # 校验并获取目标笔记类型
        new_type_name = attrs.get("note_type")
        if not new_type_name:
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes.note_type' is required"
            )
            return

        new_model = mw.col.models.byName(new_type_name)
        if not new_model:
            self.req_handler.send_error_response(
                404, "Not Found", f"Note type '{new_type_name}' not found"
            )
            return

        field_map = attrs.get("field_map", {})
        if not isinstance(field_map, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes.field_map' must be an object"
            )
            return

        old_model = mw.col.models.get(note.mid)
        old_field_names = (
            [field["name"] for field in old_model["flds"]] if old_model else []
        )
        new_field_names = [field["name"] for field in new_model["flds"]]

        # 未提供 field_map 时，按同名字段自动映射
        if not field_map:
            field_map = {
                name: name for name in old_field_names if name in new_field_names
            }

        # 校验字段映射的两侧都合法
        for old_name, new_name in field_map.items():
            if old_name not in old_field_names:
                self.req_handler.send_error_response(
                    400,
                    "Bad Request",
                    f"Field '{old_name}' not found in current note type",
                )
                return
            if new_name not in new_field_names:
                self.req_handler.send_error_response(
                    400,
                    "Bad Request",
                    f"Field '{new_name}' not found in note type '{new_type_name}'",
                )
                return

        # 按映射构建新字段值，未映射的新字段留空
        new_to_old = {}
        for old_name, new_name in field_map.items():
            new_to_old[new_name] = old_name

        new_fields = []
        for new_name in new_field_names:
            old_name = new_to_old.get(new_name)
            if old_name is not None and old_name in note:
                new_fields.append(note[old_name])
            else:
                new_fields.append("")

        # 更换模型（对齐 AnkiConnect updateNoteModel）
        note.mid = new_model["id"]
        note._fmap = mw.col.models.field_map(new_model)
        note.fields = new_fields
        mw.col.update_note(note, skip_undo_entry=True)

        logger.info(
            f"Changed note {note.id} notetype to '{new_type_name}' "
            f"(mid={new_model['id']})"
        )
        _send_note_document(self, note.id)


# 标签子资源与全局标签
##########################################################################


@api.api_route("/api/notes/<note_id>/tags")
class NoteTagsAPIHandler(api.APIHandler):
    """笔记标签子资源处理器 - /api/notes/{note_id}/tags

    GET 获取标签（getNoteTags）、POST 增量添加（addTags）、
    PUT 整体替换（updateNoteTags）、DELETE 移除指定标签（removeTags）。
    请求体（POST/PUT/DELETE）：{ "tags": ["标签A", "标签B"] }
    """

    def _read_tags_body(self):
        """读取标签请求体，校验失败时发送 400 并返回 None"""
        body = _read_json_body(self.req_handler)
        if body is None:
            return None

        tags = body.get("tags") if isinstance(body, dict) else None
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            self.req_handler.send_error_response(
                400, "Bad Request", "'tags' must be provided as a list of strings"
            )
            return None
        return tags

    def _send_tags(self, note):
        """返回笔记当前标签列表"""
        document = {
            "data": note.tags,
            "links": {"self": f"/api/notes/{note.id}/tags"},
            "meta": {"note_id": note.id, "total": len(note.tags)},
        }
        self.send_json_response(200, document)

    def do_GET(self, note_id):
        """处理获取笔记标签请求"""
        if not _check_collection(self):
            return

        note = _get_note(self, note_id)
        if note is None:
            return

        self._send_tags(note)

    def do_POST(self, note_id):
        """处理增量添加标签请求（对应 addTags）"""
        if not _check_collection(self):
            return

        note = _get_note(self, note_id)
        if note is None:
            return

        tags = self._read_tags_body()
        if tags is None:
            return

        mw.col.tags.bulk_add([note.id], " ".join(tags))

        # 重新读取笔记以获取最新标签
        note = mw.col.get_note(note.id)
        self._send_tags(note)

    def do_PUT(self, note_id):
        """处理整体替换标签请求（对应 updateNoteTags）"""
        if not _check_collection(self):
            return

        note = _get_note(self, note_id)
        if note is None:
            return

        tags = self._read_tags_body()
        if tags is None:
            return

        note.tags = list(tags)
        mw.col.update_note(note, skip_undo_entry=True)

        note = mw.col.get_note(note.id)
        self._send_tags(note)

    def do_DELETE(self, note_id):
        """处理移除指定标签请求（对应 removeTags）"""
        if not _check_collection(self):
            return

        note = _get_note(self, note_id)
        if note is None:
            return

        tags = self._read_tags_body()
        if tags is None:
            return

        mw.col.tags.bulk_remove([note.id], " ".join(tags))

        note = mw.col.get_note(note.id)
        self._send_tags(note)


@api.api_route("/api/tags")
class TagsAPIHandler(api.APIHandler):
    """标签列表处理器 - /api/tags

    GET 返回全部标签（getTags）；
    DELETE ?unused=true 清理未使用标签（clearUnusedTags）。
    """

    def do_GET(self):
        """处理获取全部标签请求"""
        if not _check_collection(self):
            return

        tags = mw.col.tags.all()
        document = {
            "data": tags,
            "links": {"self": "/api/tags"},
            "meta": {"total": len(tags)},
        }
        self.send_json_response(200, document)

    def do_DELETE(self):
        """处理清理未使用标签请求（对应 clearUnusedTags）"""
        if not _check_collection(self):
            return

        query_params = parse_qs(urlparse(self.req_handler.path).query)
        unused = query_params.get("unused", [""])[0].lower()
        if unused not in ("true", "1"):
            self.req_handler.send_error_response(
                400,
                "Bad Request",
                "DELETE /api/tags requires query parameter 'unused=true'",
            )
            return

        result = mw.col.tags.clear_unused_tags()
        removed = getattr(result, "count", 0)

        logger.info(f"Cleared {removed} unused tags")
        self.send_json_response(200, {"data": None, "meta": {"removed": removed}})


@api.api_route("/api/tags/actions/replace")
class TagsReplaceAPIHandler(api.APIHandler):
    """全局重命名标签处理器 - /api/tags/actions/replace

    对应 AnkiConnect replaceTagsInAllNotes：在所有笔记中精确匹配
    old_tag（不区分大小写）并替换为 new_tag；不会级联重命名
    子标签（如 old_tag::sub）。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return

        old_tag = body.get("old_tag") if isinstance(body, dict) else None
        new_tag = body.get("new_tag") if isinstance(body, dict) else None

        if not isinstance(old_tag, str) or not old_tag.strip():
            self.req_handler.send_error_response(
                400, "Bad Request", "'old_tag' must be a non-empty string"
            )
            return
        if not isinstance(new_tag, str) or not new_tag.strip():
            self.req_handler.send_error_response(
                400, "Bad Request", "'new_tag' must be a non-empty string"
            )
            return

        # 逐笔记精确替换（对齐 AnkiConnect replaceTagsInAllNotes）
        updated = 0
        for nid in mw.col.db.list("select id from notes"):
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue

            if note.has_tag(old_tag):
                note.remove_tag(old_tag)
                if not note.has_tag(new_tag):
                    note.add_tag(new_tag)
                mw.col.update_note(note, skip_undo_entry=True)
                updated += 1

        logger.info(f"Replaced tag '{old_tag}' with '{new_tag}' in {updated} notes")
        self.send_json_response(
            200, {"data": None, "meta": {"updated": updated}}
        )


# 笔记与标签扩展接口（v0.8，pylib 原生能力）
##########################################################################


def _parse_note_ids_param(value):
    """解析请求中的 note_ids 数组

    返回 (ids, error_detail)：value 为 None 时 ids 为 None（表示未提供，
    调用方按「作用于全部笔记」处理）；校验失败时 ids 为 None 且
    error_detail 非空。
    """
    if value is None:
        return None, None
    if not isinstance(value, list):
        return None, "'note_ids' must be an array of integers"
    ids = []
    for raw in value:
        nid = _parse_int(raw)
        if nid is None:
            return None, f"Invalid note ID: {raw}"
        ids.append(nid)
    return ids, None


def _read_note_ids_body(handler):
    """读取批量笔记动作请求体中的 note_ids（必须为非空整数数组）

    校验失败时发送 400 并返回 None，调用方应直接 return。
    """
    body = _read_json_body(handler.req_handler)
    if body is None:
        return None

    note_ids = body.get("note_ids") if isinstance(body, dict) else None
    if not isinstance(note_ids, list) or not note_ids:
        handler.req_handler.send_error_response(
            400, "Bad Request", "'note_ids' must be a non-empty array"
        )
        return None

    ids, error_detail = _parse_note_ids_param(note_ids)
    if error_detail:
        handler.req_handler.send_error_response(400, "Bad Request", error_detail)
        return None
    return ids


def _read_note_ids_tags_body(handler):
    """读取标签批量动作请求体（note_ids + tags）

    校验失败时发送 400 并返回 None；成功返回 (note_ids, tags)。
    """
    body = _read_json_body(handler.req_handler)
    if body is None:
        return None

    note_ids = body.get("note_ids") if isinstance(body, dict) else None
    tags = body.get("tags") if isinstance(body, dict) else None

    if not isinstance(note_ids, list) or not note_ids:
        handler.req_handler.send_error_response(
            400, "Bad Request", "'note_ids' must be a non-empty array"
        )
        return None
    if (
        not isinstance(tags, list)
        or not tags
        or not all(isinstance(t, str) for t in tags)
    ):
        handler.req_handler.send_error_response(
            400, "Bad Request", "'tags' must be a non-empty list of strings"
        )
        return None

    ids, error_detail = _parse_note_ids_param(note_ids)
    if error_detail:
        handler.req_handler.send_error_response(400, "Bad Request", error_detail)
        return None
    return ids, tags


def _card_ids_of_notes(note_ids):
    """汇总多篇笔记的全部卡片 id（取卡失败的笔记按无卡片处理）"""
    card_ids = []
    for nid in note_ids:
        try:
            card_ids.extend(mw.col.card_ids_of_note(nid))
        except Exception:
            continue
    return card_ids


def _serialize_av_tags(av_tags):
    """将渲染输出的 AVTag 列表序列化为 JSON 可编码结构"""
    result = []
    for tag in av_tags:
        if isinstance(tag, SoundOrVideoTag):
            result.append({"type": "sound_or_video", "filename": tag.filename})
        elif isinstance(tag, TTSTag):
            result.append(
                {
                    "type": "tts",
                    "field_text": tag.field_text,
                    "lang": tag.lang,
                    "voices": list(tag.voices),
                    "other_args": list(tag.other_args),
                    "speed": tag.speed,
                }
            )
        else:
            result.append({"type": "unknown", "detail": str(tag)})
    return result


def _serialize_tag_tree_node(node):
    """递归序列化 tags.tree() 返回的 TagTreeNode（name + children）"""
    return {
        "name": node.name,
        "children": [_serialize_tag_tree_node(child) for child in node.children],
    }


# 字段内容查找替换 / 重复笔记查找 / 渲染预览
##########################################################################


@api.api_route("/api/notes/actions/find-replace")
class NotesFindReplaceAPIHandler(api.APIHandler):
    """笔记字段内容查找替换处理器 - /api/notes/actions/find-replace

    对应 col.find_and_replace()：作用于笔记字段内容（与 notetypes.md 中
    作用于模板的 find-replace 不同）。note_ids 缺省时作用于全部笔记，
    响应 meta.updated 为变更笔记数。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return

        search = body.get("search") if isinstance(body, dict) else None
        replacement = body.get("replacement") if isinstance(body, dict) else None
        field_name = body.get("field_name") if isinstance(body, dict) else None

        if not isinstance(search, str) or not search:
            self.req_handler.send_error_response(
                400, "Bad Request", "'search' must be a non-empty string"
            )
            return
        if not isinstance(replacement, str):
            self.req_handler.send_error_response(
                400, "Bad Request", "'replacement' must be a string"
            )
            return
        if field_name is not None and not isinstance(field_name, str):
            self.req_handler.send_error_response(
                400, "Bad Request", "'field_name' must be a string"
            )
            return

        regex = bool(body.get("regex", False))
        match_case = bool(body.get("match_case", False))

        # note_ids 缺省时作用于全部笔记
        note_ids, error_detail = _parse_note_ids_param(body.get("note_ids"))
        if error_detail:
            self.req_handler.send_error_response(400, "Bad Request", error_detail)
            return
        if note_ids is None:
            note_ids = list(mw.col.find_notes(""))

        try:
            result = mw.col.find_and_replace(
                note_ids=note_ids,
                search=search,
                replacement=replacement,
                regex=regex,
                field_name=field_name or None,
                match_case=match_case,
            )
        except Exception as e:
            self.req_handler.send_error_response(
                400, "Bad Request", f"Error finding and replacing: {str(e)}"
            )
            return

        updated = getattr(result, "count", 0)
        logger.info(
            f"Find and replace in note fields: '{search}' -> '{replacement}', "
            f"updated={updated}"
        )
        self.send_json_response(200, {"data": None, "meta": {"updated": updated}})


@api.api_route("/api/notes/dupes")
class NoteDupesAPIHandler(api.APIHandler):
    """重复笔记查找处理器 - /api/notes/dupes

    对应 col.find_dupes(field, search)：按指定字段的值分组，
    返回有重复的组。查询参数 field（必填，字段名）与
    query（可选，在结果中进一步过滤的 Anki 搜索词）。
    """

    def do_GET(self):
        if not _check_collection(self):
            return

        query_params = parse_qs(urlparse(self.req_handler.path).query)
        field = query_params.get("field", [None])[0]
        search = query_params.get("query", [""])[0]

        if not field:
            self.req_handler.send_error_response(
                400, "Bad Request", "Query parameter 'field' is required"
            )
            return

        # 字段名不存在或搜索词非法时 find_dupes 会抛异常
        try:
            dupes = mw.col.find_dupes(field, search)
        except Exception as e:
            self.req_handler.send_error_response(
                400, "Bad Request", f"Invalid field name or query: {str(e)}"
            )
            return

        data = [
            {"value": value, "note_ids": [str(nid) for nid in note_ids]}
            for value, note_ids in dupes
        ]
        document = {
            "data": data,
            "links": {"self": f"/api/notes/dupes?field={quote_plus(field)}"},
            "meta": {
                "total": len(data),
                "notes": sum(len(group["note_ids"]) for group in data),
            },
        }
        self.send_json_response(200, document)


@api.api_route("/api/notes/actions/render-preview")
class NoteRenderPreviewAPIHandler(api.APIHandler):
    """不落库渲染预览处理器 - /api/notes/actions/render-preview

    对应 note.ephemeral_card()：按给定的笔记类型与字段内容临时渲染，
    不写入任何数据。可选 custom_template（{"front": ..., "back": ...}）
    与 css 覆盖类型默认模板做试渲染。响应结构与
    GET /api/cards/{id}/render 一致（question/answer/css/av_tags）。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return
        if not isinstance(body, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "Request body must be an object"
            )
            return

        # 校验并获取笔记类型
        note_type_name = body.get("note_type")
        if not note_type_name or not isinstance(note_type_name, str):
            self.req_handler.send_error_response(
                400, "Bad Request", "'note_type' is required"
            )
            return

        model = mw.col.models.byName(note_type_name)
        if not model:
            self.req_handler.send_error_response(
                404, "Not Found", f"Note type '{note_type_name}' not found"
            )
            return

        # 深拷贝后再套用覆盖项，避免改动集合中的类型定义
        model = copy.deepcopy(model)

        # 模板序号（标准模型按序号取模板，cloze 模型恒为 0）
        template_ord = body.get("template_ord", 0)
        template_ord = _parse_int(template_ord)
        if template_ord is None or not 0 <= template_ord < len(model["tmpls"]):
            self.req_handler.send_error_response(
                400,
                "Bad Request",
                f"'template_ord' must be an integer between 0 and "
                f"{len(model['tmpls']) - 1}",
            )
            return

        # css 覆盖类型默认样式
        css = body.get("css")
        if css is not None:
            if not isinstance(css, str):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'css' must be a string"
                )
                return
            model["css"] = css

        # custom_template 覆盖所选模板的正反面
        custom_template = body.get("custom_template")
        if custom_template is not None:
            if not isinstance(custom_template, dict):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'custom_template' must be an object"
                )
                return
            front = custom_template.get("front")
            back = custom_template.get("back")
            if front is not None and not isinstance(front, str):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'custom_template.front' must be a string"
                )
                return
            if back is not None and not isinstance(back, str):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'custom_template.back' must be a string"
                )
                return
            tmpl = model["tmpls"][template_ord]
            if front is not None:
                tmpl["qfmt"] = front
            if back is not None:
                tmpl["afmt"] = back

        fill_empty = bool(body.get("fill_empty", False))

        # 构建临时笔记并填充字段（不写入集合，未知字段名忽略）
        note = Note(mw.col, model)
        fields_map = body.get("fields", {})
        if fields_map is not None and not isinstance(fields_map, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'fields' must be an object"
            )
            return
        if isinstance(fields_map, dict):
            for field_name, field_value in fields_map.items():
                try:
                    note[field_name] = (
                        str(field_value) if field_value is not None else ""
                    )
                except Exception:
                    # 忽略不存在的字段名
                    pass

        # 不落库渲染；模板语法错误等以 400 返回
        try:
            card = note.ephemeral_card(
                ord=template_ord,
                custom_note_type=model,
                fill_empty=fill_empty,
            )
            output = card.render_output()
        except Exception as e:
            self.req_handler.send_error_response(
                400, "Bad Request", f"Error rendering preview: {str(e)}"
            )
            return

        document = {
            "data": {
                "type": "card-render",
                "attributes": {
                    "question": output.question_text,
                    "answer": output.answer_text,
                    "css": output.css,
                    "question_av_tags": _serialize_av_tags(
                        output.question_av_tags
                    ),
                    "answer_av_tags": _serialize_av_tags(output.answer_av_tags),
                },
            }
        }
        self.send_json_response(200, document)


# 笔记级调度（挂起 / 恢复 / 搁置）
##########################################################################


@api.api_route("/api/notes/actions/suspend")
class NotesSuspendAPIHandler(api.APIHandler):
    """笔记级挂起处理器 - /api/notes/actions/suspend

    对应 sched.suspend_notes()：挂起笔记的全部卡片，
    响应 meta.affected_cards 为受影响卡片数。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        note_ids = _read_note_ids_body(self)
        if note_ids is None:
            return

        # 先取各笔记的卡片，用于统计受影响卡片数
        card_ids = _card_ids_of_notes(note_ids)
        result = mw.col.sched.suspend_notes(note_ids)
        affected = getattr(result, "count", len(card_ids))

        logger.info(
            f"Suspended {len(note_ids)} notes ({affected} cards affected)"
        )
        self.send_json_response(
            200, {"data": None, "meta": {"affected_cards": affected}}
        )


@api.api_route("/api/notes/actions/unsuspend")
class NotesUnsuspendAPIHandler(api.APIHandler):
    """笔记级恢复处理器 - /api/notes/actions/unsuspend

    对应 sched.unsuspend_cards()（按笔记取卡）：恢复笔记的全部
    挂起/搁置卡片，响应 meta.affected_cards 为受影响卡片数。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        note_ids = _read_note_ids_body(self)
        if note_ids is None:
            return

        # unsuspend 以卡片为单位，先取各笔记的全部卡片
        card_ids = _card_ids_of_notes(note_ids)
        if card_ids:
            mw.col.sched.unsuspend_cards(card_ids)

        logger.info(
            f"Unsuspended {len(card_ids)} cards of {len(note_ids)} notes"
        )
        self.send_json_response(
            200, {"data": None, "meta": {"affected_cards": len(card_ids)}}
        )


@api.api_route("/api/notes/actions/bury")
class NotesBuryAPIHandler(api.APIHandler):
    """笔记级搁置处理器 - /api/notes/actions/bury

    对应 sched.bury_notes()：当日搁置笔记的全部卡片（次日自动回到
    队列），响应 meta.affected_cards 为受影响卡片数。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        note_ids = _read_note_ids_body(self)
        if note_ids is None:
            return

        # 先取各笔记的卡片，用于统计受影响卡片数
        card_ids = _card_ids_of_notes(note_ids)
        result = mw.col.sched.bury_notes(note_ids)
        affected = getattr(result, "count", len(card_ids))

        logger.info(f"Buried {len(note_ids)} notes ({affected} cards affected)")
        self.send_json_response(
            200, {"data": None, "meta": {"affected_cards": affected}}
        )


# 标签增强（层级树 / 批量增删 / 查找替换 / 更改父级）
##########################################################################


@api.api_route("/api/tags/tree")
class TagsTreeAPIHandler(api.APIHandler):
    """标签层级树处理器 - /api/tags/tree

    对应 tags.tree()：返回嵌套树结构
    [{ "name": "英语", "children": [{ "name": "听力", "children": [] }] }]。
    """

    def do_GET(self):
        if not _check_collection(self):
            return

        tree = mw.col.tags.tree()
        # 根节点 name 为空，其 children 即顶层标签
        data = [_serialize_tag_tree_node(child) for child in tree.children]

        def count_nodes(nodes):
            return sum(1 + count_nodes(node["children"]) for node in nodes)

        document = {
            "data": data,
            "links": {"self": "/api/tags/tree"},
            "meta": {"total": count_nodes(data)},
        }
        self.send_json_response(200, document)


@api.api_route("/api/tags/actions/attach")
class TagsAttachAPIHandler(api.APIHandler):
    """批量加标签处理器 - /api/tags/actions/attach

    对应 tags.bulk_add()：为多篇笔记添加标签，
    响应 meta.updated 为变更笔记数。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        parsed = _read_note_ids_tags_body(self)
        if parsed is None:
            return
        note_ids, tags = parsed

        result = mw.col.tags.bulk_add(note_ids, " ".join(tags))
        updated = getattr(result, "count", 0)

        logger.info(f"Attached tags {tags} to notes, updated={updated}")
        self.send_json_response(200, {"data": None, "meta": {"updated": updated}})


@api.api_route("/api/tags/actions/detach")
class TagsDetachAPIHandler(api.APIHandler):
    """批量移除标签处理器 - /api/tags/actions/detach

    对应 tags.bulk_remove()：从多篇笔记移除标签，
    响应 meta.updated 为变更笔记数。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        parsed = _read_note_ids_tags_body(self)
        if parsed is None:
            return
        note_ids, tags = parsed

        result = mw.col.tags.bulk_remove(note_ids, " ".join(tags))
        updated = getattr(result, "count", 0)

        logger.info(f"Detached tags {tags} from notes, updated={updated}")
        self.send_json_response(200, {"data": None, "meta": {"updated": updated}})


@api.api_route("/api/tags/actions/find-replace")
class TagsFindReplaceAPIHandler(api.APIHandler):
    """标签名查找替换处理器 - /api/tags/actions/find-replace

    对应 tags.find_and_replace()：每个标签单独匹配；replacement 为
    空字符串（或替换结果为空）时删除匹配标签。note_ids 缺省时
    作用于全部笔记，响应 meta.updated 为变更笔记数。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return

        search = body.get("search") if isinstance(body, dict) else None
        replacement = body.get("replacement") if isinstance(body, dict) else None

        if not isinstance(search, str) or not search:
            self.req_handler.send_error_response(
                400, "Bad Request", "'search' must be a non-empty string"
            )
            return
        # replacement 允许为空字符串（表示删除匹配标签）
        if not isinstance(replacement, str):
            self.req_handler.send_error_response(
                400, "Bad Request", "'replacement' must be a string"
            )
            return

        regex = bool(body.get("regex", False))
        match_case = bool(body.get("match_case", False))

        # note_ids 缺省时作用于全部笔记
        note_ids, error_detail = _parse_note_ids_param(body.get("note_ids"))
        if error_detail:
            self.req_handler.send_error_response(400, "Bad Request", error_detail)
            return
        if note_ids is None:
            note_ids = list(mw.col.find_notes(""))

        try:
            result = mw.col.tags.find_and_replace(
                note_ids, search, replacement, regex, match_case
            )
        except Exception as e:
            self.req_handler.send_error_response(
                400, "Bad Request", f"Error finding and replacing tags: {str(e)}"
            )
            return

        updated = getattr(result, "count", 0)
        logger.info(
            f"Find and replace tags: '{search}' -> '{replacement}', "
            f"updated={updated}"
        )
        self.send_json_response(200, {"data": None, "meta": {"updated": updated}})


@api.api_route("/api/tags/actions/reparent")
class TagsReparentAPIHandler(api.APIHandler):
    """更改标签父级处理器 - /api/tags/actions/reparent

    对应 tags.reparent()：将 tags 移到 new_parent 之下，
    new_parent 为空字符串表示移到顶层。
    响应 meta.updated 为变更笔记数。
    """

    def do_POST(self):
        if not _check_collection(self):
            return

        body = _read_json_body(self.req_handler)
        if body is None:
            return

        tags = body.get("tags") if isinstance(body, dict) else None
        new_parent = body.get("new_parent") if isinstance(body, dict) else None

        if (
            not isinstance(tags, list)
            or not tags
            or not all(isinstance(t, str) for t in tags)
        ):
            self.req_handler.send_error_response(
                400, "Bad Request", "'tags' must be a non-empty list of strings"
            )
            return
        # new_parent 必须出现；空字符串表示移到顶层
        if not isinstance(new_parent, str):
            self.req_handler.send_error_response(
                400, "Bad Request", "'new_parent' must be a string"
            )
            return

        try:
            result = mw.col.tags.reparent(tags, new_parent)
        except Exception as e:
            self.req_handler.send_error_response(
                400, "Bad Request", f"Error reparenting tags: {str(e)}"
            )
            return

        updated = getattr(result, "count", 0)
        logger.info(
            f"Reparented tags {tags} to '{new_parent}', updated={updated}"
        )
        self.send_json_response(200, {"data": None, "meta": {"updated": updated}})
