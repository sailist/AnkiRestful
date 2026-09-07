"""
媒体（Media）API 处理模块

实现 docs/media.md 中规划的接口：
- POST   /api/media                 上传媒体文件（JSON+Base64 / url / path / multipart）
- GET    /api/media/<filename>      下载媒体文件（默认二进制，?format=base64 返回 JSON:API）
- DELETE /api/media/<filename>      删除媒体文件
- GET    /api/media                 媒体文件列表（pattern glob 过滤 + 分页）
- GET    /api/media/directory       媒体目录绝对路径

v0.8 扩展接口（docs/media.md 附录，pylib 原生能力）：
- POST /api/media/actions/check         检查媒体（未引用/缺失报告，对齐 media.check）
- POST /api/media/trash/actions/empty   清空回收站（对齐 media.empty_trash）
- POST /api/media/trash/actions/restore 从回收站还原（对齐 media.restore_trash）
- POST /api/media/actions/force-resync  强制下次全量媒体同步（对齐 media.force_resync）

路由通过 @api.api_route 注册，本模块被导入时自动完成注册，无需修改 api.py。
静态路由 /api/media/directory、/api/media/actions/*、/api/media/trash/actions/*
由 api.get_api_handler 精确匹配优先命中，不会落入动态路由 /api/media/<filename>。

实现对齐 AnkiConnect 的 storeMediaFile / retrieveMediaFile / deleteMediaFile /
getMediaFilesNames / getMediaDirPath。
"""

import base64
import glob
import json
import logging
import mimetypes
import os
import re
import unicodedata
import urllib.request
from urllib.parse import unquote, quote, quote_plus, urlparse, parse_qs

from aqt import mw

import api
import schema

logger = logging.getLogger()
logger.info("API media module loaded")


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


def _media_dir():
    """返回媒体目录路径（对齐 AnkiConnect getMediaDirPath）"""
    return mw.col.media.dir()


def _sanitize_filename(filename):
    """文件名安全清理（对齐 AnkiConnect retrieveMediaFile 的处理顺序）

    去除路径分隔符、统一 Unicode NFC 形式，并调用 Anki 的非法字符清理。
    清理结果为 "." / ".." 时返回空串（上层据此判定非法文件名并返回 400）。
    """
    filename = os.path.basename(filename)
    filename = unicodedata.normalize("NFC", filename)
    media = mw.col.media
    strip = getattr(media, "strip_illegal", None) or getattr(
        media, "stripIllegal", None
    )
    if strip is not None:
        filename = strip(filename)
    if filename in (".", ".."):
        return ""
    return filename


def _write_media_data(filename, data):
    """写入媒体文件，返回实际保存的文件名（兼容 Anki 新旧接口命名）"""
    media = mw.col.media
    write = getattr(media, "write_data", None) or getattr(media, "writeData", None)
    if write is None:
        # 兜底：直接写入媒体目录
        path = os.path.join(_media_dir(), filename)
        with open(path, "wb") as f:
            f.write(data)
        return filename
    return write(filename, data)


def _trash_media_file(filename):
    """将媒体文件移入回收站（对齐 AnkiConnect deleteMediaFile）"""
    mw.col.media.trash_files([filename])


