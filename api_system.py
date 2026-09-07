"""
系统（System）API 处理模块

实现 docs/system.md 中规划的全部接口：
- GET  /api/system/version                 插件与 Anki 版本（对齐 version）
- GET  /api/system/capabilities            已注册接口清单（对齐 apiReflect）
- POST /api/system/actions/sync            触发 AnkiWeb 同步（对齐 sync）
- POST /api/system/actions/multi           批量调用（对齐 multi）
- POST /api/system/actions/reload          重载集合（对齐 reloadCollection）
- GET  /api/system/profiles                配置档案列表（对齐 getProfiles）
- GET  /api/system/profiles/active         当前配置档案（对齐 getActiveProfile）
- POST /api/system/profiles/active         切换配置档案（对齐 loadProfile）
- POST /api/system/actions/export          导出 .apkg（对齐 exportPackage）
- POST /api/system/actions/import          导入 .apkg（对齐 importPackage）
- POST /api/system/actions/check-database  数据库检查（对齐 guiCheckDatabase，
                                           按 docs/gui.md 的建议以系统动作形式提供）

以及 docs/system.md 附录中的系统扩展接口（v0.7，pylib 原生能力）：
- GET    /api/system/undo-status           撤销/重做可用性与操作名
- POST   /api/system/actions/undo          撤销
- POST   /api/system/actions/redo          重做
- GET    /api/system/config                全部集合配置
- GET    /api/system/config/<key>          读取单个配置键
- PUT    /api/system/config/<key>          写入配置键（任意 JSON 值）
- DELETE /api/system/config/<key>          删除配置键
- GET    /api/system/preferences           全局偏好（proto 转 JSON）
- PATCH  /api/system/preferences           合并式更新偏好
- POST   /api/system/actions/backup        立即创建备份
- GET    /api/system/progress              后端长任务进度
- POST   /api/system/actions/abort         中止当前后端操作
- GET    /api/system/sync/status           同步状态（是否需要同步/全量）
- GET    /api/system/sync/media-status     媒体同步状态
- POST   /api/system/actions/full-upload   全量上传（需 confirm）
- POST   /api/system/actions/full-download 全量下载（需 confirm）
- POST   /api/system/actions/optimize      数据库 vacuum+analyze
- GET    /api/system/collection            集合元信息

路由通过 @api.api_route 注册，本模块被导入时自动完成注册，无需修改 api.py。
"""

import io
import json
import logging
import os
from dataclasses import asdict
from urllib.parse import unquote, urlparse

import aqt
from aqt import gui_hooks, mw
from aqt.qt import QTimer
from anki import sync_pb2
from anki.errors import UndoEmpty
from anki.exporting import AnkiPackageExporter
from anki.importing import AnkiPackageImporter
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError

import api
import schema

logger = logging.getLogger()
logger.info("API system module loaded")


