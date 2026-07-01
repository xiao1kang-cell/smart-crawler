"""
评论页压测脚本（1页/ASIN）

对比两种方案：
  - 静态IP账号 (static_ip 非空): Playwright + Cookie
  - 动态代理账号 (static_ip 为空):  curl_cffi  + Cookie

前置步骤：
  给 10 个测试账号打标签（label 区分，避免影响生产账号池）：
    UPDATE crawler_accounts SET label='stress_review_test'
    WHERE username IN ('acc1','acc2',...);

用法：
  python -m amazon_crawler.shuler.services.amazon.stress_test_review \\
    --workers 3 --region US --hours 8

  --workers  并发进程数（建议 = 同时活跃账号数，账号由 AccountScheduler 调度）
  --region   站点（默认 US）
  --hours    测试时长（默认 8）

指标写入 Redis，key 前缀：stress_review:{username}:*
主进程每 30 分钟打印一次汇总报告。
"""

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from multiprocessing import Process

import redis
from loguru import logger

os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

LABEL = "stress_review_test"
REDIS_KEY_PREFIX = "stress_review"
REPORT_INTERVAL = 30 * 60      # 每30分钟报告一次
STOP_CONSECUTIVE_FAIL = 5       # 连续失败N次停止该账号
STOP_CAPTCHA = 3                # CAPTCHA命中N次停止该账号


# ── Redis 指标工具 ──────────────────────────────────────────────────────────────

def _redis_client():
    from app.crawlers.amazon_crawler.shuler.util.config import (
        REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
    )
    return redis.Redis(
        host=REDIS_HOST, port=REDIS_PORT,
        username=REDIS_USERNAME, password=REDIS_PASSWORD,
        db=REDIS_DB, decode_responses=True,
    )


def metric_incr(r: redis.Redis, username: str, field: str):
    r.incr(f"{REDIS_KEY_PREFIX}:{username}:{field}")
    r.expire(f"{REDIS_KEY_PREFIX}:{username}:{field}", 86400 * 3)


def metric_set(r: redis.Redis, username: str, field: str, value):
    r.set(f"{REDIS_KEY_PREFIX}:{username}:{field}", str(value), ex=86400 * 3)


def metric_get(r: redis.Redis, username: str, field: str, default=0):
    val = r.get(f"{REDIS_KEY_PREFIX}:{username}:{field}")
    try:
        return int(val) if val is not None else default
    except Exception:
        return val or default


# ── 单进程 Worker ────────────────────────────────────────────────────────────────

