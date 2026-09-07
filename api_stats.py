"""
统计（Stats）API 处理模块

实现 docs/stats.md 中规划的接口：
- GET /api/stats/reviews/today   今日复习统计（对齐 getNumCardsReviewedToday）
- GET /api/stats/reviews/by-day  按日复习统计（对齐 getNumCardsReviewedByDay）
- GET /api/stats/collection      集合统计报告（对齐 getCollectionStatsHTML）
- GET /api/stats/decks           各牌组统计（对齐 getDeckStats）

v0.8 扩展接口（docs/stats.md 附录，pylib 原生统计图）：
- GET /api/stats/forecast    未来到期预测（对齐 stats.dueGraph）
- GET /api/stats/intervals   间隔分布（对齐 stats.ivlGraph）
- GET /api/stats/hourly      分时段记忆保留率（对齐 stats.hourGraph）
- GET /api/stats/card-types  卡片类型构成（对齐 stats.cardGraph）

路由通过 @api.api_route 注册，本模块被导入时自动完成注册，无需修改 api.py。
"""

import logging
import time
from urllib.parse import urlparse, parse_qs

from anki.stats import PERIOD_LIFE, PERIOD_MONTH, PERIOD_YEAR
from anki.utils import ids2str
from aqt import mw

import api
import schema

logger = logging.getLogger()
logger.info("API stats module loaded")


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


def _day_cutoff():
    """返回调度器的日界时间戳（兼容 Anki 新旧接口命名）"""
    sched = mw.col.sched
    cutoff = getattr(sched, "day_cutoff", None)
    if cutoff is None:
        cutoff = sched.dayCutoff
    return cutoff


def _parse_bool(value, default=False):
    """解析布尔查询参数（true/1/yes 视为 True，false/0/no 视为 False）"""
    if value is None:
        return default
    return value.strip().lower() in ("true", "1", "yes")


def _send_html_response(req_handler, status_code, html):
    """直接发送 HTML 响应（不走 send_json_response）"""
    data = html.encode("utf-8")
    req_handler.send_response(status_code)
    req_handler.send_header("Content-Type", "text/html; charset=utf-8")
    req_handler.send_header("Content-Length", str(len(data)))
    req_handler.send_header("Access-Control-Allow-Origin", "*")
    req_handler.end_headers()
    req_handler.wfile.write(data)


def _collect_deck_tree_children(parent_node):
    """递归收集牌组树的所有节点（对齐 AnkiConnect collectDeckTreeChildren）"""
    all_nodes = {parent_node.deck_id: parent_node}
    for child in parent_node.children:
        for deck_id, child_node in _collect_deck_tree_children(child).items():
            all_nodes[deck_id] = child_node
    return all_nodes


# 路由处理器
##########################################################################


@api.api_route("/api/stats/reviews/today")
class StatsReviewsTodayAPIHandler(api.APIHandler):
    """今日复习统计处理器 - /api/stats/reviews/today"""

    def do_GET(self):
        """返回今日复习卡片数（对齐 getNumCardsReviewedToday）"""
        if not _check_collection(self):
            return

        review_count = mw.col.db.scalar(
            "select count() from revlog where id > ?",
            (_day_cutoff() - 86400) * 1000,
        )

        document = {
            "data": {
                "type": "stats",
                "id": "reviews-today",
                "attributes": {
                    "date": time.strftime("%Y-%m-%d", time.localtime()),
                    "review_count": review_count,
                },
            },
            "links": {"self": "/api/stats/reviews/today"},
        }
        self.send_json_response(200, document)


@api.api_route("/api/stats/reviews/by-day")
class StatsReviewsByDayAPIHandler(api.APIHandler):
    """按日复习统计处理器 - /api/stats/reviews/by-day"""

    def do_GET(self):
        """返回有复习记录的每一天（对齐 getNumCardsReviewedByDay）"""
        if not _check_collection(self):
            return

        # 按日界时刻的小时数偏移，保证「天」的划分与 Anki 一致
        hour_offset = (
            int(time.strftime("%H", time.localtime(_day_cutoff()))) * 3600
        )
        rows = mw.col.db.all(
            'select date(id/1000 - ?, "unixepoch", "localtime") as day, '
            "count() from revlog group by day order by day desc",
            hour_offset,
        )

        resources = [
            {
                "type": "stats",
                "id": day,
                "attributes": {"date": day, "review_count": count},
            }
            for day, count in rows
        ]

        document = {
            "data": resources,
            "links": {"self": "/api/stats/reviews/by-day"},
            "meta": {
                "days": len(resources),
                "total_reviews": sum(count for _, count in rows),
            },
        }
        self.send_json_response(200, document)


