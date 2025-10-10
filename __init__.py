# Anki Restful API Addon
# Provides a simple RESTful API for Anki
import sys
import os

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__))))

import json
import threading
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from anki.hooks import addHook
from aqt.utils import showInfo
import traceback
import logging
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
import schema

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger()


def setup_logging():
    """Setup local rotating file logging for INFO level."""
    try:
        addon_dir = os.path.dirname(os.path.abspath(__file__))
        log_dir = os.path.join(addon_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "ankirestful.log")

        # Avoid adding duplicate handlers on module reload
        for h in list(logger.handlers):
            try:
                if (
                    isinstance(h, logging.FileHandler)
                    and getattr(h, "baseFilename", None) == log_file
                ):
                    return
            except Exception:
                continue

        file_handler = RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s [%(filename)s:%(lineno)d]: %(message)s"
            )
        )
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)
        # prevent double logging to root
        logger.propagate = False
    except Exception:
        # If logging setup fails, don't break the addon
        pass


setup_logging()


# Load configuration
def load_config():
    """Load configuration from config.json"""
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # Default configuration if file doesn't exist or is invalid
        return {
            "server": {"host": "localhost", "port": 8080, "auto_start": True},
            "api": {"enable_cors": True, "max_connections": 10},
        }


CONFIG = load_config()


class AnkiAPIHandler(BaseHTTPRequestHandler):

    def do_METHOD(self, method):
        """Handle METHOD requests"""
        try:
            # Parse URL path
            parsed_path = urllib.parse.urlparse(self.path)
            path = parsed_path.path

            logger.info(f"HTTP {method} {path}")

            # Route handling
            if path.startswith("/api/"):
                self.handle_api_request(path, method=method)
            else:
                self.send_error_response(404, f"Endpoint {path} not found")

        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            # 客户端中断连接，这是正常情况，不需要记录错误
            logger.info(f"Client disconnected during {method} request: {str(e)}")
        except Exception as e:
            try:
                self.send_error_response(500, f"Internal server error: {str(e)}")
            except (
                ConnectionAbortedError,
                ConnectionResetError,
                BrokenPipeError,
            ) as conn_e:
                # 客户端中断连接，这是正常情况，不需要记录错误
                logger.info(
                    f"Client disconnected while sending internal error: {str(conn_e)}"
                )

    def do_DELETE(self):
        return self.do_METHOD("DELETE")

    def do_POST(self):
        return self.do_METHOD("POST")

    def do_PUT(self):
        return self.do_METHOD("PUT")

    def do_OPTIONS(self):
        """处理OPTIONS请求，用于CORS预检"""
        try:
            # 解析URL路径
            parsed_path = urllib.parse.urlparse(self.path)
            path = parsed_path.path

            logger.info(f"HTTP OPTIONS {path}")

            # 设置CORS头
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.api+json")

            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"
            )
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type, Authorization"
            )
            self.send_header("Access-Control-Max-Age", "86400")  # 24小时

            self.end_headers()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            # 客户端中断连接，这是正常情况，不需要记录错误
            logger.info(f"Client disconnected during OPTIONS request: {str(e)}")
        except Exception as e:
            try:
                self.send_error_response(500, f"Internal server error: {str(e)}")
            except (
                ConnectionAbortedError,
                ConnectionResetError,
                BrokenPipeError,
            ) as conn_e:
                # 客户端中断连接，这是正常情况，不需要记录错误
                logger.info(
                    f"Client disconnected while sending internal error: {str(conn_e)}"
                )

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path

        # hack: restart handling
        if path == "/restart":
            self.handle_restart()
        else:
            return self.do_METHOD("GET")

    def handle_restart(self):
        """Handle /restart endpoint - 动态重载API处理器"""
        try:
            # 重新加载api模块
            import sys

            # 如果api模块已加载，先删除它
            if "api" in sys.modules:
                del sys.modules["api"]

            # 重新导入api模块
            import api

            # 调用重载函数
            result = api.reload_api_handlers()

            # 更新全局api模块引用
            globals()["api"] = api

            response = {
                "message": "API server handlers reloaded successfully",
                "result": result,
                "version": api.api_version,
                "timestamp": time.time(),
            }
            self.send_json_response(200, response)

        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            # 客户端中断连接，这是正常情况，不需要记录错误
            logger.info(f"Client disconnected during restart: {str(e)}")
        except Exception as e:
            try:
                self.send_error_response(
                    500, f"Failed to reload API handlers: {str(e)}"
                )
            except (
                ConnectionAbortedError,
                ConnectionResetError,
                BrokenPipeError,
            ) as conn_e:
                # 客户端中断连接，这是正常情况，不需要记录错误
                logger.info(f"Client disconnected while sending error: {str(conn_e)}")
            except Exception as inner_e:
                # 其他异常仍然需要记录
                logger.error(f"Error handling restart exception: {str(inner_e)}")

    def handle_api_request(self, path, method):
        """Handle API requests - 委托给对应的处理器"""
        try:
            # 动态导入api模块以获取最新的处理器
            import api

            handler, params = api.get_api_handler(path, self, method)

            if handler:
                fn = getattr(handler, f"do_{method.upper()}")
                if not fn:
                    self.send_error_response(
                        404, f"API endpoint {method} not found: {path}"
                    )
                    return
                # 如果有参数，传递给handle方法；否则直接调用
                try:
                    fn(**params)
                except ValueError:
                    self.send_error_response(
                        400,
                        "Bad Request",
                        f"Invalid parameter: {params}, {traceback.format_exc()}",
                    )
                except Exception as e:
                    self.send_error_response(
                        500,
                        "Internal Server Error",
                        f"Error getting note details: {str(e)}, {traceback.format_exc()}",
                    )

            else:
                self.send_error_response(
                    404, f"API endpoint {method} not found: {path}"
                )

        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            # 客户端中断连接，这是正常情况，不需要记录错误
            logger.info(f"Client disconnected during API request: {str(e)}")
        except ImportError as e:
            try:
                self.send_error_response(
                    500, f"Failed to import API handlers: {str(e)}"
                )
            except (
                ConnectionAbortedError,
                ConnectionResetError,
                BrokenPipeError,
            ) as conn_e:
                # 客户端中断连接，这是正常情况，不需要记录错误
                logger.info(
                    f"Client disconnected while sending import error: {str(conn_e)}"
                )
        except Exception:
            try:
                self.send_error_response(
                    500, f"Error handling API request: {traceback.format_exc()}"
                )
            except (
                ConnectionAbortedError,
                ConnectionResetError,
                BrokenPipeError,
            ) as conn_e:
                # 客户端中断连接，这是正常情况，不需要记录错误
                logger.info(
                    f"Client disconnected while sending API error: {str(conn_e)}"
                )

    def send_json_response(self, status_code, document):
        """发送JSON响应"""
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/vnd.api+json")
            # 动态导入CONFIG以获取最新配置
            try:
                from . import CONFIG

                if CONFIG.get("api", {}).get("enable_cors", True):
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header(
                        "Access-Control-Allow-Methods",
                        "GET, POST, PUT, DELETE, OPTIONS",
                    )
                    self.send_header(
                        "Access-Control-Allow-Headers", "Content-Type, Authorization"
                    )
            except Exception as e:
                # 如果导入失败，默认启用CORS
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header(
                    "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"
                )
                self.send_header(
                    "Access-Control-Allow-Headers", "Content-Type, Authorization"
                )

            self.end_headers()

            # 将dataclass转换为字典，然后序列化为JSON
            if hasattr(document, "__dict__"):
                # 对于dataclass对象，使用asdict
                json_data = asdict(document)
            else:
                # 对于普通字典，直接使用
                json_data = document

            self.wfile.write(json.dumps(json_data, ensure_ascii=False).encode("utf-8"))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            # 客户端中断连接，这是正常情况，不需要记录错误
            logger.info(f"Client disconnected: {str(e)}")
        except Exception as e:
            # 其他异常仍然需要记录
            logger.error(f"Error sending response: {str(e)}")

    def send_error_response(self, status_code, title, detail=None):
        """Send error response"""
        try:
            error = schema.Error(
                status=status_code,
                title=title,
                detail=detail,
            )
            self.send_json_response(status_code, error)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            # 客户端中断连接，这是正常情况，不需要记录错误
            logger.info(f"Client disconnected while sending error: {str(e)}")
        except Exception as e:
            # 其他异常仍然需要记录
            logger.error(f"Error sending error response: {str(e)}")

    def log_message(self, format, *args):
        """Override log_message to reduce console output"""
        pass  # Silence default logging


