"""Crawlee-Python PoC · 验证 ALL-IN 路线下用 Crawlee 做底座的可行性

================================================================================
背景
================================================================================
smart-crawler 目前的爬虫栈以 curl_cffi (impersonate Chrome TLS) + Scrapling
(StealthyFetcher) + 自定义 site profile (sites.yaml) 为主。ALL-IN 路线（46 站
+ 5 社媒 + ASR + 多区域 K8s 部署）下，自维护调度 / 重试 / 持久化 / 代理路由
的成本会指数级上升。

Crawlee-Python (Apify 出品 · 6.5k⭐) 提供开箱即用的：
    · BeautifulSoupCrawler / PlaywrightCrawler / HttpCrawler 三档
    · Request queue 持久化（SQLite/PostgreSQL/Redis）
    · 自动 retry + exponential backoff
    · 代理池路由 + session pooling
    · 结构化 dataset 输出（JSON Lines / CSV / Excel）
    · 内置 fingerprint 注入（绕 Cloudflare / Datadome 友好）

本 PoC 用 Hacker News 作为温和测试站 · 验证 Crawlee 安装、运行、输出三件套。
不替换现有 crawlers/ 下任何代码 · 只是 ALL-IN 路线下的底座选型验证。

================================================================================
运行
================================================================================
    pip install 'crawlee[beautifulsoup]'
    python -m backend.app.crawlee_poc

预期输出：
    [PoC] 启动 Crawlee BeautifulSoupCrawler ...
    [PoC] 抓取 https://news.ycombinator.com
    [PoC]  1. <title> by <user>
    [PoC]  2. <title> by <user>
    ...
    [PoC] 完成 · 共 N 条 · 耗时 X.X s

================================================================================
不要在生产中跑这个文件 · 仅本地 demo · 用户睡眠时段勿启动
================================================================================
"""
from __future__ import annotations

import asyncio
from datetime import datetime

# 延迟导入 · 即使没装 crawlee，模块依然可以被解析
try:
    from crawlee.beautifulsoup_crawler import BeautifulSoupCrawler, BeautifulSoupCrawlingContext
    _CRAWLEE_AVAILABLE = True
except ImportError:
    _CRAWLEE_AVAILABLE = False
    BeautifulSoupCrawler = None  # type: ignore
    BeautifulSoupCrawlingContext = None  # type: ignore


TEST_URL = "https://news.ycombinator.com"
TOP_N = 10


async def run_poc() -> list[dict]:
    """跑一次 PoC 抓取 · 返回前 10 条 (title, user, score, link)。"""
    if not _CRAWLEE_AVAILABLE:
        raise RuntimeError(
            "crawlee 未安装。运行 pip install 'crawlee[beautifulsoup]' 后重试。"
        )

    collected: list[dict] = []

    crawler = BeautifulSoupCrawler(
        max_requests_per_crawl=1,
        max_request_retries=3,
    )

    @crawler.router.default_handler
    async def handler(ctx: BeautifulSoupCrawlingContext) -> None:
        ctx.log.info(f"[PoC] 抓取 {ctx.request.url}")
        rows = ctx.soup.select("tr.athing")[:TOP_N]
        for i, row in enumerate(rows, 1):
            title_cell = row.select_one("span.titleline > a")
            if not title_cell:
                continue
            sibling = row.find_next_sibling("tr")
            score_el = sibling.select_one("span.score") if sibling else None
            user_el = sibling.select_one("a.hnuser") if sibling else None
            item = {
                "rank": i,
                "title": title_cell.get_text(strip=True),
                "link": title_cell.get("href", ""),
                "score": score_el.get_text(strip=True) if score_el else "",
                "user": user_el.get_text(strip=True) if user_el else "",
                "collected_at": datetime.utcnow().isoformat() + "Z",
            }
            collected.append(item)
            print(f"[PoC] {i:>2}. {item['title'][:60]:<60} | by {item['user'] or '-':<14} | {item['score'] or '-'}")
        await ctx.push_data(collected)

    started = datetime.utcnow()
    await crawler.run([TEST_URL])
    elapsed = (datetime.utcnow() - started).total_seconds()
    print(f"[PoC] 完成 · 共 {len(collected)} 条 · 耗时 {elapsed:.1f}s")
    return collected


def main() -> None:
    """CLI 入口 · python -m backend.app.crawlee_poc"""
    print(f"[PoC] 启动 Crawlee BeautifulSoupCrawler · target={TEST_URL} · n={TOP_N}")
    if not _CRAWLEE_AVAILABLE:
        print("[PoC] crawlee 未安装 · 请运行 pip install 'crawlee[beautifulsoup]'")
        return
    asyncio.run(run_poc())


if __name__ == "__main__":
    main()
