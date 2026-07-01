# ------------------------------
# 1. MongoDB 账号操作层（专属）
# ------------------------------
import os
import time

import pymongo

from app.crawlers.amazon_crawler.shuler.services.amazon.account_ import Account
from typing import List, Optional, Dict, Any

from app.crawlers.amazon_crawler.shuler.util.config import *


class MongoAccountDB:
    def __init__(self):
        self.client = pymongo.MongoClient(MONGO_URI)
        self.db = self.client.get_default_database()
        self.coll_accounts = self.db[MONGO_COLL_ACCOUNTS]
        self.coll_usage = self.db[MONGO_COLL_USAGE]
        # 建立索引加速查询
        # self.coll_usage.create_index("username")
        # self.coll_usage.create_index("used_at")
        # self.coll_usage.create_index("asin")


    def load_all_accounts(self, filter_conditions: Optional[Dict[str, Any]] = None) -> List[Account]:
        """
        加载账号（通用版）：支持传入任意筛选条件，未传参时仅筛选state=1的账号
        :param filter_conditions: 可选，自定义筛选条件字典（如 {"country": "US"} 等）
        :return: 符合条件的Account对象列表
        """
        # 1. 基础条件：固定筛选state=1（必选）
        base_query = {"state": 1}
        # 2. 合并自定义筛选条件（如果传入）
        if filter_conditions and isinstance(filter_conditions, dict):
            # 合并字典：自定义条件会覆盖基础条件（如需避免覆盖，可调整合并逻辑）
            query = {**base_query, **filter_conditions}
        else:
            query = base_query

        # 3. 执行查询并转换为Account对象
        docs = list(self.coll_accounts.find(query))
        return [Account.from_mongo_dict(doc) for doc in docs]

    def update_account(self, account: Account):
        """更新账号状态"""
        self.coll_accounts.update_one(
            {"username": account.username},
            {"$set": account.to_dict()},
            upsert=True
        )

    def add_usage_record(self, username: str, asin: str, success: bool, task_id: str):
        """添加账号使用记录"""
        self.coll_usage.insert_one({
            "username": username,
            "asin": asin,
            "used_at": time.time(),
            "success": success,
            "task_id": task_id,
            "ip": os.getenv("HOST_IP", "unknown")
        })

    def get_usage_count(self, username: str, seconds: int) -> int:
        """查询账号指定时间内的使用次数"""
        now = time.time()
        return self.coll_usage.count_documents({
            "username": username,
            "used_at": {"$gte": now - seconds}
        })
