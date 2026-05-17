"""Agent 发现层 —— 让 AI Agent / 配 Agent 的人能"找到"smart-crawler。

落地 playbook「Agents 是新分发渠道」：能力 API 化还不够，还要可被发现。
本模块暴露机器可读的发现端点，Agent 抓站或工具目录收录时即可识别能力：

- GET /llms.txt              站点 AI 可读简介（llmstxt.org 约定）
- GET /.well-known/mcp.json  MCP 服务器发现清单
- GET /.well-known/ai-plugin.json  OpenAI 插件式清单（兼容旧发现工具）
- GET /agents.json           能力 / 工具 / 接入方式总清单
"""
from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse

router = APIRouter(tags=["discovery"])

BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://smartcrawler.io")

_SUMMARY = (
    "为 AI Agent 打造的跨境电商竞品数据采集引擎。覆盖 9 大家居品牌 "
    "46 个独立站 + 21 个评论渠道，持续采集结构化的竞品商品 / 价格 / 促销、"
    "消费者口碑(VOC)评论 + NLP 情感分析、Google Shopping 竞争格局。"
)

# MCP 工具清单（与 mcp_server.py 保持一致）—— 发现层用，便于目录收录。
_TOOLS = [
    {"name": "list_data_sources",
     "description": "列出全部数据源：46 个竞品站 + 评论平台 + Google Shopping"},
    {"name": "search_competitor_products",
     "description": "按品牌/国家/关键词/品类/价格/促销搜索竞品商品"},
    {"name": "get_product_detail",
     "description": "取单个商品完整信息 + 历史价格曲线"},
    {"name": "list_promotions",
     "description": "列出竞品当前促销活动及折扣率"},
    {"name": "get_voc_reviews",
     "description": "取消费者口碑评论 + NLP 情感/分类标注"},
    {"name": "voc_summary",
     "description": "口碑分析汇总：情感分布 + 痛点分类占比"},
    {"name": "competitor_landscape",
     "description": "Google Shopping 某关键词下各商家出现占有率"},
]


@router.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
def llms_txt() -> str:
    """站点 AI 可读简介 —— Agent 抓站时优先读取，快速判断能力。"""
    tools = "\n".join(f"- {t['name']}: {t['description']}" for t in _TOOLS)
    return f"""# smart-crawler

> {_SUMMARY}

smart-crawler 把"网页采集"做成 AI Agent 可直接调用的数据服务：自适应抓取、
抗封锁、可回溯。Agent 不需要自己写爬虫，直接调用结构化的竞品情报能力。

## Agent 接入（推荐 MCP）

- [MCP 服务器]({BASE_URL}/mcp): streamable-http 传输，{len(_TOOLS)} 个工具，开箱即用
- [发现清单]({BASE_URL}/.well-known/mcp.json): MCP 服务器元数据
- [能力总览]({BASE_URL}/agents.json): 工具 + REST 端点 + 接入方式

## REST API（备选）

- [OpenAPI 规格]({BASE_URL}/openapi.json): OpenAPI 3.1
- [交互式文档]({BASE_URL}/docs): Swagger UI
- 鉴权: 请求头 `X-API-Key: sck_...`（在控制台「API 接入」生成）
- 主要端点: `/api/v1/products` `/api/v1/promotions` `/api/v1/site/{{site}}`

## MCP 工具

{tools}

## 覆盖范围

- 品牌: SONGMICS / VASAGLE / FEANDREA / Costway / Homary / Vidaxl / Flexispot / VonHaus
- 站点: 46 个独立站，覆盖 US/UK/DE/FR/IT/ES/CA 等 12 国
- 口碑: Trustpilot / Reviews.io / Google Maps 共 21 个评论渠道
"""


@router.get("/.well-known/mcp.json", include_in_schema=False)
def well_known_mcp() -> JSONResponse:
    """MCP 服务器发现清单 —— MCP 客户端 / 工具目录收录用。"""
    return JSONResponse({
        "name": "smart-crawler",
        "description": _SUMMARY,
        "version": "0.1.0",
        "mcp": {
            "url": f"{BASE_URL}/mcp",
            "transport": "streamable-http",
        },
        "tools": _TOOLS,
        "documentation": f"{BASE_URL}/llms.txt",
        "homepage": BASE_URL,
    })


@router.get("/.well-known/ai-plugin.json", include_in_schema=False)
def well_known_ai_plugin() -> JSONResponse:
    """OpenAI 插件式清单 —— 兼容仍用该约定的发现工具 / 目录。"""
    return JSONResponse({
        "schema_version": "v1",
        "name_for_model": "smart_crawler",
        "name_for_human": "smart-crawler",
        "description_for_model": (
            "跨境电商竞品数据采集引擎。提供竞品商品/价格/促销查询、消费者口碑"
            "(VOC)评论 + NLP 情感分析、Google Shopping 竞争格局。优先用 MCP "
            f"服务器 {BASE_URL}/mcp（{len(_TOOLS)} 个工具）。"
        ),
        "description_for_human": _SUMMARY,
        "api": {"type": "openapi", "url": f"{BASE_URL}/openapi.json"},
        "auth": {"type": "service_http", "authorization_type": "header"},
        "logo_url": f"{BASE_URL}/favicon.svg",
        "legal_info_url": BASE_URL,
    })


@router.get("/agents.json", include_in_schema=False)
def agents_json() -> JSONResponse:
    """能力总清单 —— 一个端点看全 Agent 能怎么用 smart-crawler。"""
    return JSONResponse({
        "name": "smart-crawler",
        "summary": _SUMMARY,
        "principle": "Agents 是新的分发渠道：做能力，不做界面。",
        "access": {
            "mcp": {
                "url": f"{BASE_URL}/mcp",
                "transport": "streamable-http",
                "recommended": True,
                "tool_count": len(_TOOLS),
            },
            "rest": {
                "openapi": f"{BASE_URL}/openapi.json",
                "docs": f"{BASE_URL}/docs",
                "auth": "header X-API-Key: sck_...",
                "endpoints": [
                    "/api/v1/products", "/api/v1/promotions",
                    "/api/v1/site/{site}", "/api/datasources",
                ],
            },
        },
        "tools": _TOOLS,
        "discovery": {
            "llms_txt": f"{BASE_URL}/llms.txt",
            "well_known_mcp": f"{BASE_URL}/.well-known/mcp.json",
            "well_known_ai_plugin": f"{BASE_URL}/.well-known/ai-plugin.json",
        },
    })
