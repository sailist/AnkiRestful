"""
笔记类型（NoteTypes）API 处理模块
实现 docs/notetypes.md 中标记「未实现」的全部接口：
- 笔记类型的按 id 查询与创建
- 字段（fields）的查询/添加/修改/删除
- 模板（templates）的查询/添加/整体更新/重命名排序/删除
- 样式（styling）的查询与整体替换
- 模板内查找替换动作
以及附录「笔记类型扩展接口（v0.7/v0.8）」：
- 删除/克隆笔记类型、内置蓝本列表与按蓝本创建
- 列表/详情/模板透出 use_count，详情透出并可修改 sort_field
按照 JSON:API 规范实现
"""

import json
import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

from aqt import mw

import api

logger = logging.getLogger()
logger.info("API notetypes module loaded")

try:
    from anki.consts import MODEL_CLOZE
except ImportError:
    # 兼容旧版本 Anki（MODEL_CLOZE 恒为 1）
    MODEL_CLOZE = 1

try:
    from anki.stdmodels import StockNotetypeKind
    from anki.utils import from_json_bytes
except ImportError:
    # 兼容无蓝本接口的旧版本 Anki
    StockNotetypeKind = None
    from_json_bytes = None

try:
    from anki.errors import CardTypeError, InvalidInput, TemplateError

    # 后端校验类错误（输入问题），写库时映射为 400
    _BACKEND_VALIDATION_ERRORS = (InvalidInput, TemplateError, CardTypeError)
except ImportError:
    # 兼容无对应错误类的旧版本 Anki：写库异常统一按 500 处理
    _BACKEND_VALIDATION_ERRORS = ()

# 内置蓝本：stock_kind -> pylib StockNotetypeKind 枚举名
# 顺序与 stdmodels.get_stock_notetypes 保持一致
_STOCK_KINDS = [
    ("basic", "KIND_BASIC"),
    ("basic-reversed", "KIND_BASIC_AND_REVERSED"),
    ("basic-optional-reversed", "KIND_BASIC_OPTIONAL_REVERSED"),
    ("basic-typing", "KIND_BASIC_TYPING"),
    ("cloze", "KIND_CLOZE"),
    ("image-occlusion", "KIND_IMAGE_OCCLUSION"),
]


# ---------------------------------------------------------------------------
# 通用辅助函数
# ---------------------------------------------------------------------------


def _unquote(value):
    """解码可能的 URL 编码路径参数（如中文名）"""
    try:
        return unquote(value)
    except Exception:
        return value


def _resolve_stock_kind(handler, stock_kind):
    """把 stock_kind 字符串解析为 pylib 蓝本枚举，失败时发送 400 并返回 None"""
    if StockNotetypeKind is not None:
        for slug, enum_name in _STOCK_KINDS:
            if slug == stock_kind:
                return getattr(StockNotetypeKind, enum_name, None)
    valid = ", ".join(slug for slug, _ in _STOCK_KINDS)
    handler.req_handler.send_error_response(
        400,
        "Bad Request",
        f"Unknown stock_kind '{stock_kind}', valid values: {valid}",
    )
    return None


def _get_stock_model(kind):
    """按蓝本枚举获取一份全新的内置笔记类型 dict（每次调用返回独立副本）"""
    return from_json_bytes(mw.col._backend.get_stock_notetype_legacy(kind))


def _save_model(handler, model):
    """持久化笔记类型修改，失败时发送错误响应并返回 False

    参考 AnkiConnect save_model：models.update_dict 完成实际写入并刷新缓存；
    旧版本 Anki 仅有 save，做兼容处理。

    后端校验类错误（如模板引用不存在的字段）属于输入问题，映射为 400，
    detail 仅取后端错误消息文本（BackendError.__str__ 即消息本身，不含堆栈）；
    其他意外错误返回 500，detail 同样只含 str(e)。
    返回 False 时调用方应直接 return。
    """
    models = mw.col.models
    try:
        if hasattr(models, "update_dict"):
            models.update_dict(model)
        else:
            models.save(model)
    except _BACKEND_VALIDATION_ERRORS as e:
        handler.req_handler.send_error_response(400, "Bad Request", str(e))
        return False
    except Exception as e:
        logger.error(f"Failed to save note type '{model.get('name')}': {e}")
        handler.req_handler.send_error_response(
            500, "Internal Server Error", str(e)
        )
        return False
    return True


def _add_model(handler, model):
    """把新建的笔记类型写入集合（mm.add），错误处理与 _save_model 一致

    返回 False 时调用方应直接 return。
    """
    try:
        mw.col.models.add(model)
    except _BACKEND_VALIDATION_ERRORS as e:
        handler.req_handler.send_error_response(400, "Bad Request", str(e))
        return False
    except Exception as e:
        logger.error(f"Failed to add note type '{model.get('name')}': {e}")
        handler.req_handler.send_error_response(
            500, "Internal Server Error", str(e)
        )
        return False
    return True