def _download_url(url):
    """从 URL 下载文件内容（对齐 AnkiConnect storeMediaFile 的 url 方式）"""
    request = urllib.request.Request(url, headers={"User-Agent": "AnkiRestful"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _disposition_param(disposition, param):
    """从 Content-Disposition 头中提取参数值（支持 RFC 5987 编码形式）"""
    # 优先匹配 param*=UTF-8''... 形式
    match = re.search(r"(?:^|;)\s*" + param + r"\*=(?:[^']*)''([^;]+)", disposition)
    if match:
        return unquote(match.group(1).strip())
    # 匹配带引号形式
    match = re.search(r'(?:^|;)\s*' + param + r'="([^"]*)"', disposition)
    if match:
        return match.group(1)
    # 匹配不带引号形式
    match = re.search(r"(?:^|;)\s*" + param + r"=([^;]+)", disposition)
    if match:
        return match.group(1).strip()
    return None


def _parse_multipart(body, boundary):
    """手写简易 multipart/form-data 解析（Python 3.13 已无 cgi 模块）

    按 boundary 分割各 part，提取 Content-Disposition 中的 name 与 filename；
    二进制内容全程按 bytes 处理，不做 decode。

    Args:
        body (bytes): 请求体原始字节
        boundary (bytes): 分界符

    Returns:
        (fields, files)：fields 为 {name: str} 普通表单字段，
        files 为 {name: (filename, bytes)} 文件字段。
    """
    fields = {}
    files = {}
    delimiter = b"--" + boundary

    for segment in body.split(delimiter):
        # 跳过空段与结束标记
        if not segment or segment == b"--":
            continue
        # 每个 part 以 CRLF 开头、以 CRLF 结尾（结尾 CRLF 属于分界符）
        if segment.startswith(b"\r\n"):
            segment = segment[2:]
        if segment.endswith(b"\r\n"):
            segment = segment[:-2]
        if not segment or segment == b"--":
            continue

        header_blob, sep, content = segment.partition(b"\r\n\r\n")
        if not sep:
            continue

        # 头部按 latin-1 解码（HTTP 头部安全编码）
        headers = {}
        for line in header_blob.split(b"\r\n"):
            name, sep2, value = line.partition(b":")
            if sep2:
                headers[name.strip().lower().decode("latin-1")] = value.strip().decode(
                    "latin-1"
                )

        disposition = headers.get("content-disposition", "")
        if "form-data" not in disposition:
            continue

        field_name = _disposition_param(disposition, "name")
        filename = _disposition_param(disposition, "filename")

        if filename is not None:
            # 浏览器通常直接以 UTF-8 字节发送非 ASCII 文件名，
            # 头部按 latin-1 解码后需要还原（filename*= 形式已正确解码，不受影响）
            try:
                filename = filename.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            # 文件字段：内容保持 bytes
            files[field_name or "file"] = (filename, content)
        elif field_name:
            fields[field_name] = content.decode("utf-8", "replace")

    return fields, files


def _send_binary_response(req_handler, status_code, data, content_type):
    """直接发送二进制响应（不走 send_json_response）"""
    req_handler.send_response(status_code)
    req_handler.send_header("Content-Type", content_type)
    req_handler.send_header("Content-Length", str(len(data)))
    req_handler.send_header("Access-Control-Allow-Origin", "*")
    req_handler.end_headers()
    req_handler.wfile.write(data)


def _build_media_resource(filename, size=None):
    """构建媒体资源对象（普通 dict，与 docs/media.md 示例一致）"""
    attributes = {"filename": filename}
    if size is not None:
        attributes["size"] = size
    return {
        "type": "media",
        "id": filename,
        "attributes": attributes,
    }


# 路由处理器
##########################################################################


@api.api_route("/api/media")
class MediaCollectionAPIHandler(api.APIHandler):
    """媒体集合处理器 - /api/media（上传与列表）"""

    def do_POST(self):
        """上传媒体文件

        支持两种提交方式：
        1. JSON + Base64（data_base64 / url / path 三选一，对齐 storeMediaFile）
        2. multipart/form-data（适合大文件）
        """
        if not _check_collection(self):
            return

        # 查询参数：overwrite=false 时同名文件返回 409
        query_params = parse_qs(urlparse(self.req_handler.path).query)
        overwrite = query_params.get("overwrite", ["true"])[0].lower() != "false"

        content_length = int(self.req_handler.headers.get("Content-Length", 0))
        if content_length <= 0:
            self.req_handler.send_error_response(
                400, "Bad Request", "Empty request body"
            )
            return

        content_type = self.req_handler.headers.get("Content-Type", "")
        media_type = content_type.split(";")[0].strip().lower()

        filename = None
        file_bytes = None

        if media_type == "multipart/form-data":
            # 方式二：multipart/form-data
            match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
            if not match:
                self.req_handler.send_error_response(
                    400, "Bad Request", "Missing multipart boundary"
                )
                return
            boundary = (match.group(1) or match.group(2)).strip().encode("latin-1")

            raw_body = self.req_handler.rfile.read(content_length)
            fields, files = _parse_multipart(raw_body, boundary)
            if not files:
                self.req_handler.send_error_response(
                    400, "Bad Request", "No file part found in multipart body"
                )
                return

            # 取第一个文件字段
            filename, file_bytes = next(iter(files.values()))
            # 允许通过普通表单字段 filename 覆盖文件名
            if fields.get("filename"):
                filename = fields["filename"]
        else:
            # 方式一：JSON + Base64 / url / path
            body = _read_json_body(self.req_handler)
            if body is None:
                return

            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'data' object is required"
                )
                return

            if data.get("type") != "media":
                self.req_handler.send_error_response(
                    409, "Conflict", "Resource type must be 'media'"
                )
                return

            attrs = data.get("attributes", {})
            if not isinstance(attrs, dict):
                self.req_handler.send_error_response(
                    400, "Bad Request", "'attributes' object is required"
                )
                return

            filename = attrs.get("filename")
            if not filename:
                self.req_handler.send_error_response(
                    400, "Bad Request", "'attributes.filename' is required"
                )
                return

            if attrs.get("data_base64"):
                try:
                    file_bytes = base64.b64decode(attrs["data_base64"])
                except Exception:
                    self.req_handler.send_error_response(
                        400, "Bad Request", "Invalid base64 data"
                    )
                    return
            elif attrs.get("path"):
                try:
                    with open(attrs["path"], "rb") as f:
                        file_bytes = f.read()
                except Exception as e:
                    self.req_handler.send_error_response(
                        400, "Bad Request", f"Cannot read file from path: {str(e)}"
                    )
                    return
            elif attrs.get("url"):
                # scheme 白名单：urlopen 支持 file:// 等协议，
                # 不加限制可读取本机任意文件，仅允许 http/https
                url_scheme = urlparse(str(attrs["url"])).scheme.lower()
                if url_scheme not in ("http", "https"):
                    self.req_handler.send_error_response(
                        400,
                        "Bad Request",
                        f"Unsupported url scheme '{url_scheme}', "
                        "only http/https are allowed",
                    )
                    return
                try:
                    file_bytes = _download_url(attrs["url"])
                except Exception as e:
                    self.req_handler.send_error_response(
                        400, "Bad Request", f"Cannot download from url: {str(e)}"
                    )
                    return
            else:
                self.req_handler.send_error_response(
                    400,
                    "Bad Request",
                    "One of 'data_base64', 'url' or 'path' is required",
                )
                return

        # 文件名安全清理
        filename = _sanitize_filename(filename)
        if not filename:
            self.req_handler.send_error_response(
                400, "Bad Request", "Invalid filename"
            )
            return

        # overwrite=false 时同名文件返回 409
        target_path = os.path.join(_media_dir(), filename)
        if not overwrite and os.path.exists(target_path):
            self.req_handler.send_error_response(
                409, "Conflict", f"Media file '{filename}' already exists"
            )
            return

        # 覆盖写入前先删除已有文件（对齐 AnkiConnect deleteExisting=True）
        if overwrite and os.path.exists(target_path):
            try:
                _trash_media_file(filename)
            except Exception as e:
                logger.warning(f"Failed to trash existing media file: {str(e)}")

        try:
            saved_name = _write_media_data(filename, file_bytes)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error storing media file: {str(e)}"
            )
            return

        if not saved_name:
            saved_name = filename

        document = {
            "data": _build_media_resource(saved_name, size=len(file_bytes)),
            "links": {"self": "/api/media/" + quote(saved_name)},
        }
        self.send_json_response(201, document)

    def do_GET(self):
        """获取媒体文件列表（pattern glob 过滤 + 分页，对齐 getMediaFilesNames）"""
        if not _check_collection(self):
            return

        query_params = parse_qs(urlparse(self.req_handler.path).query)
        pattern = query_params.get("pattern", ["*"])[0]

        # 拒绝跨目录的 glob 模式，防止逃逸出媒体目录
        if "/" in pattern or "\\" in pattern or ".." in pattern:
            self.req_handler.send_error_response(
                400, "Bad Request", "Invalid pattern: must not contain path separators"
            )
            return

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

        paths = glob.glob(os.path.join(_media_dir(), pattern))
        names = sorted(os.path.basename(p) for p in paths if os.path.isfile(p))

        total = len(names)
        pages = (total + limit - 1) // limit
        offset = (page - 1) * limit
        page_names = names[offset : offset + limit]

        # 构建分页链接
        base_url = "/api/media"
        pattern_qs = ""
        if pattern != "*":
            pattern_qs = "&pattern=" + quote_plus(pattern)
        links = schema.Links(
            self=f"{base_url}?page={page}&limit={limit}{pattern_qs}",
            first=f"{base_url}?page=1&limit={limit}{pattern_qs}",
            last=f"{base_url}?page={pages}&limit={limit}{pattern_qs}",
        )
        if page > 1:
            links.prev = f"{base_url}?page={page - 1}&limit={limit}{pattern_qs}"
        if page < pages:
            links.next = f"{base_url}?page={page + 1}&limit={limit}{pattern_qs}"

        document = {
            "data": page_names,
            "links": links,
            "meta": {
                "total": total,
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total": total,
                    "pages": pages,
                },
            },
        }
        self.send_json_response(200, document)


