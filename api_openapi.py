"""
OpenAPI 文档端点模块

提供 GET /api/openapi.json：遍历 api.API_ROUTES 中当前已注册的路由，
动态生成符合 OpenAPI 3.1 规范的文档（普通 dict）并以 JSON 返回。

路由元数据（summary / tags / 查询参数 / 请求体 / 响应）集中维护在
_ROUTE_METADATA 中，键与 API_ROUTES 一致（<param> 形式路径）。
缺失元数据的路由在模块加载与文档生成时仅记录 warning，并输出占位
summary，保证本端点在任何情况下可用。

路由通过 @api.api_route 注册，本模块被导入时自动完成注册，无需修改 api.py。
"""

import logging
import re

import api

logger = logging.getLogger()
logger.info("API openapi module loaded")

# OpenAPI 规范版本与服务地址（与 docs/README.md 的 Base URL 约定一致）
_OPENAPI_VERSION = "3.1.0"
_SERVER_URL = "http://localhost:8102"

# 处理器方法反射顺序（与 api.py 根端点的枚举方式一致，含 PATCH）
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# 常用错误响应描述
_R400 = "请求参数或请求体校验失败"
_R401 = "未认证（未登录 AnkiWeb 或 API Key 无效）"
_R404 = "目标资源不存在"
_R409 = "资源冲突（type 不符 / 重复 / 违反约束）"
_R500 = "服务器内部错误"
_R501 = "该能力尚未实现"
_R502 = "同步失败（上游网关错误）"
_R503 = "Collection 未加载"


# 元数据辅助构造函数
##########################################################################


def _qp(name, description, schema_type="string", default=None, enum=None,
        required=False):
    """构造一个查询参数描述（OpenAPI Parameter Object）"""
    schema = {"type": schema_type}
    if default is not None:
        schema["default"] = default
    if enum:
        schema["enum"] = enum
    return {
        "name": name,
        "in": "query",
        "required": required,
        "schema": schema,
        "description": description,
    }


def _pagination_params():
    """统一的分页查询参数（page/limit，默认 1/20）"""
    return [
        _qp("page", "页码（从 1 开始）", "integer", default=1),
        _qp("limit", "每页条数", "integer", default=20),
    ]


def _note_list_params():
    """GET /api/notes 的查询参数"""
    return _pagination_params() + [
        _qp("query", "Anki 原生搜索语法（如 deck:CS tag:英语 is:due）"),
        _qp("modified_since", "仅返回该 Unix 秒级时间戳之后修改的笔记", "integer"),
        _qp("fields", "轻量模式：仅返回 id/guid/tags/modified", enum=["summary"]),
    ]


def _card_list_params():
    """GET /api/cards 的查询参数"""
    return _pagination_params() + [
        _qp("query", "Anki 原生搜索语法（如 deck:CS is:due）"),
        _qp("modified_since", "仅返回该 Unix 秒级时间戳之后修改的卡片", "integer"),
    ]


def _graph_params():
    """统计图端点（forecast/intervals/hourly/card-types）的公共查询参数"""
    return [
        _qp("deck_id", "限定统计范围的牌组 ID（含子牌组），缺省为整个集合",
            "integer"),
        _qp("period", "统计周期", enum=["month", "year", "life"],
            default="month"),
        _qp("format", "输出格式（json 暂未实现，请求时返回 501）",
            enum=["html", "json"], default="html"),
    ]


# 路径参数描述（schema 统一为 string，整数语义在 description 中说明）
_PATH_PARAM_DESCRIPTIONS = {
    "note_id": "笔记 ID（整数）",
    "card_id": "卡片 ID（整数）",
    "deck_id": "牌组 ID（整数）",
    "config_id": "牌组配置组 ID（整数）",
    "notetype_id": "笔记类型 ID（整数）",
    "note_type_name": "笔记类型名称（需 URL 编码）",
    "field_name": "字段名称（需 URL 编码）",
    "template_name": "模板名称（需 URL 编码）",
    "filename": "媒体文件名（需 URL 编码）",
    "key": "配置键名（需 URL 编码）",
}


# 公共结构（components.schemas）
##########################################################################

_COMPONENT_SCHEMAS = {
    # ---- JSON:API 信封 ----
    "JsonApiDocument": {
        "type": "object",
        "description": "JSON:API 顶层文档信封（data/errors/meta/links）",
        "properties": {
            "data": {
                "description": "资源对象、资源对象数组或 null",
                "oneOf": [
                    {"type": "object"},
                    {"type": "array", "items": {"type": "object"}},
                    {"type": "null"},
                ],
            },
            "errors": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/Error"},
            },
            "meta": {
                "type": "object",
                "properties": {
                    "pagination": {
                        "$ref": "#/components/schemas/PaginationMeta"
                    },
                },
            },
            "links": {"$ref": "#/components/schemas/Links"},
        },
    },
    "Links": {
        "type": "object",
        "description": "JSON:API 链接集合",
        "properties": {
            "self": {"type": "string"},
            "related": {"type": "string"},
            "first": {"type": "string"},
            "last": {"type": "string"},
            "prev": {"type": "string"},
            "next": {"type": "string"},
        },
    },
    "Error": {
        "type": "object",
        "description": "错误响应（单个错误对象，与既有实现一致）",
        "properties": {
            "id": {"type": ["string", "null"]},
            "status": {"type": "integer"},
            "code": {"type": "string"},
            "title": {"type": "string"},
            "detail": {"type": "string"},
            "source": {"type": "object"},
            "meta": {"type": "object"},
        },
    },
    "PaginationMeta": {
        "type": "object",
        "description": "分页元信息（位于 meta.pagination）",
        "properties": {
            "page": {"type": "integer"},
            "limit": {"type": "integer"},
            "total": {"type": "integer"},
            "pages": {"type": "integer"},
        },
    },
    # ---- 资源对象 ----
    "NoteResource": {
        "type": "object",
        "description": "笔记资源对象",
        "properties": {
            "type": {"type": "string", "const": "notes"},
            "id": {"type": "string", "description": "笔记 ID（整数的字符串形式）"},
            "attributes": {
                "type": "object",
                "properties": {
                    "guid": {"type": "string"},
                    "note_type": {"type": "string", "description": "笔记类型名称"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "fields": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "字段名 -> 字段内容（HTML）",
                    },
                    "created": {"type": "integer", "description": "Unix 秒级时间戳"},
                    "modified": {"type": "integer", "description": "Unix 秒级时间戳"},
                },
            },
            "relationships": {"type": "object"},
            "links": {"$ref": "#/components/schemas/Links"},
        },
    },
    "CardResource": {
        "type": "object",
        "description": "卡片资源对象（type/queue/due 等字段为数据库原值）",
        "properties": {
            "type": {"type": "string", "const": "cards"},
            "id": {"type": "string", "description": "卡片 ID（整数的字符串形式）"},
            "attributes": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "integer"},
                    "deck_id": {"type": "integer"},
                    "deck_name": {"type": "string"},
                    "template_index": {"type": "integer"},
                    "type": {"type": "integer", "description": "卡片类型（数据库原值）"},
                    "queue": {"type": "integer", "description": "队列（数据库原值）"},
                    "interval": {"type": "integer"},
                    "ease_factor": {"type": "integer"},
                    "reviews": {"type": "integer"},
                    "lapses": {"type": "integer"},
                    "due": {"type": "integer", "description": "到期值（含义随卡片状态变化）"},
                    "flag": {"type": "integer", "description": "旗帜（0-7）"},
                },
            },
            "relationships": {"type": "object"},
            "links": {"$ref": "#/components/schemas/Links"},
        },
    },
    "DeckResource": {
        "type": "object",
        "description": "牌组资源对象",
        "properties": {
            "type": {"type": "string", "const": "decks"},
            "id": {"type": "string", "description": "牌组 ID（整数的字符串形式）"},
            "attributes": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "review_count": {"type": "integer"},
                    "new_count": {"type": "integer"},
                    "learning_count": {"type": "integer"},
                    "total_cards": {"type": "integer"},
                    "total_notes": {"type": "integer"},
                    "deck_config_id": {"type": "integer"},
                    "deck_config_name": {"type": "string"},
                    "created": {"type": "integer"},
                    "modified": {"type": "integer"},
                    "is_filtered": {"type": "boolean", "description": "是否筛选牌组"},
                },
            },
            "relationships": {"type": "object"},
            "links": {"$ref": "#/components/schemas/Links"},
        },
    },
    "NoteTypeResource": {
        "type": "object",
        "description": "笔记类型资源对象",
        "properties": {
            "type": {"type": "string", "const": "notetypes"},
            "id": {"type": "string", "description": "笔记类型 ID（整数的字符串形式）"},
            "attributes": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "fields": {"type": "array", "items": {"type": "string"}},
                    "templates": {"type": "array", "items": {"type": "string"}},
                    "use_count": {"type": "integer", "description": "使用该类型的笔记数"},
                    "css": {"type": "string"},
                    "cloze": {"type": "boolean"},
                    "sort_field": {"type": ["string", "null"]},
                },
            },
            "links": {"$ref": "#/components/schemas/Links"},
        },
    },
    # ---- 笔记/标签写操作请求体 ----
    "NoteCreateRequest": {
        "type": "object",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "deck_id", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "notes"},
                    "deck_id": {"type": "integer", "description": "目标牌组 ID"},
                    "attributes": {
                        "type": "object",
                        "required": ["note_type"],
                        "properties": {
                            "note_type": {"type": "string", "description": "笔记类型名称"},
                            "fields": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
    },
    "NotePatchRequest": {
        "type": "object",
        "description": "fields 按名合并更新、tags 整体替换、deck_id 移动全部卡片；"
        "三者至少提供一个",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "notes"},
                    "id": {"type": "string", "description": "提供时必须与路径一致"},
                    "attributes": {
                        "type": "object",
                        "properties": {
                            "fields": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                            "tags": {"type": "array", "items": {"type": "string"}},
                            "deck_id": {"type": "integer"},
                        },
                    },
                },
            },
        },
    },
    "NoteBatchCreateRequest": {
        "type": "object",
        "description": "批量创建（逐项处理，失败位置返回 null 并在 errors 中指明下标）",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["type", "deck_id", "attributes"],
                    "properties": {
                        "type": {"type": "string", "const": "notes"},
                        "deck_id": {"type": "integer"},
                        "attributes": {
                            "type": "object",
                            "required": ["note_type"],
                            "properties": {
                                "note_type": {"type": "string"},
                                "fields": {
                                    "type": "object",
                                    "additionalProperties": {"type": "string"},
                                },
                                "tags": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "options": {
                                    "type": "object",
                                    "properties": {
                                        "allow_duplicate": {"type": "boolean"},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    "NoteValidateRequest": {
        "type": "object",
        "description": "创建前预检（不写入）；data 为单条笔记对象或其数组，"
        "结构同 NoteCreateRequest",
        "required": ["data"],
        "properties": {
            "data": {
                "oneOf": [
                    {"type": "object"},
                    {"type": "array", "items": {"type": "object"}},
                ],
            },
        },
    },
    "NoteNotetypeChangeRequest": {
        "type": "object",
        "description": "更换笔记类型；field_map 缺省时按同名字段自动映射",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "notes"},
                    "attributes": {
                        "type": "object",
                        "required": ["note_type"],
                        "properties": {
                            "note_type": {
                                "type": "string",
                                "description": "目标笔记类型名称",
                            },
                            "field_map": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                                "description": "旧字段名 -> 新字段名",
                            },
                        },
                    },
                },
            },
        },
    },
    "TagsWriteRequest": {
        "type": "object",
        "required": ["tags"],
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "标签列表（POST 增量添加 / PUT 整体替换 / DELETE 移除）",
            },
        },
    },
    "TagReplaceRequest": {
        "type": "object",
        "required": ["old_tag", "new_tag"],
        "properties": {
            "old_tag": {"type": "string", "description": "被替换的标签（精确匹配）"},
            "new_tag": {"type": "string"},
        },
    },
    "NoteIdsActionRequest": {
        "type": "object",
        "required": ["note_ids"],
        "properties": {
            "note_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
            },
        },
    },
    "NoteIdsTagsActionRequest": {
        "type": "object",
        "required": ["note_ids", "tags"],
        "properties": {
            "note_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
        },
    },
    "FindReplaceRequest": {
        "type": "object",
        "description": "查找替换（笔记字段内容或标签名共用）；note_ids 缺省时"
        "作用于全部笔记",
        "required": ["search", "replacement"],
        "properties": {
            "search": {"type": "string", "minLength": 1},
            "replacement": {"type": "string", "description": "允许为空字符串"},
            "note_ids": {"type": "array", "items": {"type": "integer"}},
            "field_name": {"type": "string", "description": "限定单个字段（仅笔记）"},
            "regex": {"type": "boolean", "default": False},
            "match_case": {"type": "boolean", "default": False},
        },
    },
    "TagsReparentRequest": {
        "type": "object",
        "required": ["tags", "new_parent"],
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
            "new_parent": {
                "type": "string",
                "description": "新父级标签名；空字符串表示移到顶层",
            },
        },
    },
    "NoteRenderPreviewRequest": {
        "type": "object",
        "description": "不落库渲染预览（note.ephemeral_card）",
        "required": ["note_type"],
        "properties": {
            "note_type": {"type": "string", "description": "笔记类型名称"},
            "fields": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
            "template_ord": {"type": "integer", "default": 0},
            "css": {"type": "string", "description": "覆盖类型默认样式"},
            "custom_template": {
                "type": "object",
                "properties": {
                    "front": {"type": "string"},
                    "back": {"type": "string"},
                },
            },
            "fill_empty": {"type": "boolean", "default": False},
        },
    },
}