def _get_model_or_404(handler, notetype_id):
    """按 id 获取笔记类型，失败时发送错误响应并返回 None"""
    try:
        model_id = int(notetype_id)
    except (TypeError, ValueError):
        handler.req_handler.send_error_response(
            400, "Bad Request", f"Invalid note type id: {notetype_id}"
        )
        return None

    model = mw.col.models.get(model_id)
    if not model:
        handler.req_handler.send_error_response(
            404, "Not Found", f"Note type with ID {model_id} not found"
        )
        return None
    return model


def _read_json_body(handler):
    """读取并解析请求体 JSON，失败时发送 400 并返回 None"""
    content_length = int(handler.req_handler.headers.get("Content-Length", 0))
    if content_length <= 0:
        handler.req_handler.send_error_response(
            400, "Bad Request", "Empty request body"
        )
        return None

    raw_body = handler.req_handler.rfile.read(content_length).decode("utf-8")
    try:
        return json.loads(raw_body)
    except Exception:
        handler.req_handler.send_error_response(
            400, "Bad Request", "Invalid JSON body"
        )
        return None


def _validate_data_object(handler, body, expected_type):
    """校验 JSON:API data 对象结构与 type，失败时发送错误响应并返回 None"""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        handler.req_handler.send_error_response(
            400, "Bad Request", "'data' object is required"
        )
        return None

    if data.get("type") != expected_type:
        handler.req_handler.send_error_response(
            409, "Conflict", f"Resource type must be '{expected_type}'"
        )
        return None
    return data


def _find_field(model, field_name):
    """按名称查找字段，未找到返回 None"""
    field_map = mw.col.models.field_map(model)
    entry = field_map.get(field_name)
    if entry is None:
        return None
    return entry[1]


def _find_template(model, template_name):
    """按名称查找模板，未找到返回 None"""
    for template in model["tmpls"]:
        if template["name"] == template_name:
            return template
    return None


def _field_order(model, field_name):
    """返回字段在笔记类型中的当前位置，未找到返回 -1"""
    for i, field in enumerate(model["flds"]):
        if field["name"] == field_name:
            return i
    return -1


def _template_order(model, template_name):
    """返回模板在笔记类型中的当前位置，未找到返回 -1"""
    for i, template in enumerate(model["tmpls"]):
        if template["name"] == template_name:
            return i
    return -1


def _fields_on_templates(model):
    """计算每个模板正反面引用的字段名（参考 AnkiConnect modelFieldsOnTemplates）"""
    templates = {}
    for template in model["tmpls"]:
        fields = []
        for side in ["qfmt", "afmt"]:
            fields_for_side = []

            # based on _fieldsOnTemplate from aqt/clayout.py
            matches = re.findall("{{[^#/}]+?}}", template[side])
            for match in matches:
                # 去掉花括号与修饰符（如 cloze:、hint: 等前缀）
                match = re.sub(r"[{}]", "", match)
                match = match.split(":")[-1]

                # 背面忽略正面已出现的字段以及 FrontSide 占位符
                if match == "FrontSide" or (side == "afmt" and match in fields[0]):
                    continue
                fields_for_side.append(match)

            fields.append(fields_for_side)

        templates[template["name"]] = fields
    return templates


def _used_in_templates(model, field_name):
    """返回引用了指定字段的模板名称列表"""
    used = []
    for template_name, sides in _fields_on_templates(model).items():
        if field_name in sides[0] or field_name in sides[1]:
            used.append(template_name)
    return used


# ---------------------------------------------------------------------------
# 资源构建辅助函数（schema 中暂无对应 dataclass，使用普通 dict）
# ---------------------------------------------------------------------------


def _note_type_detail_resource(model):
    """构建完整笔记类型资源（在 byname 结构基础上另含 css/cloze 标记、使用量与排序字段）"""
    fields = model["flds"]
    # 排序/查重依据字段（v0.8，pylib models.sort_idx 即 model["sortf"]）
    sort_index = model.get("sortf", 0)
    sort_field = None
    if 0 <= sort_index < len(fields):
        sort_field = fields[sort_index]["name"]
    return {
        "type": "notetypes",
        "id": str(model["id"]),
        "attributes": {
            "name": model["name"],
            "fields": [field["name"] for field in fields],
            "templates": [template["name"] for template in model["tmpls"]],
            "css": model.get("css", ""),
            "cloze": model.get("type", 0) == MODEL_CLOZE,
            "use_count": mw.col.models.use_count(model),
            "sort_field": sort_field,
        },
        "links": {"self": f"/api/notetypes/{model['id']}"},
    }


def _field_resource(model, field):
    """构建字段资源对象（聚合字体/描述/模板引用信息）"""
    return {
        "type": "notetype-fields",
        "id": field["name"],
        "attributes": {
            "name": field["name"],
            "order": _field_order(model, field["name"]),
            "font": field.get("font", ""),
            "font_size": field.get("size", 0),
            "description": field.get("description", ""),
            "used_in_templates": _used_in_templates(model, field["name"]),
        },
    }


