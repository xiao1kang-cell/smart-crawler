"""
Windows browser-node for account import.

Linux API only writes account import jobs into MySQL. This node must run on a
Windows machine with BitBrowser installed, because create_browser talks to the
local BitBrowser API at 127.0.0.1:54345.
"""

import argparse
import os
import socket
import time
import traceback
from typing import Dict, List

from loguru import logger

from app.crawlers.amazon_crawler.shuler.services.amazon.account_add import (
    build_account_record,
    create_browser,
    normalize_account_username,
    proxy_to_url,
    resolve_account_proxy,
)
from app.crawlers.amazon_crawler.shuler.util.config import setup_logger
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB


DEFAULT_NODE_ID = socket.gethostname().split(".")[0]


def _build_proxy_suffix_map(items: List[Dict]) -> Dict[int, int]:
    suffix_by_item_id: Dict[int, int] = {}
    static_index = 0
    for attempted_index, item in enumerate(items, start=1):
        if str(item.get("proxy") or "").strip():
            suffix_by_item_id[int(item["id"])] = attempted_index
        else:
            static_index += 1
            suffix_by_item_id[int(item["id"])] = static_index
    return suffix_by_item_id


def _process_item(db: MySQLTaskDB, job: Dict, item: Dict, suffix: int, node_id: str) -> None:
    item_id = int(item["id"])
    job_id = str(job["job_id"])
    db.mark_account_import_item_running(item_id, node_id)
    db.refresh_account_import_job_stats(job_id)

    account = {
        "row_no": int(item.get("row_no") or 0),
        "username": str(item.get("username") or "").strip(),
        "password": str(item.get("password") or ""),
        "country": str(item.get("country") or job.get("target_country") or "").strip().upper(),
        "totp_secret": str(item.get("totp_secret") or ""),
        "browser_id": str(item.get("browser_id") or "").strip(),
        "proxy": str(item.get("proxy") or "").strip(),
        "cookies": {},
    }
    account["username"] = normalize_account_username(account["username"], account["country"])

    try:
        if account["browser_id"]:
            browser_id = account["browser_id"]
            proxy_item, static_ip = resolve_account_proxy(
                account,
                suffix,
                static_ip_count=int(job.get("static_ip_count") or 0),
                static_ip_pool=job.get("static_ip_pool") or [],
            )
            proxy_url = proxy_to_url(proxy_item)
            proxy_config = {"http": proxy_url, "https": proxy_url}
        else:
            browser_id, proxy_config, static_ip = create_browser(
                account,
                suffix,
                static_ip_count=int(job.get("static_ip_count") or 0),
                static_ip_pool=job.get("static_ip_pool") or [],
            )

        if not browser_id:
            raise RuntimeError("create_browser_failed")

        db.insert_account(build_account_record(account, browser_id, proxy_config, static_ip))
        db.mark_account_import_item_done(item_id, account["username"], browser_id)
        db.refresh_account_import_job_stats(job_id)
        logger.info(
            f"[AccountBrowserNode] 导入成功 job={job_id} row={account['row_no']} "
            f"username={account['username']} browser_id={browser_id}"
        )
        time.sleep(0.5)
    except Exception as exc:
        reason = str(exc)[:500]
        db.mark_account_import_item_failed(item_id, reason)
        db.refresh_account_import_job_stats(job_id)
        logger.error(
            f"[AccountBrowserNode] 导入失败 job={job_id} row={account['row_no']} "
            f"username={account['username']} error={reason}"
        )


def _process_job(db: MySQLTaskDB, job: Dict, node_id: str) -> None:
    job_id = str(job["job_id"])
    logger.info(f"[AccountBrowserNode] 领取账号导入任务 job={job_id} node={node_id}")
    items = db.get_account_import_items(job_id)
    suffix_by_item_id = _build_proxy_suffix_map(items)

    for item in items:
        if int(item.get("status") or 0) != 0:
            continue
        _process_item(db, job, item, suffix_by_item_id.get(int(item["id"]), 1), node_id)

    db.refresh_account_import_job_stats(job_id, final_status=2)
    logger.info(f"[AccountBrowserNode] 账号导入任务完成 job={job_id}")


def run_forever(node_id: str, poll_interval: float = 5.0, stale_seconds: int = 900, once: bool = False) -> None:
    setup_logger("account_browser_node")
    logger.info(
        f"[AccountBrowserNode] 启动 node={node_id}, poll_interval={poll_interval}s, "
        f"stale_seconds={stale_seconds}, APP_ENV={os.getenv('APP_ENV', 'dev')}"
    )

    while True:
        db = MySQLTaskDB()
        try:
            db.ensure_account_import_tables()
            db.ensure_static_ip_column()
            reset_count = db.reset_stale_account_import_jobs(stale_seconds=stale_seconds)
            if reset_count:
                logger.warning(f"[AccountBrowserNode] 已重置超时账号导入任务: {reset_count}")

            job = db.claim_next_account_import_job(node_id)
            if job:
                _process_job(db, job, node_id)
            elif once:
                logger.info("[AccountBrowserNode] 没有待处理账号导入任务，退出")
                return
        except KeyboardInterrupt:
            logger.warning("[AccountBrowserNode] 收到中断信号，退出")
            return
        except Exception:
            logger.error(f"[AccountBrowserNode] 主循环异常: {traceback.format_exc()}")
        finally:
            try:
                db.close()
            except Exception:
                pass

        if once:
            return
        time.sleep(max(float(poll_interval or 5), 1.0))


def main() -> None:
    parser = argparse.ArgumentParser(description="Windows BitBrowser account import node")
    parser.add_argument("--node-id", default=os.getenv("CRAWLER_NODE_ID") or DEFAULT_NODE_ID)
    parser.add_argument("--poll-interval", type=float, default=float(os.getenv("ACCOUNT_BROWSER_NODE_POLL", "5")))
    parser.add_argument("--stale-seconds", type=int, default=int(os.getenv("ACCOUNT_IMPORT_STALE_SECONDS", "900")))
    parser.add_argument("--once", action="store_true", help="只处理一次待处理任务后退出")
    args = parser.parse_args()
    run_forever(
        node_id=args.node_id,
        poll_interval=args.poll_interval,
        stale_seconds=args.stale_seconds,
        once=args.once,
    )


if __name__ == "__main__":
    main()