def run_stress_worker(worker_id: int, region: str, hours: float):
    """
    单个压测 worker 进程。
    使用 AccountScheduler (label=stress_review_test) 做会话粘性调度：
      - 同一账号连续跑 session_budget 个任务后自动休息、切换账号
      - 静态IP账号 → Playwright；动态代理账号 → curl_cffi（与 get_reviews_main.py 逻辑一致）
    """
    from app.crawlers.amazon_crawler.shuler.util.config import setup_logger
    setup_logger("stress_worker")

    from app.crawlers.amazon_crawler.shuler.services.amazon.reviews import Reviews
    from app.crawlers.amazon_crawler.shuler.services.amazon.reviews_playwright import PlaywrightReviewScraper
    from app.crawlers.amazon_crawler.shuler.services.amazon.get_reviews_main import _insert_reviews_to_mysql
    from app.crawlers.amazon_crawler.shuler.util.account_scheduler import HumanLikeAccountManager as AccountManager
    from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
    from app.crawlers.amazon_crawler.shuler.util.influxdb_sink import init_metrics
    from app.crawlers.amazon_crawler.shuler.util.config import APP_ENV

    init_metrics(env=APP_ENV)

    worker_name = f"stress-worker-{worker_id}-{os.getpid()}"
    logger.info(f"[{worker_name}] 启动, region={region}, 时长={hours}h")

    account_manager = AccountManager(worker_id=worker_name, account_label=LABEL)
    mysql_db = MySQLTaskDB()
    r = _redis_client()

    end_time = time.time() + hours * 3600
    scraper = None          # Playwright scraper（跨任务复用）
    current_username = None

    def _close_scraper():
        nonlocal scraper, current_username
        if scraper is not None:
            try:
                scraper.close_session()
            except Exception:
                pass
            scraper = None
            current_username = None

    try:
        while time.time() < end_time:
            task_id = None
            success = False

            try:
                # ── 拉取任务 ──────────────────────────────────────────────────────
                tasks = mysql_db.claim_stress_review_tasks(region=region, worker_name=worker_name)
                if not tasks:
                    time.sleep(10)
                    continue

                task_row = tasks[0]
                asin = task_row["asin"]
                country = task_row["country"]
                task_id = task_row["id"]

                # ── 获取账号（会话粘性） ───────────────────────────────────────
                account = account_manager.get_account({"country": country})
                if not account:
                    logger.warning(f"[{worker_name}] 无可用账号，等待60s")
                    time.sleep(60)
                    continue

                username = account.username

                # ── 检查停止条件 ───────────────────────────────────────────────
                stop_reason = metric_get(r, username, "stop_reason", "")
                if stop_reason:
                    logger.warning(f"[{worker_name}] 账号 {username} 已触发停止({stop_reason})，跳过")
                    time.sleep(5)
                    continue

                # ── 判断走哪条路径（与 get_reviews_main 逻辑一致） ─────────────
                proxy_ = getattr(account, 'proxy_', {}) or {}
                proxy_http = proxy_.get('http', '') if isinstance(proxy_, dict) else ''
                use_curl = 'zone-custom-region-' in proxy_http

                # ── 账号切换时处理浏览器 ─────────────────────────────────────
                if account.username != current_username:
                    _close_scraper()
                    current_username = account.username
                    if not use_curl:
                        scraper = PlaywrightReviewScraper(
                            account_info=account,
                            task={"asin": "INIT", "country": country, "id": str(task_id)},
                        )
                        logger.info(f"[{worker_name}] 新建Playwright会话 账号={current_username}")
                    else:
                        logger.info(f"[{worker_name}] curl_cffi路径 账号={current_username}")

                # ── 构造 payload（强制 max_pages=1） ─────────────────────────
                payload = {
                    "id": task_id,
                    "task_id": str(task_id),
                    "country": country,
                    "asin": asin,
                    "max_pages": 1,
                    "query_conditions": {},
                }

                # ── 执行抓取 ───────────────────────────────────────────────────
                proxy_type = "curl_cffi" if use_curl else "playwright"
                logger.info(f"[{worker_name}] {proxy_type} ASIN={asin} 账号={current_username}")

                if use_curl:
                    review = Reviews(account)
                    reviews = review.get_reviews_main(payload, worker_name, account_manager)
                else:
                    reviews = scraper.run(payload, worker_id=worker_name,
                                         account_manager=account_manager)

                # None = 爬取出错/被拦截；[] = 页面正常但无评论，仍算成功
                success = reviews is not None
                review_count = len(reviews) if reviews else 0

                if success:
                    metric_incr(r, username, "success")
                    metric_set(r, username, "last_asin", asin)
                    metric_set(r, username, "proxy_type", proxy_type)
                    logger.info(f"[{worker_name}] OK: {asin} reviews={review_count} 账号={username}")
                    if reviews:
                        _insert_reviews_to_mysql(mysql_db, reviews, asin, country,
                                                 table_name="crawler_reviews")
                else:
                    metric_incr(r, username, "fail")
                    logger.warning(f"[{worker_name}] FAIL: {asin} 账号={username}")

                # ── 检查停止条件（失败次数用 Redis INCR 累计） ─────────────────
                consec_key = f"{REDIS_KEY_PREFIX}:{username}:consec_fail"
                if success:
                    r.set(consec_key, 0, ex=3600)
                else:
                    consec = r.incr(consec_key)
                    r.expire(consec_key, 3600)
                    if int(consec) >= STOP_CONSECUTIVE_FAIL:
                        metric_set(r, username, "stop_reason",
                                   f"consecutive_fail_{STOP_CONSECUTIVE_FAIL}")
                        logger.warning(f"[{worker_name}] 账号 {username} 连续失败{STOP_CONSECUTIVE_FAIL}次，标记停止")

                captcha = metric_get(r, username, "captcha")
                if captcha >= STOP_CAPTCHA:
                    metric_set(r, username, "stop_reason", f"captcha_{STOP_CAPTCHA}")
                    logger.warning(f"[{worker_name}] 账号 {username} CAPTCHA达到{STOP_CAPTCHA}次，标记停止")

                try:
                    account_manager.release_account(
                        account_manager.scheduler.get_current_account(worker_name) or account,
                        asin, success, str(task_id), pages_fetched=1
                    )
                except Exception as rel_err:
                    logger.warning(f"[{worker_name}] release_account 失败（不影响任务结果）: {rel_err}")

            except Exception:
                error_msg = traceback.format_exc()
                logger.error(f"[{worker_name}] 异常: {error_msg[:300]}")
                if current_username:
                    metric_incr(r, current_username, "fail")
                time.sleep(5)  # 异常后短暂等待，避免死循环
            finally:
                if task_id is not None:
                    mysql_db.complete_stress_review_task(task_id, success)

    finally:
        _close_scraper()
        account_manager.force_release()
        logger.info(f"[{worker_name}] 退出")


# ── 配额初始化（Option B） ───────────────────────────────────────────────────────