def _template_resource(model, template):
    """构建模板资源对象（含正反面内容、位置与卡片使用量）"""
    order = _template_order(model, template["name"])
    # 使用该模板的卡片数量（v0.8，pylib models.template_use_count）
    use_count = 0
    if model.get("id") and order >= 0:
        use_count = mw.col.models.template_use_count(model["id"], order)
    return {
        "type": "notetype-templates",
        "id": template["name"],
        "attributes": {
            "name": template["name"],
            "order": order,
            "front": template.get("qfmt", ""),
            "back": template.get("afmt", ""),
            "use_count": use_count,
        },
    }


def _styling_resource(model):
    """构建样式资源对象"""
    return {
        "type": "notetype-styling",
        "id": str(model["id"]),
        "attributes": {"css": model.get("css", "")},
    }


# ---------------------------------------------------------------------------
# 笔记类型本体
# ---------------------------------------------------------------------------


@api.api_route("/api/notetypes")
class NoteTypesCreateAPIHandler(api.NoteTypesAPIHandler):
    """笔记类型处理器 - /api/notetypes

    继承 api.py 中已实现的列表 GET，追加创建笔记类型的 POST。
    路由注册会覆盖原有 /api/notetypes 条目，GET 行为由继承保持不变。
    """

    def do_GET(self, **params):
        """笔记类型列表（v0.8：每项 attributes 在原有结构基础上增加 use_count）"""
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        mm = mw.col.models
        # 各类型被笔记使用的数量（pylib models.all_use_counts）
        use_counts = {entry.id: entry.use_count for entry in mm.all_use_counts()}

        resources = []
        for model in mm.all():
            resources.append(
                {
                    "type": "notetypes",
                    "id": str(model["id"]),
                    "attributes": {
                        "name": model["name"],
                        "fields": [field["name"] for field in model["flds"]],
                        "templates": [
                            template["name"] for template in model["tmpls"]
                        ],
                        "use_count": use_counts.get(model["id"], 0),
                    },
                    "links": {"self": f"/api/notetypes/{model['id']}"},
                }
            )

        document = {
            "data": resources,
            "links": {"self": "/api/notetypes"},
            "meta": {"total": len(resources)},
        }

        self.send_json_response(200, document)

    def do_POST(self):
        """
        创建笔记类型（参考 AnkiConnect createModel）
        body:
        {
            "data": {
                "type": "notetypes",
                "attributes": {
                    "name": "词汇卡",
                    "fields": ["单词", "释义"],
                    "css": ".card { font-family: sans-serif; }",
                    "templates": [
                        { "name": "正面", "front": "{{单词}}", "back": "{{释义}}" }
                    ],
                    "cloze": false
                }
            }
        }

        v0.8：attributes 增加可选 stock_kind（内置蓝本标识，见
        GET /api/notetypes/stock），提供时按蓝本创建，与
        fields/templates/css/cloze 互斥；此时 name 可选，缺省使用蓝本名称。
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        body = _read_json_body(self)
        if body is None:
            return

        data = _validate_data_object(self, body, "notetypes")
        if data is None:
            return

        attrs = data.get("attributes", {})
        if not isinstance(attrs, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes' object is required"
            )
            return

        name = attrs.get("name")
        in_order_fields = attrs.get("fields", [])
        css = attrs.get("css")
        card_templates = attrs.get("templates", [])
        is_cloze = bool(attrs.get("cloze", False))
        stock_kind = attrs.get("stock_kind")

        mm = mw.col.models

        if stock_kind is not None:
            # 按内置蓝本创建（v0.8），与 fields/templates/css/cloze 互斥
            if (
                attrs.get("fields") is not None
                or attrs.get("templates") is not None
                or attrs.get("css") is not None
                or attrs.get("cloze") is not None
            ):
                self.req_handler.send_error_response(
                    400,
                    "Bad Request",
                    "'stock_kind' is mutually exclusive with "
                    "'fields'/'templates'/'css'/'cloze'",
                )
                return

            kind = _resolve_stock_kind(self, str(stock_kind))
            if kind is None:
                return

            model = _get_stock_model(kind)
            if name:
                # 显式指定名称且同名类型已存在时返回 409
                name = str(name)
                if mm.byName(name):
                    self.req_handler.send_error_response(
                        409, "Conflict", f"Note type '{name}' already exists"
                    )
                    return
                model["name"] = name

            # 写入集合（mm.add 即完成保存；同名时 pylib 自动加后缀保证唯一）
            if not _add_model(self, model):
                return
        else:
            if not name:
                self.req_handler.send_error_response(
                    400, "Bad Request", "'attributes.name' is required"
                )
                return

            if not isinstance(in_order_fields, list) or len(in_order_fields) == 0:
                self.req_handler.send_error_response(
                    400,
                    "Bad Request",
                    "Must provide at least one field for 'fields'",
                )
                return

            if not isinstance(card_templates, list) or len(card_templates) == 0:
                self.req_handler.send_error_response(
                    400,
                    "Bad Request",
                    "Must provide at least one template for 'templates'",
                )
                return

            # 同名类型已存在时返回 409
            if mm.byName(name):
                self.req_handler.send_error_response(
                    409, "Conflict", f"Note type '{name}' already exists"
                )
                return

            # 创建新笔记类型
            model = mm.new(name)
            if is_cloze:
                model["type"] = MODEL_CLOZE

            # 按顺序创建字段
            for field_name in in_order_fields:
                field = mm.new_field(str(field_name))
                mm.add_field(model, field)

            # 提供 css 时使用，否则保留 Anki 默认样式
            if css is not None:
                model["css"] = str(css)

            # 创建卡片模板
            card_count = 1
            for card in card_templates:
                if not isinstance(card, dict):
                    self.req_handler.send_error_response(
                        400, "Bad Request", "Each template must be an object"
                    )
                    return

                card_name = card.get("name") or f"Card {card_count}"
                template = mm.new_template(str(card_name))
                card_count += 1
                template["qfmt"] = str(card.get("front", ""))
                template["afmt"] = str(card.get("back", ""))
                mm.add_template(model, template)

            # 写入集合（mm.add 即完成保存，无需 update_dict）
            if not _add_model(self, model):
                return

        logger.info(f"Created note type: {model['name']} (id={model['id']})")

        document = {
            "data": _note_type_detail_resource(model),
            "links": {"self": f"/api/notetypes/{model['id']}"},
        }

        self.send_json_response(201, document)


@api.api_route("/api/notetypes/<notetype_id>")
class NoteTypeDetailAPIHandler(api.APIHandler):
    """笔记类型详情处理器 - /api/notetypes/{notetype_id}"""

    def do_GET(self, notetype_id):
        """按 id 查询笔记类型，返回 name/fields/templates/css/cloze"""
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        document = {
            "data": _note_type_detail_resource(model),
            "links": {"self": f"/api/notetypes/{model['id']}"},
        }

        self.send_json_response(200, document)

    def do_PATCH(self, notetype_id):
        """
        修改排序/查重依据字段（v0.8，pylib models.set_sort_index）
        body:
        { "data": { "type": "notetypes",
                    "attributes": { "sort_field": "单词" } } }
        sort_field 为字段名或字段下标（从 0 开始）。
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        body = _read_json_body(self)
        if body is None:
            return

        data = _validate_data_object(self, body, "notetypes")
        if data is None:
            return

        attrs = data.get("attributes", {})
        if not isinstance(attrs, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes' object is required"
            )
            return

        if "sort_field" not in attrs:
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes.sort_field' is required"
            )
            return

        sort_field = attrs["sort_field"]
        mm = mw.col.models

        # 字段名与下标均可：全数字字符串与 JSON 数字按下标解析，
        # 其余字符串按字段名解析；显式拒绝 bool 与非整数值
        # （bool 是 int 子类、int(1.9) 会静默取整，需先排除）
        sort_index = None
        if isinstance(sort_field, bool):
            pass
        elif isinstance(sort_field, int):
            sort_index = sort_field
        elif isinstance(sort_field, float):
            if sort_field.is_integer():
                sort_index = int(sort_field)
        elif isinstance(sort_field, str):
            if sort_field.isdigit():
                sort_index = int(sort_field)
            else:
                entry = mm.field_map(model).get(sort_field)
                if entry is None:
                    self.req_handler.send_error_response(
                        400,
                        "Bad Request",
                        f"Field '{sort_field}' not found in note type "
                        f"'{model['name']}'",
                    )
                    return
                sort_index = entry[0]

        if sort_index is None:
            self.req_handler.send_error_response(
                400,
                "Bad Request",
                "'sort_field' should be a field name or index",
            )
            return

        # set_sort_index 内部校验下标范围，越界时抛异常
        try:
            mm.set_sort_index(model, sort_index)
        except Exception:
            self.req_handler.send_error_response(
                400, "Bad Request", f"Invalid sort field index: {sort_index}"
            )
            return

        if not _save_model(self, model):
            return

        logger.info(
            f"Set sort field of note type {model['name']} to index {sort_index}"
        )

        document = {
            "data": _note_type_detail_resource(model),
            "links": {"self": f"/api/notetypes/{model['id']}"},
        }

        self.send_json_response(200, document)

    def do_DELETE(self, notetype_id):
        """删除笔记类型（v0.7，pylib models.remove 语义：连同全部笔记与卡片）

        查询参数 force 默认 false：类型仍被笔记使用时返回 409
        （detail 含 use_count）；force=true 时强制删除。
        响应 meta 给出 { "deleted": "类型名", "removed_notes": 12 }。
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        query_params = parse_qs(urlparse(self.req_handler.path).query)
        force = query_params.get("force", ["false"])[0].lower() == "true"

        mm = mw.col.models
        use_count = mm.use_count(model)

        if use_count > 0 and not force:
            self.req_handler.send_error_response(
                409,
                "Conflict",
                f"Note type '{model['name']}' is still used by "
                f"{use_count} note(s) (use_count={use_count}); "
                f"pass force=true to delete it anyway",
            )
            return

        deleted_name = model["name"]
        mm.remove(model["id"])

        logger.info(
            f"Deleted note type '{deleted_name}' (id={model['id']}), "
            f"removed {use_count} note(s)"
        )

        document = {
            "data": None,
            "meta": {"deleted": deleted_name, "removed_notes": use_count},
            "links": {"self": "/api/notetypes"},
        }

        self.send_json_response(200, document)


# ---------------------------------------------------------------------------
# 字段（Fields）
# ---------------------------------------------------------------------------


@api.api_route("/api/notetypes/<notetype_id>/fields")
class NoteTypeFieldsAPIHandler(api.APIHandler):
    """笔记类型字段列表处理器 - /api/notetypes/{notetype_id}/fields"""

    def do_GET(self, notetype_id):
        """聚合返回字段的完整信息（名称/位置/字体/描述/模板引用）"""
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        resources = [_field_resource(model, field) for field in model["flds"]]

        document = {
            "data": resources,
            "links": {"self": f"/api/notetypes/{model['id']}/fields"},
            "meta": {"notetype_id": model["id"], "count": len(resources)},
        }

        self.send_json_response(200, document)

    def do_POST(self, notetype_id):
        """
        添加字段（参考 AnkiConnect modelFieldAdd，新字段追加到末尾）
        body:
        { "data": { "type": "notetype-fields", "attributes": { "name": "音标" } } }
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        body = _read_json_body(self)
        if body is None:
            return

        data = _validate_data_object(self, body, "notetype-fields")
        if data is None:
            return

        attrs = data.get("attributes", {})
        field_name = attrs.get("name") if isinstance(attrs, dict) else None
        if not field_name:
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes.name' is required"
            )
            return
        field_name = str(field_name)

        # 同名字段已存在时返回 409
        if _find_field(model, field_name) is not None:
            self.req_handler.send_error_response(
                409, "Conflict", f"Field '{field_name}' already exists"
            )
            return

        mm = mw.col.models
        field = mm.new_field(field_name)
        mm.add_field(model, field)
        if not _save_model(self, model):
            return

        logger.info(f"Added field '{field_name}' to note type {model['name']}")

        document = {
            "data": _field_resource(model, field),
            "links": {
                "self": f"/api/notetypes/{model['id']}/fields/{field_name}"
            },
        }

        self.send_json_response(201, document)


