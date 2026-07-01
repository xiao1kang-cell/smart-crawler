# ------------------------------
# 5. 监控面板（实时状态）
# ------------------------------
import time

from loguru import logger

from app.crawlers.amazon_crawler.shuler.services.amazon.account_ import Account
from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import MAX_FAIL
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB


class TaskMonitor:
    @staticmethod
    def show_status(table_name: str = None):
        """展示当前任务&账号状态
        
        Args:
            table_name: 指定任务表名，如 'crawl_subtasks', 'asin_tasks', 'crawl_single_tasks' 等
                       不指定则自动尝试 crawl_subtasks，失败回退到 asin_tasks
        """
        # 账号状态（MySQL）
        mysql_acc_db = MySQLTaskDB()
        rows = mysql_acc_db.load_all_accounts()
        accounts = [Account.from_dict(r) for r in rows]
        mysql_acc_db.close()
        now = time.time()
        total_acc = len(accounts)
        used_acc = len([a for a in accounts if a.is_used])
        cool_acc = len([a for a in accounts if a.cooldown_until > now])
        fail_acc = len([a for a in accounts if a.fail_count >= MAX_FAIL])

        # 任务状态（MySQL）
        mysql_db = MySQLTaskDB()
        try:
            # 如果指定了表名，直接查询指定表
            if table_name:
                target_table = table_name
            else:
                # 默认先尝试 crawl_subtasks
                target_table = "crawl_subtasks"

            count_tasks = getattr(mysql_db, "count_tasks_by_legacy_table", None)
            if count_tasks:
                pending_task = count_tasks(target_table, 0)
                running_task = count_tasks(target_table, 1)
                success_task = count_tasks(target_table, 2)
                fail_task = count_tasks(target_table, 3)
            else:
                mysql_db.cursor.execute(f"SELECT COUNT(*) as cnt FROM {target_table} WHERE status=0")
                pending_task = mysql_db.cursor.fetchone()["cnt"]
                mysql_db.cursor.execute(f"SELECT COUNT(*) as cnt FROM {target_table} WHERE status=1")
                running_task = mysql_db.cursor.fetchone()["cnt"]
                mysql_db.cursor.execute(f"SELECT COUNT(*) as cnt FROM {target_table} WHERE status=2")
                success_task = mysql_db.cursor.fetchone()["cnt"]
                mysql_db.cursor.execute(f"SELECT COUNT(*) as cnt FROM {target_table} WHERE status=3")
                fail_task = mysql_db.cursor.fetchone()["cnt"]
        except Exception:
            # 失败时回退到 asin_tasks（仅当未指定表名时）
            if not table_name:
                mysql_db.cursor.execute("SELECT COUNT(*) as cnt FROM asin_tasks WHERE status=0")
                pending_task = mysql_db.cursor.fetchone()["cnt"]
                mysql_db.cursor.execute("SELECT COUNT(*) as cnt FROM asin_tasks WHERE status=1")
                running_task = mysql_db.cursor.fetchone()["cnt"]
                mysql_db.cursor.execute("SELECT COUNT(*) as cnt FROM asin_tasks WHERE status=2")
                success_task = mysql_db.cursor.fetchone()["cnt"]
                mysql_db.cursor.execute("SELECT COUNT(*) as cnt FROM asin_tasks WHERE status=3")
                fail_task = mysql_db.cursor.fetchone()["cnt"]
            else:
                # 指定了表名但查询失败，抛出让调用方处理
                raise
        mysql_db.close()

        # 打印监控信息
        logger.info("=" * 80)
        logger.info("📊 任务&账号调度监控面板")
        logger.info(f"[账号状态] 总:{total_acc} | 使用中:{used_acc} | 冷却中:{cool_acc} | 失败锁定:{fail_acc}")
        logger.info(f"[任务状态] 待执行:{pending_task} | 执行中:{running_task} | 已完成:{success_task} | 执行失败:{fail_task}")
        logger.info("=" * 80)