# AnkiConnect action 对照表（硬编码，用于 capabilities 接口的兼容性自查）
# 键为路由路径（<param> 形式），值为对应的 AnkiConnect action 名列表
ROUTE_ACTION_MAP = {
    "/api": ["apiReflect"],
    "/api/": ["apiReflect"],
    "/api/notes": ["addNote", "findNotes", "notesInfo", "getNoteLists"],
    "/api/notes/<note_id>": ["notesInfo", "deleteNotes", "updateNoteFields", "updateNote", "updateNoteTags"],
    "/api/notes/<note_id>/cards": ["cardsInfo", "findCards"],
    "/api/notes/batch": ["addNotes"],
    "/api/notes/validate": ["canAddNotes", "canAddNotesWithErrorDetail"],
    "/api/notes/actions/remove-empty": ["removeEmptyNotes"],
    "/api/notes/<note_id>/notetype": ["changeNotetypeOfNotes"],
    "/api/notes/<note_id>/tags": ["addTags", "removeTags", "getNoteTags"],
    "/api/tags": ["getTags", "clearUnusedTags"],
    "/api/tags/actions/replace": ["replaceTags"],
    "/api/decks": ["deckNames", "deckNamesAndIds", "getDecks", "createDeck"],
    "/api/decks/<deck_id>": ["getDeckStats", "deleteDecks"],
    "/api/decks/<deck_id>/cards": ["findCards", "cardsInfo"],
    "/api/decks/<deck_id>/notes": ["findNotes", "notesInfo"],
    "/api/decks/<deck_id>/actions/move-cards": ["changeDeck"],
    "/api/decks/<deck_id>/config": ["getDeckConfig", "saveDeckConfig", "setDeckConfigId"],
    "/api/deck-configs": ["cloneDeckConfigId"],
    "/api/deck-configs/<config_id>": ["removeDeckConfigId"],
    "/api/notetypes": ["modelNames", "modelNamesAndIds", "createModel"],
    "/api/notetypes/byname/<note_type_name>": ["modelNames", "modelFieldNames", "modelTemplates"],
    "/api/notetypes/<notetype_id>": ["modelNamesAndIds", "modelFieldNames", "updateModel"],
    "/api/notetypes/<notetype_id>/fields": ["modelFieldNames", "modelFieldAdd", "modelFieldRename", "modelFieldSetFont", "modelFieldSetFontSize", "modelFieldSetDescription"],
    "/api/notetypes/<notetype_id>/fields/<field_name>": ["modelFieldRename", "modelFieldRemove", "modelFieldReposition"],
    "/api/notetypes/<notetype_id>/templates": ["modelTemplates", "modelTemplateAdd", "modelTemplateUpdate", "modelTemplateRename", "modelTemplateReposition", "modelTemplateRemove"],
    "/api/notetypes/<notetype_id>/templates/<template_name>": ["modelTemplateRename", "modelTemplateReposition", "modelTemplateRemove"],
    "/api/notetypes/<notetype_id>/styling": ["modelStyling", "updateModelStyling"],
    "/api/notetypes/<notetype_id>/actions/find-replace": ["findAndReplaceInModels"],
    "/api/cards": ["findCards", "cardsInfo"],
    "/api/cards/<card_id>": ["cardsInfo"],
    "/api/cards/<card_id>/note": ["cardsToNotes", "notesInfo"],
    "/api/cards/<card_id>/status": ["isSuspended", "isDue", "getIntervals", "getEaseFactors"],
    "/api/cards/actions/status": ["areSuspended", "areDue", "getIntervals", "getEaseFactors"],
    "/api/cards/<card_id>/schedule": ["setSpecificValueOfCard"],
    "/api/cards/actions/suspend": ["suspend"],
    "/api/cards/actions/unsuspend": ["unsuspend"],
    "/api/cards/actions/forget": ["forgetCards"],
    "/api/cards/actions/relearn": ["relearnCards"],
    "/api/cards/actions/answer": ["answerCards"],
    "/api/cards/actions/move": ["setDeck"],
    "/api/cards/<card_id>/reviews": ["reviewsOfCard", "getLatestReviewID", "insertReviews"],
    "/api/media": ["storeMediaFile", "getMediaFilesNames"],
    "/api/media/directory": ["getMediaDirPath"],
    "/api/media/<filename>": ["retrieveMediaFile", "deleteMediaFile"],
    "/api/stats/reviews/today": ["getNumCardsReviewedToday"],
    "/api/stats/reviews/by-day": ["getNumCardsReviewedByDay"],
    "/api/stats/collection": ["getCollectionStatsHTML"],
    "/api/stats/decks": ["getDeckStats"],
    "/api/system/version": ["version"],
    "/api/system/capabilities": ["apiReflect"],
    "/api/system/actions/sync": ["sync"],
    "/api/system/actions/multi": ["multi"],
    "/api/system/actions/reload": ["reloadCollection"],
    "/api/system/profiles": ["getProfiles"],
    "/api/system/profiles/active": ["getActiveProfile", "loadProfile"],
    "/api/system/actions/export": ["exportPackage"],
    "/api/system/actions/import": ["importPackage", "guiImportFile"],
    "/api/system/actions/check-database": ["guiCheckDatabase"],
    # v0.7 系统扩展接口（pylib 原生能力，无 AnkiConnect 对照）
    "/api/system/undo-status": [],
    "/api/system/actions/undo": [],
    "/api/system/actions/redo": [],
    "/api/system/config": [],
    "/api/system/config/<key>": [],
    "/api/system/preferences": [],
    "/api/system/actions/backup": [],
    "/api/system/progress": [],
    "/api/system/actions/abort": [],
    "/api/system/sync/status": [],
    "/api/system/sync/media-status": [],
    "/api/system/actions/full-upload": [],
    "/api/system/actions/full-download": [],
    "/api/system/actions/optimize": [],
    "/api/system/collection": [],
    # v0.8 复习与工具接口（pylib 原生能力，无 AnkiConnect 对照）
    "/api/review/queue": [],
    "/api/utils/compare-answer": [],
    "/api/search/build": [],
    # v0.8 统计图接口（pylib 原生能力，无 AnkiConnect 对照）
    "/api/stats/forecast": [],
    "/api/stats/intervals": [],
    "/api/stats/hourly": [],
    "/api/stats/card-types": [],
    # v0.8 媒体扩展接口（pylib 原生能力，无 AnkiConnect 对照）
    "/api/media/actions/check": [],
    "/api/media/trash/actions/empty": [],
    "/api/media/trash/actions/restore": [],
    "/api/media/actions/force-resync": [],
    # v0.8 笔记类型扩展接口（pylib 原生能力，无 AnkiConnect 对照）
    "/api/notetypes/stock": [],
    "/api/notetypes/<notetype_id>/actions/copy": [],
    # v0.8 牌组扩展接口（pylib 原生能力，无 AnkiConnect 对照）
    "/api/decks/tree": [],
    "/api/decks/<deck_id>/actions/rebuild": [],
    "/api/decks/<deck_id>/actions/empty": [],
    "/api/decks/<deck_id>/actions/unbury": [],
    "/api/decks/<deck_id>/actions/extend-limits": [],
    "/api/decks/<deck_id>/custom-study-defaults": [],
    "/api/decks/<deck_id>/actions/custom-study": [],
    # v0.8 卡片扩展接口（pylib 原生能力，无 AnkiConnect 对照）
    "/api/cards/actions/set-flag": [],
    "/api/cards/actions/bury": [],
    "/api/cards/actions/unbury": [],
    "/api/cards/<card_id>/render": [],
    "/api/cards/<card_id>/memory-state": [],
    "/api/cards/<card_id>/scheduling-states": [],
    "/api/cards/empty-report": [],
    "/api/cards/actions/remove-empty": [],
    "/api/cards/actions/reposition": [],
    # v0.8 笔记扩展接口（笔记级 suspend/unsuspend 对照 AnkiConnect 同名卡片动作）
    "/api/notes/actions/find-replace": [],
    "/api/notes/dupes": [],
    "/api/notes/actions/render-preview": [],
    "/api/notes/actions/suspend": ["suspend"],
    "/api/notes/actions/unsuspend": ["unsuspend"],
    "/api/notes/actions/bury": [],
    # v0.8 标签扩展接口（attach/detach 对照 AnkiConnect addTags/removeTags）
    "/api/tags/tree": [],
    "/api/tags/actions/attach": ["addTags"],
    "/api/tags/actions/detach": ["removeTags"],
    "/api/tags/actions/find-replace": [],
    "/api/tags/actions/reparent": [],
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


def _run_on_main(func, timeout=60):
    """在 Anki 主线程执行函数并等待结果（ProfileManager/Qt 调用必须如此）

    HTTP 处理运行在 API 服务线程，而 mw.pm（ProfileManager）的 sqlite
    连接与 Qt/GUI 对象只允许在主线程使用，直接调用会抛出线程检查异常。
    """
    import threading

    result = {}
    done = threading.Event()

    def wrapper():
        try:
            result["value"] = func()
        except Exception as e:
            result["error"] = e
        finally:
            done.set()

    mw.taskman.run_on_main(wrapper)
    done.wait(timeout)
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _parse_json_body(req_handler, required=True):
    """
    读取并解析请求体 JSON

    Returns:
        解析后的对象；请求体为空且 required=False 时返回 {}；
        解析失败时已通过 req_handler 发送错误响应并返回 None
    """
    content_length = int(req_handler.headers.get("Content-Length", 0))
    if content_length <= 0:
        if not required:
            return {}
        req_handler.send_error_response(400, "Bad Request", "Empty request body")
        return None

    raw_body = req_handler.rfile.read(content_length).decode("utf-8")
    try:
        return json.loads(raw_body)
    except Exception:
        req_handler.send_error_response(400, "Bad Request", "Invalid JSON body")
        return None


def _get_deck_by_name(name):
    """按名称获取牌组（兼容新旧 Anki 的 by_name/byName 命名）"""
    decks = mw.col.decks
    if hasattr(decks, "by_name"):
        return decks.by_name(name)
    return decks.byName(name)


def _build_profile_resource(name):
    """构建配置档案资源对象"""
    return {
        "type": "profiles",
        "id": name,
        "attributes": {"name": name},
    }


# 路由处理器
##########################################################################


@api.api_route("/api/system/version")
class SystemVersionAPIHandler(api.APIHandler):
    """版本信息处理器 - /api/system/version"""

    def do_GET(self):
        """返回插件版本与 Anki 版本（对应 AnkiConnect version）"""
        document = {
            "data": {
                "type": "system",
                "id": "version",
                "attributes": {
                    "plugin_version": api.api_version,
                    "anki_version": aqt.appVersion,
                },
            },
            "links": {"self": "/api/system/version"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/system/capabilities")
class SystemCapabilitiesAPIHandler(api.APIHandler):
    """接口清单处理器 - /api/system/capabilities"""

    def do_GET(self):
        """返回已注册路由及方法清单（对应 AnkiConnect apiReflect）

        routes 为「路由 -> 支持的 HTTP 方法」映射；
        ankiconnect_equivalents 为全部已覆盖的 AnkiConnect action 名（去重排序）；
        meta.route_action_map 给出逐路由的 action 名对照（硬编码，仅供参考）。
        """
        routes = {}
        for route_path, handler_class in api.API_ROUTES.items():
            methods = [
                m
                for m in ["GET", "POST", "PUT", "PATCH", "DELETE"]
                if getattr(handler_class, f"do_{m}", None)
            ]
            routes[route_path] = methods

        action_names = set()
        for names in ROUTE_ACTION_MAP.values():
            action_names.update(names)

        document = {
            "data": {
                "type": "capabilities",
                "id": "capabilities",
                "attributes": {
                    "routes": routes,
                    "ankiconnect_equivalents": sorted(action_names),
                },
            },
            "links": {"self": "/api/system/capabilities"},
            "meta": {
                "total_routes": len(routes),
                "route_action_map": ROUTE_ACTION_MAP,
            },
        }
        self.send_json_response(200, document)


@api.api_route("/api/system/actions/sync")
class SystemSyncAPIHandler(api.APIHandler):
    """同步动作处理器 - /api/system/actions/sync"""

    def do_POST(self):
        """触发 AnkiWeb 同步（对应 AnkiConnect sync）

        同步会阻塞当前请求线程，期间其他请求可能被阻塞，客户端应设置较长超时。
        同步失败返回 502 与错误详情。
        col.sync_collection 已完成全部同步工作（集合与媒体），无需再调用
        GUI 同步按钮入口 mw.onSync()（那会另起一次完整 GUI 同步流）。
        """
        if not _check_collection(self):
            return

        try:
            # ProfileManager 的 sqlite/配置访问必须在主线程执行
            auth = _run_on_main(mw.pm.sync_auth)
            if not auth:
                self.req_handler.send_error_response(
                    502,
                    "Bad Gateway",
                    "Sync failed: AnkiWeb auth not configured",
                )
                return

            media_syncing_enabled = _run_on_main(mw.pm.media_syncing_enabled)
            out = mw.col.sync_collection(auth, media_syncing_enabled)
            accepted_sync_statuses = [out.NO_CHANGES, out.NORMAL_SYNC]
            if out.required not in accepted_sync_statuses:
                self.req_handler.send_error_response(
                    502,
                    "Bad Gateway",
                    f"Sync status {out.required} not one of "
                    f"{accepted_sync_statuses} - see "
                    "SyncCollectionResponse.ChangesRequired for list of sync statuses",
                )
                return
        except Exception as e:
            self.req_handler.send_error_response(
                502, "Bad Gateway", f"Sync failed: {str(e)}"
            )
            return

        document = {
            "data": None,
            "links": {"self": "/api/system/actions/sync"},
            "meta": {"synced": True},
        }
        self.send_json_response(200, document)


class _CapturedRequestHandler:
    """
    轻量响应捕获器

    包装/替代真实 req_handler 传给 api.get_api_handler 得到的处理器，
    捕获其 send_json_response / send_error_response 的输出而不是写 socket。
    提供处理器运行所需的全部属性：headers / rfile / path /
    send_json_response / send_error_response（另附 send_response /
    send_header / end_headers / wfile，兼容直接写原始响应的处理器）。
    """

    def __init__(self, path, body=None):
        self.path = path
        body_bytes = b""
        if body is not None:
            body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Length": str(len(body_bytes))}
        self.rfile = io.BytesIO(body_bytes)
        self.wfile = io.BytesIO()
        self.response_headers = {}
        self.status_code = 200
        self.document = None
        self.error = None

    def send_json_response(self, status_code, document):
        """捕获 JSON 响应（dataclass 转为 dict）"""
        self.status_code = status_code
        if hasattr(document, "__dict__"):
            self.document = asdict(document)
        else:
            self.document = document

    def send_error_response(self, status_code, title, detail=None):
        """捕获错误响应（与真实 req_handler 的错误文档结构一致）"""
        self.status_code = status_code
        self.error = asdict(schema.Error(status=status_code, title=title, detail=detail))

    # 以下为原始响应写接口，供少量绕过 send_json_response 的处理器使用
    def send_response(self, status_code):
        self.status_code = status_code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def result_body(self):
        """汇总捕获结果：优先 JSON 文档，其次错误文档，最后原始响应体"""
        if self.document is not None:
            return self.document
        if self.error is not None:
            return self.error
        raw = self.wfile.getvalue()
        if raw:
            try:
                return raw.decode("utf-8")
            except Exception:
                return None
        return None


@api.api_route("/api/system/actions/multi")
class SystemMultiAPIHandler(api.APIHandler):
    """批量调用处理器 - /api/system/actions/multi"""

    # 允许批量调用的 HTTP 方法
    ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

    def do_POST(self):
        """
        一次请求内顺序执行多个接口调用（对应 AnkiConnect multi）

        body:
        {
            "requests": [
                { "method": "GET", "path": "/api/decks" },
                { "method": "POST", "path": "/api/notes", "body": { "...": "..." } }
            ]
        }

        响应 data 为与请求一一对应的 {status, body} 数组；
        任一请求失败不影响其他请求执行（不做事务回滚）。
        """
        body = _parse_json_body(self.req_handler)
        if body is None:
            return

        requests = body.get("requests") if isinstance(body, dict) else None
        if not isinstance(requests, list):
            self.req_handler.send_error_response(
                400, "Bad Request", "'requests' must be an array"
            )
            return

        results = []
        for index, item in enumerate(requests):
            results.append(self._execute_sub_request(index, item))

        document = {
            "data": results,
            "links": {"self": "/api/system/actions/multi"},
            "meta": {"total": len(results)},
        }
        self.send_json_response(200, document)

    def _execute_sub_request(self, index, item):
        """执行单个子请求并返回 {status, body}；异常就地捕获，不影响后续请求"""
        if not isinstance(item, dict):
            return self._sub_error(400, "Bad Request", f"requests[{index}] must be an object")

        method = item.get("method")
        path = item.get("path")
        sub_body = item.get("body")

        if not isinstance(method, str) or method.upper() not in self.ALLOWED_METHODS:
            return self._sub_error(
                400,
                "Bad Request",
                f"requests[{index}].method must be one of {list(self.ALLOWED_METHODS)}",
            )
        method = method.upper()

        if not isinstance(path, str) or not path.startswith("/api/"):
            return self._sub_error(
                400, "Bad Request", f"requests[{index}].path must start with '/api/'"
            )

        captured = _CapturedRequestHandler(path, sub_body)

        # 路由匹配只使用路径部分（与 __init__.py 的分发逻辑一致），
        # 完整 path（含查询串）保留在捕获器上供处理器解析查询参数
        route_path = urlparse(path).path

        try:
            handler, params = api.get_api_handler(route_path, captured, method)
            if handler is None:
                return self._sub_error(404, "Not Found", f"API endpoint not found: {path}")

            fn = getattr(handler, f"do_{method}", None)
            if fn is None:
                return self._sub_error(
                    404, "Not Found", f"API endpoint {method} not found: {path}"
                )

            fn(**params)
        except ValueError:
            logger.exception(f"multi sub-request ValueError: {method} {path}")
            return self._sub_error(
                400, "Bad Request", f"Invalid parameter for {method} {path}"
            )
        except Exception as e:
            logger.exception(f"multi sub-request error: {method} {path}")
            return self._sub_error(
                500, "Internal Server Error", f"Error handling {method} {path}: {str(e)}"
            )

        return {"status": captured.status_code, "body": captured.result_body()}

    @staticmethod
    def _sub_error(status, title, detail):
        """构造子请求错误项（结构与 JSON:API Error 文档一致）"""
        return {
            "status": status,
            "body": asdict(schema.Error(status=status, title=title, detail=detail)),
        }


@api.api_route("/api/system/actions/reload")
class SystemReloadAPIHandler(api.APIHandler):
    """重载集合处理器 - /api/system/actions/reload"""

    def do_POST(self):
        """丢弃内存中的集合状态并从磁盘重新加载（对应 AnkiConnect reloadCollection）

        无请求体。注意：重载后 mw.col 的内部状态被重置，
        后续请求必须重新通过 mw.col 访问集合，不得持有旧引用。
        """
        if not _check_collection(self):
            return

        try:
            mw.col.reset()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error reloading collection: {str(e)}"
            )
            return

        document = {
            "data": None,
            "links": {"self": "/api/system/actions/reload"},
            "meta": {"reloaded": True},
        }
        self.send_json_response(200, document)


@api.api_route("/api/system/profiles")
class SystemProfilesAPIHandler(api.APIHandler):
    """配置档案列表处理器 - /api/system/profiles"""

    def do_GET(self):
        """返回全部配置档案名称（对应 AnkiConnect getProfiles）"""
        try:
            # ProfileManager 的 sqlite 访问必须在主线程执行
            profiles = _run_on_main(mw.pm.profiles)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error getting profiles: {str(e)}"
            )
            return

        document = {
            "data": profiles,
            "links": {"self": "/api/system/profiles"},
            "meta": {"total": len(profiles)},
        }
        self.send_json_response(200, document)


@api.api_route("/api/system/profiles/active")
class SystemActiveProfileAPIHandler(api.APIHandler):
    """当前配置档案处理器 - /api/system/profiles/active"""

    def do_GET(self):
        """返回当前活动配置档案（对应 AnkiConnect getActiveProfile）"""
        try:
            # ProfileManager 的属性访问必须在主线程执行
            name = _run_on_main(lambda: mw.pm.name)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error getting active profile: {str(e)}"
            )
            return

        if not name:
            self.req_handler.send_error_response(
                503, "Service Unavailable", "No active profile"
            )
            return

        document = {
            "data": _build_profile_resource(name),
            "links": {"self": "/api/system/profiles/active"},
        }
        self.send_json_response(200, document)

    def do_POST(self):
        """
        切换配置档案（对应 AnkiConnect loadProfile）

        body:
        { "data": { "type": "profiles", "attributes": { "name": "测试账户" } } }

        最佳努力实现，需在 GUI 线程执行，可能存在风险：
        切换档案涉及主窗口的卸载/重载操作（unloadProfileAndShowProfileManager /
        loadProfile），切换过程中集合会被关闭再重新打开，期间其他请求可能收到 503；
        当主窗口可见时，实际加载动作通过 QTimer 异步等待窗口关闭后执行，
        本接口先于切换完成返回。
        """
        body = _parse_json_body(self.req_handler)
        if body is None:
            return

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "'data' object is required"
            )
            return

        if data.get("type") != "profiles":
            self.req_handler.send_error_response(
                409, "Conflict", "Resource type must be 'profiles'"
            )
            return

        attrs = data.get("attributes", {})
        name = attrs.get("name") if isinstance(attrs, dict) else None
        if not name or not str(name).strip():
            self.req_handler.send_error_response(
                400, "Bad Request", "'attributes.name' is required"
            )
            return
        name = str(name)

        try:
            # ProfileManager 的 sqlite 访问必须在主线程执行
            profiles = _run_on_main(mw.pm.profiles)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error getting profiles: {str(e)}"
            )
            return

        if name not in profiles:
            self.req_handler.send_error_response(
                404, "Not Found", f"Profile '{name}' not found"
            )
            return

        try:
            # 切换档案涉及主窗口卸载/重载与 QTimer，全部在主线程执行
            _run_on_main(lambda: self._load_profile(name))
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error loading profile: {str(e)}"
            )
            return

        document = {
            "data": _build_profile_resource(name),
            "links": {"self": "/api/system/profiles/active"},
        }
        self.send_json_response(200, document)

    @staticmethod
    def _load_profile(name):
        """按 AnkiConnect loadProfile 的方式切换档案（需在 GUI 线程执行，可能存在风险）"""
        if mw.isVisible():
            cur_profile = mw.pm.name
            if cur_profile != name:
                mw.unloadProfileAndShowProfileManager()

                def waiter():
                    # 等待主窗口关闭后再加载目标档案；
                    # 若同步尚未结束就调用 loadProfile 会出问题，
                    # 因此通过 QTimer 轮询窗口状态
                    if mw.isVisible():
                        QTimer.singleShot(1000, waiter)
                    else:
                        SystemActiveProfileAPIHandler._load_profile(name)

                waiter()
        else:
            mw.pm.load(name)
            mw.loadProfile()
            mw.profileDiag.closeWithoutQuitting()