@api.api_route("/api/notetypes/<notetype_id>/fields/<field_name>")
class NoteTypeFieldDetailAPIHandler(api.APIHandler):
    """单个字段处理器 - /api/notetypes/{notetype_id}/fields/{field_name}"""

    def do_PATCH(self, notetype_id, field_name):
        """
        按需更新字段属性（参考 AnkiConnect modelFieldRename /
        modelFieldReposition / modelFieldSetFont / modelFieldSetFontSize /
        modelFieldSetDescription）
        body:
        {
            "data": {
                "type": "notetype-fields",
                "attributes": {
                    "new_name": "Word",
                    "order": 1,
                    "font": "Times New Roman",
                    "font_size": 24,
                    "description": "目标单词"
                }
            }
        }
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        decoded_name = _unquote(field_name)
        field = _find_field(model, decoded_name)
        if field is None:
            self.req_handler.send_error_response(
                404,
                "Not Found",
                f"Field '{decoded_name}' not found in note type '{model['name']}'",
            )
            return

        body = _read_json_body(self)
        if body is None:
            return

        data = _validate_data_object(self, body, "notetype-fields")
        if data is None:
            return

        attrs = data.get("attributes", {})
        if not isinstance(attrs, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes' object is required"
            )
            return

        mm = mw.col.models

        # 重命名
        new_name = attrs.get("new_name")
        if new_name is not None:
            new_name = str(new_name)
            if new_name != field["name"] and _find_field(model, new_name) is not None:
                self.req_handler.send_error_response(
                    409, "Conflict", f"Field '{new_name}' already exists"
                )
                return
            mm.rename_field(model, field, new_name)

        # 调整位置
        if "order" in attrs:
            try:
                new_index = int(attrs["order"])
            except (TypeError, ValueError):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'order' should be an integer"
                )
                return
            mm.reposition_field(model, field, new_index)

        # 设置字体
        if "font" in attrs:
            if not isinstance(attrs["font"], str):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'font' should be a string"
                )
                return
            field["font"] = attrs["font"]

        # 设置字号
        if "font_size" in attrs:
            if not isinstance(attrs["font_size"], int):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'font_size' should be an integer"
                )
                return
            field["size"] = attrs["font_size"]

        # 设置描述（旧版本 Anki 无 description 键时忽略）
        if "description" in attrs:
            if not isinstance(attrs["description"], str):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'description' should be a string"
                )
                return
            if "description" in field:
                field["description"] = attrs["description"]

        if not _save_model(self, model):
            return

        document = {
            "data": _field_resource(model, field),
            "links": {
                "self": f"/api/notetypes/{model['id']}/fields/{field['name']}"
            },
        }

        self.send_json_response(200, document)

    def do_DELETE(self, notetype_id, field_name):
        """删除字段（参考 AnkiConnect modelFieldRemove）

        字段仍被模板引用时返回 409 并列出引用它的模板。
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        decoded_name = _unquote(field_name)
        field = _find_field(model, decoded_name)
        if field is None:
            self.req_handler.send_error_response(
                404,
                "Not Found",
                f"Field '{decoded_name}' not found in note type '{model['name']}'",
            )
            return

        # 笔记类型至少保留一个字段
        if len(model["flds"]) <= 1:
            self.req_handler.send_error_response(
                400, "Bad Request", "Cannot remove the last field of a note type"
            )
            return

        # 字段仍被模板引用时返回 409 并列出引用模板
        used = _used_in_templates(model, decoded_name)
        if used:
            self.req_handler.send_error_response(
                409,
                "Conflict",
                f"Field '{decoded_name}' is used in templates: {', '.join(used)}",
            )
            return

        resource = _field_resource(model, field)

        mm = mw.col.models
        mm.remove_field(model, field)
        if not _save_model(self, model):
            return

        logger.info(f"Removed field '{decoded_name}' from note type {model['name']}")

        document = {
            "data": resource,
            "links": {"self": f"/api/notetypes/{model['id']}/fields"},
        }

        self.send_json_response(200, document)


