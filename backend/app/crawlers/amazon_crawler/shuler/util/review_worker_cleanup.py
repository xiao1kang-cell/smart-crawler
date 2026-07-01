"""Shared cleanup helpers for review workers."""
import traceback
from typing import Callable, Optional

from loguru import logger


def close_review_browser_target(target, worker_label: str = "") -> bool:
    """Close either PlaywrightReviewScraper or Reviews/AmazonBase browser state."""
    if target is None:
        return False

    prefix = f"[{worker_label}] " if worker_label else ""
    try:
        if hasattr(target, "close_session"):
            target.close_session()
        elif hasattr(target, "_close_browser"):
            try:
                target._close_browser()
            except Exception:
                logger.error(f"{prefix}_close_browser 异常: {traceback.format_exc()}")
    except Exception:
        logger.error(f"{prefix}close_session 异常: {traceback.format_exc()}")

    try:
        if hasattr(target, "_quit_fingerprint_browser"):
            target._quit_fingerprint_browser()
        else:
            fingerprint_id = (
                getattr(target, "fingerprint_id", None)
                or getattr(getattr(target, "account_info", None), "fingerprint_id", None)
            )
            if fingerprint_id:
                from app.crawlers.amazon_crawler.shuler.util.fingerprint_browser import close_browser
                close_browser(fingerprint_id)
    except Exception:
        logger.error(f"{prefix}关闭指纹浏览器异常: {traceback.format_exc()}")

    return True


def cleanup_review_worker_on_signal(
    *,
    worker_label: str,
    signum: int,
    mysql_db,
    account_manager,
    task_info: Optional[dict],
    browser_target,
    active_account,
    close_browser_callback: Callable[[], None],
) -> None:
    """Best-effort cleanup before PyCharm/console escalates SIGINT to SIGKILL."""
    task_info = dict(task_info or {})
    if task_info:
        _reset_review_task(mysql_db, task_info, worker_label, signum)

    account_for_release = getattr(browser_target, "account_info", None) or active_account
    _release_review_account_direct(mysql_db, account_for_release, worker_label, signum)

    # Log before closing: in debugger/console stop, SIGKILL may arrive during close.
    logger.warning(f"[{worker_label}] 停止信号兜底：开始关闭指纹浏览器")
    try:
        close_browser_callback()
        logger.warning(f"[{worker_label}] 停止信号兜底：指纹浏览器关闭流程已完成")
    except Exception:
        logger.error(f"[{worker_label}] 停止信号关闭指纹浏览器异常: {traceback.format_exc()[:500]}")

    _force_release_account_manager(account_manager, worker_label)


def _reset_review_task(mysql_db, task_info: dict, worker_label: str, signum: int) -> None:
    table = task_info.get("table", "")
    row_id = task_info.get("id")
    asin = task_info.get("asin", "")
    if not row_id:
        return

    try:
        if table == "crawl_single_tasks":
            mysql_db.reset_single_tasks_by_ids(
                [int(row_id)],
                error_msg=f"review worker interrupted by signal {signum}",
            )
            task_name = "single"
        elif table == "crawler_asin_tasks_temp":
            mysql_db.reset_temp_tasks_by_ids(
                [int(row_id)],
                note=f"review worker interrupted by signal {signum}",
            )
            task_name = "temp"
        elif table == "asin_tasks":
            mysql_db.update_task_status(
                int(row_id),
                0,
                f"review worker interrupted by signal {signum}",
                table_name="asin_tasks",
            )
            task_name = "legacy"
        else:
            logger.warning(f"[{worker_label}] 停止信号兜底：未知评论任务表 table={table} id={row_id}")
            return
        logger.warning(f"[{worker_label}] 停止信号兜底：已退回 {task_name} 任务 id={row_id} asin={asin}")
    except Exception:
        logger.error(f"[{worker_label}] 停止信号退回评论任务异常: {traceback.format_exc()[:500]}")


def _release_review_account_direct(mysql_db, account, worker_label: str, signum: int) -> None:
    username = getattr(account, "username", "") if account else ""
    platform = getattr(account, "platform", "amazon") if account else "amazon"
    if username:
        try:
            mysql_db.release_account_by_username(
                username,
                platform=platform or "amazon",
                note=f"review worker interrupted by signal {signum}",
            )
            logger.warning(f"[{worker_label}] 停止信号兜底：已直接释放账号 {username}")
        except Exception:
            logger.error(f"[{worker_label}] 停止信号直接释放账号异常: {traceback.format_exc()[:500]}")


def _force_release_account_manager(account_manager, worker_label: str) -> None:
    try:
        account_manager.force_release()
        logger.warning(f"[{worker_label}] 停止信号兜底：已通知账号管理器释放当前账号")
    except Exception:
        logger.error(f"[{worker_label}] 停止信号释放账号异常: {traceback.format_exc()}")
