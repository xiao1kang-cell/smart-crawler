import json
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from loguru import logger

# 获取旧 shuler 根目录；日志仍写在旧目录，方便沿用旧平台排查习惯。
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = Path(__file__).resolve().parents[6]


def _load_platform_env() -> Path | None:
    try:
        from app.envfile import load_env_file

        return load_env_file(PROJECT_DIR / ".env")
    except Exception:
        raw_env = (
            os.getenv("APP_ENV")
            or os.getenv("SC_ENV")
            or os.getenv("ENV")
            or "test"
        ).strip().lower()
        suffix = "production" if raw_env in {"prod", "production", "online"} else "test"
        path = PROJECT_DIR / f".env.{suffix}"
        if path.exists():
            load_dotenv(path, verbose=True)
            return path
    return None


def _legacy_app_env(loaded_env_path: Path | None) -> str:
    raw_env = (
        os.getenv("APP_ENV")
        or os.getenv("SC_ENV")
        or os.getenv("ENV")
        or ""
    ).strip().lower()
    if raw_env in {"prod", "production", "online"}:
        return "prod"
    if loaded_env_path and loaded_env_path.name == ".env.production":
        return "prod"
    return "dev"


def _bridge_redis_env() -> None:
    redis_url = os.getenv("AMAZON_VOC_REDIS_URL") or os.getenv("REDIS_URL")
    if not redis_url:
        return
    parsed = urlparse(redis_url)
    if parsed.hostname:
        os.environ.setdefault("REDIS_HOST", parsed.hostname)
    if parsed.port:
        os.environ.setdefault("REDIS_PORT", str(parsed.port))
    if parsed.username:
        os.environ.setdefault("REDIS_USERNAME", parsed.username)
    if parsed.password:
        os.environ.setdefault("REDIS_PASSWORD", parsed.password)
    db = parsed.path.strip("/") if parsed.path else ""
    if db:
        os.environ.setdefault("REDIS_DB", db)
    os.environ.setdefault("REDIS_QUEUE_DB", os.getenv("AMAZON_VOC_REDIS_QUEUE_DB", db or "0"))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except Exception:
        return default


_loaded_env_path = _load_platform_env()
APP_ENV = _legacy_app_env(_loaded_env_path)
os.environ["APP_ENV"] = APP_ENV
_bridge_redis_env()
logger.info(f"当前环境: APP_ENV={APP_ENV}，加载新平台配置: {_loaded_env_path or 'process env'}")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# 固定日志目录（绝对路径），避免因不同 cwd 写到不同 logs 目录
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 共享日志格式（daemon/api 等多线程共用一个文件，保留 worker 字段方便区分）
def _format_shared(record):
    extra = record['extra']
    extra.setdefault('worker', '')
    extra.setdefault('account', '')
    extra.setdefault('country', '')
    extra.setdefault('asin', '')
    extra.setdefault('ip', '')
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "worker={extra[worker]} account={extra[account]} country={extra[country]} "
        "asin={extra[asin]} ip={extra[ip]} | "
        "<level>{message}</level>\n"
    )

# 独立进程日志格式（每个 worker 独立文件，去掉冗余的 worker= 字段）
def _format_per_process(record):
    extra = record['extra']
    extra.setdefault('account', '')
    extra.setdefault('country', '')
    extra.setdefault('asin', '')
    extra.setdefault('ip', '')
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "account={extra[account]} country={extra[country]} "
        "asin={extra[asin]} ip={extra[ip]} | "
        "<level>{message}</level>\n"
    )