@api.api_route("/api/media/directory")
class MediaDirectoryAPIHandler(api.APIHandler):
    """媒体目录处理器 - /api/media/directory（静态路由，优先于 /api/media/<filename>）"""

    def do_GET(self):
        """返回媒体目录绝对路径（对齐 getMediaDirPath）"""
        if not _check_collection(self):
            return

        document = {
            "data": {
                "type": "media-directory",
                "attributes": {"path": os.path.abspath(_media_dir())},
            },
            "links": {"self": "/api/media/directory"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/media/<filename>")
class MediaFileAPIHandler(api.APIHandler):
    """单个媒体文件处理器 - /api/media/<filename>"""

    def do_GET(self, filename):
        """下载媒体文件

        默认返回文件二进制（Content-Type 按扩展名推断）；
        ?format=base64 时返回 JSON:API 文档（对齐 retrieveMediaFile）。
        """
        if not _check_collection(self):
            return

        # 路径参数可能是 URL 编码的文件名
        filename = _sanitize_filename(unquote(filename))
        if not filename:
            self.req_handler.send_error_response(
                400, "Bad Request", "Invalid filename"
            )
            return
        path = os.path.join(_media_dir(), filename)
        if not os.path.isfile(path):
            self.req_handler.send_error_response(
                404, "Not Found", f"Media file '{filename}' not found"
            )
            return

        try:
            with open(path, "rb") as f:
                file_bytes = f.read()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error reading media file: {str(e)}"
            )
            return

        query_params = parse_qs(urlparse(self.req_handler.path).query)
        if query_params.get("format", [""])[0] == "base64":
            # 返回 JSON:API 文档
            resource = _build_media_resource(filename, size=len(file_bytes))
            resource["attributes"]["data_base64"] = base64.b64encode(
                file_bytes
            ).decode("ascii")
            document = {
                "data": resource,
                "links": {"self": "/api/media/" + quote(filename)},
            }
            self.send_json_response(200, document)
            return

        # 默认直接返回二进制，Content-Type 按扩展名推断
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        _send_binary_response(self.req_handler, 200, file_bytes, content_type)

    def do_DELETE(self, filename):
        """删除媒体文件（对齐 deleteMediaFile，移入回收站）"""
        if not _check_collection(self):
            return

        filename = _sanitize_filename(unquote(filename))
        if not filename:
            self.req_handler.send_error_response(
                400, "Bad Request", "Invalid filename"
            )
            return
        path = os.path.join(_media_dir(), filename)
        if not os.path.isfile(path):
            self.req_handler.send_error_response(
                404, "Not Found", f"Media file '{filename}' not found"
            )
            return

        try:
            _trash_media_file(filename)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error deleting media file: {str(e)}"
            )
            return

        document = {
            "data": None,
            "meta": {"deleted": filename},
            "links": {"self": "/api/media/" + quote(filename)},
        }
        self.send_json_response(200, document)


