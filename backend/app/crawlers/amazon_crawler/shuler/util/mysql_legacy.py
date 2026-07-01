# ------------------------------
# 2. MySQL 任务操作层（专属）
# ------------------------------
from datetime import datetime, timedelta
import time
from typing import List, Dict, Any, Optional
import json
import mysql.connector
from mysql.connector import Error
# import logging
from loguru import logger

from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import TASK_BATCH_SIZE, SINGLE_TASK_NEED_CRAWLER_DELAY_MINUTES, \
    TASK_TIMEOUT_MINUTES
from app.crawlers.amazon_crawler.shuler.util.config import *
from app.crawlers.amazon_crawler.shuler.util.task_queue_redis import DEFAULT_TASK_PRIORITY, normalize_task_priority

SINGLE_NEED_TIME_SLA_ENABLED = False

# 配置日志
# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger(__name__)

# 任务超时时间（秒）：超过这个时间未完成的任务，自动重置为待执行
# TASK_TIMEOUT_MINUTES = 30  # 5分钟，可根据你的任务处理时长调整


class MySQLTaskDB:
    def __init__(self, host=None, port=None, user=None, password=None, database=None):
        """初始化：完全禁用连接池，关闭自动提交。可传入自定义连接参数，否则使用默认配置。"""
        self._host = host or MYSQL_HOST
        self._port = port or MYSQL_PORT
        self._user = user or MYSQL_USER
        self._password = password or MYSQL_PASSWORD
        self._database = database or MYSQL_DB
        self.conn = None
        self.cursor = None
        self._closed = False  # 标记连接是否已关闭
        self._init_connection()

    def _init_connection(self):
        """初始化/重建连接（完全禁用连接池，多进程安全）"""
        try:
            # 关闭旧连接
            if self.conn and self.conn.is_connected():
                try:
                    self.cursor.close()
                    self.conn.close()
                except:
                    pass

            # 核心修复：删除 pool 相关参数，完全禁用连接池
            # autocommit=False 手动控制事务（解决 Commands out of sync）
            self.conn = mysql.connector.connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                database=self._database,
                autocommit=False,
                connection_timeout=10,  # 连接超时10秒，防止 MySQL 不可达时进程卡死
            )
            self.cursor = self.conn.cursor(dictionary=True)
            # logger.info("MySQL 连接初始化成功（禁用连接池+关闭自动提交）")
        except Error as e:
            logger.error(f"MySQL 连接失败: {str(e)}")
            raise

    def _check_connection(self):
        """检查连接有效性（已关闭则重建）"""
        if self._closed or not self.conn or not self.conn.is_connected():
            if self._closed:
                logger.warning("MySQL 连接已关闭标记，重建连接...")
            else:
                logger.warning("MySQL 连接失效，重建连接...")
            self._closed = False  # 重置标记
            self._init_connection()

    def _end_read_transaction(self):
        """结束 autocommit=False 下 SELECT 产生的隐式事务。"""
        try:
            if self.conn and self.conn.is_connected() and self.conn.in_transaction:
                self.conn.commit()
        except Exception:
            try:
                if self.conn and self.conn.is_connected():
                    self.conn.rollback()
            except Exception:
                pass

    def _rollback_active_transaction(self):
        """写事务开始前兜底清理当前连接上的残留事务。"""
        try:
            if self.conn and self.conn.is_connected() and self.conn.in_transaction:
                self.conn.rollback()
        except Exception:
            pass

    @staticmethod
    def _mysql_error_code(exc: Exception) -> int:
        code = getattr(exc, "errno", None)
        try:
            return int(code) if code is not None else 0
        except Exception:
            return 0

    @classmethod
    def _is_retryable_tx_error(cls, exc: Exception) -> bool:
        code = cls._mysql_error_code(exc)
        if code in (1205, 1213):
            return True
        msg = str(exc).lower()
        return ("deadlock" in msg) or ("lock wait timeout" in msg)

    @staticmethod
    def _normalize_single_task_source(source: str = None) -> str:
        """归一化 single worker 的 source 参数。

        None / "" / "normal" / "None" 都表示普通生产任务：排除 stress_test。
        其他非空值表示精确匹配 crawl_single_tasks.source。
        """
        value = str(source or "").strip()
        if value.lower() in ("", "none", "normal", "default"):
            return ""
        return value

    @staticmethod
    def _append_single_source_filter(
            where_parts: List[str],
            params: List[Any],
            source_filter: str,
            stress_test_label: str,
    ) -> None:
        """追加 single 任务 source 过滤条件。

        生产 worker 需要排除 stress_test，但兼容历史/外部写入的 NULL source。
        """
        if source_filter == stress_test_label:
            where_parts.append("source = %s")
            params.append(source_filter)
        elif source_filter:
            where_parts.append("source = %s")
            params.append(source_filter)
        else:
            where_parts.append("(source IS NULL OR source != %s)")
            params.append(stress_test_label)

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return value
        return value

    @staticmethod
    def _normalize_json_for_compare(value: Any) -> Any:
        value = MySQLTaskDB._json_value(value)
        if isinstance(value, dict):
            return {str(k): MySQLTaskDB._normalize_json_for_compare(v) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [MySQLTaskDB._normalize_json_for_compare(v) for v in value]
        return value

    @classmethod
    def _single_task_reuse_signature(
            cls,
            asin: str,
            region: str,
            params: Dict = None,
            source: str = "",
    ) -> str:
        params_obj = cls._json_value(params or {})
        if not isinstance(params_obj, dict):
            params_obj = {}

        normalized_body = dict(params_obj)
        normalized_body["asin"] = str(normalized_body.get("asin") or asin or "").strip().upper()
        normalized_body["region"] = str(normalized_body.get("region") or region or "").strip().upper()
        normalized_body["source"] = cls._normalize_single_task_source(normalized_body.get("source", source))
        for key in ("need_crawler_time", "retry_times", "callback", "priority"):
            normalized_body.pop(key, None)

        if "max_pages" in normalized_body:
            try:
                normalized_body["max_pages"] = int(normalized_body["max_pages"])
            except (TypeError, ValueError):
                pass

        return json.dumps(
            cls._normalize_json_for_compare(normalized_body),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _reset_timeout_tasks(self,table_name):
        """重置超时的执行中任务（核心新增：解决程序中断导致的僵尸任务）"""
        self._check_connection()
        try:
            # 确保没有残留事务
            if self.conn.in_transaction:
                self.conn.rollback()

            # 计算超时阈值：当前时间 - 超时分钟数（datetime 格式）
            timeout_threshold = datetime.now() - timedelta(minutes=TASK_TIMEOUT_MINUTES)
            # 转换为 MySQL 可识别的 datetime 字符串（格式：YYYY-MM-DD HH:MM:SS）
            timeout_threshold_str = timeout_threshold.strftime("%Y-%m-%d %H:%M:%S")
            current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            reset_reason = f"任务执行超时{TASK_TIMEOUT_MINUTES}分钟，自动重置"

            # SQL逻辑：
            # 1. status=1（执行中）
            # 2. update_time < 超时阈值（datetime 比较）
            # 3. 重置为 status=0，更新时间为当前 datetime
            reset_sql = f"""
                       UPDATE {table_name} 
                       SET status=0, 
                           update_time=%s
                          
                       WHERE status=1 AND update_time < %s;
                   """

            self.cursor.execute(
                reset_sql,
                (current_time_str, timeout_threshold_str)
            )

            # 获取受影响的行数
            affected_rows = self.cursor.rowcount
            self.conn.commit()

            if affected_rows > 0:
                logger.info(f"✅ 重置了 {affected_rows} 个超时任务（status=1→0）")
            return affected_rows
        except Error as e:
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"重置超时任务失败: {str(e)}")
            return 0


    def create_sql(self,
                   table_name: Any,
                   cond_dict: Dict = None,
                   order: str = None,
                   fields: List = None,
                   group_by: str = None,  # 新增：分组字段
                   limit: Any = None,  # 新增：分页（支持数字/%s占位符）
                   for_update: bool = False,  # 新增：是否加FOR UPDATE
                   like_cond: Dict = None):  # 新增：LIKE条件（单独处理，避免和=混淆）
        """
        增强版通用查询SQL生成器
        新增支持：函数字段、GROUP BY、LIMIT、FOR UPDATE、LIKE条件、COUNT排序
        :param table_name: 表名（支持列表）
        :param cond_dict: 等值查询条件（k=v）
        :param order: 排序条件（如 "COUNT(*) ASC"）
        :param fields: 返回字段（支持函数表达式，如 ["SUBSTRING_INDEX(...) AS country", "id"]）
        :param group_by: 分组字段（如 "country"）
        :param limit: 分页限制（数字或%s占位符）
        :param for_update: 是否添加FOR UPDATE行锁
        :param like_cond: LIKE查询条件（如 {"shulex_commodity_id": "AMAZON-%-%"}）
        """
        # ========== 1. 处理表名 ==========
        if isinstance(table_name, list):
            table_name = ",".join(table_name)

        # ========== 2. 处理返回字段（支持函数表达式） ==========
        if not fields:
            fields_sql = f"select * from {table_name}"
        else:
            if isinstance(fields, list):
                fields = ",".join(fields)
                fields_sql = f"select {fields} from {table_name}"
            else:
                raise ValueError(
                    "fields must be a list (support function expressions like 'SUBSTRING_INDEX(...) AS country')")

        # ========== 3. 处理查询条件（等值+LIKE） ==========
        con_sql_parts = []  # 用列表拼接，避免字符串拼接的冗余AND/OR

        if cond_dict:
            for k, v in cond_dict.items():
                if v is None:
                    # 空值：is null
                    con_sql_parts.append(f"{k} is null")
                elif isinstance(v, list) and len(v) > 0:
                    # 列表值：生成IN条件
                    if all(isinstance(item, str) and item.startswith('%') for item in v):
                        # 全是占位符（如[%s]），不加引号
                        in_values = ",".join(v)
                    else:
                        # 普通值：区分字符串/数字，字符串加单引号
                        in_values = []
                        for item in v:
                            if isinstance(item, str) and not item.startswith('%'):
                                in_values.append(f"'{item}'")
                            else:
                                in_values.append(str(item))
                        in_values = ",".join(in_values)
                    con_sql_parts.append(f"{k} IN ({in_values})")
                else:
                    # 普通等值条件（原有逻辑）
                    if isinstance(v, list):
                        con_sql_parts.append(f"{k} = {v[0]}")
                    else:
                        # 支持%s占位符不加引号
                        con_sql_parts.append(f"{k} = '{v}'" if not str(v).startswith('%') else f"{k} = {v}")

        # 处理LIKE条件（新增）
        if like_cond:
            for k, v in like_cond.items():
                con_sql_parts.append(f"{k} LIKE '{v}'")

        # 拼接所有条件
        con_sql = " and ".join(con_sql_parts) if con_sql_parts else ""

        # ========== 4. 拼接基础SQL ==========
        sql = fields_sql
        if con_sql:
            sql += f" where {con_sql}"

        # ========== 5. 处理分组（新增） ==========
        if group_by:
            sql += f" group by {group_by}"

        # ========== 6. 处理排序 ==========
        if order:
            # 支持复杂排序（如 "COUNT(*) ASC"）
            sql += f" order by {order}"

        # ========== 7. 处理LIMIT（新增） ==========
        if limit is not None:
            sql += f" limit {limit}"

        # ========== 8. 处理FOR UPDATE（新增） ==========
        if for_update:
            sql += " for update"

        print('select:' + sql)
        return sql

    def pull_pending_tasks(self, table_name: Any,
                   cond_dict: Dict = None,
                   order: str = None,
                   fields: List = None,
                   group_by: str = None,  # 新增：分组字段
                   limit: Any = None,  # 新增：分页（支持数字/%s占位符）
                   for_update: bool = False,  # 新增：是否加FOR UPDATE
                   like_cond: Dict = None) -> List[Dict]:
        """
        :param table_name: 表名（支持列表）
        :param cond_dict: 等值查询条件（k=v）
        :param order: 排序条件（如 "COUNT(*) ASC"）
        :param fields: 返回字段（支持函数表达式，如 ["SUBSTRING_INDEX(...) AS country", "id"]）
        :param group_by: 分组字段（如 "country"）
        :param limit: 分页限制（数字或%s占位符）
        :param for_update: 是否添加FOR UPDATE行锁
        :param like_cond: LIKE查询条件（如 {"shulex_commodity_id": "AMAZON-%-%"}）

        :return:
        """
        self._check_connection()
        # 第一步：先重置所有超时的执行中任务（核心修复）
        self._reset_timeout_tasks(table_name)

        tasks = []
        try:
            # 1. 开启事务（原子操作：查询+更新）
            self.conn.start_transaction()

            # 2. 清空游标（兼容所有版本）
            try:
                self.cursor.fetchall()
            except:
                pass
            # ========== 步骤1：提取所有待执行任务的国家，锁定一个目标国家 ==========
            # 核心SQL：用 SUBSTRING_INDEX 提取国家码（AMAZON-UK-XXX → UK）

            # if not country:
            #     sql = """
            #                       SELECT SUBSTRING_INDEX(SUBSTRING_INDEX(shulex_commodity_id, '-', 2), '-', -1) AS country ,id, shulex_commodity_id
            #                       FROM asin_tasks
            #                       WHERE status = '0'
            #
            #                         AND shulex_commodity_id LIKE 'AMAZON-%-%' # 过滤格式合法的任务
            #                       GROUP BY country
            #                       ORDER BY COUNT(*) DESC # 优先拉取任务数最多的国家
            #                       LIMIT %s
            #                       FOR UPDATE;
            #                 """
            #     self.cursor.execute(sql, (TASK_BATCH_SIZE,))  # 参数化 LIMIT，避免拼接
            # else:
            #     sql = """
            #                     SELECT SUBSTRING_INDEX(SUBSTRING_INDEX(shulex_commodity_id, '-', 2), '-', -1) AS country ,id, shulex_commodity_id
            #                     FROM asin_tasks
            #                     WHERE status = '0'
            #                         AND SUBSTRING_INDEX(SUBSTRING_INDEX(shulex_commodity_id, '-', 2), '-', -1) = %s
            #                         AND shulex_commodity_id LIKE 'AMAZON-%-%' # 过滤格式合法的任务
            #                     GROUP BY country
            #                     ORDER BY COUNT(*) ASC # 优先拉取任务数最少的国家
            #                     LIMIT %s
            #                     FOR UPDATE;
            #               """
            #     self.cursor.execute(sql, (country,TASK_BATCH_SIZE,))  # 参数化 LIMIT，避免拼接

            sql = self.create_sql(table_name,cond_dict,order,fields,group_by,limit,for_update,like_cond)
            self.cursor.execute(sql)

            tasks = self.cursor.fetchall()

            # 4. 标记为执行中（事务内执行）
            if tasks:
                task_ids = [t["id"] for t in tasks]
                placeholders = ','.join(['%s'] * len(task_ids))
                current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                update_sql = f"""
                    UPDATE `{table_name}`  -- 加反引号避免表名和关键字冲突
                    SET status=1, update_time=%s 
                    WHERE id IN ({placeholders})
                """
                self.cursor.execute(
                    update_sql,
                    (current_time_str,) + tuple(task_ids)
                )

            # 5. 提交事务（释放锁，确保更新生效）
            self.conn.commit()
            # logger.info(f"成功拉取 {len(tasks)} 个任务，事务已提交")
            return tasks

        except Error as e:
            # 出错回滚事务，释放锁
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"拉取任务失败: {str(e)}")
            # 重置游标
            try:
                self.cursor.fetchall()
            except:
                pass
            return []

    def _table_exists(self, table_name: str) -> bool:
        """检查表是否已存在"""
        self.cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table_name,)
        )
        return self.cursor.fetchone()["cnt"] > 0

    def _index_exists(self, table_name: str, index_name: str) -> bool:
        """检查索引是否已存在。"""
        self.cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
            (table_name, index_name),
        )
        return self.cursor.fetchone()["cnt"] > 0

    def ensure_single_task_indexes(self) -> None:
        """补齐 single 任务接口/worker 依赖的索引。"""
        if not self._table_exists("crawl_single_tasks"):
            return
        changed = False
        if not self._index_exists("crawl_single_tasks", "idx_single_reuse"):
            self.cursor.execute(
                "ALTER TABLE crawl_single_tasks "
                "ADD KEY idx_single_reuse (asin, region, status, updated_at)"
            )
            changed = True
            logger.info("[mysql] crawl_single_tasks 已添加索引 idx_single_reuse")
        if changed:
            self.conn.commit()

    @staticmethod
    def _decode_json_field(value, default):
        if value is None or value == "":
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except Exception:
            return default

    def ensure_account_import_tables(self) -> None:
        """创建账号导入异步任务表。Linux API 入队，Windows browser-node 消费。"""
        self._check_connection()
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS `crawler_account_import_jobs` (
                `id` BIGINT NOT NULL AUTO_INCREMENT,
                `job_id` VARCHAR(64) NOT NULL,
                `status` TINYINT NOT NULL DEFAULT 0 COMMENT '0=pending 1=running 2=done 3=failed',
                `node_id` VARCHAR(128) NOT NULL DEFAULT '',
                `account_type` VARCHAR(10) NOT NULL DEFAULT '',
                `target_country` VARCHAR(10) NOT NULL DEFAULT '',
                `proxy_strategy` VARCHAR(24) NOT NULL DEFAULT '',
                `static_ip_count` INT NOT NULL DEFAULT 0,
                `static_ip_pool` JSON NULL,
                `limit_count` INT NOT NULL DEFAULT 0,
                `source_rows` INT NOT NULL DEFAULT 0,
                `queued_rows` INT NOT NULL DEFAULT 0,
                `attempted_rows` INT NOT NULL DEFAULT 0,
                `success_count` INT NOT NULL DEFAULT 0,
                `failed_count` INT NOT NULL DEFAULT 0,
                `existing_browser_count` INT NOT NULL DEFAULT 0,
                `file_proxy_count` INT NOT NULL DEFAULT 0,
                `created_usernames` JSON NULL,
                `failed_items` JSON NULL,
                `error_msg` TEXT NULL,
                `created_by` VARCHAR(64) NOT NULL DEFAULT 'admin',
                `created_at` DATETIME NOT NULL,
                `updated_at` DATETIME NOT NULL,
                `started_at` DATETIME DEFAULT NULL,
                `finished_at` DATETIME DEFAULT NULL,
                PRIMARY KEY (`id`),
                UNIQUE KEY `uniq_account_import_job_id` (`job_id`),
                KEY `idx_account_import_status` (`status`, `created_at`),
                KEY `idx_account_import_node` (`node_id`, `status`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='账号导入任务';
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS `crawler_account_import_items` (
                `id` BIGINT NOT NULL AUTO_INCREMENT,
                `job_id` VARCHAR(64) NOT NULL,
                `row_no` INT NOT NULL DEFAULT 0,
                `username` VARCHAR(128) NOT NULL DEFAULT '',
                `password` VARCHAR(256) NOT NULL DEFAULT '',
                `totp_secret` VARCHAR(256) NOT NULL DEFAULT '',
                `country` VARCHAR(10) NOT NULL DEFAULT '',
                `browser_id` VARCHAR(128) NOT NULL DEFAULT '',
                `had_browser_id` TINYINT NOT NULL DEFAULT 0,
                `proxy` TEXT NULL,
                `status` TINYINT NOT NULL DEFAULT 0 COMMENT '0=pending 1=running 2=done 3=failed',
                `node_id` VARCHAR(128) NOT NULL DEFAULT '',
                `error_msg` VARCHAR(512) NOT NULL DEFAULT '',
                `created_at` DATETIME NOT NULL,
                `updated_at` DATETIME NOT NULL,
                PRIMARY KEY (`id`),
                KEY `idx_account_import_item_job` (`job_id`, `id`),
                KEY `idx_account_import_item_status` (`job_id`, `status`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='账号导入明细';
        """)
        self.conn.commit()

    def create_account_import_job(self, *, job_id: str, accounts: List[Dict],
                                  account_type: str, target_country: str,
                                  static_ip_count: int = 0, static_ip_pool: List = None,
                                  limit_count: int = 0, proxy_strategy: str = "",
                                  created_by: str = "admin") -> Dict:
        self.ensure_account_import_tables()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        source_rows = len(accounts or [])
        safe_limit = max(int(limit_count or 0), 0)
        queued_accounts = list(accounts or [])
        if safe_limit:
            queued_accounts = queued_accounts[:safe_limit]
        existing_browser_count = sum(1 for item in queued_accounts if str(item.get("browser_id") or "").strip())
        file_proxy_count = sum(1 for item in queued_accounts if str(item.get("proxy") or "").strip())

        try:
            self.cursor.execute(
                """
                INSERT INTO crawler_account_import_jobs
                    (job_id, status, node_id, account_type, target_country, proxy_strategy,
                     static_ip_count, static_ip_pool, limit_count, source_rows, queued_rows,
                     attempted_rows, success_count, failed_count, existing_browser_count,
                     file_proxy_count, created_usernames, failed_items, error_msg,
                     created_by, created_at, updated_at)
                VALUES
                    (%s, 0, '', %s, %s, %s, %s, CAST(%s AS JSON), %s, %s, %s,
                     0, 0, 0, %s, %s, CAST(%s AS JSON), CAST(%s AS JSON), '',
                     %s, %s, %s)
                """,
                (
                    job_id,
                    account_type,
                    target_country,
                    proxy_strategy,
                    int(static_ip_count or 0),
                    json.dumps(static_ip_pool or [], ensure_ascii=False),
                    safe_limit,
                    source_rows,
                    len(queued_accounts),
                    existing_browser_count,
                    file_proxy_count,
                    json.dumps([], ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    created_by or "admin",
                    now,
                    now,
                ),
            )
            if queued_accounts:
                rows = []
                for item in queued_accounts:
                    browser_id = str(item.get("browser_id") or "").strip()
                    rows.append((
                        job_id,
                        int(item.get("row_no") or 0),
                        str(item.get("username") or "").strip(),
                        str(item.get("password") or ""),
                        str(item.get("totp_secret") or ""),
                        str(item.get("country") or target_country or "").strip().upper(),
                        browser_id,
                        1 if browser_id else 0,
                        str(item.get("proxy") or "").strip(),
                        now,
                        now,
                    ))
                self.cursor.executemany(
                    """
                    INSERT INTO crawler_account_import_items
                        (job_id, row_no, username, password, totp_secret, country,
                         browser_id, had_browser_id, proxy, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
            self.conn.commit()
            return self.get_account_import_job(job_id) or {"job_id": job_id}
        except Exception:
            if self.conn and self.conn.is_connected():
                self.conn.rollback()
            raise

    def get_account_import_job(self, job_id: str) -> Optional[Dict]:
        self._check_connection()
        self.cursor.execute(
            "SELECT * FROM crawler_account_import_jobs WHERE job_id=%s LIMIT 1",
            (job_id,),
        )
        row = self.cursor.fetchone()
        if not row:
            return None
        row["static_ip_pool"] = self._decode_json_field(row.get("static_ip_pool"), [])
        row["created_usernames"] = self._decode_json_field(row.get("created_usernames"), [])
        row["failed_items"] = self._decode_json_field(row.get("failed_items"), [])
        status_map = {0: "pending", 1: "running", 2: "done", 3: "failed"}
        row["status_desc"] = status_map.get(int(row.get("status") or 0), "unknown")
        return row

    def claim_next_account_import_job(self, node_id: str) -> Optional[Dict]:
        self.ensure_account_import_tables()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.cursor.execute("START TRANSACTION")
            self.cursor.execute(
                """
                SELECT * FROM crawler_account_import_jobs
                WHERE status = 0
                ORDER BY id ASC
                LIMIT 1
                FOR UPDATE
                """
            )
            row = self.cursor.fetchone()
            if not row:
                self.conn.commit()
                return None
            job_id = row["job_id"]
            self.cursor.execute(
                """
                UPDATE crawler_account_import_jobs
                SET status=1, node_id=%s, started_at=COALESCE(started_at, %s), updated_at=%s
                WHERE job_id=%s
                """,
                (node_id, now, now, job_id),
            )
            self.conn.commit()
            return self.get_account_import_job(job_id)
        except Exception:
            if self.conn and self.conn.is_connected():
                self.conn.rollback()
            raise

    def reset_stale_account_import_jobs(self, stale_seconds: int = 900) -> int:
        self.ensure_account_import_tables()
        cutoff = datetime.now() - timedelta(seconds=max(int(stale_seconds or 900), 60))
        cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.cursor.execute(
                """
                UPDATE crawler_account_import_items i
                JOIN crawler_account_import_jobs j ON j.job_id = i.job_id
                SET i.status=0, i.node_id='', i.error_msg='', i.updated_at=%s
                WHERE j.status=1 AND j.updated_at < %s AND i.status=1
                """,
                (now, cutoff_text),
            )
            self.cursor.execute(
                """
                UPDATE crawler_account_import_jobs
                SET status=0, node_id='', error_msg='', updated_at=%s
                WHERE status=1 AND updated_at < %s
                """,
                (now, cutoff_text),
            )
            affected = self.cursor.rowcount
            self.conn.commit()
            return int(affected or 0)
        except Exception:
            if self.conn and self.conn.is_connected():
                self.conn.rollback()
            raise

    def get_account_import_items(self, job_id: str) -> List[Dict]:
        self._check_connection()
        self.cursor.execute(
            "SELECT * FROM crawler_account_import_items WHERE job_id=%s ORDER BY id ASC",
            (job_id,),
        )
        return self.cursor.fetchall() or []

    def mark_account_import_item_running(self, item_id: int, node_id: str) -> None:
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cursor.execute(
            """
            UPDATE crawler_account_import_items
            SET status=1, node_id=%s, error_msg='', updated_at=%s
            WHERE id=%s AND status=0
            """,
            (node_id, now, item_id),
        )
        self.conn.commit()

    def mark_account_import_item_done(self, item_id: int, username: str, browser_id: str) -> None:
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cursor.execute(
            """
            UPDATE crawler_account_import_items
            SET status=2, username=%s, browser_id=%s, error_msg='', updated_at=%s
            WHERE id=%s
            """,
            (username, browser_id, now, item_id),
        )
        self.conn.commit()

    def mark_account_import_item_failed(self, item_id: int, error_msg: str) -> None:
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cursor.execute(
            """
            UPDATE crawler_account_import_items
            SET status=3, error_msg=%s, updated_at=%s
            WHERE id=%s
            """,
            (str(error_msg or "")[:500], now, item_id),
        )
        self.conn.commit()

    def refresh_account_import_job_stats(self, job_id: str, final_status: Optional[int] = None,
                                         error_msg: str = "") -> Optional[Dict]:
        self._check_connection()
        self.cursor.execute(
            """
            SELECT
                COUNT(1) AS total_count,
                SUM(CASE WHEN status <> 0 THEN 1 ELSE 0 END) AS attempted_rows,
                SUM(CASE WHEN status = 2 THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN status = 3 THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN status = 0 THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) AS running_count
            FROM crawler_account_import_items
            WHERE job_id=%s
            """,
            (job_id,),
        )
        stats = self.cursor.fetchone() or {}
        self.cursor.execute(
            """
            SELECT username
            FROM crawler_account_import_items
            WHERE job_id=%s AND status=2
            ORDER BY id ASC
            LIMIT 50
            """,
            (job_id,),
        )
        created_usernames = [str(row.get("username") or "") for row in (self.cursor.fetchall() or [])]
        self.cursor.execute(
            """
            SELECT row_no, username, error_msg
            FROM crawler_account_import_items
            WHERE job_id=%s AND status=3
            ORDER BY id ASC
            LIMIT 200
            """,
            (job_id,),
        )
        failed_items = [
            {
                "row_no": int(row.get("row_no") or 0),
                "username": str(row.get("username") or ""),
                "reason": str(row.get("error_msg") or "unknown"),
            }
            for row in (self.cursor.fetchall() or [])
        ]

        total_count = int(stats.get("total_count") or 0)
        pending_count = int(stats.get("pending_count") or 0)
        running_count = int(stats.get("running_count") or 0)
        if final_status is None and total_count > 0 and pending_count == 0 and running_count == 0:
            final_status = 2

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        set_parts = [
            "attempted_rows=%s",
            "success_count=%s",
            "failed_count=%s",
            "created_usernames=CAST(%s AS JSON)",
            "failed_items=CAST(%s AS JSON)",
            "updated_at=%s",
        ]
        params = [
            int(stats.get("attempted_rows") or 0),
            int(stats.get("success_count") or 0),
            int(stats.get("failed_count") or 0),
            json.dumps(created_usernames, ensure_ascii=False),
            json.dumps(failed_items, ensure_ascii=False),
            now,
        ]
        if final_status is not None:
            set_parts.append("status=%s")
            params.append(int(final_status))
            if int(final_status) in (2, 3):
                set_parts.append("finished_at=%s")
                params.append(now)
        if error_msg:
            set_parts.append("error_msg=%s")
            params.append(str(error_msg)[:1000])
        params.append(job_id)
        self.cursor.execute(
            f"UPDATE crawler_account_import_jobs SET {', '.join(set_parts)} WHERE job_id=%s",
            tuple(params),
        )
        self.conn.commit()
        return self.get_account_import_job(job_id)

    def init_queue_tables(self):
        """初始化任务队列表（主任务+子任务+单任务），建表前先检查是否已存在"""
        self._check_connection()

        tables = {
            # "crawl_tasks": """
            #     CREATE TABLE `crawl_tasks` (
            #         `id` BIGINT NOT NULL AUTO_INCREMENT,
            #         `task_no` VARCHAR(64) NOT NULL,
            #         `task_type` VARCHAR(32) NOT NULL,
            #         `query_conditions` JSON NULL,
            #         `status` TINYINT NOT NULL DEFAULT 0,
            #         `total_subtasks` INT NOT NULL DEFAULT 0,
            #         `running_subtasks` INT NOT NULL DEFAULT 0,
            #         `success_subtasks` INT NOT NULL DEFAULT 0,
            #         `failed_subtasks` INT NOT NULL DEFAULT 0,
            #         `created_at` DATETIME NOT NULL,
            #         `updated_at` DATETIME NOT NULL,
            #         PRIMARY KEY (`id`),
            #         UNIQUE KEY `uniq_task_no` (`task_no`),
            #         KEY `idx_task_status` (`status`)
            #     ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            # """,
            # "crawl_subtasks": """
            #     CREATE TABLE `crawl_subtasks` (
            #         `id` BIGINT NOT NULL AUTO_INCREMENT,
            #         `task_id` BIGINT NOT NULL,
            #         `asin` VARCHAR(32) NOT NULL,
            #         `country` VARCHAR(10) NOT NULL,
            #         `max_pages` INT NOT NULL DEFAULT 3,
            #         `task_type` VARCHAR(32) NOT NULL,
            #         `query_conditions` JSON NULL,
            #         `status` TINYINT NOT NULL DEFAULT 0,
            #         `retry_times` INT NOT NULL DEFAULT 2,
            #         `retry_count` INT NOT NULL DEFAULT 0,
            #         `worker_name` VARCHAR(64) NULL,
            #         `error_msg` TEXT NULL,
            #         `result_count` INT NOT NULL DEFAULT 0,
            #         `created_at` DATETIME NOT NULL,
            #         `updated_at` DATETIME NOT NULL,
            #         PRIMARY KEY (`id`),
            #         KEY `idx_subtask_status` (`status`),
            #         KEY `idx_task_id` (`task_id`),
            #         KEY `idx_country_status` (`country`, `status`)
            #     ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            # """,
            #

            "crawl_single_tasks": """
                CREATE TABLE `crawl_single_tasks` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT,
                    `task_id` VARCHAR(64) NOT NULL COMMENT '生成的唯一任务ID',
                    `asin` VARCHAR(32) NOT NULL,
                    `region` VARCHAR(10) NOT NULL COMMENT '国家/站点编码，如 US/DE/JP',
                    `priority` INT NOT NULL DEFAULT 100 COMMENT '任务优先级，数字越小越优先',
                    `need_crawler_time` DATETIME NOT NULL COMMENT '需要抓取的时间（调度排序依据）',
                    `params` JSON NULL COMMENT '业务参数（动态透传，含 max_pages/query_conditions 等所有字段）',
                    `source` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '任务来源：空=正常任务，stress_test=压测任务',
                    `status` TINYINT NOT NULL DEFAULT 0 COMMENT '0=待执行 1=执行中 2=成功 3=失败',
                    `retry_count` INT NOT NULL DEFAULT 0,
                    `worker_name` VARCHAR(64) NULL,
                    `error_msg` TEXT NULL,
                    `result_count` INT NOT NULL DEFAULT 0,
                    `result` LONGTEXT NULL COMMENT '抓取结果JSON（直接存MySQL，查询时一次返回）',
                    `callback_url` VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '任务完成后回调地址',
                    `oss_object_key` VARCHAR(512) NOT NULL DEFAULT '' COMMENT 'OSS结果文件key',
                    `oss_result_url` TEXT NULL COMMENT 'OSS结果签名URL',
                    `callback_status` TINYINT NOT NULL DEFAULT 3 COMMENT '0=待回调 1=成功 2=失败 3=无callback',
                    `callback_attempts` INT NOT NULL DEFAULT 0 COMMENT '回调尝试次数',
                    `callback_last_error` TEXT NULL COMMENT '最近一次回调/上传错误',
                    `callback_updated_at` DATETIME NULL COMMENT '最近一次回调状态更新时间',
                    `created_at` DATETIME NOT NULL,
                    `updated_at` DATETIME NOT NULL,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uniq_task_id` (`task_id`),
                    KEY `idx_status_need_time` (`status`, `need_crawler_time`),
                    KEY `idx_single_priority` (`status`, `priority`, `need_crawler_time`),
                    KEY `idx_region_status` (`region`, `status`),
                    KEY `idx_source_status` (`source`, `status`),
                    KEY `idx_callback_retry` (`callback_status`, `callback_updated_at`),
                    KEY `idx_single_reuse` (`asin`, `region`, `status`, `updated_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
            "crawler_accounts": """
                CREATE TABLE `crawler_accounts` (
                `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                `username` VARCHAR(64) NOT NULL COMMENT '账号（手机号等）',
                `password` VARCHAR(128) NOT NULL COMMENT '密码',
                `country` VARCHAR(10) NOT NULL COMMENT '国家编码，如 us/jp/de',
                `cookies` JSON NULL COMMENT 'Cookies信息',
                `proxy_` JSON NULL COMMENT '代理配置',
                `fingerprint_id` VARCHAR(128) NOT NULL DEFAULT '' COMMENT '指纹浏览器ID',
                `totp_secret` VARCHAR(128) NULL DEFAULT NULL COMMENT '二次验证码密钥',
                `state` TINYINT NOT NULL DEFAULT 1 COMMENT '1=可用 0=不可用 -1=异常',
                `is_used` TINYINT NOT NULL DEFAULT 0 COMMENT '0=空闲 1=占用中',
                `last_used_time` DOUBLE NOT NULL DEFAULT 0.0 COMMENT '最后使用时间戳',
                `fail_count` INT NOT NULL DEFAULT 0 COMMENT '失败次数',
                `cooldown_until` DOUBLE NOT NULL DEFAULT 0.0 COMMENT '冷却结束时间戳',
                `city` VARCHAR(64) DEFAULT '' COMMENT '城市',
                `user_agent` VARCHAR(4096) DEFAULT '' COMMENT 'User Agent',
                `refresh_time` VARCHAR(32) DEFAULT '' COMMENT 'Cookies刷新时间',
                `create_time` VARCHAR(32) NOT NULL COMMENT '创建时间',
                `update_time` VARCHAR(32) NOT NULL COMMENT '更新时间',
                `quota_factor` DECIMAL(3,2) NOT NULL DEFAULT 1.00 COMMENT '日预算配额因子',
                `label` VARCHAR(32)  NULL DEFAULT '' COMMENT '账号分组标签，压测账号填 stress_test',
                PRIMARY KEY (`id`),
                UNIQUE KEY `uniq_username` (`username`),
                KEY `idx_state_country` (`state`, `country`),
                KEY `idx_is_used` (`is_used`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫账号表';
            """,
            "reviews_error": """
                CREATE TABLE `reviews_error` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT,
                    `asin` VARCHAR(32) NOT NULL,
                    `country` VARCHAR(10) NOT NULL,
                    `resp` LONGTEXT NOT NULL COMMENT '异常HTML响应内容',
                    `review_data` JSON NULL COMMENT '解析失败的原始review数据',
                    `task_info` JSON NULL COMMENT '任务信息（含asin/country等）',
                    `error_msg` VARCHAR(512) DEFAULT '' COMMENT '错误信息',
                    `status` TINYINT DEFAULT 0 COMMENT '0=待处理 1=已处理',
                    `created_at` DATETIME NOT NULL,
                    `updated_at` DATETIME NOT NULL,
                    PRIMARY KEY (`id`),
                    KEY `idx_asin` (`asin`),
                    KEY `idx_status` (`status`),
                    KEY `idx_created_at` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
            "account_usage_log": """
                CREATE TABLE `account_usage_log` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT,
                    `task_id` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '任务ID',
                    `asin` VARCHAR(32) NOT NULL DEFAULT '' COMMENT 'ASIN',
                    `country` VARCHAR(10) NOT NULL DEFAULT '' COMMENT '国家编码',
                    `username` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '使用的账号',
                    `success` TINYINT NOT NULL DEFAULT 0 COMMENT '1=成功 0=失败',
                    `review_count` INT NOT NULL DEFAULT 0 COMMENT '实际抓取评论数',
                    `expected_count` INT NOT NULL DEFAULT 0 COMMENT '预期评论数',
                    `start_time` DATETIME NULL COMMENT '任务开始时间',
                    `end_time` DATETIME NULL COMMENT '任务结束时间',
                    `duration_seconds` INT NOT NULL DEFAULT 0 COMMENT '耗时（秒）',
                    `retry_count` INT NOT NULL DEFAULT 0 COMMENT '重试次数',
                    `error_msg` TEXT NULL COMMENT '错误信息',
                    `worker_id` VARCHAR(64) DEFAULT '' COMMENT '执行进程/机器标识',
                    `ip` VARCHAR(46) DEFAULT '' COMMENT '机器IP',
                    `task_type` VARCHAR(32) DEFAULT 'review' COMMENT '任务类型',
                    `created_at` DATETIME NOT NULL,
                    PRIMARY KEY (`id`),
                    KEY `idx_ul_username` (`username`),
                    KEY `idx_ul_asin` (`asin`),
                    KEY `idx_ul_country` (`country`),
                    KEY `idx_ul_success` (`success`),
                    KEY `idx_ul_created_at` (`created_at`),
                    KEY `idx_ul_username_created` (`username`, `created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
            "account_daily_summary": """
                CREATE TABLE `account_daily_summary` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT,
                    `username` VARCHAR(64) NOT NULL COMMENT '账号',
                    `date` DATE NOT NULL COMMENT '统计日期',
                    `country` VARCHAR(10) NOT NULL DEFAULT '' COMMENT '国家编码',
                    `total_tasks` INT NOT NULL DEFAULT 0 COMMENT '任务总数',
                    `success_tasks` INT NOT NULL DEFAULT 0,
                    `failed_tasks` INT NOT NULL DEFAULT 0,
                    `total_pages` INT NOT NULL DEFAULT 0 COMMENT '总翻页数',
                    `total_reviews` INT NOT NULL DEFAULT 0 COMMENT '总评论数',
                    `captcha_count` INT NOT NULL DEFAULT 0,
                    `ban_count` INT NOT NULL DEFAULT 0,
                    `login_redirect_count` INT NOT NULL DEFAULT 0,
                    `robot_check_count` INT NOT NULL DEFAULT 0,
                    `proxy_rotate_count` INT NOT NULL DEFAULT 0,
                    `avg_duration_seconds` FLOAT NOT NULL DEFAULT 0,
                    `total_duration_seconds` INT NOT NULL DEFAULT 0,
                    `session_count` INT NOT NULL DEFAULT 0,
                    `active_hour_distribution` JSON NULL COMMENT '每小时请求数分布 {"9":5,"10":8}',
                    `request_interval_stddev` FLOAT DEFAULT NULL COMMENT '请求间隔标准差(秒)',
                    `last_fresh_login_at` DATETIME DEFAULT NULL COMMENT '最近完整登录时间',
                    `cookie_age_hours` FLOAT DEFAULT NULL COMMENT 'Cookie 年龄(小时)',
                    `distinct_ips` INT NOT NULL DEFAULT 0 COMMENT '不同IP数量',
                    `distinct_asins` INT NOT NULL DEFAULT 0,
                    `error_rate` FLOAT NOT NULL DEFAULT 0 COMMENT '错误率',
                    `created_at` DATETIME NOT NULL,
                    `updated_at` DATETIME NOT NULL,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uniq_user_date` (`username`, `date`),
                    KEY `idx_ads_date` (`date`),
                    KEY `idx_ads_country` (`country`),
                    KEY `idx_ads_error_rate` (`error_rate`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日账号聚合指标';
            """,
            "account_risk_profile": """
                CREATE TABLE `account_risk_profile` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT,
                    `username` VARCHAR(64) NOT NULL COMMENT '账号',
                    `country` VARCHAR(10) NOT NULL DEFAULT '' COMMENT '国家编码',
                    `risk_score` FLOAT NOT NULL DEFAULT 0 COMMENT '风险分 0-100',
                    `risk_level` VARCHAR(16) NOT NULL DEFAULT 'low' COMMENT 'low/medium/high/critical',
                    `avg_daily_error_rate_7d` FLOAT DEFAULT 0,
                    `avg_daily_ban_count_7d` FLOAT DEFAULT 0,
                    `avg_daily_captcha_count_7d` FLOAT DEFAULT 0,
                    `total_days_active_30d` INT DEFAULT 0,
                    `trend_direction` VARCHAR(16) DEFAULT 'stable' COMMENT 'improving/stable/worsening',
                    `recommended_daily_budget` INT DEFAULT NULL COMMENT '建议每日任务预算',
                    `recommended_page_budget` INT DEFAULT NULL COMMENT '建议每日翻页预算',
                    `recommended_rest_minutes` INT DEFAULT NULL COMMENT '建议休息时长(分)',
                    `last_analyzed_at` DATETIME NOT NULL,
                    `created_at` DATETIME NOT NULL,
                    `updated_at` DATETIME NOT NULL,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uniq_user_country` (`username`, `country`),
                    KEY `idx_arp_risk_level` (`risk_level`),
                    KEY `idx_arp_risk_score` (`risk_score`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='账号风险画像';
            """,
            "account_ip_log": """
                CREATE TABLE `account_ip_log` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT,
                    `username` VARCHAR(64) NOT NULL,
                    `ip` VARCHAR(200) NOT NULL,
                    `country` VARCHAR(10) NOT NULL DEFAULT '',
                    `first_seen_at` DATETIME NOT NULL,
                    `last_seen_at` DATETIME NOT NULL,
                    `request_count` INT NOT NULL DEFAULT 1,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uniq_user_ip` (`username`, `ip`),
                    KEY `idx_ail_ip` (`ip`),
                    KEY `idx_ail_last_seen` (`last_seen_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='账号IP使用记录';
            """,
            "stress_test_log": """
                CREATE TABLE `stress_test_log` (
                    `id`            BIGINT NOT NULL AUTO_INCREMENT,
                    `test_day`      INT NOT NULL COMMENT '测试第几天（从1开始）',
                    `username`      VARCHAR(64) NOT NULL COMMENT '账号',
                    `group_name`    VARCHAR(8) NOT NULL COMMENT '分组 A/B/C',
                    `target_pages`  INT NOT NULL COMMENT '当日目标页面数',
                    `actual_pages`  INT NOT NULL DEFAULT 0 COMMENT '实际页面数',
                    `task_count`    INT NOT NULL DEFAULT 0 COMMENT '实际任务数',
                    `success_count` INT NOT NULL DEFAULT 0,
                    `fail_count`    INT NOT NULL DEFAULT 0,
                    `banned`        TINYINT NOT NULL DEFAULT 0 COMMENT '1=当日有封号',
                    `created_at`    DATE NOT NULL COMMENT '记录日期',
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uniq_user_day` (`username`, `test_day`),
                    KEY `idx_test_day` (`test_day`),
                    KEY `idx_created_at` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='压力测试每日结果汇总';
            """,
            "crawler_queue_depth_snapshot": """
                CREATE TABLE `crawler_queue_depth_snapshot` (
                    `id`          BIGINT NOT NULL AUTO_INCREMENT,
                    `queue_name`  VARCHAR(64) NOT NULL COMMENT '逻辑队列名，如 single_us/asin',
                    `redis_key`   VARCHAR(128) NOT NULL COMMENT 'Redis list key',
                    `depth`       INT NOT NULL COMMENT 'Redis LLEN 结果，-1 表示采集失败',
                    `created_at`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (`id`),
                    KEY `idx_qds_queue_time` (`queue_name`, `created_at`),
                    KEY `idx_qds_created_at` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Redis任务队列深度采样';
            """,
            "crawler_runtime_status": """
                CREATE TABLE `crawler_runtime_status` (
                    `component`   VARCHAR(64) NOT NULL COMMENT '组件名，如 daemon_main',
                    `status`      VARCHAR(16) NOT NULL DEFAULT 'ok',
                    `message`     VARCHAR(512) NOT NULL DEFAULT '',
                    `updated_at`  DATETIME NOT NULL,
                    PRIMARY KEY (`component`),
                    KEY `idx_crs_updated_at` (`updated_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫运行时心跳/状态';
            """,
        }

        for table_name, create_sql in tables.items():
            if self._table_exists(table_name):
                logger.info(f"表 {table_name} 已存在，跳过建表")
            else:
                self.cursor.execute(create_sql)
                logger.info(f"表 {table_name} 创建成功")
        self.conn.commit()
        try:
            self.ensure_single_task_indexes()
            self.ensure_single_task_priority_columns()
            self.ensure_single_task_callback_columns()
        except Exception as exc:
            try:
                self.conn.rollback()
            except Exception:
                pass
            logger.warning(f"[mysql] 补齐 crawl_single_tasks 索引失败: {exc}")

    def ensure_single_task_priority_columns(self) -> None:
        """补齐 single 任务 priority 字段。数字越小优先级越高。"""
        if not self._table_exists("crawl_single_tasks"):
            return
        self._ensure_column("crawl_single_tasks", "priority",
                            "INT NOT NULL DEFAULT 100 COMMENT '任务优先级，数字越小越优先'")
        if not self._index_exists("crawl_single_tasks", "idx_single_priority"):
            self.cursor.execute(
                "ALTER TABLE crawl_single_tasks ADD KEY idx_single_priority (status, priority, need_crawler_time)"
            )
            self.conn.commit()

    def ensure_single_task_callback_columns(self) -> None:
        """补齐 single 任务 callback/OSS 字段。"""
        if not self._table_exists("crawl_single_tasks"):
            return
        self._ensure_column("crawl_single_tasks", "callback_url",
                            "VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '任务完成后回调地址'")
        self._ensure_column("crawl_single_tasks", "oss_object_key",
                            "VARCHAR(512) NOT NULL DEFAULT '' COMMENT 'OSS结果文件key'")
        self._ensure_column("crawl_single_tasks", "oss_result_url",
                            "TEXT NULL COMMENT 'OSS结果签名URL'")
        self._ensure_column("crawl_single_tasks", "callback_status",
                            "TINYINT NOT NULL DEFAULT 3 COMMENT '0=待回调 1=成功 2=失败 3=无callback'")
        self._ensure_column("crawl_single_tasks", "callback_attempts",
                            "INT NOT NULL DEFAULT 0 COMMENT '回调尝试次数'")
        self._ensure_column("crawl_single_tasks", "callback_last_error",
                            "TEXT NULL COMMENT '最近一次回调/上传错误'")
        self._ensure_column("crawl_single_tasks", "callback_updated_at",
                            "DATETIME NULL COMMENT '最近一次回调状态更新时间'")
        if not self._index_exists("crawl_single_tasks", "idx_callback_retry"):
            self.cursor.execute(
                "ALTER TABLE crawl_single_tasks ADD KEY idx_callback_retry (callback_status, callback_updated_at)"
            )
            self.conn.commit()
        self.cursor.execute(
            "UPDATE crawl_single_tasks SET callback_status=3 WHERE callback_url='' AND callback_status=0"
        )
        if self.cursor.rowcount:
            self.conn.commit()

    def ensure_monitoring_tables(self) -> None:
        """创建 Grafana/告警使用的轻量监控表。"""
        self._check_connection()
        ddl_queue = """
            CREATE TABLE IF NOT EXISTS `crawler_queue_depth_snapshot` (
                `id`          BIGINT NOT NULL AUTO_INCREMENT,
                `queue_name`  VARCHAR(64) NOT NULL COMMENT '逻辑队列名，如 single_us/asin',
                `redis_key`   VARCHAR(128) NOT NULL COMMENT 'Redis list key',
                `depth`       INT NOT NULL COMMENT 'Redis LLEN 结果，-1 表示采集失败',
                `created_at`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (`id`),
                KEY `idx_qds_queue_time` (`queue_name`, `created_at`),
                KEY `idx_qds_created_at` (`created_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Redis任务队列深度采样';
        """
        ddl_status = """
            CREATE TABLE IF NOT EXISTS `crawler_runtime_status` (
                `component`   VARCHAR(64) NOT NULL COMMENT '组件名，如 daemon_main',
                `status`      VARCHAR(16) NOT NULL DEFAULT 'ok',
                `message`     VARCHAR(512) NOT NULL DEFAULT '',
                `updated_at`  DATETIME NOT NULL,
                PRIMARY KEY (`component`),
                KEY `idx_crs_updated_at` (`updated_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫运行时心跳/状态';
        """
        self.cursor.execute(ddl_queue)
        self.cursor.execute(ddl_status)
        self.conn.commit()

    def record_queue_depth_snapshot(self, snapshot: Dict[str, int],
                                    queue_keys: Dict[str, str]) -> None:
        """批量写入 Redis 队列深度快照，供 Grafana 用 MySQL 查询。"""
        if not snapshot:
            return
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            (str(name), str(queue_keys.get(name, "")), int(depth), now)
            for name, depth in snapshot.items()
        ]
        self.cursor.executemany(
            """
            INSERT INTO crawler_queue_depth_snapshot
                (queue_name, redis_key, depth, created_at)
            VALUES (%s, %s, %s, %s)
            """,
            rows,
        )
        self.conn.commit()

    def update_runtime_status(self, component: str, status: str = "ok",
                              message: str = "") -> None:
        """更新组件心跳/状态。"""
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cursor.execute(
            """
            INSERT INTO crawler_runtime_status (component, status, message, updated_at)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                status = VALUES(status),
                message = VALUES(message),
                updated_at = VALUES(updated_at)
            """,
            (component, status, (message or "")[:512], now),
        )
        self.conn.commit()

    def get_runtime_statuses(self, components: List[str]) -> Dict[str, Dict[str, Any]]:
        """按 component 批量读取运行时状态。"""
        names = [str(item) for item in components if item]
        if not names:
            return {}
        self._check_connection()
        placeholders = ",".join(["%s"] * len(names))
        self.cursor.execute(
            f"""
            SELECT component, status, message, updated_at
            FROM crawler_runtime_status
            WHERE component IN ({placeholders})
            """,
            tuple(names),
        )
        rows = self.cursor.fetchall() or []
        return {str(row["component"]): row for row in rows}

    def cleanup_queue_depth_snapshots(self, retain_hours: int = 72) -> int:
        """清理旧队列深度采样，避免监控表无限增长。"""
        self._check_connection()
        self.cursor.execute(
            """
            DELETE FROM crawler_queue_depth_snapshot
            WHERE created_at < DATE_SUB(NOW(), INTERVAL %s HOUR)
            """,
            (int(retain_hours),),
        )
        affected = self.cursor.rowcount
        self.conn.commit()
        return int(affected or 0)

    def create_single_task(self, task_id: str, asin: str, region: str,
                           need_crawler_time: str, params: Dict = None,
                           source: str = "") -> Dict:
        """创建单个抓取任务（动态参数，全部存 params JSON）

        :param source: 任务来源标签（如 'stress_test'），写入 crawl_single_tasks.source，
                       worker 侧可按 source 过滤只消费对应来源的任务。
        """
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        params_json = json.dumps(params or {}, ensure_ascii=False)
        source_val = str(source or "")
        priority = normalize_task_priority((params or {}).get("priority"), DEFAULT_TASK_PRIORITY)
        callback_url = str((params or {}).get("callback") or "").strip()
        callback_status = 0 if callback_url else 3
        try:
            self._rollback_active_transaction()
            self.conn.start_transaction()
            insert_sql = """
                INSERT INTO crawl_single_tasks
                (task_id, asin, region, priority, need_crawler_time, params, source, status, retry_count,
                 callback_url, callback_status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, CAST(%s AS JSON), %s, 0, 0, %s, %s, %s, %s)
            """
            self.cursor.execute(insert_sql, (
                task_id, asin.upper(), region.upper(), priority, need_crawler_time,
                params_json, source_val, callback_url, callback_status, now, now
            ))
            row_id = self.cursor.lastrowid
            self.conn.commit()
            return {"id": row_id, "task_id": task_id, "asin": asin.upper(), "region": region.upper(),
                    "priority": priority, "need_crawler_time": need_crawler_time, "source": source_val}
        except Exception:
            if self.conn.is_connected():
                self.conn.rollback()
            raise

    def create_single_task_from_result(
            self,
            task_id: str,
            asin: str,
            region: str,
            need_crawler_time: str,
            params: Dict = None,
            source: str = "",
            reused_task: Dict = None,
    ) -> Dict:
        """创建一条新的已完成 single 任务，结果复制自最近成功任务。"""
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        params_json = json.dumps(params or {}, ensure_ascii=False)
        source_val = str(source or "")
        priority = normalize_task_priority((params or {}).get("priority"), DEFAULT_TASK_PRIORITY)
        callback_url = str((params or {}).get("callback") or "").strip()
        callback_status = 0 if callback_url else 3
        reused_task = reused_task or {}
        result_data = reused_task.get("result")
        if result_data is None:
            result_json = None
        elif isinstance(result_data, str):
            result_json = result_data
        else:
            result_json = json.dumps(result_data, ensure_ascii=False)

        result_count = int(reused_task.get("result_count") or 0)
        error_msg = str(reused_task.get("error_msg") or "")
        try:
            self._rollback_active_transaction()
            self.conn.start_transaction()
            insert_sql = """
                INSERT INTO crawl_single_tasks
                (task_id, asin, region, priority, need_crawler_time, params, source, status, retry_count,
                 result_count, result, error_msg, callback_url, callback_status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, CAST(%s AS JSON), %s, 2, 0, %s, %s, %s, %s, %s, %s, %s)
            """
            self.cursor.execute(insert_sql, (
                task_id, asin.upper(), region.upper(), priority, need_crawler_time,
                params_json, source_val, result_count, result_json, error_msg[:1000],
                callback_url, callback_status, now, now
            ))
            row_id = self.cursor.lastrowid
            self.conn.commit()
            return {
                "id": row_id,
                "task_id": task_id,
                "asin": asin.upper(),
                "region": region.upper(),
                "priority": priority,
                "need_crawler_time": need_crawler_time,
                "source": source_val,
                "status": 2,
                "result_count": result_count,
                "result": result_data,
                "error_msg": error_msg,
                "updated_at": now,
                "reused_from_task_id": reused_task.get("task_id"),
            }
        except Exception:
            if self.conn.is_connected():
                self.conn.rollback()
            raise

    def find_recent_reusable_single_task(
            self,
            asin: str,
            region: str,
            params: Dict = None,
            source: str = "",
            minutes: int = 20,
    ) -> Dict:
        """查找最近同业务条件的成功 single 任务，命中后复制结果给新任务。"""
        self._check_connection()

        try:
            ttl_minutes = max(int(minutes or 0), 0)
        except (TypeError, ValueError):
            ttl_minutes = 20
        if ttl_minutes <= 0:
            return {}

        source_filter = self._normalize_single_task_source(source)
        target_signature = self._single_task_reuse_signature(asin, region, params, source_filter)

        where_parts = [
            "asin = %s",
            "region = %s",
            "status = 2",
            "updated_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)",
        ]
        query_params: List[Any] = [str(asin or "").strip().upper(), str(region or "").strip().upper(), ttl_minutes]

        sql = f"""
            SELECT id, task_id, asin, region, priority, need_crawler_time, params, source, status,
                   retry_count, result_count, error_msg, created_at, updated_at
            FROM crawl_single_tasks
            WHERE {' AND '.join(where_parts)}
            ORDER BY updated_at DESC
            LIMIT 50
        """
        self.cursor.execute(sql, tuple(query_params))
        rows = self.cursor.fetchall() or []
        self._end_read_transaction()

        for row in rows:
            row_signature = self._single_task_reuse_signature(
                row.get("asin"),
                row.get("region"),
                row.get("params") or {},
                row.get("source") or "",
            )
            if row_signature != target_signature:
                continue

            row_params = self._json_value(row.get("params") or {})
            row["params"] = row_params if isinstance(row_params, dict) else {}
            self.cursor.execute(
                "SELECT result FROM crawl_single_tasks WHERE id=%s",
                (row["id"],),
            )
            result_row = self.cursor.fetchone() or {}
            self._end_read_transaction()
            result_raw = result_row.get("result")
            if result_raw and isinstance(result_raw, str):
                try:
                    row["result"] = json.loads(result_raw)
                except (json.JSONDecodeError, TypeError):
                    row["result"] = result_raw
            else:
                row["result"] = result_raw
            return row
        return {}

    def claim_single_tasks(self, limit: int = 1, region: str = None,
                           task_type: str = None, worker_name: str = "",
                           need_crawler_delay_minutes: int = SINGLE_TASK_NEED_CRAWLER_DELAY_MINUTES,
                           source: str = None) -> List[Dict]:
        """
        拉取并锁定待执行的单任务（status: 0 -> 1）
        排序规则：priority ASC, need_crawler_time ASC, id ASC。
        need_crawler_time 只作为同优先级下的辅助排序字段，不再作为可执行门槛。

        :param source: 若指定，只拉取 source = 该值的任务（如 'stress_test'）；
                       None 表示拉取正常任务，自动排除 source='stress_test' 的压测任务。
        """
        from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import STRESS_TEST_LABEL
        source_filter = self._normalize_single_task_source(source)
        # single 任务由 WorkerRecoveryTracker + SINGLE_TASK_HARD_TIMEOUT_SECONDS 接管中断恢复。
        # 这里不再使用旧的 TASK_TIMEOUT_MINUTES 重置逻辑，避免运行中任务被提前放回队列。
        self._check_connection()
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.conn.start_transaction()
                delay_minutes = max(int(need_crawler_delay_minutes or 0), 0)

                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if SINGLE_NEED_TIME_SLA_ENABLED:
                    timeout_where_parts = [
                        "status NOT IN (2, 3)",
                        "need_crawler_time < DATE_SUB(NOW(), INTERVAL %s MINUTE)",
                    ]
                    timeout_params = [delay_minutes]
                    if region:
                        timeout_where_parts.append("region = %s")
                        timeout_params.append(region.upper())
                    if task_type:
                        timeout_where_parts.append("JSON_UNQUOTE(JSON_EXTRACT(params, '$.task_type')) = %s")
                        timeout_params.append(task_type)
                    self._append_single_source_filter(
                        timeout_where_parts, timeout_params, source_filter, STRESS_TEST_LABEL
                    )
                    timeout_sql = f"""
                        UPDATE crawl_single_tasks
                        SET status = 3,
                            updated_at = %s,
                            error_msg = %s
                        WHERE {' AND '.join(timeout_where_parts)}
                    """
                    self.cursor.execute(timeout_sql, (now, '任务超时，设置为失败，', *timeout_params))

                where_parts = [
                    "status = 0",
                ]
                params = []
                if SINGLE_NEED_TIME_SLA_ENABLED:
                    where_parts.append("need_crawler_time >= DATE_SUB(NOW(), INTERVAL %s MINUTE)")
                    params.append(delay_minutes)
                if region:
                    where_parts.append("region = %s")
                    params.append(region.upper())
                if task_type:
                    where_parts.append("JSON_UNQUOTE(JSON_EXTRACT(params, '$.task_type')) = %s")
                    params.append(task_type)
                self._append_single_source_filter(
                    where_parts, params, source_filter, STRESS_TEST_LABEL
                )

                select_sql = f"""
                    SELECT id, task_id, asin, region, priority, need_crawler_time, params, retry_count
                    FROM crawl_single_tasks
                    WHERE {' AND '.join(where_parts)}
                    ORDER BY priority ASC, need_crawler_time ASC, id ASC
                    LIMIT %s
                    FOR UPDATE
                """
                params.append(limit)
                self.cursor.execute(select_sql, tuple(params))
                tasks = self.cursor.fetchall()

                if tasks:
                    ids = [item["id"] for item in tasks]
                    placeholders = ",".join(["%s"] * len(ids))
                    update_sql = f"""
                        UPDATE crawl_single_tasks
                        SET status = 1, worker_name = %s, updated_at = %s
                        WHERE id IN ({placeholders})
                    """
                    self.cursor.execute(update_sql, (worker_name, now, *ids))
                self.conn.commit()
                return tasks
            except Exception as exc:
                if self.conn.is_connected():
                    self.conn.rollback()
                if self._is_retryable_tx_error(exc) and attempt < (max_retries - 1):
                    wait_seconds = 0.2 * (attempt + 1)
                    logger.warning(
                        f"claim_single_tasks 出现可重试事务异常(code={self._mysql_error_code(exc)}), "
                        f"第 {attempt + 1}/{max_retries} 次重试, wait={wait_seconds:.1f}s, err={exc}"
                    )
                    time.sleep(wait_seconds)
                    continue
                raise

    def claim_single_task_by_id(self, row_id: int, region: str = None,
                                source: str = None,
                                worker_name: str = "") -> Optional[Dict]:
        """
        BLPOP 路径：按 MySQL 主键 id 拉取一条 single 任务并原子标记 status=1。
        Redis 新队列值为 id:asin，不再依赖所有表都有 task_id 字段。
        need_crawler_time 只作为排序字段，不再作为可执行门槛。
        """
        from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import STRESS_TEST_LABEL
        source_filter = self._normalize_single_task_source(source)
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if self.conn.in_transaction:
                self.conn.rollback()
            self.conn.start_transaction()

            where_parts = [
                "id = %s",
                "status = 0",
            ]
            params = [int(row_id)]
            if region:
                where_parts.append("region = %s")
                params.append(region.upper())
            self._append_single_source_filter(
                where_parts, params, source_filter, STRESS_TEST_LABEL
            )

            self.cursor.execute(
                f"SELECT id, task_id, asin, region, priority, need_crawler_time, params, retry_count "
                f"FROM crawl_single_tasks WHERE {' AND '.join(where_parts)} FOR UPDATE",
                params,
            )
            row = self.cursor.fetchone()
            if not row:
                self.conn.commit()
                return None

            self.cursor.execute(
                "UPDATE crawl_single_tasks SET status=1, worker_name=%s, updated_at=%s "
                "WHERE id=%s AND status=0",
                (worker_name, now, row["id"]),
            )
            affected = self.cursor.rowcount
            self.conn.commit()
            return row if affected else None
        except Exception:
            if self.conn.is_connected():
                self.conn.rollback()
            raise

    def claim_single_task_by_task_id(self, task_id: str, region: str = None,
                                     source: str = None,
                                     worker_name: str = "") -> Optional[Dict]:
        """
        BLPOP 路径：按 task_id 拉取一条 single 任务并原子标记 status=1。
        过滤同 claim_single_tasks（source 过滤）；need_crawler_time 不再作为可执行门槛。

        返回单条 dict（含 id/task_id/asin/region/need_crawler_time/params/retry_count）
        或 None（任务已被其他 worker 拿走 / 已超时 / 已完成）。
        """
        from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import STRESS_TEST_LABEL
        source_filter = self._normalize_single_task_source(source)
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if self.conn.in_transaction:
                self.conn.rollback()
            self.conn.start_transaction()

            where_parts = [
                "task_id = %s",
                "status = 0",
            ]
            params = [task_id]
            if region:
                where_parts.append("region = %s")
                params.append(region.upper())
            self._append_single_source_filter(
                where_parts, params, source_filter, STRESS_TEST_LABEL
            )

            self.cursor.execute(
                f"SELECT id, task_id, asin, region, priority, need_crawler_time, params, retry_count "
                f"FROM crawl_single_tasks WHERE {' AND '.join(where_parts)} FOR UPDATE",
                params,
            )
            row = self.cursor.fetchone()
            if not row:
                self.conn.commit()
                return None

            self.cursor.execute(
                "UPDATE crawl_single_tasks SET status=1, worker_name=%s, updated_at=%s "
                "WHERE id=%s AND status=0",
                (worker_name, now, row["id"]),
            )
            affected = self.cursor.rowcount
            self.conn.commit()
            return row if affected else None
        except Exception:
            if self.conn.is_connected():
                self.conn.rollback()
            raise

    def claim_temp_task_by_id(self, row_id: int) -> Optional[Dict]:
        """BLPOP 路径：按主键 id 拉取一条 temp 任务并原子标记 status=1。"""
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if self.conn.in_transaction:
                self.conn.rollback()
            self.conn.start_transaction()
            self.cursor.execute(
                "SELECT id, asin, country, query_conditions, max_pages, batch_no "
                "FROM crawler_asin_tasks_temp WHERE id=%s AND status=0 FOR UPDATE",
                (row_id,),
            )
            row = self.cursor.fetchone()
            if not row:
                self.conn.commit()
                return None
            self.cursor.execute(
                "UPDATE crawler_asin_tasks_temp SET status=1, update_time=%s "
                "WHERE id=%s AND status=0",
                (now, row_id),
            )
            affected = self.cursor.rowcount
            self.conn.commit()
            return row if affected else None
        except Exception:
            if self.conn.is_connected():
                self.conn.rollback()
            raise

    def claim_asin_task_by_id(self, row_id: int, region: str = None) -> Optional[Dict]:
        """BLPOP 路径：按 MySQL 主键 id 拉取一条 asin 详情任务并原子标记 status=1。"""
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if self.conn.in_transaction:
                self.conn.rollback()
            self.conn.start_transaction()
            where = "id=%s AND status=0 AND need_crawler_time <= NOW()"
            params = [int(row_id)]
            if region:
                where += " AND region=%s"
                params.append(region.upper())
            self.cursor.execute(
                "SELECT id, task_id, asin, region, priority, need_crawler_time, retry_count "
                "FROM crawl_asin_detail_tasks "
                f"WHERE {where} FOR UPDATE",
                tuple(params),
            )
            row = self.cursor.fetchone()
            if not row:
                self.conn.commit()
                return None
            self.cursor.execute(
                "UPDATE crawl_asin_detail_tasks SET status=1, updated_at=%s "
                "WHERE id=%s AND status=0",
                (now, row["id"]),
            )
            affected = self.cursor.rowcount
            self.conn.commit()
            return row if affected else None
        except Exception:
            if self.conn.is_connected():
                self.conn.rollback()
            raise

    def claim_asin_task_by_task_id(self, task_id: str, region: str = None) -> Optional[Dict]:
        """BLPOP 路径：按 task_id 拉取一条 asin 详情任务并原子标记 status=1。"""
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if self.conn.in_transaction:
                self.conn.rollback()
            self.conn.start_transaction()
            where = "task_id=%s AND status=0 AND need_crawler_time <= NOW()"
            params = [task_id]
            if region:
                where += " AND region=%s"
                params.append(region.upper())
            self.cursor.execute(
                "SELECT id, task_id, asin, region, priority, need_crawler_time, retry_count "
                "FROM crawl_asin_detail_tasks "
                f"WHERE {where} FOR UPDATE",
                tuple(params),
            )
            row = self.cursor.fetchone()
            if not row:
                self.conn.commit()
                return None
            self.cursor.execute(
                "UPDATE crawl_asin_detail_tasks SET status=1, updated_at=%s "
                "WHERE id=%s AND status=0",
                (now, row["id"]),
            )
            affected = self.cursor.rowcount
            self.conn.commit()
            return row if affected else None
        except Exception:
            if self.conn.is_connected():
                self.conn.rollback()
            raise

    def reset_timeout_tasks(self, timeout_minutes: int = 30, table_name: str = "crawl_single_tasks") -> int:
        """
        重置超时任务：status=1(执行中)或status=3(失败)且超过timeout_minutes未更新的任务，重置为status=0
        返回重置的任务数量
        """
        self._check_connection()
        now = datetime.now()
        timeout_time = (now - timedelta(minutes=timeout_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")

        try:
            # 如果已有事务在进行中，先提交或回滚
            if self.conn.in_transaction:
                try:
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
            
            self.conn.start_transaction()
            # 根据表名确定时间字段名和是否有 error_msg
            time_field = "update_time" if table_name in ("asin_tasks", "crawler_asin_tasks_temp") else "updated_at"
            has_error_msg = table_name not in ("asin_tasks", "crawler_asin_tasks_temp")  # 只有新表有 error_msg
            
            # 先查询有多少超时的任务
            select_sql = f"""
                SELECT id, status, {time_field}
                FROM `{table_name}`
                WHERE status IN (1, 3)
                  AND {time_field} < %s
                FOR UPDATE
            """
            self.cursor.execute(select_sql, (timeout_time,))
            timeout_tasks = self.cursor.fetchall()

            if not timeout_tasks:
                self.conn.commit()
                return 0

            # 重置为status=0
            ids = [t["id"] for t in timeout_tasks]
            placeholders = ",".join(["%s"] * len(ids))
            
            # 根据表结构构造不同的 UPDATE SQL
            if has_error_msg:
                update_sql = f"""
                    UPDATE `{table_name}`
                    SET status = 0, {time_field} = %s, error_msg = CONCAT(IFNULL(error_msg, ''), ' [超时重置]')
                    WHERE id IN ({placeholders})
                """
            else:
                update_sql = f"""
                    UPDATE `{table_name}`
                    SET status = 0, {time_field} = %s
                    WHERE id IN ({placeholders})
                """
            self.cursor.execute(update_sql, (current_time_str, *ids))
            self.conn.commit()

            count = len(timeout_tasks)
            # for t in timeout_tasks:
            #     logger.warning(f"任务超时重置: id={t['id']}, 原status={t['status']}, 上次更新={t[time_field]}")
            logger.info(f"共重置 {count} 个超时任务")
            return count

        except Exception as e:
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"reset_timeout_tasks 异常: {str(e)}")
            raise

    def update_single_task_result(self, task_id: str, success: bool,
                                  result_count: int = 0, error_msg: str = "",
                                  result_data: Any = None,
                                  force_final: bool = False,
                                  expected_row_id: int = None,
                                  expected_worker_name: str = ""):
        """更新单任务执行结果（按 task_id 字符串匹配），result_data 直接存 MySQL"""
        def _alert_final_failure(err_text: str):
            try:
                from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message
                send_custom_robot_group_message(
                    msg=(
                        f"update_single_task_result 最终失败 | "
                        f"task_id={task_id} success={success} result_count={result_count} "
                        f"error={err_text[:500]}"
                    ),
                    at_mobiles=['17398238551']
                )
            except Exception:
                logger.error("update_single_task_result 告警发送失败")

        self._check_connection()
        status = 2 if success else 3
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result_json = json.dumps(result_data, ensure_ascii=False) if result_data is not None else None
        max_attempts = 4
        for attempt in range(max_attempts):
            try:
                self.conn.start_transaction()
                self.cursor.execute(
                    "SELECT id, status, retry_count, params, worker_name FROM crawl_single_tasks WHERE task_id=%s FOR UPDATE",
                    (task_id,)
                )
                row = self.cursor.fetchone()
                if not row:
                    self.conn.rollback()
                    return {}

                current_status = int(row.get("status", 0))
                if expected_row_id is not None and int(row.get("id") or 0) != int(expected_row_id):
                    self.conn.rollback()
                    return {"status": current_status, "skipped": True, "reason": "row_id_mismatch"}
                if expected_worker_name:
                    current_worker = str(row.get("worker_name") or "")
                    if current_status != 1 or current_worker != str(expected_worker_name):
                        self.conn.rollback()
                        return {
                            "status": current_status,
                            "skipped": True,
                            "reason": "worker_mismatch",
                            "worker_name": current_worker,
                        }
                # 已成功任务不允许再被失败回写降级
                if current_status == 2 and not success:
                    self.conn.rollback()
                    return {"status": 2, "skipped": True}

                params = row.get("params") or {}
                if isinstance(params, str):
                    params = json.loads(params)
                retry_times = int(params.get("retry_times", 2))

                if success:
                    self.cursor.execute(
                        "UPDATE crawl_single_tasks SET status=%s, result_count=%s, result=%s, error_msg=%s, updated_at=%s WHERE task_id=%s",
                        (status, result_count, result_json, error_msg[:1000], now, task_id)
                    )
                    new_status = status
                    new_retry_count = int(row.get("retry_count") or 0)
                else:
                    next_retry = int(row["retry_count"]) + 1
                    if force_final:
                        self.cursor.execute(
                            "UPDATE crawl_single_tasks SET status=%s, retry_count=%s, error_msg=%s, updated_at=%s WHERE task_id=%s AND status<>2",
                            (status, next_retry, error_msg[:1000], now, task_id)
                        )
                        new_status = status
                    elif (
                        next_retry <= retry_times
                        or '无可用账号，退回队列' in error_msg
                        or '账号调度锁忙' in error_msg
                    ):
                        self.cursor.execute(
                            "UPDATE crawl_single_tasks SET status=0, retry_count=%s, error_msg=%s, updated_at=%s WHERE task_id=%s AND status<>2",
                            (next_retry, error_msg[:1000], now, task_id)
                        )
                        new_status = 0
                    else:
                        self.cursor.execute(
                            "UPDATE crawl_single_tasks SET status=%s, retry_count=%s, error_msg=%s, updated_at=%s WHERE task_id=%s AND status<>2",
                            (status, next_retry, error_msg[:1000], now, task_id)
                        )
                        new_status = status
                    new_retry_count = next_retry
                self.conn.commit()
                return {
                    "status": new_status,
                    "retry_count": new_retry_count,
                    "retry_times": retry_times,
                }
            except mysql.connector.Error as e:
                if self.conn.is_connected():
                    self.conn.rollback()

                err_no = getattr(e, "errno", None)
                is_retryable = err_no in (1213, 1205)
                if is_retryable and attempt < max_attempts - 1:
                    wait_s = 0.2 * (attempt + 1)
                    logger.warning(
                        f"update_single_task_result 重试: task_id={task_id}, errno={err_no}, attempt={attempt + 1}/{max_attempts}, wait={wait_s:.1f}s"
                    )
                    time.sleep(wait_s)
                    continue
                _alert_final_failure(f"mysql_error errno={err_no}, msg={str(e)}")
                raise
            except Exception:
                if self.conn.is_connected():
                    self.conn.rollback()
                if attempt >= max_attempts - 1:
                    _alert_final_failure("unexpected_error")
                raise

    def reset_single_tasks_by_ids(self, row_ids: List[int], error_msg: str = "") -> int:
        """将执行中的 single 评论任务退回待执行。用于进程中断兜底。"""
        ids = [int(i) for i in row_ids if i is not None]
        if not ids:
            return 0
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ",".join(["%s"] * len(ids))
        try:
            self.cursor.execute(
                f"UPDATE crawl_single_tasks "
                f"SET status=0, error_msg=%s, updated_at=%s "
                f"WHERE status=1 AND id IN ({placeholders})",
                tuple([(error_msg or "worker interrupted")[:1000], now, *ids]),
            )
            affected = int(self.cursor.rowcount or 0)
            self.conn.commit()
            return affected
        except Exception as e:
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"[mysql] reset_single_tasks_by_ids 失败: {e}")
            return 0

    def fail_or_retry_single_task_by_id(
            self,
            row_id: int,
            error_msg: str = "",
            *,
            force_final: bool = False,
            only_running: bool = True,
    ) -> Dict:
        """运行中 single 任务异常中断时，按 retry_times 累加 retry_count 并决定重试或最终失败。"""
        if row_id is None:
            return {}
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if self.conn.in_transaction:
                self.conn.rollback()
            self.conn.start_transaction()
            where_sql = "id=%s"
            params = [int(row_id)]
            if only_running:
                where_sql += " AND status=1"
            self.cursor.execute(
                f"SELECT id, task_id, status, retry_count, params FROM crawl_single_tasks WHERE {where_sql} FOR UPDATE",
                tuple(params),
            )
            row = self.cursor.fetchone()
            if not row:
                self.conn.rollback()
                return {}

            current_status = int(row.get("status") or 0)
            if current_status == 2:
                self.conn.rollback()
                return {"status": 2, "skipped": True}

            task_params = row.get("params") or {}
            if isinstance(task_params, str):
                try:
                    task_params = json.loads(task_params)
                except Exception:
                    task_params = {}
            retry_times = int(task_params.get("retry_times", 2))
            next_retry = int(row.get("retry_count") or 0) + 1
            new_status = 3 if force_final or next_retry > retry_times else 0

            self.cursor.execute(
                "UPDATE crawl_single_tasks "
                "SET status=%s, retry_count=%s, error_msg=%s, worker_name=NULL, updated_at=%s "
                "WHERE id=%s AND status<>2",
                (new_status, next_retry, (error_msg or "worker interrupted")[:1000], now, int(row_id)),
            )
            self.conn.commit()
            return {
                "status": new_status,
                "retry_count": next_retry,
                "retry_times": retry_times,
                "task_id": row.get("task_id"),
            }
        except Exception as e:
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"[mysql] fail_or_retry_single_task_by_id 失败 row_id={row_id}: {e}")
            return {}

    def reset_temp_tasks_by_ids(self, row_ids: List[int], note: str = "") -> int:
        """将执行中的 temp 评论任务退回待执行。用于进程中断兜底。"""
        ids = [int(i) for i in row_ids if i is not None]
        if not ids:
            return 0
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ",".join(["%s"] * len(ids))
        try:
            self.cursor.execute(
                f"UPDATE crawler_asin_tasks_temp "
                f"SET status=0, update_time=%s "
                f"WHERE status=1 AND id IN ({placeholders})",
                tuple([now, *ids]),
            )
            affected = int(self.cursor.rowcount or 0)
            self.conn.commit()
            return affected
        except Exception as e:
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"[mysql] reset_temp_tasks_by_ids 失败: {e}")
            return 0

    def get_single_task_detail(self, task_id: str) -> Dict:
        """按 task_id 查询单任务详情"""
        self._check_connection()
        self.cursor.execute("SELECT * FROM crawl_single_tasks WHERE task_id=%s", (task_id,))
        return self.cursor.fetchone() or {}

    def list_single_tasks(self, limit: int = 20, region: str = None, status: int = None) -> List[Dict]:
        """查询单任务列表，支持按 region 和 status 过滤（不返回 result 大字段，避免列表查询过重）"""
        self._check_connection()
        where_parts = []
        params = []
        if region:
            where_parts.append("region = %s")
            params.append(region.upper())
        if status is not None:
            where_parts.append("status = %s")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        sql = f"""
            SELECT id, task_id, asin, region, priority, need_crawler_time, params, status,
                   retry_count, worker_name, error_msg, result_count, created_at, updated_at
            FROM crawl_single_tasks
            {where_sql}
            ORDER BY need_crawler_time ASC
            LIMIT %s
        """
        params.append(limit)
        self.cursor.execute(sql, tuple(params))
        return self.cursor.fetchall() or []

    def get_single_task_result(self, task_id: str) -> Dict:
        """按 task_id 查询单任务状态 + 结果数据（专用于结果回传接口）"""
        self._check_connection()
        self.cursor.execute(
            "SELECT task_id, asin, region, priority, status, result_count, result, error_msg, "
            "callback_url, oss_object_key, oss_result_url, callback_status, callback_attempts, "
            "callback_last_error, callback_updated_at, updated_at "
            "FROM crawl_single_tasks WHERE task_id=%s",
            (task_id,)
        )
        row = self.cursor.fetchone()
        if not row:
            return {}
        result_raw = row.get("result")
        if result_raw and isinstance(result_raw, str):
            try:
                row["result"] = json.loads(result_raw)
            except (json.JSONDecodeError, TypeError):
                pass
        return row

    def update_single_task_callback_state(
            self,
            task_id: str,
            callback_url: str = None,
            oss_object_key: str = None,
            oss_result_url: str = None,
            callback_status: int = None,
            callback_last_error: str = "",
            increment_attempts: bool = False,
    ) -> int:
        """更新 single 任务 callback/OSS 状态。"""
        self._check_connection()
        set_parts = ["callback_updated_at=%s"]
        params: List[Any] = [datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
        if callback_url is not None:
            set_parts.append("callback_url=%s")
            params.append(str(callback_url or "")[:1024])
        if oss_object_key is not None:
            set_parts.append("oss_object_key=%s")
            params.append(str(oss_object_key or "")[:512])
        if oss_result_url is not None:
            set_parts.append("oss_result_url=%s")
            params.append(str(oss_result_url or ""))
        if callback_status is not None:
            set_parts.append("callback_status=%s")
            params.append(int(callback_status))
        if callback_last_error is not None:
            set_parts.append("callback_last_error=%s")
            params.append(str(callback_last_error or "")[:2000])
        if increment_attempts:
            set_parts.append("callback_attempts=callback_attempts+1")
        params.append(task_id)
        self.cursor.execute(
            f"UPDATE crawl_single_tasks SET {', '.join(set_parts)} WHERE task_id=%s",
            tuple(params),
        )
        affected = int(self.cursor.rowcount or 0)
        self.conn.commit()
        return affected

    def list_retryable_single_callbacks(
            self,
            limit: int = 50,
            max_attempts: int = 5,
            min_retry_interval_seconds: int = 300,
    ) -> List[Dict]:
        """拉取已终态但 callback 未成功的 single 任务，用于 daemon 补发。"""
        self._check_connection()
        safe_limit = min(max(int(limit or 1), 1), 500)
        safe_attempts = max(int(max_attempts or 1), 1)
        retry_interval = max(int(min_retry_interval_seconds or 0), 0)
        sql = """
            SELECT task_id, asin, region, priority, need_crawler_time, params, status, result_count,
                   result, error_msg, callback_url, oss_object_key, oss_result_url,
                   callback_status, callback_attempts, callback_last_error, callback_updated_at, updated_at
            FROM crawl_single_tasks
            WHERE status IN (2, 3)
              AND callback_url <> ''
              AND callback_status IN (0, 2)
              AND callback_attempts < %s
              AND (
                    (callback_updated_at IS NULL AND updated_at <= DATE_SUB(NOW(), INTERVAL %s SECOND))
                    OR callback_updated_at <= DATE_SUB(NOW(), INTERVAL %s SECOND)
              )
            ORDER BY callback_updated_at IS NULL DESC, callback_updated_at ASC, updated_at ASC
            LIMIT %s
        """
        self.cursor.execute(sql, (safe_attempts, retry_interval, retry_interval, safe_limit))
        rows = self.cursor.fetchall() or []
        for row in rows:
            for col in ("result", "params"):
                raw = row.get(col)
                if raw and isinstance(raw, str):
                    try:
                        row[col] = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        row[col] = [] if col == "result" else {}
        return rows

    # --------------------------------------------------
    # 账号管理（crawler_accounts）
    # --------------------------------------------------
    def load_all_accounts(self, filter_conditions: Dict = None) -> List[Dict]:
        """
        加载账号列表，支持按 state/country 等条件筛选。
        返回 dict 列表，由调用方转为 Account 对象。

        label 隔离逻辑（通过 filter_conditions 传入）：
          - filter_conditions 包含 'label' key → 只返回该 label 的账号（压测 worker 专用）
          - filter_conditions 不包含 'label' key → 自动排除 label='stress_test' 的账号（生产 worker 保护）

        platform 隔离：未显式传入 platform 时，强制按 'amazon' 过滤，防止跨平台串台。
        """
        self._check_connection()
        where_parts = ["state = 1"]
        params = []
        has_label_filter = False
        has_platform_filter = False
        if filter_conditions:
            for key, val in filter_conditions.items():
                if key == "label":
                    has_label_filter = True
                if key == "platform":
                    has_platform_filter = True
                where_parts.append(f"`{key}` = %s")
                params.append(val)
        # 平台隔离：调用方未指定 platform 时默认只查 amazon
        if not has_platform_filter:
            where_parts.append("`platform` = %s")
            params.append("amazon")
        # 生产 worker 自动排除压测账号（只有在没有明确 label 过滤时才加此排除条件）
        if not has_label_filter:
            from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import STRESS_TEST_LABEL
            where_parts.append("(label IS NULL OR label != %s)")
            params.append(STRESS_TEST_LABEL)
        sql = f"SELECT * FROM crawler_accounts WHERE {' AND '.join(where_parts)}"
        self.cursor.execute(sql, tuple(params))
        rows = self.cursor.fetchall() or []
        # JSON 列可能返回字符串，统一解析
        for row in rows:
            for col in ("cookies", "proxy_"):
                val = row.get(col)
                if isinstance(val, str):
                    try:
                        row[col] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        row[col] = {}
            # is_used: tinyint → bool
            row["is_used"] = bool(row.get("is_used", 0))
        return rows

    def release_timeout_accounts_by_filter(
            self,
            filter_conditions: Dict = None,
            timeout_threshold_ts: float = 0.0,
    ) -> int:
        """批量释放匹配条件下已超过占用超时阈值的账号。"""
        self._check_connection()
        where_parts = [
            "state = 1",
            "is_used = 1",
            "last_used_time > 0",
            "last_used_time < %s",
        ]
        params = [float(timeout_threshold_ts or 0.0)]
        has_label_filter = False
        has_platform_filter = False
        if filter_conditions:
            for key, val in filter_conditions.items():
                if key == "label":
                    has_label_filter = True
                if key == "platform":
                    has_platform_filter = True
                where_parts.append(f"`{key}` = %s")
                params.append(val)
        if not has_platform_filter:
            where_parts.append("`platform` = %s")
            params.append("amazon")
        if not has_label_filter:
            from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import STRESS_TEST_LABEL
            where_parts.append("(label IS NULL OR label != %s)")
            params.append(STRESS_TEST_LABEL)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = f"""
            UPDATE crawler_accounts
            SET is_used = 0, cooldown_until = 0, update_time = %s
            WHERE {' AND '.join(where_parts)}
        """
        self.cursor.execute(sql, (now, *params))
        affected = int(self.cursor.rowcount or 0)
        self.conn.commit()
        return affected

    def load_available_account_candidates(
            self,
            filter_conditions: Dict = None,
            now_ts: float = None,
            limit: int = 0,
    ) -> List[Dict]:
        """加载可参与调度的账号候选，按最久未使用排序；limit<=0 表示不限制。"""
        self._check_connection()
        now_ts = float(now_ts if now_ts is not None else time.time())
        limit = max(0, int(limit or 0))
        where_parts = [
            "state = 1",
            "is_used = 0",
            "(cooldown_until IS NULL OR cooldown_until <= %s)",
        ]
        params = [now_ts]
        has_label_filter = False
        has_platform_filter = False
        if filter_conditions:
            for key, val in filter_conditions.items():
                if key == "label":
                    has_label_filter = True
                if key == "platform":
                    has_platform_filter = True
                where_parts.append(f"`{key}` = %s")
                params.append(val)
        if not has_platform_filter:
            where_parts.append("`platform` = %s")
            params.append("amazon")
        if not has_label_filter:
            from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import STRESS_TEST_LABEL
            where_parts.append("(label IS NULL OR label != %s)")
            params.append(STRESS_TEST_LABEL)

        limit_sql = "LIMIT %s" if limit > 0 else ""
        sql = f"""
            SELECT *
            FROM crawler_accounts
            WHERE {' AND '.join(where_parts)}
            ORDER BY last_used_time ASC, username ASC
            {limit_sql}
        """
        execute_params = (*params, limit) if limit > 0 else tuple(params)
        self.cursor.execute(sql, execute_params)
        rows = self.cursor.fetchall() or []
        for row in rows:
            for col in ("cookies", "proxy_"):
                val = row.get(col)
                if isinstance(val, str):
                    try:
                        row[col] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        row[col] = {}
            row["is_used"] = bool(row.get("is_used", 0))
        return rows

    def count_accounts_by_filter(self, filter_conditions: Dict = None, active_only: bool = False) -> int:
        """
        统计账号表里是否存在某类账号。

        active_only=False 时不按 state 过滤，用于区分：
          - 账号库根本没有该 country/platform/label 的账号
          - 有账号但当前不可用（占用、冷却、预算耗尽、停用等）
        """
        self._check_connection()
        where_parts = []
        params = []
        has_label_filter = False
        has_platform_filter = False

        if active_only:
            where_parts.append("state = 1")

        if filter_conditions:
            for key, val in filter_conditions.items():
                if key == "label":
                    has_label_filter = True
                if key == "platform":
                    has_platform_filter = True
                where_parts.append(f"`{key}` = %s")
                params.append(val)

        if not has_platform_filter:
            where_parts.append("`platform` = %s")
            params.append("amazon")

        if not has_label_filter:
            from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import STRESS_TEST_LABEL
            where_parts.append("(label IS NULL OR label != %s)")
            params.append(STRESS_TEST_LABEL)

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        self.cursor.execute(f"SELECT COUNT(*) AS cnt FROM crawler_accounts {where_sql}", tuple(params))
        row = self.cursor.fetchone() or {}
        return int(row.get("cnt") or 0)

    def update_account(self, account_dict: Dict):
        """按 (platform, username) 更新账号状态（传入 Account.to_dict() 的结果）。
        account_dict 未携带 platform 时，默认按 'amazon' 更新。"""
        self._check_connection()
        username = account_dict.get("username")
        if not username:
            return
        platform = account_dict.get("platform") or "amazon"
        set_parts = []
        params = []
        account_dict['update_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for key, val in account_dict.items():
            if key in ("id", "username", "platform"):
                continue
            if key in ("cookies", "proxy_") and isinstance(val, dict):
                val = json.dumps(val, ensure_ascii=False)
            set_parts.append(f"`{key}` = %s")
            params.append(val)
        if not set_parts:
            return
        params.append(username)
        params.append(platform)
        sql = f"UPDATE crawler_accounts SET {', '.join(set_parts)} WHERE username = %s AND platform = %s"
        self.cursor.execute(sql, tuple(params))
        self.conn.commit()


    def insert_account(self, account_dict: Dict):
        """插入单个账号（用于 account_add 入库），username 重复则更新"""
        self._check_connection()
        cols = []
        placeholders = []
        params = []
        for key, val in account_dict.items():
            if key == "id" or key == "_id":
                continue
            cols.append(f"`{key}`")
            if key in ("cookies", "proxy_") and isinstance(val, dict):
                placeholders.append("CAST(%s AS JSON)")
                params.append(json.dumps(val, ensure_ascii=False))
            else:
                placeholders.append("%s")
                params.append(val)

        # ON DUPLICATE KEY UPDATE 部分：更新所有字段（除了 username 本身）
        update_parts = []
        for key in cols:
            col = key.strip('`')
            if col != "username":  # 更新时不需要重复设置 username
                update_parts.append(f"{key} = VALUES({key})")

        sql = f"""INSERT INTO crawler_accounts ({', '.join(cols)}) 
                      VALUES ({', '.join(placeholders)})
                      ON DUPLICATE KEY UPDATE {', '.join(update_parts)}"""
        self.cursor.execute(sql, tuple(params))
        self.conn.commit()

    def insert_accounts_batch(self, accounts: List[Dict]):
        """批量插入账号"""
        for acc in accounts:
            self.insert_account(acc)

    def get_account_by_username(self, username: str, platform: str = "amazon") -> Optional[Dict]:
        """按 (platform, username) 查询单个账号。未显式传 platform 时默认 'amazon'。"""
        self._check_connection()
        sql = """
            SELECT * FROM crawler_accounts
            WHERE username = %s AND platform = %s
            LIMIT 1
        """
        self.cursor.execute(sql, (username, platform))
        row = self.cursor.fetchone()
        if row:
            row["is_used"] = bool(row.get("is_used", 0))
            return row
        return None

    def release_account_by_username(self, username: str, platform: str = "amazon", note: str = "") -> int:
        """进程外兜底释放账号占用状态。"""
        if not username:
            return 0
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cursor.execute(
            """
            UPDATE crawler_accounts
            SET is_used=0, update_time=%s
            WHERE username=%s AND platform=%s
            """,
            (now, username, platform or "amazon"),
        )
        affected = int(self.cursor.rowcount or 0)
        self.conn.commit()
        if affected:
            logger.warning(f"[mysql] 已释放账号 username={username} platform={platform} note={note}")
        return affected

    # --------------------------------------------------
    # 异常评论数据存储（reviews_error）
    # --------------------------------------------------
    def insert_reviews_error(self, asin: str, country: str, resp: str,
                             review_data: Dict = None, task_info: Dict = None,
                             error_msg: str = ""):
        """插入解析异常的评论数据"""
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        review_data_json = json.dumps(review_data or {}, ensure_ascii=False)
        task_info_json = json.dumps(task_info or {}, ensure_ascii=False)
        sql = """
            INSERT INTO reviews_error
            (asin, country, resp, review_data, task_info, error_msg, status, created_at, updated_at)
            VALUES (%s, %s, %s, CAST(%s AS JSON), CAST(%s AS JSON), %s, 0, %s, %s)
        """
        self.cursor.execute(sql, (asin, country, resp, review_data_json, task_info_json,
                                  error_msg[:512], now, now))
        self.conn.commit()

    def get_pending_errors(self, limit: int = 100) -> List[Dict]:
        """获取待处理的异常数据（status=0）"""
        self._check_connection()
        sql = """
            SELECT id, asin, country, resp, review_data, task_info, error_msg, created_at
            FROM reviews_error
            WHERE status = 0
            ORDER BY id ASC
            LIMIT %s
        """
        self.cursor.execute(sql, (limit,))
        rows = self.cursor.fetchall() or []
        # JSON 解析
        for row in rows:
            for col in ("review_data", "task_info"):
                val = row.get(col)
                if isinstance(val, str):
                    try:
                        row[col] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        row[col] = {}
        return rows

    def mark_error_processed(self, error_id: int):
        """标记异常数据为已处理"""
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = "UPDATE reviews_error SET status = 1, updated_at = %s WHERE id = %s"
        self.cursor.execute(sql, (now, error_id))
        self.conn.commit()

    def delete_error(self, error_id: int):
        """删除已处理的异常数据（del_error_data 用）"""
        self._check_connection()
        sql = "DELETE FROM reviews_error WHERE id = %s"
        self.cursor.execute(sql, (error_id,))
        self.conn.commit()

    def create_task_with_subtasks(self, task_no: str, task_type: str, subtasks: List[Dict]) -> Dict:
        """创建主任务及其子任务（事务）"""
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        query_json = json.dumps({}, ensure_ascii=False)

        try:
            self.conn.start_transaction()
            insert_parent_sql = """
                INSERT INTO crawl_tasks
                (task_no, task_type, query_conditions, status, total_subtasks, running_subtasks, success_subtasks, failed_subtasks, created_at, updated_at)
                VALUES (%s, %s, CAST(%s AS JSON), %s, %s, 0, 0, 0, %s, %s)
            """
            self.cursor.execute(
                insert_parent_sql,
                (task_no, task_type, query_json, 0, len(subtasks), now, now)
            )
            task_id = self.cursor.lastrowid

            insert_child_sql = """
                INSERT INTO crawl_subtasks
                (task_id, asin, country, max_pages, task_type, query_conditions, status, retry_times, retry_count, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, CAST(%s AS JSON), %s, %s, 0, %s, %s)
            """
            child_rows = []
            for subtask in subtasks:
                child_query_json = json.dumps(subtask.get("query_conditions") or {}, ensure_ascii=False)
                child_rows.append((
                    task_id,
                    subtask["asin"],
                    subtask["country"].upper(),
                    int(subtask.get("max_pages", 3)),
                    subtask.get("task_type", task_type),
                    child_query_json,
                    0,
                    int(subtask.get("retry_times", 2)),
                    now,
                    now,
                ))
            self.cursor.executemany(insert_child_sql, child_rows)
            self.conn.commit()
            return {"task_id": task_id, "task_no": task_no, "total_subtasks": len(subtasks)}
        except Exception:
            if self.conn.is_connected():
                self.conn.rollback()
            raise

    def claim_subtasks(self, limit: int = 1, country: str = None, task_type: str = None, worker_name: str = "") -> List[Dict]:
        """拉取并锁定待执行子任务（status: 0 -> 1）"""
        self._check_connection()
        try:
            self.conn.start_transaction()
            where_parts = ["status = 0"]
            params = []
            if country:
                where_parts.append("country = %s")
                params.append(country.upper())
            if task_type:
                where_parts.append("task_type = %s")
                params.append(task_type)

            select_sql = f"""
                SELECT id, task_id, asin, country, max_pages, task_type, query_conditions, retry_times, retry_count
                FROM crawl_subtasks
                WHERE {' AND '.join(where_parts)}
                ORDER BY id ASC
                LIMIT %s
                FOR UPDATE
            """
            params.append(limit)
            self.cursor.execute(select_sql, tuple(params))
            tasks = self.cursor.fetchall()

            if tasks:
                ids = [item["id"] for item in tasks]
                placeholders = ",".join(["%s"] * len(ids))
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                update_sql = f"""
                    UPDATE crawl_subtasks
                    SET status = 1, worker_name = %s, updated_at = %s
                    WHERE id IN ({placeholders})
                """
                self.cursor.execute(update_sql, (worker_name, now, *ids))
            self.conn.commit()
            return tasks
        except Exception:
            if self.conn.is_connected():
                self.conn.rollback()
            raise

    def update_subtask_result(self, subtask_id: int, success: bool, result_count: int = 0, error_msg: str = ""):
        """更新子任务执行结果，并刷新主任务汇总状态"""
        self._check_connection()
        status = 2 if success else 3
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.conn.start_transaction()
            self.cursor.execute("SELECT task_id, retry_count, retry_times FROM crawl_subtasks WHERE id=%s FOR UPDATE", (subtask_id,))
            row = self.cursor.fetchone()
            if not row:
                self.conn.rollback()
                return

            if success:
                self.cursor.execute(
                    """
                    UPDATE crawl_subtasks
                    SET status=%s, result_count=%s, error_msg=NULL, updated_at=%s
                    WHERE id=%s
                    """,
                    (status, result_count, now, subtask_id)
                )
            else:
                next_retry_count = int(row["retry_count"]) + 1
                if next_retry_count <= int(row["retry_times"]):
                    self.cursor.execute(
                        """
                        UPDATE crawl_subtasks
                        SET status=0, retry_count=%s, error_msg=%s, updated_at=%s
                        WHERE id=%s
                        """,
                        (next_retry_count, error_msg[:1000], now, subtask_id)
                    )
                else:
                    self.cursor.execute(
                        """
                        UPDATE crawl_subtasks
                        SET status=%s, retry_count=%s, error_msg=%s, updated_at=%s
                        WHERE id=%s
                        """,
                        (status, next_retry_count, error_msg[:1000], now, subtask_id)
                    )

            self._refresh_parent_status(row["task_id"])
            self.conn.commit()
        except Exception:
            if self.conn.is_connected():
                self.conn.rollback()
            raise

    def _refresh_parent_status(self, task_id: int):
        self.cursor.execute(
            """
            SELECT
                SUM(CASE WHEN status=1 THEN 1 ELSE 0 END) AS running_cnt,
                SUM(CASE WHEN status=2 THEN 1 ELSE 0 END) AS success_cnt,
                SUM(CASE WHEN status=3 THEN 1 ELSE 0 END) AS fail_cnt,
                COUNT(*) AS total_cnt,
                SUM(CASE WHEN status IN (0,1) THEN 1 ELSE 0 END) AS unfinished_cnt
            FROM crawl_subtasks
            WHERE task_id=%s
            """,
            (task_id,)
        )
        stat = self.cursor.fetchone() or {}
        running_cnt = int(stat.get("running_cnt") or 0)
        success_cnt = int(stat.get("success_cnt") or 0)
        fail_cnt = int(stat.get("fail_cnt") or 0)
        total_cnt = int(stat.get("total_cnt") or 0)
        unfinished_cnt = int(stat.get("unfinished_cnt") or 0)
        if unfinished_cnt > 0:
            parent_status = 1 if running_cnt > 0 else 0
        else:
            parent_status = 2 if fail_cnt == 0 else 3

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cursor.execute(
            """
            UPDATE crawl_tasks
            SET status=%s,
                total_subtasks=%s,
                running_subtasks=%s,
                success_subtasks=%s,
                failed_subtasks=%s,
                updated_at=%s
            WHERE id=%s
            """,
            (parent_status, total_cnt, running_cnt, success_cnt, fail_cnt, now, task_id)
        )

    def get_task_detail(self, task_id: int) -> Dict:
        """查询主任务详情及子任务统计"""
        self._check_connection()
        self.cursor.execute("SELECT * FROM crawl_tasks WHERE id=%s", (task_id,))
        task = self.cursor.fetchone()
        if not task:
            return {}
        self.cursor.execute(
            """
            SELECT status, COUNT(*) AS cnt
            FROM crawl_subtasks
            WHERE task_id=%s
            GROUP BY status
            """,
            (task_id,)
        )
        grouped = self.cursor.fetchall() or []
        task["subtask_status"] = {str(row["status"]): row["cnt"] for row in grouped}
        return task

    def list_tasks(self, limit: int = 20) -> List[Dict]:
        self._check_connection()
        self.cursor.execute(
            """
            SELECT * FROM crawl_tasks
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,)
        )
        return self.cursor.fetchall() or []


    def update_task_status(self, task_id: int, status: int, note: str = "",table_name = 'asin_tasks'):
        """更新任务状态：单独事务，避免影响主流程"""
        self._check_connection()
        try:
            # 如果已有事务在进行中，先提交或回滚
            if self.conn.in_transaction:
                try:
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
            
            self.conn.start_transaction()
            # 清空游标
            try:
                self.cursor.fetchall()
            except:
                pass
            current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            update_sql = f"""
                   UPDATE `{table_name}` 
                   SET status=%s, update_time=%s 
                   WHERE id=%s
               """
            self.cursor.execute(
                update_sql,
                (status, current_time_str,  task_id)
            )
            self.conn.commit()
            logger.info(f"任务 {task_id} 状态更新为 {status}")

        except Error as e:
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"更新任务 {task_id} 失败: {str(e)}")
            try:
                self.cursor.fetchall()
            except:
                pass

    # --------------------------------------------------
    # 账号使用日志（account_usage_log）
    # --------------------------------------------------
    def insert_usage_log(self, task_id="", asin="", country="",
                         username="", success=False,
                         review_count=0, expected_count=0,
                         start_time="", end_time="",
                         duration_seconds=0, retry_count=0,
                         error_msg="", worker_id="",
                         ip="", task_type="review"):
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = """
            INSERT INTO account_usage_log
            (task_id, asin, country, username, success, review_count, expected_count,
             start_time, end_time, duration_seconds, retry_count, error_msg,
             worker_id, ip, task_type, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        try:
            self.cursor.execute(sql, (
                task_id, asin.upper(), country.upper(), username,
                1 if success else 0, review_count, expected_count,
                start_time or now, end_time or now, duration_seconds,
                retry_count, (error_msg or "")[:2000], worker_id,
                ip or os.getenv("HOST_IP", ""), task_type, now
            ))
            self.conn.commit()
        except Exception as e:
            if self.conn and self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"insert_usage_log error: {e}")

    def get_usage_log_stats(self, country=None, start_date=None, end_date=None):
        self._check_connection()
        cf = "AND UPPER(country) = %s"
        cp = [country.upper()] if country else []
        dc, dp = "", []
        if start_date and end_date:
            dc = "AND created_at BETWEEN %s AND %s"
            dp = [start_date, end_date + " 23:59:59"]

        def q(sql, params=()):
            self.cursor.execute(sql, tuple(params))
            return self.cursor.fetchone() or {}

        row = q(
            f"SELECT COALESCE(SUM(CEIL(GREATEST(review_count, 0) / 10)), 0) as total_requests, "
            f"COUNT(DISTINCT asin) as total_asins, "
            f"SUM(success) as ok, SUM(1-success) as fail, "
            f"SUM(review_count) as reviews, AVG(duration_seconds) as avg_dur "
            f"FROM account_usage_log WHERE 1=1 {cf if country else ''} {dc}",
            cp + dp,
        )

        used = q(f"SELECT COUNT(DISTINCT username) as v FROM account_usage_log "
                 f"WHERE success=1 {cf if country else ''} {dc}", cp + dp)
        total_reviews = int(row.get("reviews") or 0)
        accounts_used = int(used.get("v") or 1) or 1
        return {
            "total_requests": int(row.get("total_requests") or 0),
            "total_asins": int(row.get("total_asins") or 0),
            "success_requests": int(row.get("ok") or 0),
            "fail_requests": int(row.get("fail") or 0),

            "total_reviews": total_reviews,
            "avg_duration": round(float(row.get("avg_dur") or 0), 1),
            "accounts_used": int(used.get("v") or 0),
            "avg_output_per_account": round(total_reviews / accounts_used, 1),
        }

    def get_usage_log_by_account(self, country=None, start_date=None, end_date=None):
        self._check_connection()
        cf = "AND UPPER(country) = %s"
        cp = [country.upper()] if country else []
        dc, dp = "", []
        if start_date and end_date:
            dc = "AND created_at BETWEEN %s AND %s"
            dp = [start_date, end_date + " 23:59:59"]
        sql = (f"SELECT username, country, COUNT(*) as requests, SUM(success) as ok, "
               f"SUM(review_count) as reviews, AVG(duration_seconds) as avg_dur "
               f"FROM account_usage_log WHERE 1=1 {cf if country else ''} {dc} "
               f"GROUP BY username, country ORDER BY reviews DESC")
        self.cursor.execute(sql, tuple(cp + dp))
        rows = self.cursor.fetchall() or []
        result = []
        for r in rows:
            req = int(r["requests"]) or 1
            result.append({
                "username": r["username"],
                "country": r["country"],
                "requests": req,
                "success": int(r["ok"] or 0),
                "reviews": int(r["reviews"] or 0),
                "avg_duration": round(float(r["avg_dur"] or 0), 1),
                "success_rate": round(int(r["ok"] or 0) / req * 100, 1),
            })
        return result

    def close(self):
        """安全关闭连接（带重复关闭保护）"""
        if self._closed:
            return  # 已关闭，直接返回
        try:
            if self.cursor:
                self.cursor.close()
            if self.conn and self.conn.is_connected():
                self.conn.close()
            # logger.info("MySQL 连接已关闭")
        except Error as e:
            logger.error(f"关闭连接失败: {str(e)}")
        finally:
            self._closed = True  # 标记为已关闭

    # --------------------------------------------------
    # 静态IP / 商品详情任务支持
    # --------------------------------------------------

    def claim_stress_review_tasks(self, region: str, worker_name: str,
                                     limit: int = 1) -> List[Dict]:
        """
        从 crawler_asin_tasks_temp 原子拉取压测任务（FOR UPDATE），标记为执行中。
        超过 30 分钟未完成的 status=1 任务自动重置。
        """
        self._check_connection()
        # 重置超时任务
        try:
            timeout_str = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
            self.cursor.execute(
                "UPDATE crawler_asin_tasks_temp SET status=0, update_time=%s "
                "WHERE status=1 AND update_time < %s",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), timeout_str)
            )
            self.conn.commit()
        except Exception:
            pass

        try:
            self.conn.start_transaction()
            self.cursor.execute(
                "SELECT * FROM crawler_asin_tasks_temp "
                "WHERE status=0 AND country=%s "
                "ORDER BY id ASC LIMIT %s FOR UPDATE",
                (region.upper(), limit)
            )
            tasks = self.cursor.fetchall()
            if tasks:
                ids = [t["id"] for t in tasks]
                placeholders = ",".join(["%s"] * len(ids))
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.cursor.execute(
                    f"UPDATE crawler_asin_tasks_temp SET status=1, update_time=%s "
                    f"WHERE id IN ({placeholders})",
                    (now, *ids)
                )
            self.conn.commit()
            return tasks or []
        except Exception as e:
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"[mysql] claim_stress_review_tasks 失败: {e}")
            return []

    def complete_stress_review_task(self, task_id: int, success: bool):
        """更新压测任务状态：success→2, 失败→0(归还重试)"""
        self._check_connection()
        status = 2 if success else 0
        try:
            self.cursor.execute(
                "UPDATE crawler_asin_tasks_temp SET status=%s, update_time=%s WHERE id=%s",
                (status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id)
            )
            self.conn.commit()
        except Exception as e:
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"[mysql] complete_stress_review_task 失败: {e}")

    def ensure_static_ip_column(self):
        """为 crawler_accounts 添加 static_ip 列（幂等）"""
        self._check_connection()
        self.cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'crawler_accounts' "
            "AND column_name = 'static_ip'"
        )
        if self.cursor.fetchone()["cnt"] == 0:
            self.cursor.execute(
                "ALTER TABLE crawler_accounts ADD COLUMN "
                "`static_ip` VARCHAR(512) NULL DEFAULT NULL COMMENT '静态代理完整URL，如 http://user:pass@38.213.252.67:2333'"
            )
            self.conn.commit()
            logger.info("[mysql] crawler_accounts 已添加 static_ip 列")

    def get_account_static_ip(self, username: str, platform: str = "amazon") -> Optional[str]:
        """查询账号绑定的静态代理URL。未显式传 platform 时默认 'amazon'。"""
        self._check_connection()
        self.cursor.execute(
            "SELECT static_ip FROM crawler_accounts WHERE username = %s AND platform = %s LIMIT 1",
            (username, platform)
        )
        row = self.cursor.fetchone()
        return row.get("static_ip") if row else None

    def get_all_static_ips(self, country: str = None, platform: str = "amazon") -> List[Dict]:
        """返回空闲账号（is_used=0）的静态代理URL列表，含账号username。
        country 传入时按国家过滤（忽略大小写）。platform 默认 'amazon'。"""
        self._check_connection()
        if country:
            self.cursor.execute(
                "SELECT username, static_ip FROM crawler_accounts "
                "WHERE static_ip IS NOT NULL AND static_ip != '' AND state = 1 AND is_used = 0 "
                "AND platform = %s AND LOWER(country) = %s",
                (platform, country.lower())
            )
        else:
            self.cursor.execute(
                "SELECT username, static_ip FROM crawler_accounts "
                "WHERE static_ip IS NOT NULL AND static_ip != '' AND state = 1 AND is_used = 0 "
                "AND platform = %s",
                (platform,)
            )
        return self.cursor.fetchall() or []

    def ensure_asin_detail_tasks_table(self):
        """创建商品详情任务表（幂等）。若旧表 crawl_asin_tasks 存在则自动重命名。"""
        self._check_connection()
        # 旧表迁移：存在旧名且新名不存在时自动 RENAME
        if self._table_exists("crawl_asin_tasks") and not self._table_exists("crawl_asin_detail_tasks"):
            self.cursor.execute("RENAME TABLE crawl_asin_tasks TO crawl_asin_detail_tasks")
            self.conn.commit()
            logger.info("[mysql] crawl_asin_tasks → crawl_asin_detail_tasks 重命名完成")
        if not self._table_exists("crawl_asin_detail_tasks"):
            self.cursor.execute("""
                CREATE TABLE `crawl_asin_detail_tasks` (
                    `id`                BIGINT NOT NULL AUTO_INCREMENT,
                    `task_id`           VARCHAR(64) NULL DEFAULT NULL COMMENT '外部请求唯一标识',
                    `asin`              VARCHAR(20) NOT NULL,
                    `region`            VARCHAR(10) NOT NULL,
                    `priority`          INT NOT NULL DEFAULT 0,
                    `status`            TINYINT NOT NULL DEFAULT 0 COMMENT '0=pending 1=running 2=done 3=failed',
                    `retry_count`       INT NOT NULL DEFAULT 0,
                    `batch_no`          VARCHAR(64) NULL,
                    `need_crawler_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '最早执行时间',
                    `result`            JSON NULL,
                    `error_msg`         VARCHAR(512) NULL,
                    `created_at`        DATETIME NOT NULL,
                    `updated_at`        DATETIME NOT NULL,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uniq_task_id` (`task_id`),
                    KEY `idx_status_region` (`status`, `region`),
                    KEY `idx_need_crawler_time` (`need_crawler_time`),
                    KEY `idx_batch` (`batch_no`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品详情爬取任务队列'
            """)
            self.conn.commit()
            logger.info("[mysql] 创建 crawl_asin_detail_tasks 表")
        # 确保旧表迁移后也有新字段（task_id / need_crawler_time）
        self._ensure_column("crawl_asin_detail_tasks", "task_id",
                            "VARCHAR(64) NULL DEFAULT NULL COMMENT '外部请求唯一标识'",
                            unique_key="uniq_task_id")
        self._ensure_column("crawl_asin_detail_tasks", "need_crawler_time",
                            "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '最早执行时间'")

    def _ensure_column(self, table: str, column: str, definition: str,
                       unique_key: str = None):
        """为指定表添加缺失列（幂等）"""
        self.cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
            (table, column)
        )
        if self.cursor.fetchone()["cnt"] == 0:
            alter = f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}"
            if unique_key:
                alter += f", ADD UNIQUE KEY `{unique_key}` (`{column}`)"
            self.cursor.execute(alter)
            self.conn.commit()
            logger.info(f"[mysql] {table} 已添加 {column} 列")

    def pull_asin_detail_task(self, region: str = None) -> Optional[Dict]:
        """拉取一条到期待执行的商品详情任务（原子操作，带FOR UPDATE）"""
        self._check_connection()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            timeout_threshold = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            self.cursor.execute(
                "UPDATE crawl_asin_detail_tasks SET status=0, updated_at=%s "
                "WHERE status=1 AND updated_at < %s",
                (now_str, timeout_threshold)
            )
            self.conn.commit()
        except Exception:
            pass

        try:
            self.conn.start_transaction()
            where = "status = 0 AND need_crawler_time <= %s"
            params: list = [now_str]
            if region:
                where += " AND region = %s"
                params.append(region.upper())
            self.cursor.execute(
                f"SELECT * FROM crawl_asin_detail_tasks WHERE {where} "
                f"ORDER BY priority DESC, need_crawler_time ASC, id ASC LIMIT 1 FOR UPDATE",
                tuple(params)
            )
            task = self.cursor.fetchone()
            if not task:
                self.conn.rollback()
                return None
            self.cursor.execute(
                "UPDATE crawl_asin_detail_tasks SET status=1, updated_at=%s WHERE id=%s",
                (now_str, task["id"])
            )
            self.conn.commit()
            return task
        except Exception as e:
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"[mysql] pull_asin_detail_task 失败: {e}")
            return None

    def complete_asin_detail_task(self, task_id: int, result: Optional[Dict], success: bool,
                                  error_msg: str = ""):
        """更新商品详情任务结果"""
        self._check_connection()
        st = 2 if success else 3
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result_json = json.dumps(result, ensure_ascii=False) if result is not None else None
        try:
            self.cursor.execute(
                "UPDATE crawl_asin_detail_tasks SET status=%s, result=%s, error_msg=%s, updated_at=%s "
                "WHERE id=%s",
                (st, result_json, error_msg[:512] if error_msg else None, now, task_id)
            )
            self.conn.commit()
        except Exception as e:
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"[mysql] complete_asin_detail_task 失败: {e}")

    def reset_asin_detail_tasks_by_ids(self, task_ids: List[int], error_msg: str = "") -> int:
        """将执行中的商品详情任务退回待执行。用于进程中断兜底，避免 status=1 残留。"""
        ids = [int(i) for i in task_ids if i is not None]
        if not ids:
            return 0
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ",".join(["%s"] * len(ids))
        try:
            self.cursor.execute(
                f"UPDATE crawl_asin_detail_tasks "
                f"SET status=0, error_msg=%s, updated_at=%s "
                f"WHERE status=1 AND id IN ({placeholders})",
                tuple([(error_msg or "worker interrupted")[:512], now, *ids]),
            )
            affected = int(self.cursor.rowcount or 0)
            self.conn.commit()
            return affected
        except Exception as e:
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"[mysql] reset_asin_detail_tasks_by_ids 失败: {e}")
            return 0

    def insert_asin_detail_result(self, asin: str, region: str, result: dict):
        """将商品详情结果写入 crawl_asin_detail_tasks（按 asin+region upsert）"""
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result_json = json.dumps(result, ensure_ascii=False)
        try:
            self.cursor.execute(
                "UPDATE crawl_asin_detail_tasks SET result=%s, status=2, updated_at=%s "
                "WHERE asin=%s AND region=%s AND status IN (0,1)",
                (result_json, now, asin, region.upper())
            )
            if self.cursor.rowcount == 0:
                self.cursor.execute(
                    "INSERT INTO crawl_asin_detail_tasks "
                    "(asin, region, status, result, created_at, updated_at) "
                    "VALUES (%s, %s, 2, %s, %s, %s)",
                    (asin, region.upper(), result_json, now, now)
                )
            self.conn.commit()
        except Exception as e:
            if self.conn.is_connected():
                self.conn.rollback()
            logger.error(f"[mysql] insert_asin_detail_result 失败: {e}")

    def create_asin_detail_task(self, task_id: str, asin: str, region: str,
                                priority: int = 0,
                                need_crawler_time: str = None) -> Dict:
        """创建一条商品详情任务，返回 {task_id, id}"""
        self._check_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nct = need_crawler_time or now
        try:
            self.cursor.execute(
                "INSERT INTO crawl_asin_detail_tasks "
                "(task_id, asin, region, priority, status, need_crawler_time, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, 0, %s, %s, %s)",
                (task_id, asin.upper(), region.upper(), priority, nct, now, now)
            )
            row_id = self.cursor.lastrowid
            self.conn.commit()
            return {"task_id": task_id, "id": row_id, "asin": asin.upper(), "region": region.upper()}
        except Exception as e:
            if self.conn.is_connected():
                self.conn.rollback()
            raise e

    def get_asin_detail_task_result(self, task_id: str) -> Dict:
        """按 task_id 查询商品详情任务状态+结果"""
        self._check_connection()
        self.cursor.execute(
            "SELECT task_id, asin, region, status, need_crawler_time, result, error_msg, updated_at "
            "FROM crawl_asin_detail_tasks WHERE task_id = %s",
            (task_id,)
        )
        row = self.cursor.fetchone()
        if not row:
            return {}
        result_raw = row.get("result")
        if result_raw and isinstance(result_raw, str):
            try:
                row["result"] = json.loads(result_raw)
            except (json.JSONDecodeError, TypeError):
                pass
        return row

    # ========================== 推特模块 ==========================

    def init_twitter_tables(self):
        """初始化推特相关表（幂等）"""
        self._check_connection()
        tables = {
            "twitter_tasks": """
                CREATE TABLE `twitter_tasks` (
                    `id`           BIGINT AUTO_INCREMENT PRIMARY KEY,
                    `task_type`    VARCHAR(20)  NOT NULL COMMENT 'search/tweet_detail/tweet_replies',
                    `input`        VARCHAR(512) NOT NULL COMMENT '关键词或 tweet_id',
                    `lang`         VARCHAR(10)  DEFAULT '' COMMENT '语言过滤，空=不限',
                    `status`       TINYINT      NOT NULL DEFAULT 0 COMMENT '0=待执行 1=执行中 2=成功 3=失败',
                    `result_count` INT          NOT NULL DEFAULT 0,
                    `error_msg`    VARCHAR(512) DEFAULT '',
                    `retry_count`  TINYINT      NOT NULL DEFAULT 0,
                    `created_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    `updated_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    KEY `idx_status_type` (`status`, `task_type`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='推特任务队列'
            """,
            "twitter_doc_ids": """
                CREATE TABLE `twitter_doc_ids` (
                    `operation_name` VARCHAR(128) NOT NULL PRIMARY KEY,
                    `query_id`       VARCHAR(64)  NOT NULL,
                    `updated_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    KEY `idx_updated` (`updated_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='推特 GraphQL doc_id 管理'
            """,
        }
        for table_name, ddl in tables.items():
            if not self._table_exists(table_name):
                self.cursor.execute(ddl)
                self.conn.commit()
                logger.info(f"[twitter] 创建表: {table_name}")

        initial = {"SearchTimeline": "Yw6L66Pw54NHKuq4Dp7b4Q"}
        for op, qid in initial.items():
            self.cursor.execute(
                "INSERT IGNORE INTO twitter_doc_ids (operation_name, query_id) VALUES (%s, %s)",
                (op, qid),
            )
        self.conn.commit()

    def create_twitter_tweet_table(self, table_name: str):
        """创建按日推文结果表（幂等）"""
        self._check_connection()
        if self._table_exists(table_name):
            return
        self.cursor.execute(f"""
            CREATE TABLE `{table_name}` (
                `id`              BIGINT AUTO_INCREMENT PRIMARY KEY,
                `tweet_id`        VARCHAR(32)  NOT NULL,
                `task_id`         BIGINT       NOT NULL,
                `task_type`       VARCHAR(20)  NOT NULL,
                `author_id`       VARCHAR(32)  DEFAULT '',
                `author_name`     VARCHAR(128) DEFAULT '',
                `author_screen`   VARCHAR(128) DEFAULT '',
                `content`         TEXT,
                `lang`            VARCHAR(10)  DEFAULT '',
                `like_count`      INT          DEFAULT 0,
                `retweet_count`   INT          DEFAULT 0,
                `reply_count`     INT          DEFAULT 0,
                `quote_count`     INT          DEFAULT 0,
                `bookmark_count`  INT          DEFAULT 0,
                `tweet_created_at` DATETIME    NULL,
                `query_keyword`   VARCHAR(256) DEFAULT '',
                `parent_tweet_id` VARCHAR(32)  DEFAULT '',
                `created_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY `uniq_tweet_task` (`tweet_id`, `task_id`),
                KEY `idx_task_id` (`task_id`),
                KEY `idx_tweet_created` (`tweet_created_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='推特推文结果'
        """)
        self.conn.commit()
        logger.info(f"[twitter] 创建推文结果表: {table_name}")

    def poll_twitter_task(self) -> Optional[Dict]:
        """拉取一条待执行推特任务（FOR UPDATE 加锁）"""
        self._check_connection()
        try:
            if self.conn.in_transaction:
                self.conn.rollback()
            self.cursor.execute(
                "SELECT * FROM twitter_tasks WHERE status=0 ORDER BY id ASC LIMIT 1 FOR UPDATE"
            )
            row = self.cursor.fetchone()
            if not row:
                self.conn.rollback()
                return None
            self.cursor.execute(
                "UPDATE twitter_tasks SET status=1, updated_at=NOW() WHERE id=%s",
                (row["id"],),
            )
            self.conn.commit()
            return row
        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
            logger.error(f"[twitter] poll_twitter_task 失败: {e}")
            return None

    def update_twitter_task(self, task_id: int, **kwargs):
        """更新推特任务字段（status/result_count/error_msg 等）"""
        self._check_connection()
        if not kwargs:
            return
        set_parts = [f"`{k}` = %s" for k in kwargs]
        params = list(kwargs.values()) + [task_id]
        self.cursor.execute(
            f"UPDATE twitter_tasks SET {', '.join(set_parts)}, updated_at=NOW() WHERE id=%s",
            tuple(params),
        )
        self.conn.commit()

    def insert_twitter_tweets(self, table_name: str, rows: List[Dict]):
        """批量插入推文结果（重复 tweet_id+task_id 则忽略）"""
        self._check_connection()
        if not rows:
            return
        cols = [
            "tweet_id", "task_id", "task_type", "author_id", "author_name",
            "author_screen", "content", "lang", "like_count", "retweet_count",
            "reply_count", "quote_count", "bookmark_count", "tweet_created_at",
            "query_keyword", "parent_tweet_id",
        ]
        placeholders = ", ".join(["%s"] * len(cols))
        col_names = ", ".join([f"`{c}`" for c in cols])
        sql = f"INSERT IGNORE INTO `{table_name}` ({col_names}) VALUES ({placeholders})"
        values = [tuple(row.get(c, "") for c in cols) for row in rows]
        self.cursor.executemany(sql, values)
        self.conn.commit()

    def get_twitter_doc_ids(self) -> Dict[str, str]:
        """读取所有 doc_id，返回 {operation_name: query_id}"""
        self._check_connection()
        self.cursor.execute("SELECT operation_name, query_id FROM twitter_doc_ids")
        rows = self.cursor.fetchall() or []
        return {r["operation_name"]: r["query_id"] for r in rows}

    def upsert_twitter_doc_ids(self, doc_ids: Dict[str, str]):
        """批量写入或更新 doc_id"""
        self._check_connection()
        for op, qid in doc_ids.items():
            self.cursor.execute(
                """INSERT INTO twitter_doc_ids (operation_name, query_id)
                   VALUES (%s, %s)
                   ON DUPLICATE KEY UPDATE query_id=VALUES(query_id), updated_at=NOW()""",
                (op, qid),
            )
        self.conn.commit()

    def __del__(self):
        """析构函数：自动关闭连接"""
        try:
            self.close()
        except Exception:
            pass