def setup_logger(name: str, worker_id: int = None) -> str:
    """
    各入口程序启动时调用。
    worker_id 有值时每个进程写独立文件：logs/single_worker_01_2026-05-18.log
    worker_id 为 None 时共用一个文件：logs/daemon_2026-05-18.log
    返回实际日志文件路径（供调用方 print 展示）。
    """
    import sys
    # 移除 loguru 默认的 stderr sink，防止终端缓冲区（Windows Terminal）积累海量日志占满内存
    logger.remove()
    if worker_id is not None:
        prefix = f"{name}_{worker_id:02d}"
        fmt = _format_per_process
    else:
        prefix = name
        fmt = _format_shared
    log_file = os.path.join(LOG_DIR, f"{prefix}_{{time:YYYY-MM-DD}}.log")
    logger.add(
        log_file,
        rotation="00:00",
        retention="7 days",
        encoding="utf-8",
        level="INFO",
        format=fmt,
        colorize=False,
        enqueue=True,
    )
    # 终端只打 WARNING 以上，避免 100 进程刷屏，但能看到关键告警
    logger.add(
        sys.stderr,
        level="WARNING",
        format="<yellow>{time:HH:mm:ss}</yellow> | <level>{level: <8}</level> | <level>{message}</level>",
        colorize=True,
        enqueue=True,
    )
    return os.path.join(LOG_DIR, f"{prefix}_{__import__('datetime').date.today()}.log")

# ------------------------------
# 全局配置常量
# ------------------------------
# MongoDB
MONGO_URI = os.getenv("MONGO_URI")
MONGO_COLL_ACCOUNTS = os.getenv("MONGO_COLL_ACCOUNTS")
MONGO_COLL_USAGE = os.getenv("MONGO_COLL_USAGE")

# MySQL - 主数据库
MYSQL_HOST = os.getenv("MYSQL_HOST") or "localhost"
MYSQL_PORT = _env_int("MYSQL_PORT", 3306)
MYSQL_USER = os.getenv("MYSQL_USER") or os.getenv("POSTGRES_USER") or "smart_crawler"
MYSQL_PASSWORD = (
    os.getenv("MYSQL_PASSWORD")
    or os.getenv("POSTGRES_PASSWORD")
    or os.getenv("ADMIN_PASSWORD")
    or "local-dev-password"
)
MYSQL_DB = os.getenv("MYSQL_DB") or os.getenv("POSTGRES_DB") or "smart_crawler"

# MySQL - Shulex 数据库（独立连接）
MYSQL_HOST_SHULEX = os.getenv("MYSQL_HOST_SHULEX") or MYSQL_HOST
MYSQL_PORT_SHULEX = _env_int("MYSQL_PORT_SHULEX", MYSQL_PORT)
MYSQL_USER_SHULEX = os.getenv("MYSQL_USER_SHULEX") or MYSQL_USER
MYSQL_PASSWORD_SHULEX = os.getenv("MYSQL_PASSWORD_SHULEX") or MYSQL_PASSWORD
MYSQL_DB_SHULEX = os.getenv("MYSQL_DB_SHULEX") or MYSQL_DB

# Redis
REDIS_HOST = os.getenv("REDIS_HOST") or "localhost"
REDIS_PORT = _env_int("REDIS_PORT", 6379)
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
REDIS_USERNAME = os.getenv("REDIS_USERNAME") or None
REDIS_DB = _env_int("REDIS_DB", 0)
REDIS_QUEUE_DB = _env_int("REDIS_QUEUE_DB", REDIS_DB)

# 比特浏览器可执行文件路径（本机直接启动时使用）
BIT_BROWSER_APP_PATH = os.getenv("BIT_BROWSER_APP_PATH", "")

# Admin API 认证配置
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "@admin")
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "change-me-session-secret")
ADMIN_SESSION_TTL_SECONDS = int(os.getenv("ADMIN_SESSION_TTL_SECONDS", "43200"))

# 公网 API Key 鉴权（外部调用方在 X-API-Key header 中传入）
API_KEY = os.getenv("API_KEY", "")

# Sellersprite API config.
SELLERSPRITE_API_KEY = os.getenv("SELLERSPRITE_API_KEY", "")
SELLERSPRITE_BASE_URL = os.getenv("SELLERSPRITE_BASE_URL", "https://api.sellersprite.com/v1").rstrip("/")

