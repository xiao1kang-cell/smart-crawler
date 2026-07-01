"""
商品详情页压测脚本

静态IP（StaticIPPool）优先，无静态IP可用时降级到动态旋转代理。
指标按代理类型（static_ip / dynamic）分组统计，对比封禁率和吞吐速率。

任务来源：crawler_asin_tasks_temp（与评论页压测共用同一任务池）

用法：
  python -m amazon_crawler.shuler.services.amazon.stress_test_asin \\
    --workers 5 --region US --hours 8

  --workers  并发进程数（建议与静态IP数量相近）
  --region   站点（默认 US）
  --hours    测试时长（默认 8）
"""

import argparse
import os
import random
import time
import traceback
from datetime import date, datetime
from multiprocessing import Process

import redis
from loguru import logger

os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

REDIS_KEY_PREFIX = "stress_asin"
REPORT_INTERVAL = 30 * 60
PROXY_TYPES = ("static_ip", "dynamic")


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


def metric_incr(r: redis.Redis, proxy_type: str, field: str):
    r.incr(f"{REDIS_KEY_PREFIX}:{proxy_type}:{field}")
    r.expire(f"{REDIS_KEY_PREFIX}:{proxy_type}:{field}", 86400 * 3)


def metric_get(r: redis.Redis, proxy_type: str, field: str, default=0):
    val = r.get(f"{REDIS_KEY_PREFIX}:{proxy_type}:{field}")
    try:
        return int(val) if val is not None else default
    except Exception:
        return val or default


def _make_dynamic_proxy(region: str) -> dict:
    from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import PROXY_MAPPING
    r = PROXY_MAPPING.get(region.upper(), 'us')
    session_id = int(time.time() * 1000)
    url = (
        f'http://PsFaJMphAU0hH1s20E-zone-custom-region-{r}'
        f'-session-{session_id}-sessTime-5'
        f':iWrz7GbWhm@a477c1a8e06d7ff8.qzc.na.grassdata.net:2333'
    )
    return {'http': url, 'https': url}


# ── 单进程 Worker ────────────────────────────────────────────────────────────────

def run_asin_stress_worker(worker_id: int, region: str, hours: float):
    from app.crawlers.amazon_crawler.shuler.util.config import setup_logger
    setup_logger("asin_stress_worker")

    from app.crawlers.amazon_crawler.shuler.services.amazon.asins import ASINS, _USER_AGENTS
    from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
    from app.crawlers.amazon_crawler.shuler.util.static_ip_pool import StaticIPPool

    worker_name = f"asin-stress-{worker_id}-{os.getpid()}"
    worker_uas = random.sample(_USER_AGENTS, 2)
    logger.info(f"[{worker_name}] 启动, region={region}, 时长={hours}h, UA={[u.split('Chrome/')[1][:6] for u in worker_uas]}")

    mysql_db = MySQLTaskDB()
    mysql_db.ensure_asin_detail_tasks_table()
    r = _redis_client()
    pool = StaticIPPool(mysql_db=mysql_db, redis_client=r)

    end_time = time.time() + hours * 3600

    try:
        while time.time() < end_time:
            task_id = None
            success = False
            static_proxy_url = None
            proxy_type = "dynamic"

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

                # ── 获取代理（静态优先，动态降级） ───────────────────────────────
                # 评论任务持有 review 锁时该IP被自动跳过，确保不同时使用同一IP
                static_proxy_url, proxy, static_account = pool.acquire_for_product(region=region)
                if static_proxy_url:
                    proxy_type = "static_ip"
                    logger.debug(f"[{worker_name}] 使用静态IP: {static_proxy_url.split('@')[-1]} 账号={static_account}")
                else:
                    proxy = _make_dynamic_proxy(region)
                    proxy_type = "dynamic"
                    static_account = None
                    logger.debug(f"[{worker_name}] 降级动态代理")

                # ── 执行抓取 ──────────────────────────────────────────────────────
                logger.info(f"[{worker_name}] [{proxy_type}] GET {asin}")
                asins_obj = ASINS({"asin": asin, "country": country}, user_agents=worker_uas)
                initial_cookies = (
                    pool.get_ip_cookies(static_proxy_url, region=country)
                    if static_proxy_url else ""
                )
                result = asins_obj.get_product_detail(proxy=proxy, initial_cookies=initial_cookies)
                success = result is not None
                if static_proxy_url:
                    captcha_cookies = getattr(asins_obj, "_captcha_cookies", "")
                    if success:
                        pool.set_ip_cookies(
                            static_proxy_url, captcha_cookies or initial_cookies,
                            region=country,
                        )
                    elif initial_cookies and not captcha_cookies:
                        pool.delete_ip_cookies(static_proxy_url, region=country)

                if success:
                    try:
                        metric_incr(r, proxy_type, "success")
                    except Exception:
                        pass
                    title = str(result.get("title", ""))[:40]
                    logger.info(f"[{worker_name}] OK: {asin} title={title} [{proxy_type}]")
                    mysql_db.insert_asin_detail_result(asin, country, result)
                    if static_proxy_url:
                        pool.mark_ip_success(static_proxy_url)
                        # 记录账号今日商品页数（与评论页数分开统计）
                        if static_account:
                            today = date.today().isoformat()
                            try:
                                r.hincrby(f"acc_day:{static_account}:{today}", "product_page_count", 1)
                            except Exception:
                                pass
                else:
                    try:
                        metric_incr(r, proxy_type, "fail")
                    except Exception:
                        pass
                    logger.warning(f"[{worker_name}] FAIL: {asin} [{proxy_type}]")
                    if static_proxy_url:
                        pool.mark_ip_failed(static_proxy_url)

                # ── 请求间隔（避免IP被风控） ─────────────────────────────────────
                time.sleep(random.uniform(8, 15))

            except Exception:
                err = traceback.format_exc()
                logger.error(f"[{worker_name}] 异常: {err[:400]}")
                try:
                    metric_incr(r, proxy_type, "fail")
                except Exception:
                    pass
                time.sleep(5)
            finally:
                if static_proxy_url:
                    try:
                        pool.release(static_proxy_url)
                    except Exception:
                        pass
                if task_id is not None:
                    try:
                        mysql_db.complete_stress_review_task(task_id, success)
                    except Exception:
                        pass

    except Exception:
        logger.error(f"[{worker_name}] 进程致命异常，退出:\n{traceback.format_exc()}")

    logger.info(f"[{worker_name}] 时间到，退出")


