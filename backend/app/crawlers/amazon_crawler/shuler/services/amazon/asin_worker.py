"""
商品详情爬取 Worker（多线程版）

curl_cffi 是 I/O 密集型操作，网络等待期间自动释放 GIL，多线程足够，
相比多进程节省大量内存，且 MySQL/Redis 连接可复用，启动更快。

代理优先级：
  1. 静态IP池（与评论任务共享，空闲时使用）
  2. 动态旋转代理（静态IP全忙时自动降级）

启动方式：
  python -m amazon_crawler.shuler.services.amazon.asin_worker --workers 30 --region US
  python -m amazon_crawler.shuler.services.amazon.asin_worker --workers 10  # 不指定region则抓所有
"""

import argparse
import random
import threading
import time
import traceback
from typing import Optional
from urllib.parse import urlparse

import redis as redis_lib
from loguru import logger

from app.crawlers.amazon_crawler.shuler.services.amazon.asins import ASINS, _USER_AGENTS
from app.crawlers.amazon_crawler.shuler.util.config import (
    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
)
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message
from app.crawlers.amazon_crawler.shuler.util.static_ip_pool import StaticIPPool
from app.crawlers.amazon_crawler.shuler.util.stop_signal import (
    check_stop_signal, clear_stop_signal, configure_stop_signal_scope,
    get_stop_signal_key, install_signal_handlers,
)
from app.crawlers.amazon_crawler.shuler.util.task_queue_redis import (
    KEY_ASIN, parse_queue_payload, pop_task as _redis_pop_task,
)
from app.crawlers.amazon_crawler.shuler.util.worker_recovery import WorkerRecoveryTracker

# 共享 Redis 客户端（redis-py 线程安全）和 StaticIPPool
_redis_client: Optional[redis_lib.Redis] = None
_pool: Optional[StaticIPPool] = None
_pool_lock = threading.Lock()
_running_tasks = {}
_running_tasks_lock = threading.Lock()


def _set_running_task(worker_id: int, task: dict) -> None:
    with _running_tasks_lock:
        _running_tasks[worker_id] = {
            "id": int(task["id"]),
            "asin": task.get("asin", ""),
            "region": task.get("region", ""),
        }


def _clear_running_task(worker_id: int) -> None:
    with _running_tasks_lock:
        _running_tasks.pop(worker_id, None)


def _reset_running_tasks_on_signal(signum: int) -> None:
    """IDE/控制台强中断兜底：先把本进程正在跑的任务退回待执行，避免 status=1 残留。"""
    with _running_tasks_lock:
        tasks = list(_running_tasks.values())
    if not tasks:
        return
    ids = [task["id"] for task in tasks]
    try:
        db = MySQLTaskDB()
        affected = db.reset_asin_detail_tasks_by_ids(
            ids,
            error_msg=f"worker interrupted by signal {signum}",
        )
        db.close()
        logger.warning(f"[AsinMain] 已退回中断中的 asin 任务 ids={ids}, affected={affected}")
    except Exception:
        logger.error(f"[AsinMain] 中断兜底退回 asin 任务失败: {traceback.format_exc()[:500]}")


def _sleep_or_stop(seconds: float) -> None:
    """分段 sleep，收到停止信号后尽快回到任务边界退出。"""
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        if check_stop_signal():
            return
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))


def _get_shared_pool() -> StaticIPPool:
    """StaticIPPool 只需一个实例，内部操作全走 Redis 已是原子的"""
    global _redis_client, _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _redis_client = redis_lib.Redis(
                    host=REDIS_HOST, port=REDIS_PORT,
                    username=REDIS_USERNAME, password=REDIS_PASSWORD,
                    db=REDIS_DB, decode_responses=True,
                )
                _pool = StaticIPPool(mysql_db=MySQLTaskDB(), redis_client=_redis_client)
    return _pool


def _ip_label(proxy_url: Optional[str]) -> str:
    if not proxy_url:
        return "dynamic"
    return urlparse(proxy_url).hostname or proxy_url[:30]