class AnkiAPIServer:
    def __init__(self):
        server_config = CONFIG.get("server", {})
        self.host = server_config.get("host", "localhost")
        self.port = server_config.get("port", 8080)
        self.server = None
        self.server_thread = None

    def start(self):
        """Start the API server"""
        try:
            self.server = HTTPServer((self.host, self.port), AnkiAPIHandler)
            self.server_thread = threading.Thread(
                target=self.server.serve_forever, daemon=True
            )
            self.server_thread.start()
            showInfo(f"Anki API Server started on http://{self.host}:{self.port}")
            logger.info(f"Server started at http://{self.host}:{self.port}")
            return True
        except Exception as e:
            showInfo(f"Failed to start API server: {str(e)}")
            logger.error(f"Failed to start API server: {str(e)}")
            return False

    def stop(self):
        """Stop the API server"""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            if self.server_thread:
                self.server_thread.join()
            showInfo("Anki API Server stopped")
            logger.info("Server stopped")


# Global server instance
api_server = None


def start_api_server():
    """Start the API server"""
    global api_server
    if api_server is None:
        api_server = AnkiAPIServer()
        api_server.start()


def stop_api_server():
    """Stop the API server"""
    global api_server
    if api_server is not None:
        api_server.stop()
        api_server = None


# Initialize addon
def init_addon():
    """Initialize the addon"""
    # Auto-start API server if configured
    if CONFIG.get("server", {}).get("auto_start", True):
        start_api_server()


def cleanup():
    """Cleanup when addon is unloaded"""
    stop_api_server()


# Add hooks
addHook("profileLoaded", init_addon)