# ---------------------------------------------------------------------------
# 模板（Templates）
# ---------------------------------------------------------------------------


@api.api_route("/api/notetypes/<notetype_id>/templates")
class NoteTypeTemplatesAPIHandler(api.APIHandler):
    """笔记类型模板列表处理器 - /api/notetypes/{notetype_id}/templates"""

    def do_GET(self, notetype_id):
        """返回模板内容（名称/位置/正面/背面）"""
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        resources = [_template_resource(model, t) for t in model["tmpls"]]

        document = {
            "data": resources,
            "links": {"self": f"/api/notetypes/{model['id']}/templates"},
            "meta": {"notetype_id": model["id"], "count": len(resources)},
        }

        self.send_json_response(200, document)

    def do_POST(self, notetype_id):
        """
        添加模板（参考 AnkiConnect modelTemplateAdd）
        body:
        {
            "data": {
                "type": "notetype-templates",
                "attributes": { "name": "反面", "front": "{{释义}}", "back": "{{单词}}" }
            }
        }
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        # 填空题类型仅支持单个模板
        if model.get("type", 0) == MODEL_CLOZE:
            self.req_handler.send_error_response(
                400, "Bad Request", "Cloze note types support only one template"
            )
            return

        body = _read_json_body(self)
        if body is None:
            return

        data = _validate_data_object(self, body, "notetype-templates")
        if data is None:
            return

        attrs = data.get("attributes", {})
        template_name = attrs.get("name") if isinstance(attrs, dict) else None
        if not template_name:
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes.name' is required"
            )
            return
        template_name = str(template_name)

        # 同名模板已存在时返回 409
        if _find_template(model, template_name) is not None:
            self.req_handler.send_error_response(
                409, "Conflict", f"Template '{template_name}' already exists"
            )
            return

        mm = mw.col.models
        template = mm.new_template(template_name)
        template["qfmt"] = str(attrs.get("front", ""))
        template["afmt"] = str(attrs.get("back", ""))
        mm.add_template(model, template)
        if not _save_model(self, model):
            return

        logger.info(f"Added template '{template_name}' to note type {model['name']}")

        document = {
            "data": _template_resource(model, template),
            "links": {
                "self": f"/api/notetypes/{model['id']}/templates/{template_name}"
            },
        }

        self.send_json_response(201, document)

    def do_PUT(self, notetype_id):
        """
        按名称整体更新多个模板的内容（参考 AnkiConnect updateModelTemplates）
        body:
        {
            "data": [
                { "type": "notetype-templates", "id": "正面",
                  "attributes": { "front": "...", "back": "..." } }
            ]
        }
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        body = _read_json_body(self)
        if body is None:
            return

        data_list = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data_list, list):
            self.req_handler.send_error_response(
                400, "Bad Request", "'data' array is required"
            )
            return

        # 先校验全部条目，避免部分更新
        pending = []
        for item in data_list:
            if not isinstance(item, dict):
                self.req_handler.send_error_response(
                    400, "Bad Request", "Each item of 'data' must be an object"
                )
                return
            if item.get("type") != "notetype-templates":
                self.req_handler.send_error_response(
                    409, "Conflict", "Resource type must be 'notetype-templates'"
                )
                return
            item_name = item.get("id")
            item_attrs = item.get("attributes", {})
            if not item_name and isinstance(item_attrs, dict):
                item_name = item_attrs.get("name")
            if not item_name:
                self.req_handler.send_error_response(
                    400, "Bad Request", "'id' is required for each template item"
                )
                return
            if not isinstance(item_attrs, dict):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'attributes' object is required"
                )
                return
            pending.append((str(item_name), item_attrs))

        # 按名称匹配并更新出现的内容（未知名称跳过，与 AnkiConnect 一致）
        updated = 0
        for item_name, item_attrs in pending:
            template = _find_template(model, item_name)
            if template is None:
                logger.info(
                    f"Template '{item_name}' not found in note type "
                    f"{model['name']}, skipped"
                )
                continue
            if "front" in item_attrs:
                template["qfmt"] = str(item_attrs["front"])
            if "back" in item_attrs:
                template["afmt"] = str(item_attrs["back"])
            updated += 1

        if not _save_model(self, model):
            return

        resources = [_template_resource(model, t) for t in model["tmpls"]]

        document = {
            "data": resources,
            "links": {"self": f"/api/notetypes/{model['id']}/templates"},
            "meta": {"notetype_id": model["id"], "updated": updated},
        }

        self.send_json_response(200, document)