def worker_loop(worker_id: int, region: Optional[str] = None):
    """单个线程的主循环。每个线程持有独立的 MySQLTaskDB 连接。"""
    import os
    import socket

    recovery_tracker = WorkerRecoveryTracker(
        f"asin-worker-{socket.gethostname().split('.')[0]}-{os.getpid()}-{worker_id}"
    )
    worker_uas = random.sample(_USER_AGENTS, 2)
    logger.info(f"[AsinWorker-{worker_id}] 启动, region={region or 'ALL'}, "
                f"UA={[u.split('Chrome/')[1][:6] for u in worker_uas]}")

    # 每个线程独立 MySQL 连接（mysql.connector 非线程安全）
    mysql_db = MySQLTaskDB()
    pool = _get_shared_pool()
    consecutive_empty = 0

    try:
        while True:
            # 任务边界检查停止信号：当前任务结束后立即退出
            if check_stop_signal():
                logger.info(f"[AsinWorker-{worker_id}] 收到停止信号，退出主循环")
                break

            task = None
            static_proxy_url = None
            success = False
            error_msg = ""

            try:
                # ── 取任务：只从 Redis BLPOP；backfill 负责把 MySQL pending 补回 Redis ──
                queue_payload = _redis_pop_task(KEY_ASIN, timeout_seconds=10)
                if queue_payload:
                    try:
                        row_identifier, asin_hint = parse_queue_payload(queue_payload)
                        if row_identifier.isdigit():
                            task = mysql_db.claim_asin_task_by_id(int(row_identifier), region=region)
                        else:
                            # 兼容 Redis 中尚未消费的旧 task_id 队列值。
                            task = mysql_db.claim_asin_task_by_task_id(row_identifier, region=region)
                    except Exception:
                        logger.warning(
                            f"[AsinWorker-{worker_id}] claim_asin_task 异常 payload={queue_payload}: "
                            f"{traceback.format_exc()[:400]}"
                        )
                if not task:
                    consecutive_empty += 1
                    wait = min(10, consecutive_empty * 2)
                    logger.debug(f"[AsinWorker-{worker_id}] 无任务，等待{wait}s")
                    _sleep_or_stop(wait)
                    continue

                consecutive_empty = 0
                asin = task['asin']
                task_region = task['region']
                task_id = task['id']
                _set_running_task(worker_id, task)
                recovery_tracker.register(
                    table="crawl_asin_detail_tasks",
                    row_id=task_id,
                    task_kind="asin_detail",
                    asin=asin,
                    country=task_region,
                )

                try:
                    # ── 获取代理 ──────────────────────────────────────────────
                    static_proxy_url, static_proxy, _ = pool.acquire_for_product(region=task_region)
                    if static_proxy_url:
                        logger.info(f"[AsinWorker-{worker_id}] ASIN={asin} 国家={task_region} 静态IP={_ip_label(static_proxy_url)}")
                    else:
                        logger.info(f"[AsinWorker-{worker_id}] ASIN={asin} 国家={task_region} 动态代理")
                    print(f"[AsinWorker-{worker_id}] ASIN={asin} 国家={task_region} 代理:{static_proxy_url}")
                    # ── 执行爬取（重试/降级逻辑在 asins.py 内处理） ───────────
                    result = None
                    asins_obj = None
                    try:
                        asins_obj = ASINS({'asin': asin, 'country': task_region}, user_agents=worker_uas)
                        initial_cookies = (
                            pool.get_ip_cookies(static_proxy_url, region=task_region)
                            if static_proxy_url else ""
                        )
                        result = asins_obj.get_product_detail(
                            proxy=static_proxy, initial_cookies=initial_cookies,
                        )
                        success = result is not None
                        if static_proxy_url:
                            captcha_cookies = getattr(asins_obj, "_captcha_cookies", "")
                            if success:
                                pool.set_ip_cookies(
                                    static_proxy_url, captcha_cookies or initial_cookies,
                                    region=task_region,
                                )
                            elif initial_cookies and not captcha_cookies:
                                pool.delete_ip_cookies(static_proxy_url, region=task_region)
                        if not success:
                            error_msg = "not_product_page_or_bot"
                        else:
                            logger.info(f"[AsinWorker-{worker_id}] OK: {asin} title={result.get('title', '')[:30]}")
                        print(f"[AsinWorker-{worker_id}] OK: {asin}")
                    except Exception as e:
                        error_msg = str(e)[:512]
                        logger.warning(f"[AsinWorker-{worker_id}] ASIN={asin} 异常: {e}")
                    finally:
                        if static_proxy_url:
                            try:
                                pool.release(static_proxy_url)
                            except Exception:
                                pass

                    # ── 写回结果 ──────────────────────────────────────────────
                    try:
                        interrupted = check_stop_signal()
                        if interrupted and not success:
                            mysql_db.reset_asin_detail_tasks_by_ids(
                                [task_id],
                                error_msg="worker interrupted before asin detail writeback",
                            )
                            logger.warning(
                                f"[AsinWorker-{worker_id}] 停止信号期间任务未成功，已退回 id={task_id} asin={asin}"
                            )
                        elif not success:
                            send_custom_robot_group_message(
                                f"商品数据获取失败：[AsinWorker-{worker_id}] ASIN={asin} "
                                f"国家={task_region} IP={_ip_label(static_proxy_url)}"
                            )
                            mysql_db.complete_asin_detail_task(
                                task_id,
                                result,
                                success,
                                error_msg,
                                snapshot_html=getattr(asins_obj, "last_snapshot_html", ""),
                            )
                            logger.info(f"[AsinWorker-{worker_id}] 任务写回完成 id={task_id} asin={asin} status=3")
                        else:
                            mysql_db.complete_asin_detail_task(
                                task_id,
                                result,
                                success,
                                error_msg,
                                snapshot_html=getattr(asins_obj, "last_snapshot_html", ""),
                            )
                            logger.info(f"[AsinWorker-{worker_id}] 任务写回完成 id={task_id} asin={asin} status=2")
                    except Exception:
                        logger.error(f"[AsinWorker-{worker_id}] 任务写回异常 id={task_id} asin={asin}: {traceback.format_exc()}")
                finally:
                    _clear_running_task(worker_id)
                    recovery_tracker.clear()

                # ── 请求间隔 ──────────────────────────────────────────────────
                if check_stop_signal():
                    logger.info(f"[AsinWorker-{worker_id}] 当前任务已写回，停止信号生效，跳过请求间隔")
                    break
                _sleep_or_stop(random.uniform(8, 15) if static_proxy_url else random.uniform(3, 8))

            except Exception:
                logger.error(f"[AsinWorker-{worker_id}] 循环异常:\n{traceback.format_exc()}")
                _sleep_or_stop(5)

    except Exception:
        logger.error(f"[AsinWorker-{worker_id}] 线程致命异常，退出:\n{traceback.format_exc()}")
    finally:
        recovery_tracker.close()
        try:
            mysql_db.close()
        except Exception:
            pass
    logger.info(f"[AsinWorker-{worker_id}] 线程退出")


