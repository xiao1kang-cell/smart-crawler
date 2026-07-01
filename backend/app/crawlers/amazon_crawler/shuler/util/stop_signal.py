"""
跨 worker 共享的"优雅停止"机制（基于 Redis 信号 + OS 信号）。

使用方法：
  1. 主进程入口注册信号处理器：
       from app.crawlers.amazon_crawler.shuler.util.stop_signal import install_signal_handlers
       install_signal_handlers()
     这样 SIGTERM / SIGINT / SIGBREAK 都会写 Redis key

  2. 各 worker 主循环顶部轮询：
       from app.crawlers.amazon_crawler.shuler.util.stop_signal import check_stop_signal
       while True:
           if check_stop_signal():
               break
           ...

  3. 手动触发（不依赖信号，最可靠）：
       python -c "from app.crawlers.amazon_crawler.shuler.util.stop_signal import set_stop_signal; set_stop_signal('manual')"
     等 worker 在当前任务结束后退出后：
       redis-cli DEL crawler:stop_signal
     如果设置了 CRAWLER_STOP_SIGNAL_SCOPE，则 key 为 crawler:stop_signal:<scope>。

设计要点：
- key 带 10 分钟 TTL，避免遗忘 DEL 导致下次启动立即退出
- value 带写入时间戳，新启动的进程会忽略启动前留下的旧信号
- Redis 不可达时 check 返回 False（继续运行，不影响业务）
- Redis 客户端进程内 lazy 缓存，避免重复连接
"""
import time
import os
from typing import Callable, Optional

from loguru import logger

STOP_SIGNAL_KEY = "crawler:stop_signal"
STOP_SIGNAL_SCOPE_ENV = "CRAWLER_STOP_SIGNAL_SCOPE"
STOP_SIGNAL_TTL_SECONDS = 600
_PROCESS_START_TS = time.time()

_stop_signal_redis = None  # 进程内缓存的 Redis 客户端


def _normalize_scope(scope: Optional[str]) -> str:
    text = str(scope or "").strip().lower()
    if not text:
        return ""
    chars = []
    for ch in text:
        if ch.isalnum() or ch in ("_", "-", "."):
            chars.append(ch)
        else:
            chars.append("_")
    return "".join(chars).strip("_")


def configure_stop_signal_scope(scope: str, override: bool = True) -> str:
    """配置当前进程及其子进程使用的 stop_signal 作用域。"""
    normalized = _normalize_scope(scope)
    if normalized and (override or not os.getenv(STOP_SIGNAL_SCOPE_ENV)):
        os.environ[STOP_SIGNAL_SCOPE_ENV] = normalized
    return normalized


def get_stop_signal_scope(scope: Optional[str] = None) -> str:
    if scope is not None:
        return _normalize_scope(scope)
    return _normalize_scope(os.getenv(STOP_SIGNAL_SCOPE_ENV) or os.getenv("STOP_SIGNAL_SCOPE"))


def get_stop_signal_key(scope: Optional[str] = None) -> str:
    normalized = get_stop_signal_scope(scope)
    return f"{STOP_SIGNAL_KEY}:{normalized}" if normalized else STOP_SIGNAL_KEY


def _get_redis():
    global _stop_signal_redis
    if _stop_signal_redis is not None:
        return _stop_signal_redis
    try:
        import redis as _redis_lib
        from app.crawlers.amazon_crawler.shuler.util.config import (
            REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
        )
        _stop_signal_redis = _redis_lib.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            username=REDIS_USERNAME, password=REDIS_PASSWORD,
            db=REDIS_DB, decode_responses=True,
            socket_connect_timeout=3, socket_timeout=3,
        )
    except Exception:
        _stop_signal_redis = None
    return _stop_signal_redis


def check_stop_signal(scope: Optional[str] = None) -> bool:
    """检查 Redis 中是否设置了停止信号。Redis 不可达时返回 False（继续运行）。"""
    r = _get_redis()
    if r is None:
        return False
    try:
        value = r.get(get_stop_signal_key(scope))
        if not value:
            return False
        # 新格式：<created_ts>|<reason>。进程启动前遗留的 stop_signal 不应影响新进程。
        try:
            created_ts = float(str(value).split("|", 1)[0])
            return created_ts >= (_PROCESS_START_TS - 1.0)
        except (TypeError, ValueError):
            # 兼容旧格式 / 手动 redis-cli SET <stop_signal_key> 1 EX 600。
            return True
    except Exception:
        return False


def clear_stop_signal(scope: Optional[str] = None) -> bool:
    """清除停止信号。用于确认要重新启动 worker 前手动恢复。"""
    r = _get_redis()
    if r is None:
        logger.warning("[StopSignal] Redis 不可达，无法清除停止信号")
        return False
    try:
        key = get_stop_signal_key(scope)
        deleted = int(r.delete(key) or 0)
        logger.warning(f"[StopSignal] 已清除 Redis key={key}, deleted={deleted}")
        return deleted > 0
    except Exception as e:
        logger.error(f"[StopSignal] 清除失败: {e}")
        return False


def set_stop_signal(reason: str = "", scope: Optional[str] = None):
    """写入停止信号到 Redis（带 TTL，10 分钟后自动清除）。"""
    r = _get_redis()
    if r is None:
        logger.warning("[StopSignal] Redis 不可达，无法写入停止信号")
        return
    try:
        key = get_stop_signal_key(scope)
        value = f"{time.time():.3f}|{reason or '1'}"
        r.set(key, value, ex=STOP_SIGNAL_TTL_SECONDS)
        logger.warning(
            f"[StopSignal] 已写入 Redis key={key} reason={reason} TTL={STOP_SIGNAL_TTL_SECONDS}s"
        )
    except Exception as e:
        logger.error(f"[StopSignal] 写入失败: {e}")


def install_signal_handlers(
        logger_prefix: str = "Main",
        on_signal: Optional[Callable[[int], None]] = None,
        scope: Optional[str] = None,
):
    """注册 SIGTERM / SIGINT / SIGBREAK 处理器：收到信号即写入 Redis stop_signal。

    Windows 上 SIGTERM 不一定能触发，SIGINT 和 SIGBREAK 更可靠。
    """
    import signal

    def _on_stop_signal(signum, frame):
        sig_name = signum
        try:
            sig_name = signal.Signals(signum).name
        except Exception:
            pass
        logger.warning(f"[{logger_prefix}] 收到信号 {sig_name}，触发优雅停止")
        if on_signal:
            try:
                on_signal(signum)
            except Exception as e:
                logger.error(f"[{logger_prefix}] 停止信号回调异常: {e}")
        set_stop_signal(reason=f"signal_{signum}", scope=scope)

    signal.signal(signal.SIGTERM, _on_stop_signal)
    signal.signal(signal.SIGINT, _on_stop_signal)
    if hasattr(signal, "SIGBREAK"):  # Windows-only Ctrl+Break
        signal.signal(signal.SIGBREAK, _on_stop_signal)