@api.api_route("/api/notetypes/<notetype_id>/templates/<template_name>")
class NoteTypeTemplateDetailAPIHandler(api.APIHandler):
    """单个模板处理器 - /api/notetypes/{notetype_id}/templates/{template_name}"""

    def do_PATCH(self, notetype_id, template_name):
        """
        模板重命名/排序（参考 AnkiConnect modelTemplateRename /
        modelTemplateReposition）
        body:
        { "data": { "type": "notetype-templates",
                    "attributes": { "new_name": "逆向", "order": 1 } } }
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        decoded_name = _unquote(template_name)
        template = _find_template(model, decoded_name)
        if template is None:
            self.req_handler.send_error_response(
                404,
                "Not Found",
                f"Template '{decoded_name}' not found in note type '{model['name']}'",
            )
            return

        body = _read_json_body(self)
        if body is None:
            return

        data = _validate_data_object(self, body, "notetype-templates")
        if data is None:
            return

        attrs = data.get("attributes", {})
        if not isinstance(attrs, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes' object is required"
            )
            return

        mm = mw.col.models

        # 重命名
        new_name = attrs.get("new_name")
        if new_name is not None:
            new_name = str(new_name)
            if new_name != template["name"] and _find_template(model, new_name) is not None:
                self.req_handler.send_error_response(
                    409, "Conflict", f"Template '{new_name}' already exists"
                )
                return
            template["name"] = new_name

        # 调整位置
        if "order" in attrs:
            try:
                new_index = int(attrs["order"])
            except (TypeError, ValueError):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'order' should be an integer"
                )
                return
            mm.reposition_template(model, template, new_index)

        if not _save_model(self, model):
            return

        document = {
            "data": _template_resource(model, template),
            "links": {
                "self": f"/api/notetypes/{model['id']}/templates/{template['name']}"
            },
        }

        self.send_json_response(200, document)

    def do_DELETE(self, notetype_id, template_name):
        """删除模板（参考 AnkiConnect modelTemplateRemove）

        笔记类型至少保留一个模板，删除最后一个时返回 400。
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        decoded_name = _unquote(template_name)
        template = _find_template(model, decoded_name)
        if template is None:
            self.req_handler.send_error_response(
                404,
                "Not Found",
                f"Template '{decoded_name}' not found in note type '{model['name']}'",
            )
            return

        # 笔记类型至少保留一个模板
        if len(model["tmpls"]) <= 1:
            self.req_handler.send_error_response(
                400,
                "Bad Request",
                "Cannot remove the last template of a note type",
            )
            return

        resource = _template_resource(model, template)

        mm = mw.col.models
        mm.remove_template(model, template)
        if not _save_model(self, model):
            return

        logger.info(
            f"Removed template '{decoded_name}' from note type {model['name']}"
        )

        document = {
            "data": resource,
            "links": {"self": f"/api/notetypes/{model['id']}/templates"},
        }

        self.send_json_response(200, document)