def main():
    parser = argparse.ArgumentParser(description="商品详情爬取 Worker（多线程）")
    parser.add_argument("--workers", type=int, default=2, help="并发线程数")
    parser.add_argument("--region", type=str, default=None, help="指定站点，如 US/DE/JP，不填则抓所有")
    parser.add_argument("--clear-stop-signal", action="store_true",
                        help="启动前清除当前 worker 组 stop_signal，避免上次停止后立即退出")
    args = parser.parse_args()

    from app.crawlers.amazon_crawler.shuler.util.config import setup_logger
    setup_logger("asin_worker")

    # 注册优雅停止信号处理器
    # 收到 SIGTERM / Ctrl+C / Windows Ctrl+Break 后写当前 asin worker 组的 stop_signal，
    # 所有线程在当前任务结束后自然退出。
    stop_scope = configure_stop_signal_scope(f"asin_{args.region or 'all'}")
    install_signal_handlers(logger_prefix="AsinMain", on_signal=_reset_running_tasks_on_signal)

    if args.clear_stop_signal:
        clear_stop_signal()

    # 启动前确保表和字段存在
    db = MySQLTaskDB()
    db.ensure_asin_detail_tasks_table()
    db.ensure_static_ip_column()
    db.close()

    logger.info(
        f"启动 {args.workers} 个商品详情线程, region={args.region or 'ALL'}, "
        f"stop_scope={stop_scope}, stop_key={get_stop_signal_key()}"
    )

    threads = []
    for i in range(args.workers):
        # 必须使用非 daemon 线程；否则主线程退出会直接杀掉正在执行的任务，MySQL status 会停在 1。
        t = threading.Thread(target=worker_loop, args=(i, args.region), daemon=False, name=f"AsinWorker-{i}")
        t.start()
        threads.append(t)
        time.sleep(0.05)  # 稍微错开避免同时抢第一个任务

    try:
        # 主线程轮询：检测到停止信号后跳出，等所有 worker 线程自然结束
        while any(t.is_alive() for t in threads):
            if check_stop_signal():
                logger.info("[AsinMain] 检测到停止信号，等待 worker 线程在任务边界自然退出...")
                break
            time.sleep(2)
        # 不设置超时：必须等当前任务完成并写回 MySQL，避免 status=1 残留。
        waiting_logged_at = 0.0
        while True:
            alive = [t.name for t in threads if t.is_alive()]
            if not alive:
                break
            now = time.time()
            if now - waiting_logged_at >= 30:
                waiting_logged_at = now
                logger.info(f"[AsinMain] 等待商品详情线程结束: {alive}")
            for t in threads:
                t.join(timeout=1)
        logger.success("所有 asin worker 线程退出")
    except KeyboardInterrupt:
        logger.info("收到中断信号，等待线程自然结束...")
        for t in threads:
            t.join()


if __name__ == "__main__":
    main()