def init_stress_quotas(r: redis.Redis, account_names: list[str]):
    """将测试账号今日配额写入 Redis，避免 AccountScheduler 因日配额不足拦截压测。

    只覆盖配额字段，保留已有的 task_count / page_count，重启后不丢计数。
    """
    from datetime import date
    today = date.today().isoformat()

    print("初始化压测账号配额 (daily_budget=9999, rest_until=0)...")
    for username in account_names:
        key = f"acc_day:{username}:{today}"
        pipe = r.pipeline()
        pipe.hsetnx(key, "date", today)
        pipe.hsetnx(key, "task_count", "0")
        pipe.hsetnx(key, "page_count", "0")
        pipe.hset(key, "daily_budget", "300")
        pipe.hset(key, "daily_page_budget", "400")
        # pipe.hset(key, "quota_factor_applied", "1.0")
        pipe.hsetnx(key, "last_session_end", "0")
        pipe.hset(key, "rest_until", "0")
        pipe.hsetnx(key, "session_seq", "0")
        pipe.expire(key, 48 * 3600)
        pipe.execute()
        print(f"  {username}: daily_budget=9999, daily_page_budget=9999, rest_until=0")


# ── 报告 ─────────────────────────────────────────────────────────────────────────

def print_report(r: redis.Redis, accounts: list[str], elapsed_hours: float):
    from datetime import date
    today = date.today().isoformat()

    print(f"\n{'='*85}")
    print(f"  压测报告  已运行 {elapsed_hours:.1f}h  {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*85}")
    print(f"{'账号':<30} {'类型':<12} {'今日评论页':>10} {'今日商品页':>10} {'当次':>6} {'失败':>6} {'速率/h':>8} {'状态'}")
    print(f"{'-'*85}")
    for username in accounts:
        acc_day = r.hgetall(f"acc_day:{username}:{today}")
        today_pages = int(acc_day.get("page_count", 0))
        today_prod = int(acc_day.get("product_page_count", 0))
        run_success = metric_get(r, username, "success")
        fail = metric_get(r, username, "fail")
        stop = metric_get(r, username, "stop_reason", "")
        proxy_type = metric_get(r, username, "proxy_type", "?")
        rate = round(run_success / max(elapsed_hours, 0.1), 1)
        status = f"停止({stop})" if stop else "运行中"
        print(f"{username:<30} {proxy_type:<12} {today_pages:>10} {today_prod:>10} {run_success:>6} {fail:>6} {rate:>8} {status}")
    print(f"{'='*85}")


# ── 入口 ─────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="评论页压测（1页/ASIN）")
    parser.add_argument("--workers", type=int, default=3, help="并发进程数")
    parser.add_argument("--region", type=str, default="US")
    parser.add_argument("--hours", type=float, default=8.0, help="测试时长（小时）")
    args = parser.parse_args()

    # 查出所有打了测试标签的账号（用于报告显示）
    from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
    db = MySQLTaskDB()
    rows = db.load_all_accounts({"label": LABEL})
    account_names = [r["username"] for r in rows]
    if not account_names:
        print(f"[错误] 没有找到 label='{LABEL}' 的账号，请先执行：")
        print(f"  UPDATE crawler_accounts SET label='{LABEL}' WHERE username IN (...);")
        sys.exit(1)

    print(f"找到 {len(account_names)} 个测试账号，启动 {args.workers} 个并发进程")
    print(f"测试时长: {args.hours}h | 站点: {args.region} | 任务来源: crawler_asin_tasks_temp")

    r = _redis_client()
    # 清当次运行计数（每次启动归零）；日累计存在 stress_review_day:{date}:* 不在这里清
    for username in account_names:
        for field in ("success", "fail", "captcha", "stop_reason", "consec_fail", "proxy_type"):
            r.delete(f"{REDIS_KEY_PREFIX}:{username}:{field}")

    # 覆写今日配额，解除 AccountScheduler 的日限额限制
    # init_stress_quotas(r, account_names)

    # 启动 worker 进程
    processes = []
    for i in range(args.workers):
        p = Process(
            target=run_stress_worker,
            args=(i, args.region, args.hours),
            daemon=True,
        )
        p.start()
        processes.append(p)
        time.sleep(0.5)

    start_time = time.time()
    next_report = start_time + REPORT_INTERVAL

    try:
        while any(p.is_alive() for p in processes):
            time.sleep(10)
            if time.time() >= next_report:
                elapsed = (time.time() - start_time) / 3600
                print_report(r, account_names, elapsed)
                next_report = time.time() + REPORT_INTERVAL
    except KeyboardInterrupt:
        print("\n收到中断，停止所有进程...")
        for p in processes:
            p.terminate()

    # 最终报告
    from datetime import date
    today = date.today().isoformat()
    elapsed = (time.time() - start_time) / 3600
    print_report(r, account_names, elapsed)
    print(f"\n推算日上限（当次 {elapsed:.1f}h 速率推算）：")
    for username in account_names:
        acc_day = r.hgetall(f"acc_day:{username}:{today}")
        today_pages = int(acc_day.get("page_count", 0))
        today_prod = int(acc_day.get("product_page_count", 0))
        run_success = metric_get(r, username, "success")
        rate = run_success / max(elapsed, 0.1)
        daily = round(rate * 16)
        prod_str = f"  商品页={today_prod}" if today_prod > 0 else ""
        print(f"  {username}: 今日评论={today_pages}页{prod_str}  当次={run_success}页  速率={rate:.0f}页/h → 推算日上限 ~{daily} 页")


if __name__ == "__main__":
    main()