@api.api_route("/api/system/actions/export")
class SystemExportAPIHandler(api.APIHandler):
    """导出 .apkg 处理器 - /api/system/actions/export"""

    def do_POST(self):
        """
        导出牌组为 .apkg（对应 AnkiConnect exportPackage）

        body:
        { "deck": "CS", "path": "/tmp/CS.apkg", "include_sched": true }
        """
        if not _check_collection(self):
            return

        body = _parse_json_body(self.req_handler)
        if body is None:
            return

        if not isinstance(body, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "Request body must be an object"
            )
            return

        deck_name = body.get("deck")
        path = body.get("path")
        include_sched = bool(body.get("include_sched", False))

        if not deck_name or not str(deck_name).strip():
            self.req_handler.send_error_response(
                400, "Bad Request", "'deck' is required"
            )
            return
        if not path or not str(path).strip():
            self.req_handler.send_error_response(
                400, "Bad Request", "'path' is required"
            )
            return
        path = str(path)

        deck = _get_deck_by_name(str(deck_name))
        if deck is None:
            self.req_handler.send_error_response(
                404, "Not Found", f"Deck '{deck_name}' not found"
            )
            return

        try:
            exporter = AnkiPackageExporter(mw.col)
            exporter.did = deck["id"]
            exporter.includeSched = include_sched
            exporter.exportInto(path)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error exporting package: {str(e)}"
            )
            return

        document = {
            "data": None,
            "links": {"self": "/api/system/actions/export"},
            "meta": {
                "exported": True,
                "deck": str(deck_name),
                "path": path,
                "include_sched": include_sched,
            },
        }
        self.send_json_response(200, document)