def _oss_result_prefix() -> str:
    explicit = os.getenv("AMAZON_VOC_OSS_RESULT_PREFIX") or os.getenv("OSS_RESULT_PREFIX")
    if explicit:
        return explicit.strip().strip("/") or "crawler-data/reviews"

    legacy_root = os.getenv("AMAZON_VOC_RESULT_PREFIX", "").strip().strip("/")
    if not legacy_root:
        return "crawler-data/reviews"
    if legacy_root == "reviews" or legacy_root.endswith("/reviews"):
        return legacy_root
    return f"{legacy_root}/reviews"


# Aliyun OSS result callback config. Keep secrets in process env / deployment
# config, not in source-controlled code.
OSS_ENDPOINT = os.getenv("AMAZON_VOC_OSS_ENDPOINT") or os.getenv("OSS_ENDPOINT", "")
OSS_BUCKET = os.getenv("AMAZON_VOC_OSS_BUCKET") or os.getenv("OSS_BUCKET", "")
OSS_ACCESS_KEY_ID = os.getenv("AMAZON_VOC_OSS_ACCESS_KEY_ID") or os.getenv("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.getenv("AMAZON_VOC_OSS_ACCESS_KEY_SECRET") or os.getenv("OSS_ACCESS_KEY_SECRET", "")
OSS_RESULT_PREFIX = _oss_result_prefix()
try:
    OSS_SIGNED_URL_EXPIRES_SECONDS = int(os.getenv("AMAZON_VOC_OSS_SIGNED_URL_EXPIRES_SECONDS") or os.getenv("OSS_SIGNED_URL_EXPIRES_SECONDS", "604800"))
except Exception:
    OSS_SIGNED_URL_EXPIRES_SECONDS = 604800
try:
    CALLBACK_TIMEOUT_SECONDS = int(os.getenv("CALLBACK_TIMEOUT_SECONDS", "10"))
except Exception:
    CALLBACK_TIMEOUT_SECONDS = 10
CALLBACK_SECRET = os.getenv("CALLBACK_SECRET", "")


METRICS_ENABLED = _env_bool("METRICS_ENABLED", False)
INFLUXDB_LOG_SINK_ENABLED = METRICS_ENABLED and _env_bool("INFLUXDB_LOG_SINK_ENABLED", True)

# ------------------------------
# loguru → InfluxDB 行为日志 sink
# 将带有 asin/account 上下文的日志直接写入 InfluxDB crawler_log measurement，
# 方便按账号/ASIN/时间做行为轨迹分析和策略调整。
# ------------------------------

# 只上报这些级别（避免 DEBUG 撑爆 InfluxDB）
_LOG_SINK_LEVELS = {"INFO", "WARNING", "ERROR", "CRITICAL"}

# 推送条件：日志必须带有 asin 或 account 上下文（纯系统日志不上报）
_LOG_SINK_REQUIRE_CONTEXT = True


def _loguru_influxdb_sink(message):
    """
    loguru 自定义 sink：将每条有业务上下文的日志写入 InfluxDB crawler_log。
    依赖 influxdb_sink.get_reporter()，init_metrics() 之前的日志只落文件。
    """
    try:
        record = message.record
        level_name = record["level"].name
        if level_name not in _LOG_SINK_LEVELS:
            return

        extra = record["extra"]
        asin    = extra.get("asin", "")
        account = extra.get("account", "")
        if _LOG_SINK_REQUIRE_CONTEXT and not asin and not account:
            return

        from app.crawlers.amazon_crawler.shuler.util.influxdb_sink import get_reporter
        reporter = get_reporter()
        if reporter is None:
            return   # init_metrics() 尚未调用，静默跳过

        reporter.log.report(
            level=level_name,
            account_id=account,
            asin=asin,
            country=extra.get("country", ""),
            module=record["name"],
            function=record["function"],
            line=record["line"],
            message=record["message"],
            worker=extra.get("worker", ""),
        )
    except Exception:
        pass   # sink 失败绝不能影响主流程

if INFLUXDB_LOG_SINK_ENABLED:
    logger.add(_loguru_influxdb_sink, level="INFO", format="{message}", colorize=False)