# 牌组 / 笔记类型 / 卡片 / 媒体 / 系统写操作请求体
_COMPONENT_SCHEMAS.update({
    # ---- 牌组写操作请求体 ----
    "DeckCreateRequest": {
        "type": "object",
        "description": "创建牌组（幂等；支持 :: 层级命名）；attributes.filtered "
        "存在时创建筛选牌组，与普通创建互斥",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "decks"},
                    "attributes": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "filtered": {
                                "type": "object",
                                "properties": {
                                    "search": {"type": "string"},
                                    "search_2": {"type": "string"},
                                    "limit": {"type": "integer", "default": 100},
                                    "order": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "maximum": 9,
                                        "default": 0,
                                    },
                                    "reschedule": {"type": "boolean", "default": True},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    "DeckPatchRequest": {
        "type": "object",
        "description": "重命名 / 改父级；name 与 parent_id 至少提供一个，"
        "parent_id 为 0 表示移到顶层",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "decks"},
                    "id": {"type": "string", "description": "提供时必须与路径一致"},
                    "attributes": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "parent_id": {"type": "integer"},
                        },
                    },
                },
            },
        },
    },
    "DeckMoveCardsRequest": {
        "type": "object",
        "required": ["card_ids", "target_deck_id"],
        "properties": {
            "card_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
            },
            "target_deck_id": {"type": "integer"},
        },
    },
    "DeckConfigPutRequest": {
        "type": "object",
        "description": "完整牌组配置对象（必须含 maxTaken 键；结构参考 "
        "GET /api/decks/{deck_id}/config 的响应 attributes），"
        "也兼容 JSON:API 包裹的 data.attributes 形式",
        "required": ["maxTaken"],
        "properties": {
            "id": {"type": "integer", "description": "必须与牌组当前配置组一致"},
            "name": {"type": "string"},
            "maxTaken": {"type": "integer"},
            "new": {"type": "object"},
            "rev": {"type": "object"},
            "lapse": {"type": "object"},
        },
        "additionalProperties": True,
    },
    "DeckConfigPatchRequest": {
        "type": "object",
        "description": "切换牌组使用的配置组",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "id"],
                "properties": {
                    "type": {"type": "string", "const": "deck-configs"},
                    "id": {"type": "string", "description": "目标配置组 ID"},
                },
            },
        },
    },
    "DeckConfigCloneRequest": {
        "type": "object",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "deck-configs"},
                    "attributes": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string"},
                            "clone_from": {
                                "type": "integer",
                                "default": 1,
                                "description": "被克隆的配置组 ID",
                            },
                        },
                    },
                },
            },
        },
    },
    "DeckUnburyRequest": {
        "type": "object",
        "description": "请求体可省略，省略时按 mode=all 处理",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["all", "user_only", "sched_only"],
                "default": "all",
            },
        },
    },
    "DeckExtendLimitsRequest": {
        "type": "object",
        "required": ["new", "rev"],
        "properties": {
            "new": {"type": "integer", "minimum": 0, "description": "新卡上限增量"},
            "rev": {"type": "integer", "minimum": 0, "description": "复习上限增量"},
        },
    },
    "CustomStudyRequest": {
        "type": "object",
        "description": "自定义学习请求；以下字段恰好提供一个：new_limit_delta / "
        "review_limit_delta / forgot_days / review_ahead_days / preview_days / cram",
        "properties": {
            "deck_id": {"type": "integer", "description": "提供时必须与路径一致"},
            "new_limit_delta": {"type": "integer"},
            "review_limit_delta": {"type": "integer"},
            "forgot_days": {"type": "integer", "minimum": 0},
            "review_ahead_days": {"type": "integer", "minimum": 0},
            "preview_days": {"type": "integer", "minimum": 0},
            "cram": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 3,
                        "default": 0,
                        "description": "0=到期顺序 1=添加顺序 2=随机复习 3=全部随机",
                    },
                    "card_limit": {"type": "integer", "default": 100},
                    "tags_to_include": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "tags_to_exclude": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
    # ---- 笔记类型写操作请求体 ----
    "NoteTypeCreateRequest": {
        "type": "object",
        "description": "创建笔记类型；attributes.stock_kind（内置蓝本，见 "
        "GET /api/notetypes/stock）提供时按蓝本创建，与 "
        "fields/templates/css/cloze 互斥",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "notetypes"},
                    "attributes": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "按蓝本创建时可选，缺省用蓝本名称",
                            },
                            "fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                            },
                            "templates": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "front": {"type": "string"},
                                        "back": {"type": "string"},
                                    },
                                },
                            },
                            "css": {"type": "string"},
                            "cloze": {"type": "boolean", "default": False},
                            "stock_kind": {
                                "type": "string",
                                "enum": [
                                    "basic",
                                    "basic-reversed",
                                    "basic-optional-reversed",
                                    "basic-typing",
                                    "cloze",
                                    "image-occlusion",
                                ],
                            },
                        },
                    },
                },
            },
        },
    },
    "NoteTypeSortFieldRequest": {
        "type": "object",
        "description": "修改排序/查重依据字段",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "notetypes"},
                    "attributes": {
                        "type": "object",
                        "required": ["sort_field"],
                        "properties": {
                            "sort_field": {
                                "description": "字段名或字段下标（从 0 开始）",
                                "oneOf": [
                                    {"type": "string"},
                                    {"type": "integer"},
                                ],
                            },
                        },
                    },
                },
            },
        },
    },
    "FieldAddRequest": {
        "type": "object",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "notetype-fields"},
                    "attributes": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    },
                },
            },
        },
    },
    "FieldPatchRequest": {
        "type": "object",
        "description": "按需更新字段属性（重命名/位置/字体/字号/描述）",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "notetype-fields"},
                    "attributes": {
                        "type": "object",
                        "properties": {
                            "new_name": {"type": "string"},
                            "order": {"type": "integer"},
                            "font": {"type": "string"},
                            "font_size": {"type": "integer"},
                            "description": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
    "TemplateAddRequest": {
        "type": "object",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "notetype-templates"},
                    "attributes": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string"},
                            "front": {"type": "string"},
                            "back": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
    "TemplatesUpdateRequest": {
        "type": "object",
        "description": "按名称整体更新多个模板内容（未知名称跳过）",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["type", "id"],
                    "properties": {
                        "type": {"type": "string", "const": "notetype-templates"},
                        "id": {"type": "string", "description": "模板名称"},
                        "attributes": {
                            "type": "object",
                            "properties": {
                                "front": {"type": "string"},
                                "back": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
    "TemplatePatchRequest": {
        "type": "object",
        "description": "模板重命名/排序",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "notetype-templates"},
                    "attributes": {
                        "type": "object",
                        "properties": {
                            "new_name": {"type": "string"},
                            "order": {"type": "integer"},
                        },
                    },
                },
            },
        },
    },
    "StylingUpdateRequest": {
        "type": "object",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "notetype-styling"},
                    "attributes": {
                        "type": "object",
                        "required": ["css"],
                        "properties": {"css": {"type": "string"}},
                    },
                },
            },
        },
    },
    "NoteTypeFindReplaceRequest": {
        "type": "object",
        "required": ["find"],
        "properties": {
            "find": {"type": "string", "minLength": 1},
            "replace": {"type": "string", "default": ""},
            "front": {"type": "boolean", "default": True},
            "back": {"type": "boolean", "default": True},
            "css": {"type": "boolean", "default": False},
        },
    },
    "NoteTypeCopyRequest": {
        "type": "object",
        "description": "请求体可省略；缺省时由 pylib 自动为副本名称加后缀",
        "properties": {
            "name": {"type": "string", "minLength": 1},
        },
    },
    # ---- 卡片写操作请求体 ----
    "CardIdsActionRequest": {
        "type": "object",
        "required": ["card_ids"],
        "properties": {
            "card_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
            },
        },
    },
    "CardForgetRequest": {
        "type": "object",
        "required": ["card_ids"],
        "properties": {
            "card_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
            },
            "reset_count": {
                "type": "boolean",
                "default": False,
                "description": "重置复习/遗忘计数",
            },
            "clear_due": {
                "type": "boolean",
                "default": False,
                "description": "不恢复原新卡序号",
            },
        },
    },
    "CardAnswersRequest": {
        "type": "object",
        "required": ["answers"],
        "properties": {
            "answers": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["card_id", "ease"],
                    "properties": {
                        "card_id": {"type": "integer"},
                        "ease": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 4,
                            "description": "1=Again 2=Hard 3=Good 4=Easy",
                        },
                    },
                },
            },
        },
    },
    "CardsMoveRequest": {
        "type": "object",
        "required": ["card_ids", "deck_id"],
        "properties": {
            "card_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
            },
            "deck_id": {"type": "integer", "description": "目标牌组 ID"},
        },
    },
    "CardsSetFlagRequest": {
        "type": "object",
        "required": ["card_ids", "flag"],
        "properties": {
            "card_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
            },
            "flag": {
                "type": "integer",
                "minimum": 0,
                "maximum": 7,
                "description": "0=无旗，1-7=红橙黄绿蓝粉青",
            },
        },
    },
    "CardsRepositionRequest": {
        "type": "object",
        "required": ["card_ids"],
        "properties": {
            "card_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
            },
            "starting_from": {"type": "integer", "minimum": 0, "default": 1},
            "step_size": {"type": "integer", "minimum": 1, "default": 1},
            "randomize": {"type": "boolean", "default": False},
            "shift_existing": {"type": "boolean", "default": True},
        },
    },
    "CardSchedulePatchRequest": {
        "type": "object",
        "description": "修改调度参数（对应 setDueDate / setEaseFactors / "
        "setSpecificValueOfCard），仅更新出现的字段",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "cards"},
                    "id": {"type": "string", "description": "提供时必须与路径一致"},
                    "attributes": {
                        "type": "object",
                        "properties": {
                            "due_date": {
                                "type": "string",
                                "description": "复习卡为天偏移（如 \"0\"/\"-7\"），"
                                "新卡为序号或 \"!\" 置顶",
                            },
                            "ease_factor": {"type": "integer"},
                            "interval": {"type": "integer"},
                            "reviews": {"type": "integer"},
                            "lapses": {"type": "integer"},
                            "template_index": {"type": "integer"},
                        },
                    },
                },
            },
        },
    },
    "ReviewInsertRequest": {
        "type": "object",
        "description": "补录复习记录（全部校验通过后一次性写入）；revlog.id "
        "由 review_time（Unix 秒）×1000 得到",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["type", "attributes"],
                    "properties": {
                        "type": {"type": "string", "const": "reviews"},
                        "attributes": {
                            "type": "object",
                            "required": ["review_time", "button_pressed"],
                            "properties": {
                                "review_time": {
                                    "type": "number",
                                    "description": "Unix 秒级时间戳",
                                },
                                "button_pressed": {"type": "integer"},
                                "new_interval": {"type": "integer", "default": 0},
                                "previous_interval": {
                                    "type": "integer",
                                    "default": 0,
                                },
                                "new_factor": {"type": "integer", "default": 0},
                                "review_duration": {"type": "integer", "default": 0},
                                "review_type": {"type": "integer", "default": 0},
                                "usn": {"type": "integer"},
                            },
                        },
                    },
                },
            },
        },
    },
    # ---- 媒体上传请求体 ----
    "MediaUploadRequest": {
        "type": "object",
        "description": "JSON + Base64 上传方式（data_base64 / url / path 三选一）；"
        "大文件可用 multipart/form-data 提交（文件字段 + 可选 filename 字段）",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "media"},
                    "attributes": {
                        "type": "object",
                        "required": ["filename"],
                        "properties": {
                            "filename": {"type": "string"},
                            "data_base64": {"type": "string", "format": "byte"},
                            "url": {
                                "type": "string",
                                "description": "仅支持 http/https",
                            },
                            "path": {
                                "type": "string",
                                "description": "服务器本地文件路径",
                            },
                        },
                    },
                },
            },
        },
    },
    # ---- 复习与工具请求体 ----
    "CompareAnswerRequest": {
        "type": "object",
        "required": ["expected", "provided"],
        "properties": {
            "expected": {"type": "string", "description": "期望答案"},
            "provided": {"type": "string", "description": "用户输入"},
            "combining": {"type": "boolean", "default": True},
        },
    },
    "SearchBuildRequest": {
        "type": "object",
        "description": "query 与 nodes 互斥，恰好提供一个：query 为语法校验，"
        "nodes 为由结构化节点构造搜索串",
        "properties": {
            "query": {"type": "string", "description": "待校验的 Anki 搜索串"},
            "nodes": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "description": "每个节点恰好包含一个过滤键（deck/tag/nid/flag/"
                    "card_state/rated/dupe/field/nids/negated/group 等）",
                },
            },
            "joiner": {"type": "string", "enum": ["AND", "OR"], "default": "AND"},
        },
    },
    # ---- 系统写操作请求体 ----
    "MultiRequest": {
        "type": "object",
        "required": ["requests"],
        "properties": {
            "requests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["method", "path"],
                    "properties": {
                        "method": {
                            "type": "string",
                            "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                        },
                        "path": {
                            "type": "string",
                            "description": "以 /api/ 开头的路径（可含查询串）",
                        },
                        "body": {"description": "子请求体（任意 JSON）"},
                    },
                },
            },
        },
    },
    "ProfileSwitchRequest": {
        "type": "object",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["type", "attributes"],
                "properties": {
                    "type": {"type": "string", "const": "profiles"},
                    "attributes": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    },
                },
            },
        },
    },
    "ExportRequest": {
        "type": "object",
        "required": ["deck", "path"],
        "properties": {
            "deck": {"type": "string", "description": "牌组名称"},
            "path": {"type": "string", "description": "导出文件路径（.apkg）"},
            "include_sched": {
                "type": "boolean",
                "default": False,
                "description": "是否包含调度信息",
            },
        },
    },
    "ImportRequest": {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "description": "待导入的 .apkg 文件路径"},
        },
    },
    "BackupRequest": {
        "type": "object",
        "description": "请求体可省略，字段均可选",
        "properties": {
            "force": {
                "type": "boolean",
                "default": True,
                "description": "忽略用户配置的备份间隔",
            },
            "wait": {
                "type": "boolean",
                "default": True,
                "description": "阻塞至备份完成（meta.path 返回新备份路径）",
            },
        },
    },
    "ConfirmRequest": {
        "type": "object",
        "description": "危险操作确认",
        "required": ["confirm"],
        "properties": {
            "confirm": {"type": "boolean", "const": True},
        },
    },
    "PreferencesPatchRequest": {
        "type": "object",
        "description": "与 GET /api/system/preferences 响应相同结构的部分 JSON "
        "对象，只合并出现的字段（嵌套消息递归合并）",
        "additionalProperties": True,
    },
    "ConfigValueRequest": {
        "description": "要写入配置键的任意 JSON 值（原样写入，不包 JSON:API 信封）",
    },
})