@api.api_route("/api/system/actions/import")
class SystemImportAPIHandler(api.APIHandler):
    """导入 .apkg 处理器 - /api/system/actions/import"""

    def do_POST(self):
        """
        导入 .apkg（对应 AnkiConnect importPackage）

        body:
        { "path": "/tmp/CS.apkg" }

        导入会修改集合，建议先调用 /api/system/actions/sync 或手动备份。
        """
        if not _check_collection(self):
            return

        body = _parse_json_body(self.req_handler)
        if body is None:
            return

        if not isinstance(body, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "Request body must be an object"
            )
            return

        path = body.get("path")
        if not path or not str(path).strip():
            self.req_handler.send_error_response(
                400, "Bad Request", "'path' is required"
            )
            return
        path = str(path)

        if not os.path.isfile(path):
            self.req_handler.send_error_response(
                404, "Not Found", f"Package file '{path}' not found"
            )
            return

        try:
            # 对应 AnkiConnect startEditing，提示主窗口集合即将变化；
            # 新版本 Anki 可能已移除 requireReset，缺失时跳过；
            # 存在时为 GUI 操作，必须在主线程执行
            if hasattr(mw, "requireReset"):
                _run_on_main(mw.requireReset)
            importer = AnkiPackageImporter(mw.col, path)
            importer.run()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error importing package: {str(e)}"
            )
            return

        document = {
            "data": None,
            "links": {"self": "/api/system/actions/import"},
            "meta": {"imported": True, "path": path},
        }
        self.send_json_response(200, document)