@api.api_route("/api/stats/collection")
class StatsCollectionAPIHandler(api.APIHandler):
    """集合统计报告处理器 - /api/stats/collection"""

    def do_GET(self):
        """返回集合级统计报告（对齐 getCollectionStatsHTML）

        format=html（默认）时 Content-Type 为 text/html，直接返回报告 HTML；
        format=json 为后续版本扩展，暂返回 400。
        """
        if not _check_collection(self):
            return

        query_params = parse_qs(urlparse(self.req_handler.path).query)
        output_format = query_params.get("format", ["html"])[0].lower()

        if output_format != "html":
            self.req_handler.send_error_response(
                400,
                "Bad Request",
                f"Unsupported format '{output_format}', only 'html' is available",
            )
            return

        # whole_collection=true 统计整个集合；默认 false 统计当前选中牌组
        whole_collection = _parse_bool(
            query_params.get("whole_collection", [None])[0], default=False
        )

        try:
            stats = mw.col.stats()
            stats.wholeCollection = whole_collection
            html = stats.report()
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error generating stats: {str(e)}"
            )
            return

        _send_html_response(self.req_handler, 200, html)


@api.api_route("/api/stats/decks")
class StatsDecksAPIHandler(api.APIHandler):
    """各牌组统计处理器 - /api/stats/decks"""

    def do_GET(self):
        """按牌组返回统计（对齐 getDeckStats）

        deck_ids 查询参数为逗号分隔的牌组 id，缺省时返回全部牌组。
        """
        if not _check_collection(self):
            return

        query_params = parse_qs(urlparse(self.req_handler.path).query)
        deck_ids_param = query_params.get("deck_ids", [None])[0]

        deck_ids = None
        if deck_ids_param:
            deck_ids = []
            for raw_id in deck_ids_param.split(","):
                raw_id = raw_id.strip()
                if not raw_id:
                    continue
                try:
                    deck_ids.append(int(raw_id))
                except ValueError:
                    self.req_handler.send_error_response(
                        400, "Bad Request", f"Invalid deck id '{raw_id}'"
                    )
                    return

        try:
            due_tree = mw.col.sched.deck_due_tree()
            all_nodes = _collect_deck_tree_children(due_tree)
        except Exception as e:
            self.req_handler.send_error_response(
                500, "Internal Server Error", f"Error getting deck stats: {str(e)}"
            )
            return

        resources = []
        for deck_id in sorted(all_nodes.keys()):
            # 跳过 deck_due_tree 的合成根节点（deck_id=0、name=""，非真实牌组）
            if deck_id == 0:
                continue
            # 指定了 deck_ids 时只返回命中的牌组（未命中的静默跳过，与 AnkiConnect 一致）
            if deck_ids is not None and deck_id not in deck_ids:
                continue
            node = all_nodes[deck_id]
            resources.append(
                {
                    "type": "deck-stats",
                    "id": str(deck_id),
                    "attributes": {
                        "deck_id": deck_id,
                        "name": node.name,
                        "new_count": node.new_count,
                        "learn_count": node.learn_count,
                        "review_count": node.review_count,
                        # 低版本 Anki 无 total_in_deck，缺失时返回 None
                        "total_in_deck": getattr(node, "total_in_deck", None),
                    },
                }
            )

        document = {
            "data": resources,
            "links": schema.Links(self="/api/stats/decks"),
            "meta": {"total": len(resources)},
        }
        self.send_json_response(200, document)


# v0.8 扩展接口：统计图
##########################################################################

# period 查询参数到 pylib PERIOD_* 常量的映射
_PERIOD_TYPES = {
    "month": PERIOD_MONTH,
    "year": PERIOD_YEAR,
    "life": PERIOD_LIFE,
}

# 各统计图端点对应的 CollectionStats 方法
_GRAPH_METHODS = {
    "forecast": "dueGraph",
    "intervals": "ivlGraph",
    "hourly": "hourGraph",
    "card-types": "cardGraph",
}