# 路由元数据表（键与 api.API_ROUTES 一致，<param> 形式路径）
#
# 每条路由结构：
# {
#     "tags": ["notes"],
#     "methods": {
#         "GET": {
#             "summary": "中文简述",
#             "query": [...],            # 查询参数（可选）
#             "body": "SchemaName",      # 请求体 schema（可选）
#             "body_required": False,    # 请求体是否必传（默认 True，可选）
#             "responses": {"200": "描述", "404": _R404, ...},
#         },
#     },
# }
# responses 的值可为 "描述" 或 ("描述", kind)：kind 为 "html" / "binary" /
# "media" 时表示非 JSON 响应；为其他字符串时视为 components 中的 schema 名，
# 作为该 2xx 响应的 $ref（默认 2xx 引用 JsonApiDocument，错误码引用 Error）。
##########################################################################

_ROUTE_METADATA = {
    # ---- 根端点与本文档（system） ----
    "/api": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "API 根端点（版本与全部端点清单）",
                "responses": {"200": "根文档（meta.endpoints 为端点清单）"},
            },
        },
    },
    "/api/": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "API 根端点（版本与全部端点清单）",
                "responses": {"200": "根文档（meta.endpoints 为端点清单）"},
            },
        },
    },
    "/api/openapi.json": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "OpenAPI 3.1 规范文档（按当前已注册路由动态生成）",
                "responses": {"200": "OpenAPI 文档"},
            },
        },
    },

    # ---- 笔记与标签（notes） ----
    "/api/notes": {
        "tags": ["notes"],
        "methods": {
            "GET": {
                "summary": "获取笔记列表（分页/搜索/时间过滤/轻量模式）",
                "query": _note_list_params(),
                "responses": {
                    "200": ("笔记集合文档（含分页）", "JsonApiDocument"),
                    "400": _R400,
                    "500": _R500,
                    "503": _R503,
                },
            },
            "POST": {
                "summary": "创建笔记",
                "body": "NoteCreateRequest",
                "responses": {
                    "201": ("创建成功", "NoteResource"),
                    "400": _R400,
                    "404": "牌组或笔记类型不存在",
                    "409": _R409,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/<note_id>": {
        "tags": ["notes"],
        "methods": {
            "GET": {
                "summary": "获取笔记详情",
                "responses": {
                    "200": ("笔记文档", "NoteResource"),
                    "400": "笔记 ID 非法",
                    "404": _R404,
                    "503": _R503,
                },
            },
            "PATCH": {
                "summary": "部分更新笔记（字段/标签/牌组）",
                "body": "NotePatchRequest",
                "responses": {
                    "200": ("更新后的笔记文档", "NoteResource"),
                    "400": _R400,
                    "404": _R404,
                    "409": _R409,
                    "503": _R503,
                },
            },
            "DELETE": {
                "summary": "删除笔记",
                "responses": {
                    "200": "删除成功（返回被删笔记资源）",
                    "400": "笔记 ID 非法",
                    "404": _R404,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/<note_id>/cards": {
        "tags": ["notes"],
        "methods": {
            "GET": {
                "summary": "获取笔记的全部卡片",
                "responses": {
                    "200": "卡片集合文档",
                    "404": _R404,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/batch": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "批量创建笔记",
                "body": "NoteBatchCreateRequest",
                "responses": {
                    "200": "逐项结果（失败位置为 null，errors 指明下标）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/validate": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "创建前预检（不写入）",
                "body": "NoteValidateRequest",
                "responses": {
                    "200": "逐项校验结果（valid/detail）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/actions/remove-empty": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "清理空笔记（无请求体）",
                "responses": {
                    "200": "清理完成（meta.removed 为删除数）",
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/<note_id>/notetype": {
        "tags": ["notes"],
        "methods": {
            "PATCH": {
                "summary": "更换笔记类型",
                "body": "NoteNotetypeChangeRequest",
                "responses": {
                    "200": ("更换后的笔记文档", "NoteResource"),
                    "400": _R400,
                    "404": "笔记或目标笔记类型不存在",
                    "409": _R409,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/<note_id>/tags": {
        "tags": ["notes"],
        "methods": {
            "GET": {
                "summary": "获取笔记标签",
                "responses": {
                    "200": "标签列表",
                    "400": "笔记 ID 非法",
                    "404": _R404,
                    "503": _R503,
                },
            },
            "POST": {
                "summary": "增量添加标签",
                "body": "TagsWriteRequest",
                "responses": {
                    "200": "最新标签列表",
                    "400": _R400,
                    "404": _R404,
                    "503": _R503,
                },
            },
            "PUT": {
                "summary": "整体替换标签",
                "body": "TagsWriteRequest",
                "responses": {
                    "200": "最新标签列表",
                    "400": _R400,
                    "404": _R404,
                    "503": _R503,
                },
            },
            "DELETE": {
                "summary": "移除指定标签",
                "body": "TagsWriteRequest",
                "responses": {
                    "200": "最新标签列表",
                    "400": _R400,
                    "404": _R404,
                    "503": _R503,
                },
            },
        },
    },
    "/api/tags": {
        "tags": ["notes"],
        "methods": {
            "GET": {
                "summary": "获取全部标签",
                "responses": {
                    "200": "标签列表",
                    "503": _R503,
                },
            },
            "DELETE": {
                "summary": "清理未使用标签",
                "query": [
                    _qp("unused", "必须为 true（防误调用）",
                        enum=["true"], required=True),
                ],
                "responses": {
                    "200": "清理完成（meta.removed 为删除数）",
                    "400": "缺少 unused=true",
                    "503": _R503,
                },
            },
        },
    },
    "/api/tags/actions/replace": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "全局重命名标签（精确匹配，不级联子标签）",
                "body": "TagReplaceRequest",
                "responses": {
                    "200": "替换完成（meta.updated 为变更笔记数）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/actions/find-replace": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "笔记字段内容查找替换",
                "body": "FindReplaceRequest",
                "responses": {
                    "200": "替换完成（meta.updated 为变更笔记数）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/dupes": {
        "tags": ["notes"],
        "methods": {
            "GET": {
                "summary": "按字段查找重复笔记",
                "query": [
                    _qp("field", "按该字段的值分组查重", required=True),
                    _qp("query", "在结果中进一步过滤的 Anki 搜索词"),
                ],
                "responses": {
                    "200": "重复组列表（value + note_ids）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/actions/render-preview": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "不落库渲染预览",
                "body": "NoteRenderPreviewRequest",
                "responses": {
                    "200": "渲染结果（question/answer/css/av_tags）",
                    "400": _R400,
                    "404": "笔记类型不存在",
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/actions/suspend": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "笔记级挂起（挂起全部卡片）",
                "body": "NoteIdsActionRequest",
                "responses": {
                    "200": "挂起完成（meta.affected_cards 为受影响卡片数）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/actions/unsuspend": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "笔记级恢复（恢复全部挂起/搁置卡片）",
                "body": "NoteIdsActionRequest",
                "responses": {
                    "200": "恢复完成（meta.affected_cards 为受影响卡片数）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notes/actions/bury": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "笔记级搁置（当日有效）",
                "body": "NoteIdsActionRequest",
                "responses": {
                    "200": "搁置完成（meta.affected_cards 为受影响卡片数）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/tags/tree": {
        "tags": ["notes"],
        "methods": {
            "GET": {
                "summary": "标签层级树",
                "responses": {
                    "200": "嵌套标签树",
                    "503": _R503,
                },
            },
        },
    },
    "/api/tags/actions/attach": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "批量加标签（多笔记）",
                "body": "NoteIdsTagsActionRequest",
                "responses": {
                    "200": "添加完成（meta.updated 为变更笔记数）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/tags/actions/detach": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "批量移除标签（多笔记）",
                "body": "NoteIdsTagsActionRequest",
                "responses": {
                    "200": "移除完成（meta.updated 为变更笔记数）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/tags/actions/find-replace": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "标签名查找替换（支持正则，可删除匹配标签）",
                "body": "FindReplaceRequest",
                "responses": {
                    "200": "替换完成（meta.updated 为变更笔记数）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/tags/actions/reparent": {
        "tags": ["notes"],
        "methods": {
            "POST": {
                "summary": "更改标签父级",
                "body": "TagsReparentRequest",
                "responses": {
                    "200": "修改完成（meta.updated 为变更笔记数）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
}

# 牌组（decks）
_ROUTE_METADATA.update({
    "/api/decks": {
        "tags": ["decks"],
        "methods": {
            "GET": {
                "summary": "获取牌组列表",
                "responses": {
                    "200": "牌组集合文档",
                    "503": _R503,
                },
            },
            "POST": {
                "summary": "创建牌组（幂等；支持筛选牌组）",
                "body": "DeckCreateRequest",
                "responses": {
                    "200": "同名牌组已存在，幂等返回现有牌组",
                    "201": ("创建成功", "DeckResource"),
                    "400": _R400,
                    "409": "同名普通牌组不可转为筛选牌组",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/decks/<deck_id>": {
        "tags": ["decks"],
        "methods": {
            "GET": {
                "summary": "获取牌组详情",
                "responses": {
                    "200": ("牌组文档", "DeckResource"),
                    "400": "牌组 ID 非法",
                    "404": _R404,
                    "503": _R503,
                },
            },
            "PATCH": {
                "summary": "重命名 / 改父级",
                "body": "DeckPatchRequest",
                "responses": {
                    "200": ("更新后的牌组文档", "DeckResource"),
                    "400": _R400,
                    "404": "牌组或目标父牌组不存在",
                    "409": "重名冲突或 id 不一致",
                    "500": _R500,
                    "503": _R503,
                },
            },
            "DELETE": {
                "summary": "删除牌组",
                "query": [
                    _qp("cards_too", "true 时连同牌组内卡片一并删除；"
                        "默认 false 时非空牌组返回 409",
                        "boolean", default=False),
                ],
                "responses": {
                    "200": "删除成功",
                    "400": "牌组 ID 非法或试图删除默认牌组",
                    "404": _R404,
                    "409": "牌组非空且未传 cards_too=true",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/decks/<deck_id>/notes": {
        "tags": ["decks"],
        "methods": {
            "GET": {
                "summary": "获取牌组内笔记",
                "responses": {
                    "200": "笔记集合文档",
                    "404": _R404,
                    "503": _R503,
                },
            },
        },
    },
    "/api/decks/<deck_id>/cards": {
        "tags": ["decks"],
        "methods": {
            "GET": {
                "summary": "获取牌组内卡片（分页 + query 叠加过滤）",
                "query": _pagination_params() + [
                    _qp("query", "在牌组过滤基础上叠加的 Anki 搜索语法"),
                ],
                "responses": {
                    "200": "卡片集合文档（含分页）",
                    "400": _R400,
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/decks/<deck_id>/actions/move-cards": {
        "tags": ["decks"],
        "methods": {
            "POST": {
                "summary": "批量移出卡片到其他牌组",
                "body": "DeckMoveCardsRequest",
                "responses": {
                    "200": "移动完成（meta 含 moved/skipped_card_ids）",
                    "400": _R400,
                    "404": "来源或目标牌组不存在",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/decks/<deck_id>/config": {
        "tags": ["decks"],
        "methods": {
            "GET": {
                "summary": "获取牌组配置（配置对象原样返回）",
                "responses": {
                    "200": "配置组资源",
                    "400": "牌组 ID 非法",
                    "404": "牌组不存在或无配置",
                    "500": _R500,
                    "503": _R503,
                },
            },
            "PUT": {
                "summary": "整体替换牌组配置",
                "body": "DeckConfigPutRequest",
                "responses": {
                    "200": "保存后的配置组资源",
                    "400": _R400,
                    "404": "牌组或配置组不存在",
                    "409": "配置组 id 与牌组当前配置不一致",
                    "500": _R500,
                    "503": _R503,
                },
            },
            "PATCH": {
                "summary": "切换牌组使用的配置组",
                "body": "DeckConfigPatchRequest",
                "responses": {
                    "200": "切换后的配置组资源",
                    "400": _R400,
                    "404": "牌组或目标配置组不存在",
                    "409": _R409,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/deck-configs": {
        "tags": ["decks"],
        "methods": {
            "GET": {
                "summary": "获取全部配置组",
                "responses": {
                    "200": "配置组集合",
                    "500": _R500,
                    "503": _R503,
                },
            },
            "POST": {
                "summary": "克隆配置组",
                "body": "DeckConfigCloneRequest",
                "responses": {
                    "201": "克隆成功",
                    "400": _R400,
                    "404": "被克隆的配置组不存在",
                    "409": _R409,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/deck-configs/<config_id>": {
        "tags": ["decks"],
        "methods": {
            "DELETE": {
                "summary": "删除配置组",
                "responses": {
                    "200": "删除成功",
                    "400": "配置组 ID 非法或试图删除默认配置",
                    "404": _R404,
                    "409": "仍被牌组引用（detail 列出引用方）",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/decks/tree": {
        "tags": ["decks"],
        "methods": {
            "GET": {
                "summary": "牌组树（含 new/lrn/rev 计数）",
                "query": [
                    _qp("top_deck_id", "限定子树的牌组 ID，缺省返回整棵树",
                        "integer"),
                ],
                "responses": {
                    "200": "嵌套牌组树",
                    "400": "top_deck_id 非法",
                    "404": "top_deck_id 指定的牌组不存在",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/decks/<deck_id>/actions/rebuild": {
        "tags": ["decks"],
        "methods": {
            "POST": {
                "summary": "重建筛选牌组（无请求体）",
                "responses": {
                    "200": "重建完成（meta.card_count 为卡片数）",
                    "400": "目标不是筛选牌组",
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/decks/<deck_id>/actions/empty": {
        "tags": ["decks"],
        "methods": {
            "POST": {
                "summary": "排空筛选牌组（无请求体）",
                "responses": {
                    "200": "排空完成",
                    "400": "目标不是筛选牌组",
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/decks/<deck_id>/actions/unbury": {
        "tags": ["decks"],
        "methods": {
            "POST": {
                "summary": "解除牌组当日搁置",
                "body": "DeckUnburyRequest",
                "body_required": False,
                "responses": {
                    "200": "解除完成",
                    "400": _R400,
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/decks/<deck_id>/actions/extend-limits": {
        "tags": ["decks"],
        "methods": {
            "POST": {
                "summary": "临时提高今日新卡/复习上限",
                "body": "DeckExtendLimitsRequest",
                "responses": {
                    "200": "提高完成",
                    "400": _R400,
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/decks/<deck_id>/custom-study-defaults": {
        "tags": ["decks"],
        "methods": {
            "GET": {
                "summary": "自定义学习默认参数",
                "responses": {
                    "200": "默认参数（tags/extend_new/extend_review/available_*）",
                    "400": "牌组 ID 非法",
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/decks/<deck_id>/actions/custom-study": {
        "tags": ["decks"],
        "methods": {
            "POST": {
                "summary": "执行自定义学习",
                "body": "CustomStudyRequest",
                "responses": {
                    "200": "执行完成（meta 含 mode/params）",
                    "400": _R400,
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
})

# 笔记类型（notetypes）
_ROUTE_METADATA.update({
    "/api/notetypes": {
        "tags": ["notetypes"],
        "methods": {
            "GET": {
                "summary": "获取笔记类型列表（含 use_count）",
                "responses": {
                    "200": "笔记类型集合",
                    "503": _R503,
                },
            },
            "POST": {
                "summary": "创建笔记类型（支持内置蓝本 stock_kind）",
                "body": "NoteTypeCreateRequest",
                "responses": {
                    "201": ("创建成功", "NoteTypeResource"),
                    "400": _R400,
                    "409": "同名类型已存在",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notetypes/byname/<note_type_name>": {
        "tags": ["notetypes"],
        "methods": {
            "GET": {
                "summary": "按名称获取笔记类型",
                "responses": {
                    "200": ("笔记类型文档", "NoteTypeResource"),
                    "404": _R404,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notetypes/<notetype_id>": {
        "tags": ["notetypes"],
        "methods": {
            "GET": {
                "summary": "获取笔记类型详情",
                "responses": {
                    "200": ("笔记类型详情（含 css/cloze/use_count/sort_field）",
                            "NoteTypeResource"),
                    "400": "笔记类型 ID 非法",
                    "404": _R404,
                    "503": _R503,
                },
            },
            "PATCH": {
                "summary": "修改排序/查重依据字段",
                "body": "NoteTypeSortFieldRequest",
                "responses": {
                    "200": ("更新后的笔记类型详情", "NoteTypeResource"),
                    "400": _R400,
                    "404": _R404,
                    "409": _R409,
                    "500": _R500,
                    "503": _R503,
                },
            },
            "DELETE": {
                "summary": "删除笔记类型（连同全部笔记与卡片）",
                "query": [
                    _qp("force", "true 时强制删除；默认 false 时仍被使用返回 409",
                        "boolean", default=False),
                ],
                "responses": {
                    "200": "删除成功（meta 含 removed_notes）",
                    "400": "笔记类型 ID 非法",
                    "404": _R404,
                    "409": "类型仍被笔记使用且未传 force=true",
                    "503": _R503,
                },
            },
        },
    },
    "/api/notetypes/<notetype_id>/fields": {
        "tags": ["notetypes"],
        "methods": {
            "GET": {
                "summary": "获取字段列表（名称/位置/字体/描述/模板引用）",
                "responses": {
                    "200": "字段集合",
                    "400": "笔记类型 ID 非法",
                    "404": _R404,
                    "503": _R503,
                },
            },
            "POST": {
                "summary": "添加字段（追加到末尾）",
                "body": "FieldAddRequest",
                "responses": {
                    "201": "添加成功",
                    "400": _R400,
                    "404": _R404,
                    "409": "同名字段已存在",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notetypes/<notetype_id>/fields/<field_name>": {
        "tags": ["notetypes"],
        "methods": {
            "PATCH": {
                "summary": "更新字段属性（重命名/位置/字体/字号/描述）",
                "body": "FieldPatchRequest",
                "responses": {
                    "200": "更新后的字段资源",
                    "400": _R400,
                    "404": "笔记类型或字段不存在",
                    "409": "新名称与现有字段冲突",
                    "500": _R500,
                    "503": _R503,
                },
            },
            "DELETE": {
                "summary": "删除字段",
                "responses": {
                    "200": "删除成功（返回被删字段资源）",
                    "400": "字段 ID 非法或试图删除最后一个字段",
                    "404": "笔记类型或字段不存在",
                    "409": "字段仍被模板引用（detail 列出模板）",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notetypes/<notetype_id>/templates": {
        "tags": ["notetypes"],
        "methods": {
            "GET": {
                "summary": "获取模板列表（名称/位置/正反面/use_count）",
                "responses": {
                    "200": "模板集合",
                    "400": "笔记类型 ID 非法",
                    "404": _R404,
                    "503": _R503,
                },
            },
            "POST": {
                "summary": "添加模板",
                "body": "TemplateAddRequest",
                "responses": {
                    "201": "添加成功",
                    "400": _R400 + "（填空题类型仅支持单个模板）",
                    "404": _R404,
                    "409": "同名模板已存在",
                    "500": _R500,
                    "503": _R503,
                },
            },
            "PUT": {
                "summary": "按名称整体更新多个模板内容",
                "body": "TemplatesUpdateRequest",
                "responses": {
                    "200": "更新后的模板集合（meta.updated 为更新数）",
                    "400": _R400,
                    "404": _R404,
                    "409": _R409,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notetypes/<notetype_id>/templates/<template_name>": {
        "tags": ["notetypes"],
        "methods": {
            "PATCH": {
                "summary": "模板重命名/排序",
                "body": "TemplatePatchRequest",
                "responses": {
                    "200": "更新后的模板资源",
                    "400": _R400,
                    "404": "笔记类型或模板不存在",
                    "409": "新名称与现有模板冲突",
                    "500": _R500,
                    "503": _R503,
                },
            },
            "DELETE": {
                "summary": "删除模板",
                "responses": {
                    "200": "删除成功（返回被删模板资源）",
                    "400": "试图删除最后一个模板",
                    "404": "笔记类型或模板不存在",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notetypes/<notetype_id>/styling": {
        "tags": ["notetypes"],
        "methods": {
            "GET": {
                "summary": "获取样式（CSS）",
                "responses": {
                    "200": "样式资源",
                    "400": "笔记类型 ID 非法",
                    "404": _R404,
                    "503": _R503,
                },
            },
            "PUT": {
                "summary": "整体替换 CSS",
                "body": "StylingUpdateRequest",
                "responses": {
                    "200": "更新后的样式资源",
                    "400": _R400,
                    "404": _R404,
                    "409": _R409,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notetypes/<notetype_id>/actions/find-replace": {
        "tags": ["notetypes"],
        "methods": {
            "POST": {
                "summary": "模板正反面内容查找替换",
                "body": "NoteTypeFindReplaceRequest",
                "responses": {
                    "200": "替换完成（meta.replaced 为变更模板数）",
                    "400": _R400,
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/notetypes/stock": {
        "tags": ["notetypes"],
        "methods": {
            "GET": {
                "summary": "内置笔记类型蓝本列表",
                "responses": {
                    "200": "蓝本列表（stock_kind + name）",
                    "503": _R503,
                },
            },
        },
    },
    "/api/notetypes/<notetype_id>/actions/copy": {
        "tags": ["notetypes"],
        "methods": {
            "POST": {
                "summary": "克隆笔记类型",
                "body": "NoteTypeCopyRequest",
                "body_required": False,
                "responses": {
                    "201": ("克隆成功", "NoteTypeResource"),
                    "400": _R400,
                    "404": _R404,
                    "409": "指定名称与现有类型冲突",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
})

# 卡片（cards）
_ROUTE_METADATA.update({
    "/api/cards": {
        "tags": ["cards"],
        "methods": {
            "GET": {
                "summary": "获取卡片列表（分页/搜索/时间过滤）",
                "query": _card_list_params(),
                "responses": {
                    "200": "卡片集合文档（含分页）",
                    "400": _R400,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/<card_id>": {
        "tags": ["cards"],
        "methods": {
            "GET": {
                "summary": "获取卡片详情",
                "responses": {
                    "200": ("卡片文档", "CardResource"),
                    "400": "卡片 ID 非法",
                    "404": _R404,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/<card_id>/note": {
        "tags": ["cards"],
        "methods": {
            "GET": {
                "summary": "获取卡片所属笔记",
                "responses": {
                    "200": ("笔记文档", "NoteResource"),
                    "400": "卡片 ID 非法",
                    "404": "卡片或所属笔记不存在",
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/<card_id>/status": {
        "tags": ["cards"],
        "methods": {
            "GET": {
                "summary": "卡片状态聚合（suspended/due/intervals/ease_factor）",
                "responses": {
                    "200": "状态聚合资源",
                    "400": "卡片 ID 非法",
                    "404": _R404,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/actions/status": {
        "tags": ["cards"],
        "methods": {
            "POST": {
                "summary": "批量卡片状态查询",
                "body": "CardIdsActionRequest",
                "responses": {
                    "200": "与输入一一对应的状态数组（无效 id 位置为 null）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/<card_id>/schedule": {
        "tags": ["cards"],
        "methods": {
            "PATCH": {
                "summary": "修改卡片调度参数",
                "body": "CardSchedulePatchRequest",
                "responses": {
                    "200": ("更新后的卡片文档", "CardResource"),
                    "400": _R400,
                    "404": _R404,
                    "409": _R409,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/actions/suspend": {
        "tags": ["cards"],
        "methods": {
            "POST": {
                "summary": "批量挂起卡片",
                "body": "CardIdsActionRequest",
                "responses": {
                    "200": "挂起完成（meta 含 succeeded/failed/skipped_card_ids）",
                    "400": _R400,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/actions/unsuspend": {
        "tags": ["cards"],
        "methods": {
            "POST": {
                "summary": "批量恢复卡片",
                "body": "CardIdsActionRequest",
                "responses": {
                    "200": "恢复完成",
                    "400": _R400,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/actions/forget": {
        "tags": ["cards"],
        "methods": {
            "POST": {
                "summary": "批量忘记（重置为新卡）",
                "body": "CardForgetRequest",
                "responses": {
                    "200": "重置完成",
                    "400": _R400,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/actions/relearn": {
        "tags": ["cards"],
        "methods": {
            "POST": {
                "summary": "批量重学（放回重学队列）",
                "body": "CardIdsActionRequest",
                "responses": {
                    "200": "重学完成",
                    "400": _R400,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/actions/answer": {
        "tags": ["cards"],
        "methods": {
            "POST": {
                "summary": "批量作答（ease 1-4）",
                "body": "CardAnswersRequest",
                "responses": {
                    "200": "作答完成（meta.results 与输入一一对应）",
                    "400": _R400,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/actions/move": {
        "tags": ["cards"],
        "methods": {
            "POST": {
                "summary": "批量移入其他牌组",
                "body": "CardsMoveRequest",
                "responses": {
                    "200": "移动完成",
                    "400": _R400,
                    "404": "目标牌组不存在",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/<card_id>/reviews": {
        "tags": ["cards"],
        "methods": {
            "GET": {
                "summary": "获取复习历史",
                "query": [
                    _qp("start", "仅返回该日之后的记录（YYYY-MM-DD，本地时区）"),
                    _qp("latest", "true 时仅返回最近一条记录的 id", "boolean"),
                ],
                "responses": {
                    "200": "复习记录集合",
                    "400": "卡片 ID 非法或 start 格式错误",
                    "404": _R404,
                    "503": _R503,
                },
            },
            "POST": {
                "summary": "补录复习记录",
                "body": "ReviewInsertRequest",
                "responses": {
                    "201": "补录成功",
                    "400": _R400,
                    "404": _R404,
                    "409": "review_time 与已有记录冲突",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/actions/set-flag": {
        "tags": ["cards"],
        "methods": {
            "POST": {
                "summary": "批量设置旗帜（0-7）",
                "body": "CardsSetFlagRequest",
                "responses": {
                    "200": "设置完成",
                    "400": _R400,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/actions/bury": {
        "tags": ["cards"],
        "methods": {
            "POST": {
                "summary": "批量搁置卡片（当日有效）",
                "body": "CardIdsActionRequest",
                "responses": {
                    "200": "搁置完成",
                    "400": _R400,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/actions/unbury": {
        "tags": ["cards"],
        "methods": {
            "POST": {
                "summary": "批量解除搁置",
                "body": "CardIdsActionRequest",
                "responses": {
                    "200": "解除完成",
                    "400": _R400,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/<card_id>/render": {
        "tags": ["cards"],
        "methods": {
            "GET": {
                "summary": "渲染卡片正反面 HTML",
                "responses": {
                    "200": "渲染结果（question/answer/css/av_tags）",
                    "400": "卡片 ID 非法",
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/<card_id>/memory-state": {
        "tags": ["cards"],
        "methods": {
            "GET": {
                "summary": "FSRS 记忆状态（stability/difficulty/retrievability）",
                "query": [
                    _qp("interval", "附带该间隔（天）的 fuzz_delta", "integer"),
                ],
                "responses": {
                    "200": "记忆状态资源",
                    "400": "卡片 ID 或 interval 非法",
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/<card_id>/scheduling-states": {
        "tags": ["cards"],
        "methods": {
            "GET": {
                "summary": "四个作答按钮的下一间隔预览（仅 v3 调度器）",
                "responses": {
                    "200": "间隔预览（again/hard/good/easy）",
                    "400": "卡片 ID 非法或非 v3 调度器",
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/empty-report": {
        "tags": ["cards"],
        "methods": {
            "GET": {
                "summary": "空卡片报告",
                "responses": {
                    "200": "空卡片报告（report + notes 明细）",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/actions/remove-empty": {
        "tags": ["cards"],
        "methods": {
            "POST": {
                "summary": "清理空卡片（无请求体）",
                "responses": {
                    "200": "清理完成（meta.removed 为删除数）",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/cards/actions/reposition": {
        "tags": ["cards"],
        "methods": {
            "POST": {
                "summary": "新卡排序",
                "body": "CardsRepositionRequest",
                "responses": {
                    "200": "排序完成",
                    "400": _R400,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
})

# 媒体（media）
_ROUTE_METADATA.update({
    "/api/media": {
        "tags": ["media"],
        "methods": {
            "GET": {
                "summary": "媒体文件列表（pattern glob 过滤 + 分页）",
                "query": _pagination_params() + [
                    _qp("pattern", "glob 过滤模式（不允许路径分隔符）", default="*"),
                ],
                "responses": {
                    "200": "文件名列表（含分页）",
                    "400": _R400,
                    "503": _R503,
                },
            },
            "POST": {
                "summary": "上传媒体文件（JSON+Base64 / url / path / multipart）",
                "query": [
                    _qp("overwrite", "false 时同名文件返回 409",
                        "boolean", default=True),
                ],
                "body": "MediaUploadRequest",
                "responses": {
                    "201": "上传成功",
                    "400": _R400,
                    "409": "overwrite=false 且同名文件已存在",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/media/directory": {
        "tags": ["media"],
        "methods": {
            "GET": {
                "summary": "媒体目录绝对路径",
                "responses": {
                    "200": "媒体目录路径",
                    "503": _R503,
                },
            },
        },
    },
    "/api/media/<filename>": {
        "tags": ["media"],
        "methods": {
            "GET": {
                "summary": "下载媒体文件",
                "query": [
                    _qp("format", "base64 时返回 JSON:API 文档（默认返回二进制）",
                        enum=["base64"]),
                ],
                "responses": {
                    "200": ("文件二进制（Content-Type 按扩展名推断）；"
                            "format=base64 时为 JSON:API 文档", "media"),
                    "400": "文件名非法",
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
            "DELETE": {
                "summary": "删除媒体文件（移入回收站）",
                "responses": {
                    "200": "删除成功",
                    "400": "文件名非法",
                    "404": _R404,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/media/actions/check": {
        "tags": ["media"],
        "methods": {
            "POST": {
                "summary": "媒体检查（未引用/缺失报告，耗时长，无请求体）",
                "responses": {
                    "200": "检查报告（unused/missing/missing_media_notes）",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/media/trash/actions/empty": {
        "tags": ["media"],
        "methods": {
            "POST": {
                "summary": "清空媒体回收站（无请求体）",
                "responses": {
                    "200": "清空完成（meta.emptied 为文件数）",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/media/trash/actions/restore": {
        "tags": ["media"],
        "methods": {
            "POST": {
                "summary": "还原回收站中的媒体文件（无请求体）",
                "responses": {
                    "200": "还原完成（meta.restored 为文件数）",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/media/actions/force-resync": {
        "tags": ["media"],
        "methods": {
            "POST": {
                "summary": "强制下次全量媒体同步（无请求体）",
                "responses": {
                    "200": "已删除媒体同步索引",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
})

# 统计（stats）
_ROUTE_METADATA.update({
    "/api/stats/reviews/today": {
        "tags": ["stats"],
        "methods": {
            "GET": {
                "summary": "今日复习统计",
                "responses": {
                    "200": "今日复习卡片数",
                    "503": _R503,
                },
            },
        },
    },
    "/api/stats/reviews/by-day": {
        "tags": ["stats"],
        "methods": {
            "GET": {
                "summary": "按日复习统计",
                "responses": {
                    "200": "有复习记录的每一天（date + review_count）",
                    "503": _R503,
                },
            },
        },
    },
    "/api/stats/collection": {
        "tags": ["stats"],
        "methods": {
            "GET": {
                "summary": "集合统计报告（HTML）",
                "query": [
                    _qp("format", "输出格式（目前仅支持 html）",
                        enum=["html"], default="html"),
                    _qp("whole_collection", "true 统计整个集合；"
                        "默认 false 统计当前选中牌组", "boolean", default=False),
                ],
                "responses": {
                    "200": ("统计报告 HTML", "html"),
                    "400": "format 不支持",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/stats/decks": {
        "tags": ["stats"],
        "methods": {
            "GET": {
                "summary": "各牌组统计",
                "query": [
                    _qp("deck_ids", "逗号分隔的牌组 id，缺省返回全部牌组"),
                ],
                "responses": {
                    "200": "各牌组统计（new/learn/review/total_in_deck）",
                    "400": "deck_ids 含非法 id",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/stats/forecast": {
        "tags": ["stats"],
        "methods": {
            "GET": {
                "summary": "未来到期预测图",
                "query": _graph_params(),
                "responses": {
                    "200": ("统计图 HTML 片段", "html"),
                    "400": _R400,
                    "404": "deck_id 指定的牌组不存在",
                    "500": _R500,
                    "501": "format=json 暂未实现",
                    "503": _R503,
                },
            },
        },
    },
    "/api/stats/intervals": {
        "tags": ["stats"],
        "methods": {
            "GET": {
                "summary": "卡片间隔分布图",
                "query": _graph_params(),
                "responses": {
                    "200": ("统计图 HTML 片段", "html"),
                    "400": _R400,
                    "404": "deck_id 指定的牌组不存在",
                    "500": _R500,
                    "501": "format=json 暂未实现",
                    "503": _R503,
                },
            },
        },
    },
    "/api/stats/hourly": {
        "tags": ["stats"],
        "methods": {
            "GET": {
                "summary": "分时段记忆保留率图",
                "query": _graph_params(),
                "responses": {
                    "200": ("统计图 HTML 片段", "html"),
                    "400": _R400,
                    "404": "deck_id 指定的牌组不存在",
                    "500": _R500,
                    "501": "format=json 暂未实现",
                    "503": _R503,
                },
            },
        },
    },
    "/api/stats/card-types": {
        "tags": ["stats"],
        "methods": {
            "GET": {
                "summary": "卡片类型构成图",
                "query": _graph_params(),
                "responses": {
                    "200": ("统计图 HTML 片段", "html"),
                    "400": _R400,
                    "404": "deck_id 指定的牌组不存在",
                    "500": _R500,
                    "501": "format=json 暂未实现",
                    "503": _R503,
                },
            },
        },
    },
})

# 复习与工具（review）
_ROUTE_METADATA.update({
    "/api/review/queue": {
        "tags": ["review"],
        "methods": {
            "GET": {
                "summary": "取待学队列（幂等预览，仅 v3 调度器）",
                "query": [
                    _qp("limit", "返回卡片数", "integer", default=1),
                    _qp("intraday_learning_only",
                        "true 时仅返回当日学习队列中的卡", "boolean",
                        default=False),
                ],
                "responses": {
                    "200": "待学卡片集合（每元素 meta 含作答间隔预览，"
                    "meta.remaining 为剩余数）",
                    "400": "limit 非法或非 v3 调度器",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/utils/compare-answer": {
        "tags": ["review"],
        "methods": {
            "POST": {
                "summary": "输入型卡片答案比对",
                "body": "CompareAnswerRequest",
                "responses": {
                    "200": "比对结果（correct + diff 高亮片段）",
                    "400": _R400,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/search/build": {
        "tags": ["review"],
        "methods": {
            "POST": {
                "summary": "搜索串构造/校验（query 与 nodes 互斥）",
                "body": "SearchBuildRequest",
                "responses": {
                    "200": "校验结果（valid + normalized）或构造的搜索串",
                    "400": "搜索语法或节点非法",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
})

# 系统（system）
_ROUTE_METADATA.update({
    "/api/system/version": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "插件与 Anki 版本",
                "responses": {"200": "版本信息（plugin_version/anki_version）"},
            },
        },
    },
    "/api/system/capabilities": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "已注册接口清单（路由 -> 方法，含 AnkiConnect 对照）",
                "responses": {"200": "接口清单"},
            },
        },
    },
    "/api/system/actions/sync": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "触发 AnkiWeb 同步（阻塞，需长超时，无请求体）",
                "responses": {
                    "200": "同步完成",
                    "502": "同步失败（未配置凭据或远端错误）",
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/actions/multi": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "批量调用（顺序执行，互不影响）",
                "body": "MultiRequest",
                "responses": {
                    "200": "与请求一一对应的 {status, body} 数组",
                    "400": _R400,
                },
            },
        },
    },
    "/api/system/actions/reload": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "重载集合（从磁盘重新加载，无请求体）",
                "responses": {
                    "200": "重载完成",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/profiles": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "配置档案列表",
                "responses": {
                    "200": "档案名称列表",
                    "500": _R500,
                },
            },
        },
    },
    "/api/system/profiles/active": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "当前配置档案",
                "responses": {
                    "200": "当前档案",
                    "500": _R500,
                    "503": "无活动档案",
                },
            },
            "POST": {
                "summary": "切换配置档案（可能先于切换完成返回）",
                "body": "ProfileSwitchRequest",
                "responses": {
                    "200": "切换已发起",
                    "400": _R400,
                    "404": "档案不存在",
                    "409": _R409,
                    "500": _R500,
                },
            },
        },
    },
    "/api/system/actions/export": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "导出牌组为 .apkg",
                "body": "ExportRequest",
                "responses": {
                    "200": "导出完成（meta.path 为文件路径）",
                    "400": _R400,
                    "404": "牌组不存在",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/actions/import": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "导入 .apkg（建议先同步或备份）",
                "body": "ImportRequest",
                "responses": {
                    "200": "导入完成",
                    "400": _R400,
                    "404": "文件不存在",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/actions/check-database": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "数据库检查（耗时长，无请求体）",
                "responses": {
                    "200": "检查完成",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/undo-status": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "撤销/重做可用性与操作名",
                "responses": {
                    "200": "撤销状态",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/actions/undo": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "撤销（无请求体）",
                "responses": {
                    "200": "撤销完成（返回新的 undo-status）",
                    "409": "无可撤销内容",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/actions/redo": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "重做（无请求体）",
                "responses": {
                    "200": "重做完成（返回新的 undo-status）",
                    "409": "无可重做内容",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/config": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "全部集合配置",
                "responses": {
                    "200": "全部配置键值",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/config/<key>": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "读取单个配置键",
                "responses": {
                    "200": "配置键值",
                    "404": "配置键不存在",
                    "500": _R500,
                    "503": _R503,
                },
            },
            "PUT": {
                "summary": "写入配置键（任意 JSON 值）",
                "body": "ConfigValueRequest",
                "responses": {
                    "200": "写入后的配置键值",
                    "400": "请求体为空或 JSON 非法",
                    "500": _R500,
                    "503": _R503,
                },
            },
            "DELETE": {
                "summary": "删除配置键（幂等）",
                "responses": {
                    "200": "删除完成",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/preferences": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "全局偏好（proto 转 JSON）",
                "responses": {
                    "200": "全局偏好",
                    "500": _R500,
                    "503": _R503,
                },
            },
            "PATCH": {
                "summary": "合并式更新偏好",
                "body": "PreferencesPatchRequest",
                "responses": {
                    "200": "更新后的完整偏好",
                    "400": "未知字段或值类型非法",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/actions/backup": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "立即创建备份",
                "body": "BackupRequest",
                "body_required": False,
                "responses": {
                    "200": "备份完成（meta.path 为新备份路径；"
                    "无变化时 meta.created=false）",
                    "400": _R400,
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/progress": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "后端长任务进度（无任务时 data 为 null）",
                "responses": {
                    "200": "当前进度",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/actions/abort": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "请求中止当前后端操作（无请求体）",
                "responses": {
                    "200": "已设置中止标志",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/sync/status": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "同步状态（是否需要同步/全量）",
                "responses": {
                    "200": "同步状态（required/new_endpoint）",
                    "401": "未登录 AnkiWeb",
                    "500": _R500,
                    "502": _R502,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/sync/media-status": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "媒体同步状态",
                "responses": {
                    "200": "媒体同步状态（active/progress）",
                    "401": "未登录 AnkiWeb",
                    "500": _R500,
                    "502": _R502,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/actions/full-upload": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "全量上传（危险：本地覆盖远端）",
                "body": "ConfirmRequest",
                "responses": {
                    "200": "上传完成（集合已重新加载）",
                    "400": "缺少 {\"confirm\": true}",
                    "401": "未登录 AnkiWeb",
                    "500": _R500,
                    "502": _R502,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/actions/full-download": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "全量下载（危险：远端覆盖本地，执行前自动备份）",
                "body": "ConfirmRequest",
                "responses": {
                    "200": "下载完成（集合已重新加载）",
                    "400": "缺少 {\"confirm\": true}",
                    "401": "未登录 AnkiWeb",
                    "500": _R500,
                    "502": _R502,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/actions/optimize": {
        "tags": ["system"],
        "methods": {
            "POST": {
                "summary": "数据库优化（vacuum + analyze，无请求体）",
                "responses": {
                    "200": "优化完成",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
    "/api/system/collection": {
        "tags": ["system"],
        "methods": {
            "GET": {
                "summary": "集合元信息（crt/mod/计数/调度器版本）",
                "responses": {
                    "200": "集合元信息",
                    "500": _R500,
                    "503": _R503,
                },
            },
        },
    },
})


# 文档生成
##########################################################################


def _build_path_parameters(route_path):
    """从 <param> 形式的路由路径提取路径参数（schema 统一为 string）"""
    parameters = []
    for name in re.findall(r"<(\w+)>", route_path):
        parameters.append({
            "name": name,
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
            "description": _PATH_PARAM_DESCRIPTIONS.get(name, "路径参数"),
        })
    return parameters


def _build_response(status, description, kind):
    """构造单个响应对象

    kind 取值：
    - "json"：2xx 引用 JsonApiDocument，错误码引用 Error（默认）
    - "html"：text/html 响应（统计报告/统计图）
    - "binary"：application/octet-stream 响应
    - "media"：二进制与 JSON:API 两种内容类型（媒体下载）
    - 其他字符串：视为 components 中的 schema 名，作为 2xx 响应的 $ref
    """
    if kind == "html":
        content = {
            "text/html": {
                "schema": {"type": "string", "description": "HTML 报告片段"},
            },
        }
    elif kind == "binary":
        content = {
            "application/octet-stream": {
                "schema": {"type": "string", "format": "binary"},
            },
        }
    elif kind == "media":
        content = {
            "application/octet-stream": {
                "schema": {"type": "string", "format": "binary"},
            },
            "application/json": {
                "schema": {"$ref": "#/components/schemas/JsonApiDocument"},
            },
        }
    else:
        if status.startswith("2"):
            schema_name = kind if kind != "json" else "JsonApiDocument"
        else:
            schema_name = "Error"
        content = {
            "application/json": {
                "schema": {
                    "$ref": "#/components/schemas/" + schema_name,
                },
            },
        }
    return {"description": description, "content": content}


def _build_responses(spec):
    """按元数据中的状态码 -> 描述（或 (描述, kind)）构造 responses"""
    responses = {}
    for status, value in spec.items():
        if isinstance(value, tuple):
            description, kind = value
        else:
            description, kind = value, "json"
        responses[status] = _build_response(status, description, kind)
    return responses


def _build_operation(route_path, method, route_meta, path_parameters):
    """构造单个方法的 operation 对象（缺失元数据时使用占位 summary）"""
    method_meta = route_meta.get("methods", {}).get(method, {})

    summary = method_meta.get("summary") or route_meta.get("summary")
    if not summary:
        summary = method + " " + route_path

    operation = {
        "summary": summary,
        "tags": route_meta.get("tags", ["system"]),
    }

    parameters = list(path_parameters)
    parameters.extend(method_meta.get("query", []))
    if parameters:
        operation["parameters"] = parameters

    body_schema = method_meta.get("body")
    if body_schema:
        operation["requestBody"] = {
            "required": method_meta.get("body_required", True),
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": "#/components/schemas/" + body_schema,
                    },
                },
            },
        }

    operation["responses"] = _build_responses(
        method_meta.get("responses", {"200": "成功"})
    )
    return operation


def build_openapi_document():
    """根据 api.API_ROUTES 当前注册的路由生成 OpenAPI 3.1 文档（普通 dict）"""
    paths = {}
    for route_path, handler_class in api.API_ROUTES.items():
        # <param> 形式路径转换为 OpenAPI 的 {param} 形式
        openapi_path = re.sub(r"<(\w+)>", r"{\1}", route_path)

        route_meta = _ROUTE_METADATA.get(route_path)
        if route_meta is None:
            logger.warning(
                f"OpenAPI metadata missing for route: {route_path}, "
                "fallback to placeholder summary"
            )
            route_meta = {}

        path_parameters = _build_path_parameters(route_path)
        path_item = {}
        for method in _HTTP_METHODS:
            if getattr(handler_class, f"do_{method}", None) is None:
                continue
            path_item[method.lower()] = _build_operation(
                route_path, method, route_meta, path_parameters
            )
        paths[openapi_path] = path_item

    # 自检：paths 数量应与已注册路由数一致（每个路由至少一个方法）
    if len(paths) != len(api.API_ROUTES):
        logger.warning(
            f"OpenAPI paths count {len(paths)} does not match "
            f"registered routes count {len(api.API_ROUTES)}"
        )

    return {
        "openapi": _OPENAPI_VERSION,
        "info": {
            "title": "Anki Restful API",
            "version": api.api_version,
            "description": (
                "Anki 插件内嵌 HTTP 服务对外提供的 RESTful API，"
                "请求与响应遵循 JSON:API 规范。本文档由当前已注册的 "
                f"{len(paths)} 条路由动态生成。"
            ),
        },
        "servers": [{"url": _SERVER_URL}],
        "paths": paths,
        "components": {"schemas": _COMPONENT_SCHEMAS},
    }


# 路由处理器
##########################################################################


@api.api_route("/api/openapi.json")
class OpenAPIDocumentAPIHandler(api.APIHandler):
    """OpenAPI 文档处理器 - /api/openapi.json"""

    def do_GET(self):
        """生成并返回 OpenAPI 3.1 规范文档（application/json）"""
        self.send_json_response(200, build_openapi_document())


# 模块加载自检
##########################################################################


def _check_metadata_coverage():
    """自检路由元数据覆盖情况（缺失仅警告，不影响端点可用性）"""
    for route_path in api.API_ROUTES:
        if route_path not in _ROUTE_METADATA:
            logger.warning(f"OpenAPI metadata missing for route: {route_path}")


_check_metadata_coverage()