@api.api_route("/api/system/actions/check-database")
class SystemCheckDatabaseAPIHandler(api.APIHandler):
    """数据库检查处理器 - /api/system/actions/check-database"""

    def do_POST(self):
        """触发数据库检查（对应 AnkiConnect guiCheckDatabase）

        按 docs/gui.md 的建议以系统动作形式提供，不依赖窗口显示。
        需在 GUI 线程执行，可能存在风险：检查过程可能耗时较长并显示进度，
        期间其他请求可能阻塞，客户端应设置较长超时。
        """
        if not _check_collection(self):
            return

        try:
            if not hasattr(mw, "onCheckDB"):
                self.req_handler.send_error_response(
                    500,
                    "Internal Server Error",
                    "Database check is not supported by this Anki version",
                )
                return
            # GUI 操作必须在主线程执行
            _run_on_main(mw.onCheckDB)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error checking database: {str(e)}"
            )
            return

        document = {
            "data": None,
            "links": {"self": "/api/system/actions/check-database"},
            "meta": {"checked": True},
        }
        self.send_json_response(200, document)


# 系统扩展接口（v0.7，pylib 原生能力）
##########################################################################

# 通用辅助函数
##########################################################################


# get_config 的哨兵默认值，用于区分「键不存在」（404）与「值为 null」
_CONFIG_MISSING = object()


def _build_undo_status_resource(status):
    """根据 col.undo_status() 返回的 proto 构建 undo-status 资源对象"""
    return {
        "type": "undo-status",
        "id": "current",
        "attributes": {
            "undo_available": bool(status.undo),
            "redo_available": bool(status.redo),
            "undo_label": status.undo,
            "redo_label": status.redo,
        },
    }


def _build_config_resource(key, value):
    """构建单个配置键资源对象"""
    return {
        "type": "config",
        "id": str(key),
        "attributes": {"key": key, "value": value},
    }


def _get_sync_auth(handler):
    """获取 AnkiWeb 同步凭据；未登录时发送 401 并返回 None"""
    try:
        # ProfileManager 的 sqlite 访问必须在主线程执行
        auth = _run_on_main(mw.pm.sync_auth)
    except Exception as e:
        handler.req_handler.send_error_response(
            500, "Internal Server Error", f"Error getting sync auth: {str(e)}"
        )
        return None
    if not auth:
        handler.req_handler.send_error_response(
            401, "Unauthorized", "AnkiWeb account not logged in"
        )
        return None
    return auth


def _latest_backup_path(backup_folder, before):
    """找出备份目录中本次调用新生成的 .colpkg 备份文件路径

    before 为调用前目录内 .colpkg 文件名集合；优先取差集中的文件，
    无法确定时兜底取修改时间最新者；目录不可读或无文件时返回 None。
    """
    try:
        after = {
            name for name in os.listdir(backup_folder) if name.endswith(".colpkg")
        }
    except OSError:
        return None
    candidates = sorted(after - before) or sorted(after)
    if not candidates:
        return None
    try:
        latest = max(
            candidates,
            key=lambda name: os.path.getmtime(os.path.join(backup_folder, name)),
        )
    except OSError:
        return None
    return os.path.join(backup_folder, latest)


