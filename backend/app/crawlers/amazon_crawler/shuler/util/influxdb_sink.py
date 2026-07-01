"""
InfluxDB 指标写入模块 (MetricsCollector + Reporters)

职责：
  1. MetricsCollector：缓冲 + 批量写入 InfluxDB（线程安全，可插拔后端）
  2. Reporters：语义化上报（RequestReporter / AccountReporter / RateReporter）
  3. 无 InfluxDB 时自动降级为 ConsoleSink，零依赖

部署配置（在 .env 文件中添加）：
  INFLUXDB_URL=http://localhost:8086
  INFLUXDB_TOKEN=your-token-here
  INFLUXDB_ORG=crawler
  INFLUXDB_BUCKET=crawler

用法：
    from app.crawlers.amazon_crawler.shuler.util.influxdb_sink import init_metrics, get_reporter
    init_metrics()   # 主进程启动时调用一次
    r = get_reporter()
    if r:
        r.request.report(account_id="acc1", proxy_ip="1.2.3.4", site="US",
                         status_code=200, latency_ms=350.0, is_blocked=False)
"""
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from loguru import logger

# ─── 从环境变量读取配置（config.py 已加载 .env，这里直接 getenv）───────────────
INFLUXDB_URL    = os.getenv("INFLUXDB_URL", "")
INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN", "")
INFLUXDB_ORG    = os.getenv("INFLUXDB_ORG", "crawler")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "crawler")

BUFFER_SIZE    = int(os.getenv("METRICS_BUFFER_SIZE", "200"))
FLUSH_INTERVAL = float(os.getenv("METRICS_FLUSH_INTERVAL", "5"))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# InfluxDB 指标已停用，默认不初始化 Collector、不启动 flush 线程、不降级 ConsoleSink。
# 后续如需恢复，env 设置 METRICS_ENABLED=true 即可启用原逻辑。
METRICS_ENABLED = _env_bool("METRICS_ENABLED", False)


# ─── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class MetricPoint:
    measurement: str
    tags: Dict[str, str]
    fields: Dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Sink 协议 ─────────────────────────────────────────────────────────────────

