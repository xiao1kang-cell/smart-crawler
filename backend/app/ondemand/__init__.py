"""按需(on-demand)抓取子系统 —— 指定 URL → listing + VOC。

与整站枚举(crawlers/ + runner.py)解耦:输入一条 URL(单品或列表页),
抓取该 listing 的商品信息 + 评论原文。支持 美客多 / Lazada / 虾皮。
"""
from __future__ import annotations

from .base import OnDemandResult
from .runner import fetch

__all__ = ["OnDemandResult", "fetch"]