# ---------------------------------------------------------------------------
# 样式（Styling）
# ---------------------------------------------------------------------------


@api.api_route("/api/notetypes/<notetype_id>/styling")
class NoteTypeStylingAPIHandler(api.APIHandler):
    """笔记类型样式处理器 - /api/notetypes/{notetype_id}/styling"""

    def do_GET(self, notetype_id):
        """获取样式（参考 AnkiConnect modelStyling）"""
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        document = {
            "data": _styling_resource(model),
            "links": {"self": f"/api/notetypes/{model['id']}/styling"},
        }

        self.send_json_response(200, document)

    def do_PUT(self, notetype_id):
        """
        整体替换 CSS（参考 AnkiConnect updateModelStyling）
        body:
        { "data": { "type": "notetype-styling",
                    "attributes": { "css": ".card { font-size: 22px; }" } } }
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        body = _read_json_body(self)
        if body is None:
            return

        data = _validate_data_object(self, body, "notetype-styling")
        if data is None:
            return

        attrs = data.get("attributes", {})
        css = attrs.get("css") if isinstance(attrs, dict) else None
        if not isinstance(css, str):
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes.css' is required and must be a string"
            )
            return

        model["css"] = css
        if not _save_model(self, model):
            return

        document = {
            "data": _styling_resource(model),
            "links": {"self": f"/api/notetypes/{model['id']}/styling"},
        }

        self.send_json_response(200, document)


# ---------------------------------------------------------------------------
# 查找替换动作
# ---------------------------------------------------------------------------


@api.api_route("/api/notetypes/<notetype_id>/actions/find-replace")
class NoteTypeFindReplaceAPIHandler(api.APIHandler):
    """模板内查找替换处理器 - /api/notetypes/{notetype_id}/actions/find-replace"""

    def do_POST(self, notetype_id):
        """
        在模板正反面内容中查找替换（参考 AnkiConnect findAndReplaceInModels）
        body:
        { "find": "{{FrontSide}}", "replace": "", "front": true, "back": true }
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        body = _read_json_body(self)
        if body is None:
            return

        if not isinstance(body, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "Request body must be a JSON object"
            )
            return

        find_text = body.get("find")
        if not isinstance(find_text, str) or find_text == "":
            self.req_handler.send_error_response(
                400, "Bad Request", "'find' is required and must be a non-empty string"
            )
            return

        replace_text = body.get("replace", "")
        if not isinstance(replace_text, str):
            self.req_handler.send_error_response(
                400, "Bad Request", "'replace' must be a string"
            )
            return

        do_front = bool(body.get("front", True))
        do_back = bool(body.get("back", True))
        do_css = bool(body.get("css", False))

        # 统计发生替换的模板数量
        replaced = 0
        for template in model.get("tmpls", []):
            changed = False
            if do_front and find_text in template["qfmt"]:
                changed = True
                template["qfmt"] = template["qfmt"].replace(find_text, replace_text)
            if do_back and find_text in template["afmt"]:
                changed = True
                template["afmt"] = template["afmt"].replace(find_text, replace_text)
            if changed:
                replaced += 1

        css_replaced = False
        if do_css and find_text in model.get("css", ""):
            css_replaced = True
            model["css"] = model["css"].replace(find_text, replace_text)

        if replaced > 0 or css_replaced:
            if not _save_model(self, model):
                return

        logger.info(
            f"Find-replace in note type {model['name']}: "
            f"{replaced} template(s) updated"
        )

        document = {
            "data": None,
            "meta": {
                "notetype_id": model["id"],
                "notetype_name": model["name"],
                "replaced": replaced,
            },
            "links": {
                "self": f"/api/notetypes/{model['id']}/actions/find-replace"
            },
        }

        self.send_json_response(200, document)