# 撤销/重做
##########################################################################


@api.api_route("/api/system/undo-status")
class SystemUndoStatusAPIHandler(api.APIHandler):
    """撤销状态处理器 - /api/system/undo-status"""

    def do_GET(self):
        """返回撤销/重做的可用性与操作名（col.undo_status()）"""
        if not _check_collection(self):
            return

        try:
            status = mw.col.undo_status()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error getting undo status: {str(e)}"
            )
            return

        document = {
            "data": _build_undo_status_resource(status),
            "links": {"self": "/api/system/undo-status"},
        }
        self.send_json_response(200, document)


def _perform_undo_redo(handler, redo, self_link):
    """撤销/重做的公共逻辑：无可撤销/重做内容时 409，成功后返回新的 undo-status

    pylib 对 undo/redo 无特定线程要求，但不得与其他集合操作并发；
    在本请求线程执行与 AnkiConnect 调用集合方法的做法一致。
    注意：未调用 mw.reset() 刷新界面，GUI 显示可能在下一次自然刷新前滞后。
    """
    if not _check_collection(handler):
        return

    try:
        out = mw.col.redo() if redo else mw.col.undo()
    except UndoEmpty:
        handler.req_handler.send_error_response(
            409, "Conflict", "Nothing to redo" if redo else "Nothing to undo"
        )
        return
    except Exception as e:
        handler.req_handler.send_error_response(
            500,
            "Internal Server Error",
            f"Error performing {'redo' if redo else 'undo'}: {str(e)}",
        )
        return

    document = {
        "data": _build_undo_status_resource(mw.col.undo_status()),
        "links": {"self": self_link},
        "meta": {"operation": out.operation},
    }
    handler.send_json_response(200, document)


@api.api_route("/api/system/actions/undo")
class SystemUndoAPIHandler(api.APIHandler):
    """撤销处理器 - /api/system/actions/undo"""

    def do_POST(self):
        """撤销上一个操作（col.undo()），无可撤销内容时返回 409"""
        _perform_undo_redo(self, redo=False, self_link="/api/system/actions/undo")


@api.api_route("/api/system/actions/redo")
class SystemRedoAPIHandler(api.APIHandler):
    """重做处理器 - /api/system/actions/redo"""

    def do_POST(self):
        """重做上一个被撤销的操作（col.redo()），无可重做内容时返回 409"""
        _perform_undo_redo(self, redo=True, self_link="/api/system/actions/redo")


# 配置与偏好
##########################################################################


@api.api_route("/api/system/config")
class SystemConfigAPIHandler(api.APIHandler):
    """集合配置处理器 - /api/system/config"""

    def do_GET(self):
        """返回全部集合配置（col.all_config()）"""
        if not _check_collection(self):
            return

        try:
            config = mw.col.all_config()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error getting config: {str(e)}"
            )
            return

        document = {
            "data": {
                "type": "config",
                "id": "collection",
                "attributes": config,
            },
            "links": {"self": "/api/system/config"},
            "meta": {"total": len(config)},
        }
        self.send_json_response(200, document)


@api.api_route("/api/system/config/<key>")
class SystemConfigKeyAPIHandler(api.APIHandler):
    """单个配置键处理器 - /api/system/config/<key>"""

    def do_GET(self, key):
        """读取单个配置键（col.get_config(key)），键不存在返回 404"""
        # 路径参数可能是 URL 编码的配置键名
        key = unquote(key)
        if not _check_collection(self):
            return

        try:
            value = mw.col.get_config(key, default=_CONFIG_MISSING)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error getting config: {str(e)}"
            )
            return

        if value is _CONFIG_MISSING:
            self.req_handler.send_error_response(
                404, "Not Found", f"Config key '{key}' not found"
            )
            return

        document = {
            "data": _build_config_resource(key, value),
            "links": {"self": f"/api/system/config/{key}"},
        }
        self.send_json_response(200, document)

    def do_PUT(self, key):
        """写入配置键（col.set_config(key, val)）

        请求体即为要写入的任意 JSON 值（原样写入，不包 JSON:API 信封）。
        注意配置会随同步上传，pylib 建议不要存储超过几 KB 的数据。
        """
        # 路径参数可能是 URL 编码的配置键名
        key = unquote(key)
        if not _check_collection(self):
            return

        # 不用 _parse_json_body：合法的 JSON null 值会被其返回值误判为解析失败
        content_length = int(self.req_handler.headers.get("Content-Length", 0))
        if content_length <= 0:
            self.req_handler.send_error_response(
                400, "Bad Request", "Empty request body"
            )
            return
        raw_body = self.req_handler.rfile.read(content_length).decode("utf-8")
        try:
            value = json.loads(raw_body)
        except Exception:
            self.req_handler.send_error_response(
                400, "Bad Request", "Invalid JSON body"
            )
            return

        try:
            mw.col.set_config(key, value)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error setting config: {str(e)}"
            )
            return

        document = {
            "data": _build_config_resource(key, value),
            "links": {"self": f"/api/system/config/{key}"},
            "meta": {"updated": True},
        }
        self.send_json_response(200, document)

    def do_DELETE(self, key):
        """删除配置键（col.remove_config(key)）

        键不存在时同样返回 200（幂等删除）。
        """
        # 路径参数可能是 URL 编码的配置键名
        key = unquote(key)
        if not _check_collection(self):
            return

        try:
            mw.col.remove_config(key)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error removing config: {str(e)}"
            )
            return

        document = {
            "data": None,
            "links": {"self": f"/api/system/config/{key}"},
            "meta": {"deleted": True, "key": key},
        }
        self.send_json_response(200, document)