class BaseSink:
    name: str = "base"

    def write(self, points: List[MetricPoint]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class ConsoleSink(BaseSink):
    """开发/降级用，只打印日志，不写外部系统"""
    name = "console"

    def write(self, points: List[MetricPoint]) -> None:
        for p in points:
            logger.debug(f"[Metrics:{p.measurement}] tags={p.tags} fields={p.fields}")


class InfluxDBSink(BaseSink):
    """生产用，Line Protocol 写入 InfluxDB 2.x"""
    name = "influxdb"

    def __init__(self, url: str, token: str, org: str, bucket: str):
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS
        self._bucket = bucket
        self._org = org
        # 明确设置连接超时（5s），防止 InfluxDB 无响应时 HTTP 请求无限阻塞
        self._client = InfluxDBClient(url=url, token=token, org=org, timeout=5_000)
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
        logger.info(f"[InfluxDBSink] 连接成功: url={url} bucket={bucket}")

    def write(self, points: List[MetricPoint]) -> None:
        lines = [self._to_line(p) for p in points if p.fields]
        if not lines:
            return
        record = "\n".join(lines)
        last_exc = None
        for attempt in range(3):
            try:
                self._write_api.write(bucket=self._bucket, org=self._org,
                                      record=record)
                return
            except Exception as e:
                last_exc = e
                if attempt < 2:
                    time.sleep(min(2 ** attempt, 4))
        logger.warning(f"[InfluxDBSink] 写入失败(3次重试): {last_exc}")

    @staticmethod
    def _to_line(p: MetricPoint) -> str:
        meas = _esc_tag(p.measurement)
        tags_str = ",".join(
            f"{_esc_tag(k)}={_esc_tag(str(v))}"
            for k, v in sorted(p.tags.items()) if v is not None and str(v)
        )
        fields_str = ",".join(
            f"{_esc_tag(k)}={_field_val(v)}"
            for k, v in p.fields.items() if v is not None
        )
        ts_ns = int(p.timestamp.timestamp() * 1e9)
        if tags_str:
            return f"{meas},{tags_str} {fields_str} {ts_ns}"
        return f"{meas} {fields_str} {ts_ns}"

    def query(self, flux: str) -> List[Dict]:
        result = []
        try:
            tables = self._client.query_api().query(flux, org=self._org)
            for table in tables:
                for rec in table.records:
                    result.append(rec.values)
        except Exception as e:
            logger.warning(f"[InfluxDBSink] query 失败: {str(e)}")
        return result

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def _esc_tag(s: str) -> str:
    return s.replace(" ", r"\ ").replace(",", r"\,").replace("=", r"\=")


def _field_val(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}i"
    if isinstance(v, float):
        return str(v)
    safe = str(v).replace('"', r'\"')
    return f'"{safe}"'


# ─── MetricsCollector ──────────────────────────────────────────────────────────

class MetricsCollector:
    """
    缓冲 + 批量 flush 的指标采集器（线程安全）。
    支持多个 Sink 后端并行写入。
    """

    def __init__(
        self,
        buffer_size: int = BUFFER_SIZE,
        flush_interval: float = FLUSH_INTERVAL,
        default_tags: Optional[Dict[str, str]] = None,
    ):
        self._buffer: List[MetricPoint] = []
        self._sinks: List[BaseSink] = []
        self._lock = threading.Lock()
        self._buffer_size = buffer_size
        self._flush_interval = flush_interval
        self._default_tags = default_tags or {}
        self._running = False
        self._flush_thread: Optional[threading.Thread] = None

    def add_sink(self, sink: BaseSink) -> "MetricsCollector":
        self._sinks.append(sink)
        return self

    def record(self, measurement: str, fields: Dict[str, Any],
               tags: Optional[Dict[str, str]] = None) -> None:
        merged_tags = {**self._default_tags, **(tags or {})}
        point = MetricPoint(measurement=measurement, tags=merged_tags, fields=fields)
        batch_to_write = None
        with self._lock:
            self._buffer.append(point)
            if len(self._buffer) >= self._buffer_size:
                batch_to_write = self._buffer[:]
                self._buffer.clear()
        # 锁外做 I/O：避免 InfluxDB 慢/超时时 worker 主线程卡死
        if batch_to_write:
            self._do_write(batch_to_write)

    def flush(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            batch = self._buffer[:]
            self._buffer.clear()
        # 同样在锁外写入
        self._do_write(batch)

    def _do_write(self, batch: List[MetricPoint]) -> None:
        for sink in self._sinks:
            try:
                sink.write(batch)
            except Exception:
                logger.warning(f"[MetricsCollector] sink={sink.name} 写入失败: {traceback.format_exc()}")

    def start(self) -> None:
        self._running = True
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="MetricsFlushThread"
        )
        self._flush_thread.start()

    def shutdown(self) -> None:
        self._running = False
        if self._flush_thread:
            self._flush_thread.join(timeout=10)
        self.flush()
        for sink in self._sinks:
            sink.close()

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(self._flush_interval)
            try:
                self.flush()
            except Exception:
                pass


# ─── Reporters ─────────────────────────────────────────────────────────────────

class RequestReporter:
    """HTTP 请求结果上报 → measurement: crawler_request"""

    def __init__(self, collector: MetricsCollector):
        self._c = collector

    def report(self, *, account_id: str, proxy_ip: str, site: str,
               status_code: int, latency_ms: float, is_blocked: bool,
               url_path: str = "") -> None:
        status_group = f"{status_code // 100}xx" if status_code else "0xx"
        self._c.record(
            "crawler_request",
            fields={
                "status_code": status_code,
                "latency_ms": float(latency_ms),
                "is_blocked": is_blocked,
                "url_path": url_path[:80],
            },
            tags={
                "account_id": account_id,
                "proxy_ip": proxy_ip,
                "site": site,
                "status_group": status_group,
            },
        )


class AccountReporter:
    """账号状态 + 封禁/会话事件上报"""

    def __init__(self, collector: MetricsCollector):
        self._c = collector

    def report_status(self, *, account_id: str, site: str, status: str,
                      request_count_1h: int = 0, last_success_ago_s: float = 0) -> None:
        self._c.record(
            "account_status",
            fields={
                "request_count_1h": request_count_1h,
                "last_success_ago_s": float(last_success_ago_s),
            },
            tags={"account_id": account_id, "site": site, "status": status},
        )

    def report_ban(self, *, account_id: str, site: str, reason: str = "") -> None:
        self._c.record(
            "account_ban",
            fields={"count": 1},
            tags={"account_id": account_id, "site": site,
                  "reason": reason or "unknown"},
        )

    def report_session(self, *, account_id: str, site: str, pages: int,
                       tasks: int, session_seq: int, rest_minutes: float) -> None:
        self._c.record(
            "account_session",
            fields={
                "pages": pages,
                "tasks": tasks,
                "rest_minutes": float(rest_minutes),
            },
            tags={"account_id": account_id, "site": site,
                  "session_seq": str(session_seq)},
        )


class RateReporter:
    """速率控制参数上报 → measurement: rate_control"""

    def __init__(self, collector: MetricsCollector):
        self._c = collector

    def report(self, *, site: str, current_rps: float,
               target_rps: float, throttled: bool) -> None:
        self._c.record(
            "rate_control",
            fields={
                "current_rps": float(current_rps),
                "target_rps": float(target_rps),
                "throttled": throttled,
            },
            tags={"site": site},
        )


class LogReporter:
    """
    行为日志上报 → measurement: crawler_log
    把 loguru bind(asin/account/country) 上下文的日志写入 InfluxDB，
    方便按账号/ASIN/时间做行为轨迹分析。
    """

    def __init__(self, collector: MetricsCollector):
        self._c = collector

    def report(self, *, level: str, account_id: str, asin: str, country: str,
               module: str, function: str, line: int, message: str,
               worker: str = "") -> None:
        self._c.record(
            "crawler_log",
            tags={
                "level":   level,
                "account": account_id,
                "country": country,
                "module":  module,
            },
            fields={
                "message":  message[:500],   # InfluxDB string field 限长
                "asin":     asin,
                "function": function,
                "line":     line,
                "worker":   worker,
            },
        )


# ─── 全局单例 ───────────────────────────────────────────────────────────────────

class CombinedReporter:
    """所有 Reporter 的组合入口，通过 get_reporter() 获取全局实例"""

    def __init__(self, collector: MetricsCollector):
        self.request = RequestReporter(collector)
        self.account = AccountReporter(collector)
        self.rate = RateReporter(collector)
        self.log = LogReporter(collector)
        self._collector = collector


_collector_instance: Optional[MetricsCollector] = None
_reporter_instance: Optional[CombinedReporter] = None
_init_lock = threading.Lock()


def init_metrics(env: str = "prod") -> Optional[MetricsCollector]:
    """
    初始化全局 MetricsCollector。
    应在主进程启动时调用一次（get_reviews_main.py 的 __main__ 入口）。
    有 InfluxDB 配置 → InfluxDBSink；否则 → ConsoleSink（零外部依赖）。
    """
    global _collector_instance, _reporter_instance
    if not METRICS_ENABLED:
        _collector_instance = None
        _reporter_instance = None
        return None

    with _init_lock:
        if _collector_instance is not None:
            return _collector_instance

        collector = MetricsCollector(
            buffer_size=BUFFER_SIZE,
            flush_interval=FLUSH_INTERVAL,
            default_tags={"env": env},
        )

        if INFLUXDB_URL and INFLUXDB_TOKEN:
            try:
                sink = InfluxDBSink(INFLUXDB_URL, INFLUXDB_TOKEN,
                                    INFLUXDB_ORG, INFLUXDB_BUCKET)
                collector.add_sink(sink)
                logger.info(f"[Metrics] InfluxDB 模式: {INFLUXDB_URL}/{INFLUXDB_BUCKET}")
            except Exception:
                logger.warning(f"[Metrics] InfluxDB 初始化失败，降级 ConsoleSink:"
                               f"\n{traceback.format_exc()}")
                collector.add_sink(ConsoleSink())
        else:
            collector.add_sink(ConsoleSink())
            logger.info("[Metrics] 未配置 INFLUXDB_URL，使用 ConsoleSink（开发模式）")

        collector.start()
        _collector_instance = collector
        _reporter_instance = CombinedReporter(collector)
        return collector


def get_reporter() -> Optional[CombinedReporter]:
    """获取全局 CombinedReporter 实例（init_metrics 之前返回 None）"""
    return _reporter_instance


def get_influxdb_sink() -> Optional[InfluxDBSink]:
    """获取全局 InfluxDBSink 实例（供 BanAnalyzer 查询用）"""
    if _collector_instance is None:
        return None
    for sink in _collector_instance._sinks:
        if isinstance(sink, InfluxDBSink):
            return sink
    return None