# ---------------------------------------------------------------------------
# 内置蓝本与克隆（v0.8）
# ---------------------------------------------------------------------------


@api.api_route("/api/notetypes/stock")
class NoteTypeStockAPIHandler(api.APIHandler):
    """内置笔记类型蓝本处理器 - /api/notetypes/stock

    静态路由，精确匹配优先于 /api/notetypes/<notetype_id> 动态路由。
    """

    def do_GET(self):
        """返回内置类型的 stock_kind 与名称（pylib stdmodels.get_stock_notetypes）"""
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        resources = []
        for slug, enum_name in _STOCK_KINDS:
            kind = (
                getattr(StockNotetypeKind, enum_name, None)
                if StockNotetypeKind is not None
                else None
            )
            if kind is None:
                continue
            stock_model = _get_stock_model(kind)
            resources.append(
                {
                    "type": "notetype-stocks",
                    "id": slug,
                    "attributes": {
                        "stock_kind": slug,
                        "name": stock_model["name"],
                    },
                }
            )

        document = {
            "data": resources,
            "links": {"self": "/api/notetypes/stock"},
            "meta": {"count": len(resources)},
        }

        self.send_json_response(200, document)


@api.api_route("/api/notetypes/<notetype_id>/actions/copy")
class NoteTypeCopyAPIHandler(api.APIHandler):
    """笔记类型克隆处理器 - /api/notetypes/{notetype_id}/actions/copy"""

    def do_POST(self, notetype_id):
        """
        克隆笔记类型（v0.8，pylib models.copy）
        body 可选：{ "name": "新名称" }，缺省时 pylib 自动加后缀。
        响应 201 返回新类型资源。
        """
        if not mw.col:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "Collection not available"
            )
            return

        model = _get_model_or_404(self, notetype_id)
        if not model:
            return

        # body 可选：仅在存在请求体时解析 name
        new_name = None
        content_length = int(self.req_handler.headers.get("Content-Length", 0))
        if content_length > 0:
            body = _read_json_body(self)
            if body is None:
                return
            if not isinstance(body, dict):
                self.req_handler.send_error_response(
                    400, "Bad Request", "Request body must be a JSON object"
                )
                return
            new_name = body.get("name")
            if new_name is not None:
                new_name = str(new_name)
                if not new_name:
                    self.req_handler.send_error_response(
                        400, "Bad Request", "'name' must be a non-empty string"
                    )
                    return
                # 同名类型已存在时返回 409
                if mw.col.models.byName(new_name):
                    self.req_handler.send_error_response(
                        409, "Conflict", f"Note type '{new_name}' already exists"
                    )
                    return

        mm = mw.col.models

        # 深拷贝并立即写入集合（pylib 自动为副本名称加后缀）
        cloned = mm.copy(model)

        # 指定名称时改回目标名称并持久化
        if new_name:
            cloned["name"] = new_name
            if not _save_model(self, cloned):
                return

        logger.info(
            f"Copied note type {model['name']} to {cloned['name']} "
            f"(id={cloned['id']})"
        )

        document = {
            "data": _note_type_detail_resource(cloned),
            "links": {"self": f"/api/notetypes/{cloned['id']}"},
        }

        self.send_json_response(201, document)