def _parse_graph_params(handler):
    """解析统计图公共查询参数

    返回 (deck_id, period, output_format)；校验失败时已发送错误响应并返回
    None，调用方应直接 return。
    """
    query_params = parse_qs(urlparse(handler.req_handler.path).query)

    deck_id = None
    raw_deck_id = query_params.get("deck_id", [None])[0]
    if raw_deck_id:
        try:
            deck_id = int(raw_deck_id)
        except ValueError:
            handler.req_handler.send_error_response(
                400, "Bad Request", f"Invalid deck id '{raw_deck_id}'"
            )
            return None

    period = query_params.get("period", ["month"])[0].lower()
    if period not in _PERIOD_TYPES:
        handler.req_handler.send_error_response(
            400,
            "Bad Request",
            f"Invalid period '{period}', must be one of month/year/life",
        )
        return None

    output_format = query_params.get("format", ["html"])[0].lower()
    if output_format not in ("html", "json"):
        handler.req_handler.send_error_response(
            400,
            "Bad Request",
            f"Unsupported format '{output_format}', only 'html' and 'json' are available",
        )
        return None

    return deck_id, period, output_format


def _build_collection_stats(deck_id):
    """构建 CollectionStats 实例并按 deck_id 限定统计范围（缺省整个集合）

    指定牌组时直接向实例注入牌组范围（含子牌组），避免改动当前选中牌组。
    """
    stats = mw.col.stats()
    stats.wholeCollection = deck_id is None
    if deck_id is not None:
        deck_ids = mw.col.decks.deck_and_child_ids(deck_id)
        lim = ids2str(deck_ids)
        stats._limit = lambda: lim
        stats._revlogLimit = (
            lambda: "cid in (select id from cards where did in %s)" % lim
        )
    return stats


def _handle_graph_request(handler, graph_key):
    """统计图端点公共处理逻辑

    format=html（默认）时直返对应 Graph 方法的 HTML 片段；
    format=json 按 docs/stats.md 附录分期落地，本期返回 501。
    """
    if not _check_collection(handler):
        return

    params = _parse_graph_params(handler)
    if params is None:
        return
    deck_id, period, output_format = params

    if deck_id is not None and mw.col.decks.get(deck_id, default=False) is None:
        handler.req_handler.send_error_response(
            404, "Not Found", f"Deck with ID {deck_id} not found"
        )
        return

    if output_format == "json":
        # 结构化输出需调用后端 graphs 服务或 SQL 聚合，实现成本较高，分期落地
        handler.req_handler.send_error_response(
            501,
            "Not Implemented",
            "format=json is not implemented yet (planned for a future "
            "release); please use format=html",
        )
        return

    try:
        stats = _build_collection_stats(deck_id)
        stats.type = _PERIOD_TYPES[period]
        html = getattr(stats, _GRAPH_METHODS[graph_key])()
    except Exception as e:
        handler.req_handler.send_error_response(
            500, "Internal Server Error", f"Error generating stats: {str(e)}"
        )
        return

    _send_html_response(handler.req_handler, 200, html)


@api.api_route("/api/stats/forecast")
class StatsForecastAPIHandler(api.APIHandler):
    """未来到期预测处理器 - /api/stats/forecast"""

    def do_GET(self):
        """返回未来到期预测图（对齐 stats.dueGraph）"""
        _handle_graph_request(self, "forecast")


@api.api_route("/api/stats/intervals")
class StatsIntervalsAPIHandler(api.APIHandler):
    """间隔分布处理器 - /api/stats/intervals"""

    def do_GET(self):
        """返回卡片间隔分布图（对齐 stats.ivlGraph）"""
        _handle_graph_request(self, "intervals")


@api.api_route("/api/stats/hourly")
class StatsHourlyAPIHandler(api.APIHandler):
    """分时段记忆保留率处理器 - /api/stats/hourly"""

    def do_GET(self):
        """返回分时段记忆保留率图（对齐 stats.hourGraph）"""
        _handle_graph_request(self, "hourly")


@api.api_route("/api/stats/card-types")
class StatsCardTypesAPIHandler(api.APIHandler):
    """卡片类型构成处理器 - /api/stats/card-types"""

    def do_GET(self):
        """返回卡片类型构成图（对齐 stats.cardGraph）"""
        _handle_graph_request(self, "card-types")