@api.api_route("/api/system/preferences")
class SystemPreferencesAPIHandler(api.APIHandler):
    """全局偏好处理器 - /api/system/preferences"""

    def do_GET(self):
        """返回全局偏好（col.get_preferences()，proto 转 JSON）

        字段名为 proto 原始 snake_case 命名，枚举值为枚举名字符串；
        proto3 默认值字段（false/0/空串）不出现在响应中。
        """
        if not _check_collection(self):
            return

        try:
            prefs = mw.col.get_preferences()
            attributes = MessageToDict(prefs, preserving_proto_field_name=True)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error getting preferences: {str(e)}"
            )
            return

        document = {
            "data": {
                "type": "preferences",
                "id": "global",
                "attributes": attributes,
            },
            "links": {"self": "/api/system/preferences"},
        }
        self.send_json_response(200, document)

    def do_PATCH(self):
        """合并式更新偏好（col.set_preferences(prefs)）

        请求体为与 GET 响应相同结构的 JSON 对象，只合并请求中出现的字段，
        未出现的字段保持原值（嵌套消息同样按字段递归合并）。
        未知字段或值类型非法返回 400。
        """
        if not _check_collection(self):
            return

        body = _parse_json_body(self.req_handler)
        if body is None:
            return
        if not isinstance(body, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "Request body must be an object"
            )
            return

        try:
            prefs = mw.col.get_preferences()
            # ParseDict 只覆盖请求中出现的字段，实现合并式更新
            ParseDict(body, prefs)
            mw.col.set_preferences(prefs)
        except ParseError as e:
            self.req_handler.send_error_response(
                400, "Bad Request", f"Invalid preferences: {str(e)}"
            )
            return
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error setting preferences: {str(e)}"
            )
            return

        # 返回更新后的完整偏好
        attributes = MessageToDict(
            mw.col.get_preferences(), preserving_proto_field_name=True
        )
        document = {
            "data": {
                "type": "preferences",
                "id": "global",
                "attributes": attributes,
            },
            "links": {"self": "/api/system/preferences"},
            "meta": {"updated": True},
        }
        self.send_json_response(200, document)


# 备份与长任务
##########################################################################


@api.api_route("/api/system/actions/backup")
class SystemBackupAPIHandler(api.APIHandler):
    """备份处理器 - /api/system/actions/backup"""

    def do_POST(self):
        """立即创建备份（col.create_backup）

        body（字段均可选，默认 true）:
        { "force": true, "wait": true }

        force=true 忽略用户配置的备份间隔；wait=true 时阻塞至备份完成，
        响应 meta.path 为新备份文件路径；wait=false 时立即返回（meta.path
        为 null），客户端用 GET /api/system/progress 轮询进度。
        注意：即使 force=true，若集合自上次备份后无变化，pylib 也可能
        不创建备份，此时 meta.created=false、meta.path=null。
        """
        if not _check_collection(self):
            return

        body = _parse_json_body(self.req_handler, required=False)
        if body is None:
            return
        if not isinstance(body, dict):
            self.req_handler.send_error_response(
                400, "Bad Request", "Request body must be an object"
            )
            return

        force = bool(body.get("force", True))
        wait = bool(body.get("wait", True))

        try:
            # ProfileManager 的路径访问必须在主线程执行
            backup_folder = _run_on_main(mw.pm.backupFolder)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error getting backup folder: {str(e)}"
            )
            return

        # 记录调用前的备份文件集合，用于调用后定位新备份路径
        try:
            before = {
                name
                for name in os.listdir(backup_folder)
                if name.endswith(".colpkg")
            }
        except OSError:
            before = set()

        try:
            # GUI 在后台线程执行备份（mw.taskman），本请求线程语义相同；
            # pylib 要求调用时无活动事务，失败会抛出异常
            created = mw.col.create_backup(
                backup_folder=backup_folder,
                force=force,
                wait_for_completion=wait,
            )
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error creating backup: {str(e)}"
            )
            return

        path = None
        if created and wait:
            path = _latest_backup_path(backup_folder, before)

        document = {
            "data": None,
            "links": {"self": "/api/system/actions/backup"},
            "meta": {
                "created": bool(created),
                "path": path,
                "force": force,
                "wait": wait,
            },
        }
        self.send_json_response(200, document)