# ── 报告 ─────────────────────────────────────────────────────────────────────────

def print_report(r: redis.Redis, elapsed_hours: float, workers: int, mysql_db=None):
    today = date.today().isoformat()
    print(f"\n{'='*65}")
    print(f"  商品页压测报告  已运行 {elapsed_hours:.1f}h  "
          f"{datetime.now().strftime('%H:%M:%S')}  workers={workers}")
    print(f"{'='*65}")
    print(f"{'代理类型':<15} {'成功':>8} {'失败':>8} {'成功率':>8} {'速率/h':>10} {'推算日上限':>12}")
    print(f"{'-'*65}")
    for proxy_type in PROXY_TYPES:
        success = metric_get(r, proxy_type, "success")
        fail = metric_get(r, proxy_type, "fail")
        total = success + fail
        rate = success / max(elapsed_hours, 0.01)
        pct = f"{success / total * 100:.1f}%" if total > 0 else "—"
        daily = round(rate * 16)
        print(f"{proxy_type:<15} {success:>8} {fail:>8} {pct:>8} {rate:>10.1f} {daily:>12}")

    # 展示各静态IP账号今日商品页数（与评论页数对比）
    if mysql_db:
        try:
            rows = mysql_db.get_all_static_ips()
            accounts = sorted({r['username'] for r in rows if r.get('username')})
            if accounts:
                print(f"\n{'账号（静态IP）':<30} {'今日商品页':>10} {'今日评论页':>10}")
                print(f"{'-'*55}")
                for username in accounts:
                    acc_day = r.hgetall(f"acc_day:{username}:{today}")
                    prod = int(acc_day.get("product_page_count", 0))
                    review = int(acc_day.get("page_count", 0))
                    print(f"{username:<30} {prod:>10} {review:>10}")
        except Exception:
            pass
    print(f"{'='*65}")


# ── 入口 ─────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="商品详情页压测（静态IP vs 动态代理）")
    parser.add_argument("--workers", type=int, default=1, help="并发进程数")
    parser.add_argument("--region", type=str, default="US")
    parser.add_argument("--hours", type=float, default=8.0, help="测试时长（小时）")
    args = parser.parse_args()

    from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
    r = _redis_client()
    mysql_db = MySQLTaskDB()

    # 清空上次指标
    for proxy_type in PROXY_TYPES:
        for field in ("success", "fail"):
            r.delete(f"{REDIS_KEY_PREFIX}:{proxy_type}:{field}")

    print(f"启动 {args.workers} 个商品页压测 Worker | region={args.region} | 时长={args.hours}h")
    print(f"任务来源: crawler_asin_tasks_temp | 静态IP优先，无IP时降级动态代理")
    print(f"互斥机制: 评论任务持有IP锁时商品任务自动降级动态代理")

    processes = []
    for i in range(args.workers):
        p = Process(
            target=run_asin_stress_worker,
            args=(i, args.region, args.hours),
            daemon=True,
        )
        p.start()
        processes.append(p)
        time.sleep(0.3)

    start_time = time.time()
    next_report = start_time + REPORT_INTERVAL

    try:
        while any(p.is_alive() for p in processes):
            time.sleep(10)
            if time.time() >= next_report:
                elapsed = (time.time() - start_time) / 3600
                print_report(r, elapsed, args.workers, mysql_db)
                next_report = time.time() + REPORT_INTERVAL
    except KeyboardInterrupt:
        print("\n收到中断，停止所有进程...")
        for p in processes:
            p.terminate()

    elapsed = (time.time() - start_time) / 3600
    print_report(r, elapsed, args.workers, mysql_db)


if __name__ == "__main__":
    main()