# v0.8 扩展接口：媒体检查与回收站
##########################################################################


def _trash_dir():
    """媒体回收站目录

    注意：回收站是媒体目录的同级目录 media.trash（如 .../Anki2/账户 1/media.trash），
    而非媒体目录的子目录；该路径是 rslib 的实现细节，pylib 未暴露查询接口。
    """
    return os.path.join(os.path.dirname(_media_dir()), "media.trash")


def _count_trash_files():
    """统计回收站中的文件数（目录不存在时返回 0）

    pylib 的 empty_trash / restore_trash 均不返回处理数量，
    通过操作前回收站内的文件数推算，供响应 meta 使用。
    """
    trash = _trash_dir()
    if not os.path.isdir(trash):
        return 0
    return sum(
        1
        for name in os.listdir(trash)
        if os.path.isfile(os.path.join(trash, name))
    )


@api.api_route("/api/media/actions/check")
class MediaCheckAPIHandler(api.APIHandler):
    """媒体检查处理器 - /api/media/actions/check"""

    def do_POST(self):
        """检查媒体文件（对齐 media.check）

        扫描全部笔记字段与媒体目录，报告未被引用的文件与缺失的文件。
        耗时长，客户端应设置长超时。无请求体。
        """
        if not _check_collection(self):
            return

        try:
            result = mw.col.media.check()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error checking media: {str(e)}"
            )
            return

        # CheckMediaResponse proto 转为 dict（repeated 字段转为 list）
        document = {
            "data": {
                "type": "media-check",
                "attributes": {
                    "unused": list(result.unused),
                    "missing": list(result.missing),
                    "missing_media_notes": [
                        int(nid) for nid in result.missing_media_notes
                    ],
                    "report": result.report,
                    "have_trash": bool(result.have_trash),
                },
            },
            "links": {"self": "/api/media/actions/check"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/media/trash/actions/empty")
class MediaTrashEmptyAPIHandler(api.APIHandler):
    """清空回收站处理器 - /api/media/trash/actions/empty"""

    def do_POST(self):
        """彻底清空媒体回收站（对齐 media.empty_trash），无请求体"""
        if not _check_collection(self):
            return

        # 先统计回收站文件数，供响应 meta 使用
        count = _count_trash_files()
        try:
            mw.col.media.empty_trash()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error emptying trash: {str(e)}"
            )
            return

        document = {
            "data": None,
            "meta": {"emptied": count},
            "links": {"self": "/api/media/trash/actions/empty"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/media/trash/actions/restore")
class MediaTrashRestoreAPIHandler(api.APIHandler):
    """回收站还原处理器 - /api/media/trash/actions/restore"""

    def do_POST(self):
        """将回收站中的媒体文件还原（对齐 media.restore_trash），无请求体"""
        if not _check_collection(self):
            return

        # 先统计回收站文件数，供响应 meta 使用
        count = _count_trash_files()
        try:
            mw.col.media.restore_trash()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error restoring trash: {str(e)}"
            )
            return

        document = {
            "data": None,
            "meta": {"restored": count},
            "links": {"self": "/api/media/trash/actions/restore"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/media/actions/force-resync")
class MediaForceResyncAPIHandler(api.APIHandler):
    """强制全量媒体同步处理器 - /api/media/actions/force-resync"""

    def do_POST(self):
        """删除媒体同步索引，下次 sync 时全量重传（对齐 media.force_resync）

        用于媒体同步损坏后的修复。无请求体。
        """
        if not _check_collection(self):
            return

        try:
            mw.col.media.force_resync()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error forcing media resync: {str(e)}"
            )
            return

        document = {
            "data": None,
            "meta": {"forced_resync": True},
            "links": {"self": "/api/media/actions/force-resync"},
        }
        self.send_json_response(200, document)
