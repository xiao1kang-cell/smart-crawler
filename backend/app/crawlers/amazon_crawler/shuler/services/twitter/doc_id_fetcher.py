"""
事件驱动的 doc_id 更新器。
Worker 遇到 404 时调用 trigger()，防抖后自动从推特 JS 提取最新 doc_id 并写回 DB。
"""
import re
import threading
import time
from contextlib import contextmanager
from typing import Dict, Optional

import requests
from loguru import logger

from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
from app.crawlers.amazon_crawler.shuler.services.twitter.twitter_config import DOC_ID_DEBOUNCE_SECONDS

_JS_PATTERN = re.compile(r'\{queryId:"([^"]+)",operationName:"([^"]+)"')
_JS_URL_PATTERN = re.compile(
    r'https://abs\.twimg\.com/responsive-web/client-web/main\.[^"]+\.js'
)


class DocIdFetcher:
    """
    按需触发的 doc_id 获取器（不启动后台线程）。
    线程安全：多个 Worker 线程同时触发时，防抖保证只执行一次拉取。
    """

    def __init__(self, mysql_db=None, db_factory=None):
        self._mysql_db = mysql_db
        self._db_factory = db_factory
        self._cache: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._fetching = False
        self._last_fetch_time: float = 0.0

    @contextmanager
    def _db(self):
        if self._mysql_db is not None:
            yield self._mysql_db
            return

        db = (self._db_factory or MySQLTaskDB)()
        try:
            yield db
        finally:
            try:
                db.close()
            except Exception:
                pass

    def _load_cache_from_db(self):
        """从 DB 加载 doc_id 到内存缓存"""
        try:
            with self._db() as db:
                ids = db.get_twitter_doc_ids()
            with self._lock:
                self._cache.update(ids)
        except Exception as e:
            logger.warning(f"[DocIdFetcher] 从 DB 加载 doc_id 失败: {e}")

    def get(self, operation_name: str) -> Optional[str]:
        """获取 doc_id，内存缓存优先，缓存为空时从 DB 加载"""
        with self._lock:
            cached = self._cache.get(operation_name)
        if cached:
            return cached
        self._load_cache_from_db()
        with self._lock:
            return self._cache.get(operation_name)

    def trigger(self):
        """Worker 404 时调用。防抖：DOC_ID_DEBOUNCE_SECONDS 内只触发一次。"""
        with self._lock:
            now = time.time()
            if self._fetching:
                logger.debug("[DocIdFetcher] 已有拉取任务在进行中，跳过")
                return
            if now - self._last_fetch_time < DOC_ID_DEBOUNCE_SECONDS:
                logger.debug("[DocIdFetcher] 防抖中，跳过本次触发")
                return
            self._fetching = True

        t = threading.Thread(target=self._fetch_and_update, daemon=True)
        t.start()

    def _fetch_and_update(self):
        """实际拉取逻辑（在独立线程中运行）"""
        try:
            logger.info("[DocIdFetcher] 开始拉取推特 JS，更新 doc_id ...")
            html = requests.get("https://x.com", timeout=15)
            html.raise_for_status()
            js_url = self._extract_js_url(html.text)
            if not js_url:
                logger.warning("[DocIdFetcher] 未找到 main JS URL")
                return

            js = requests.get(js_url, timeout=30)
            js.raise_for_status()
            new_ids = self._extract_from_js(js.text)
            if not new_ids:
                logger.warning("[DocIdFetcher] JS 中未提取到任何 doc_id")
                return

            with self._db() as db:
                old_ids = db.get_twitter_doc_ids()
            changed = {op: qid for op, qid in new_ids.items() if old_ids.get(op) != qid}

            if changed:
                with self._db() as db:
                    db.upsert_twitter_doc_ids(changed)
                with self._lock:
                    self._cache.update(new_ids)
                logger.info(f"[DocIdFetcher] doc_id 已更新: {changed}")
                try:
                    from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message
                    send_custom_robot_group_message(f"[Twitter] doc_id 自动更新: {changed}")
                except Exception:
                    pass
            else:
                with self._lock:
                    self._cache.update(new_ids)
                logger.info("[DocIdFetcher] doc_id 无变化")

        except Exception as e:
            logger.error(f"[DocIdFetcher] 拉取失败: {e}")
        finally:
            with self._lock:
                self._fetching = False
                self._last_fetch_time = time.time()

    @staticmethod
    def _extract_js_url(html: str) -> Optional[str]:
        m = _JS_URL_PATTERN.search(html)
        return m.group(0) if m else None

    @staticmethod
    def _extract_from_js(js_text: str) -> Dict[str, str]:
        return {op: qid for qid, op in _JS_PATTERN.findall(js_text)}