@api.api_route("/api/system/progress")
class SystemProgressAPIHandler(api.APIHandler):
    """后端进度处理器 - /api/system/progress"""

    def do_GET(self):
        """返回后端长任务进度（col.latest_progress()）

        无任务进行中时 data 为 null；进行中时 attributes.kind 标识任务类型
        （full_sync/normal_sync/media_sync/media_check/database_check/
        importing/exporting/compute_params/compute_retention/compute_memory/
        download_update），其余字段为该任务的进度明细。
        """
        if not _check_collection(self):
            return

        try:
            progress = mw.col.latest_progress()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error getting progress: {str(e)}"
            )
            return

        kind = progress.WhichOneof("value")
        if not kind or kind == "none":
            data = None
        else:
            payload = getattr(progress, kind)
            attributes = {"kind": kind}
            if hasattr(payload, "DESCRIPTOR"):
                # 消息型负载（如 full_sync: {transferred, total}）展开为字段字典
                attributes.update(
                    MessageToDict(payload, preserving_proto_field_name=True)
                )
            else:
                # 字符串型负载（如 importing/exporting/media_check）
                attributes["message"] = payload
            data = {"type": "progress", "id": kind, "attributes": attributes}

        document = {
            "data": data,
            "links": {"self": "/api/system/progress"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/system/actions/abort")
class SystemAbortAPIHandler(api.APIHandler):
    """中止操作处理器 - /api/system/actions/abort"""

    def do_POST(self):
        """请求中止当前后端操作（col.set_wants_abort()）

        仅设置中止标志，正在运行的操作在下一个检查点自行中止；
        无操作进行时调用无副作用。
        """
        if not _check_collection(self):
            return

        try:
            mw.col.set_wants_abort()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error requesting abort: {str(e)}"
            )
            return

        document = {
            "data": None,
            "links": {"self": "/api/system/actions/abort"},
            "meta": {"abort_requested": True},
        }
        self.send_json_response(200, document)


# 同步增强
##########################################################################


@api.api_route("/api/system/sync/status")
class SystemSyncStatusAPIHandler(api.APIHandler):
    """同步状态处理器 - /api/system/sync/status"""

    def do_GET(self):
        """返回同步状态（col.sync_status(auth)），未登录 AnkiWeb 时 401

        attributes.required 为 NO_CHANGES / NORMAL_SYNC / FULL_SYNC；
        new_endpoint 为服务端要求迁移的新同步地址（无则为 null），
        本接口只报告不修改本地配置（GET 无副作用）。
        """
        if not _check_collection(self):
            return

        auth = _get_sync_auth(self)
        if auth is None:
            return

        try:
            status = mw.col.sync_status(auth)
        except Exception as e:
            self.req_handler.send_error_response(
                502, "Bad Gateway", f"Sync status check failed: {str(e)}"
            )
            return

        attributes = {
            "required": sync_pb2.SyncStatusResponse.Required.Name(status.required),
            "new_endpoint": (
                status.new_endpoint if status.HasField("new_endpoint") else None
            ),
        }
        document = {
            "data": {
                "type": "sync-status",
                "id": "collection",
                "attributes": attributes,
            },
            "links": {"self": "/api/system/sync/status"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/system/sync/media-status")
class SystemMediaSyncStatusAPIHandler(api.APIHandler):
    """媒体同步状态处理器 - /api/system/sync/media-status"""

    def do_GET(self):
        """返回媒体同步状态（col.media_sync_status()），未登录 AnkiWeb 时 401

        pylib 注释：媒体同步失败时该方法会抛出错误，此处映射为 502。
        """
        if not _check_collection(self):
            return

        # 设计契约要求本接口同样需要同步凭据
        auth = _get_sync_auth(self)
        if auth is None:
            return

        try:
            status = mw.col.media_sync_status()
        except Exception as e:
            self.req_handler.send_error_response(
                502, "Bad Gateway", f"Media sync status check failed: {str(e)}"
            )
            return

        document = {
            "data": {
                "type": "media-sync-status",
                "id": "collection",
                "attributes": {
                    "active": bool(status.active),
                    "progress": {
                        "checked": status.progress.checked,
                        "added": status.progress.added,
                        "removed": status.progress.removed,
                    },
                },
            },
            "links": {"self": "/api/system/sync/media-status"},
        }
        self.send_json_response(200, document)


def _perform_full_sync(handler, upload, self_link):
    """全量上传/下载的公共逻辑（危险操作，body 必须带 {"confirm": true}）

    对齐 GUI（aqt/sync.py）的全量同步流程：
    触发钩子 ->（下载前强制备份）-> 关闭集合 -> 执行 -> 重新打开集合并刷新界面。
    server_usn 传 None 表示跳过媒体同步（媒体将在下次普通同步时处理）。
    """
    if not _check_collection(handler):
        return

    body = _parse_json_body(handler.req_handler)
    if body is None:
        return
    if not isinstance(body, dict) or body.get("confirm") is not True:
        handler.req_handler.send_error_response(
            400,
            "Bad Request",
            'Dangerous operation requires request body {"confirm": true}',
        )
        return

    auth = _get_sync_auth(handler)
    if auth is None:
        return

    try:
        # 触发 GUI 钩子必须在主线程执行
        _run_on_main(lambda: gui_hooks.collection_will_temporarily_close(mw.col))
        if not upload:
            # 全量下载会用远端覆盖本地，先强制备份（对齐 GUI full_download）；
            # ProfileManager 的路径访问必须在主线程执行
            backup_folder = _run_on_main(mw.pm.backupFolder)
            mw.col.create_backup(
                backup_folder=backup_folder,
                force=True,
                wait_for_completion=True,
            )
        mw.col.close_for_full_sync()
    except Exception as e:
        handler.req_handler.send_error_response(
            500, "Internal Server Error", f"Full sync preparation failed: {str(e)}"
        )
        return

    # 集合已关闭，此后无论成败都必须 reopen，否则后续请求全部 503
    error = None
    try:
        mw.col.full_upload_or_download(auth=auth, server_usn=None, upload=upload)
    except Exception as e:
        error = e
    finally:
        try:
            # 重开集合并刷新界面是 GUI 操作，必须在主线程执行
            _run_on_main(lambda: mw.reopen(after_full_sync=True))
            _run_on_main(mw.reset)
        except Exception as e:
            logger.exception("full sync reopen failed")
            if error is None:
                error = e

    if error is not None:
        handler.req_handler.send_error_response(
            502, "Bad Gateway", f"Full sync failed: {str(error)}"
        )
        return

    document = {
        "data": None,
        "links": {"self": self_link},
        "meta": {"completed": True, "upload": upload},
    }
    handler.send_json_response(200, document)


@api.api_route("/api/system/actions/full-upload")
class SystemFullUploadAPIHandler(api.APIHandler):
    """全量上传处理器 - /api/system/actions/full-upload"""

    def do_POST(self):
        """用本地集合覆盖 AnkiWeb 远端（col.full_upload_or_download(upload=True)）

        body 必须带 {"confirm": true}，否则 400；未登录 AnkiWeb 时 401。
        执行后集合自动重新加载。
        """
        _perform_full_sync(
            self, upload=True, self_link="/api/system/actions/full-upload"
        )


@api.api_route("/api/system/actions/full-download")
class SystemFullDownloadAPIHandler(api.APIHandler):
    """全量下载处理器 - /api/system/actions/full-download"""

    def do_POST(self):
        """用 AnkiWeb 远端覆盖本地集合（col.full_upload_or_download(upload=False)）

        body 必须带 {"confirm": true}，否则 400；未登录 AnkiWeb 时 401。
        执行前自动强制备份，执行后集合自动重新加载。
        """
        _perform_full_sync(
            self, upload=False, self_link="/api/system/actions/full-download"
        )


# 数据库优化与集合元信息
##########################################################################


@api.api_route("/api/system/actions/optimize")
class SystemOptimizeAPIHandler(api.APIHandler):
    """数据库优化处理器 - /api/system/actions/optimize"""

    def do_POST(self):
        """执行 vacuum + analyze（col.optimize()）

        同步执行，集合较大时可能耗时较长，期间其他请求可能阻塞，
        客户端应设置较长超时。
        """
        if not _check_collection(self):
            return

        try:
            mw.col.optimize()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error optimizing database: {str(e)}"
            )
            return

        document = {
            "data": None,
            "links": {"self": "/api/system/actions/optimize"},
            "meta": {"optimized": True},
        }
        self.send_json_response(200, document)


@api.api_route("/api/system/collection")
class SystemCollectionAPIHandler(api.APIHandler):
    """集合元信息处理器 - /api/system/collection"""

    def do_GET(self):
        """返回集合元信息（crt/mod/计数/调度器版本/是否为空）

        注意 scheduler_version 为 pylib sched_ver() 的原始返回值（1 或 2），
        v3 调度器按 pylib 向后兼容约定返回 2，是否启用 v3 以 v3_scheduler 为准。
        """
        if not _check_collection(self):
            return

        try:
            attributes = {
                "created": mw.col.crt,
                # col.mod 单位为毫秒，接口契约为 Unix 秒
                "modified": mw.col.mod // 1000,
                "note_count": mw.col.note_count(),
                "card_count": mw.col.card_count(),
                "scheduler_version": mw.col.sched_ver(),
                "v3_scheduler": mw.col.v3_scheduler(),
                "is_empty": mw.col.is_empty(),
            }
        except Exception as e:
            self.req_handler.send_error_response(
                500,
                "Internal Server Error",
                f"Error getting collection info: {str(e)}",
            )
            return

        document = {
            "data": {
                "type": "collection",
                "id": "collection",
                "attributes": attributes,
            },
            "links": {"self": "/api/system/collection"},
        }
        self.send_json_response(200, document)
