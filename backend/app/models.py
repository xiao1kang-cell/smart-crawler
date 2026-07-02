"""数据模型 —— 对齐《需求规格说明书》§4 字段规格。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Index,
    String,
    Text,
    UniqueConstraint,
)

from .db import Base

BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class Site(Base):
    """标杆站点。对应 sites.yaml 的一条。"""

    __tablename__ = "sites"

    id = Column(Integer, primary_key=True)
    site = Column(String, unique=True, index=True)   # 如 songmics_us
    brand = Column(String, index=True)
    country = Column(String)
    url = Column(String)
    platform = Column(String)                        # shopify / nuxt / vue_spa
    proxy_tier = Column(String, default="none")
    crawler_config = Column(JSON)                    # per-site crawler secrets/feed URLs/options
    last_crawled = Column(DateTime)
    # 标杆追踪面板字段（2026-06-11）
    track_status = Column(String, default="tracking")  # tracking / paused / error
    source = Column(String, default="yaml")            # yaml(种子) / user(面板建)
    creator = Column(String)                            # 创建人 username
    review_rate = Column(Float)                         # 留评率(Edit 可改，影响销量估算)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime)


class Product(Base):
    """SKU 商品 —— 规格 §4.1.2 的 32 字段。(site, sku) 唯一。"""

    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("site", "sku", name="uq_site_sku"),)

    id = Column(Integer, primary_key=True)
    sku = Column(String, index=True)                 # 商品唯一标识
    spu = Column(String, index=True)                 # 标准产品单元（同款变体共享）
    title = Column(String)
    description = Column(Text)
    image_urls = Column(JSON)                        # string[]
    category_path = Column(String, index=True)
    sale_price = Column(Float)
    original_price = Column(Float)
    currency = Column(String)
    variant_id = Column(String)
    attributes = Column(JSON)                        # {"color": "Black", "size": "4 Tier"}
    ratings = Column(Float)
    review_count = Column(Integer)
    thirty_day_sales = Column(Integer)               # 预估销量（评论增量倒推）
    thirty_day_revenue = Column(Float)               # 预估营收
    status = Column(String, index=True)              # on_sale / out_of_stock / discontinued
    inventory = Column(String)
    has_video = Column(Boolean)
    has_free_shipping = Column(Boolean)
    label = Column(String)                           # NEW / BEST SELLER / TOP
    tags = Column(JSON)                              # string[]
    product_url = Column(String)
    product_type = Column(String)
    mpn = Column(String)
    gtin = Column(String)
    weight = Column(String)
    shipping_time = Column(String)
    return_policy_days = Column(Integer)
    published_at = Column(DateTime)                  # 站点发布时间
    created_time = Column(DateTime, default=datetime.utcnow)   # 首次被我方采集
    updated_time = Column(DateTime, default=datetime.utcnow)   # 最后采集
    site = Column(String, index=True)
    brand = Column(String, index=True)
    is_new = Column(Boolean, default=False)          # 新品标记（F1-012）
    is_bestseller = Column(Boolean, default=False)   # 热销标记（F1-013）


class PriceHistory(Base):
    """价格曲线 —— F1-011。每次采集到的价格快照。"""

    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True)
    site = Column(String, index=True)
    sku = Column(String, index=True)
    date = Column(Date, index=True)
    sale_price = Column(Float)
    original_price = Column(Float)
    review_count = Column(Integer)                   # 用于评论增量倒推销量


class Category(Base):
    """分类导航树 —— 规格 §6。"""

    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    site = Column(String, index=True)
    category_id = Column(String)
    category_name = Column(String)
    category_url = Column(String)
    parent_id = Column(String)
    level = Column(Integer)
    product_count = Column(Integer)
    collected_time = Column(DateTime, default=datetime.utcnow)


class Promotion(Base):
    """促销活动 —— 规格 §4.1.3 的 13 字段。"""

    __tablename__ = "promotions"

    id = Column(Integer, primary_key=True)
    sku = Column(String, index=True)
    site = Column(String, index=True)
    promotion_type = Column(String)                  # price_promotion / coupon / bundle ...
    promotion_name = Column(String)
    original_price = Column(Float)
    promotion_price = Column(Float)
    discount_percent = Column(Integer)
    threshold = Column(String)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    detected_time = Column(DateTime, default=datetime.utcnow)
    product_title = Column(String)
    product_image = Column(String)


class Trend(Base):
    """趋势日汇总 —— 规格 §5。(site, date) 唯一。"""

    __tablename__ = "trends"
    __table_args__ = (UniqueConstraint("site", "date", name="uq_site_date"),)

    id = Column(Integer, primary_key=True)
    site = Column(String, index=True)
    date = Column(Date, index=True)
    sku_count = Column(Integer)
    new_product_count = Column(Integer)
    estimated_sales = Column(Integer)
    estimated_revenue = Column(Float)
    traffic = Column(Integer)                        # 第三方数据，MVP 留空
    conversion_rate = Column(Float)                  # 第三方数据，MVP 留空
    avg_rating = Column(Float)                        # 当日在售 SKU 平均星级（趋势图用）
    review_total = Column(Integer)                    # 当日在售 SKU 评论总数（趋势图用）

    # Daily delta 字段（2026-05-24 · 遨森每日增量需求）
    price_change_count = Column(Integer, default=0)  # 当日价格变化 SKU 数
    stock_change_count = Column(Integer, default=0)  # 当日库存变化 SKU 数
    new_promo_count = Column(Integer, default=0)     # 当日新增促销数
    new_review_count = Column(Integer, default=0)    # 当日新增评论数
    avg_sentiment = Column(Float)                    # 当日评论平均情感分
    delta_summary = Column(Text)                     # LLM 生成的一句话总结


class SiteMetric(Base):
    """站点级查询汇总。

    大表指标在采集完成后刷新，页面查询只读这一张小表，避免每次请求
    对 products / crawl_urls / price_history 做全量聚合。
    """

    __tablename__ = "site_metrics"

    id = Column(Integer, primary_key=True)
    site = Column(String, unique=True, index=True)
    sku_count = Column(Integer, default=0)
    product_listing_count = Column(Integer, default=0)
    fetched_count = Column(Integer, default=0)
    discovered_product_url_count = Column(Integer, default=0)
    price_signal_count = Column(Integer, default=0)
    sales_signal_count = Column(Integer, default=0)
    revenue_signal_count = Column(Integer, default=0)
    review_signal_count = Column(Integer, default=0)
    review_history_signal_count = Column(Integer, default=0)
    weak_title_count = Column(Integer, default=0)
    currency_missing_count = Column(Integer, default=0)
    currency_mismatch_count = Column(Integer, default=0)
    promotion_count = Column(Integer, default=0)
    traffic_signal_count = Column(Integer, default=0)
    conversion_signal_count = Column(Integer, default=0)
    thirty_day_sales = Column(Integer, default=0)
    thirty_day_revenue = Column(Float, default=0.0)
    last_product_updated = Column(DateTime)
    refreshed_at = Column(DateTime, default=datetime.utcnow, index=True)


class Workspace(Base):
    """租户工作区 —— 只隔离视图、报告、API key 与用量，不复制 warehouse 数据。"""

    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, index=True)
    slug = Column(String, unique=True, index=True)
    type = Column(String, default="customer")        # internal / customer
    status = Column(String, default="active")        # active / disabled
    created_at = Column(DateTime, default=datetime.utcnow)


class WorkspaceMember(Base):
    """用户与工作区的成员关系。"""

    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id",
                                       name="uq_workspace_user"),)

    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    role = Column(String, default="member")          # owner / admin / member / viewer
    status = Column(String, default="active")
    created_at = Column(DateTime, default=datetime.utcnow)


class WorkspaceSite(Base):
    """工作区可见站点清单 —— 引用全局 Site.site。"""

    __tablename__ = "workspace_sites"
    __table_args__ = (UniqueConstraint("workspace_id", "site",
                                       name="uq_workspace_site"),)

    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    site = Column(String, index=True)
    display_name = Column(String)
    enabled = Column(Boolean, default=True)
    hidden = Column(Boolean, default=False)
    sort_order = Column(Integer, default=0)
    target_coverage_pct = Column(Float)
    target_sku_count = Column(Integer)
    report_config = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)


class ReportConfig(Base):
    """租户私有报告配置。报告数据仍从共享 warehouse 按 WorkspaceSite 读取。"""

    __tablename__ = "report_configs"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    name = Column(String)
    sites = Column(JSON)
    categories = Column(JSON)
    settings = Column(JSON)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ReportRun(Base):
    """报告生成记录。"""

    __tablename__ = "report_runs"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    report_config_id = Column(Integer, ForeignKey("report_configs.id"))
    status = Column(String, default="pending")
    output_path = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)
    error = Column(Text)


class User(Base):
    """后台账号 —— 登录鉴权。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True)
    password_hash = Column(String)
    role = Column(String, default="admin")          # admin / user / viewer
    global_role = Column(String)                    # super_admin / null
    default_workspace_id = Column(Integer, ForeignKey("workspaces.id"))
    status = Column(String, default="active")       # active / disabled
    display_name = Column(String)
    email_verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime)
    password_changed_at = Column(DateTime)
    failed_login_count = Column(Integer, default=0)
    locked_until = Column(DateTime)
    last_login_ip = Column(String)


class UserSession(Base):
    """登录会话 —— 支持 logout / 改密撤销。"""

    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    session_hash = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, index=True)
    revoked_at = Column(DateTime)
    ip_address = Column(String)
    user_agent = Column(String)


class InviteCode(Base):
    """内部邀请码 —— 明文只在创建时返回，库中只存 hash。"""

    __tablename__ = "invite_codes"

    id = Column(Integer, primary_key=True)
    code_prefix = Column(String, index=True)
    code_hash = Column(String, unique=True, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"))
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    target_type = Column(String, default="workspace")  # workspace / new_workspace
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, index=True)
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    active = Column(Boolean, default=True)
    default_role = Column(String, default="user")
    last_used_at = Column(DateTime)


class Review(Base):
    """口碑评论 —— 模块二，规格 §4.2.1 的 20 字段。(platform, review_id) 唯一。"""

    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("platform", "review_id",
                                       name="uq_platform_review"),)

    id = Column(Integer, primary_key=True)
    review_id = Column(String, index=True)          # 评论唯一标识
    platform = Column(String, index=True)           # trustpilot / google_map / ...
    site = Column(String, index=True)               # aosom_us / aosom_de ...
    reviewer_name = Column(String)
    reviewer_country = Column(String)
    rating = Column(Integer)                        # 1-5 星
    title = Column(String)
    content = Column(Text)
    language = Column(String)
    review_date = Column(DateTime, index=True)
    purchase_date = Column(DateTime)
    reply_content = Column(Text)                    # 商家回复
    reply_date = Column(DateTime)
    sku = Column(String)                            # 关联 SKU（部分平台提供）
    product_url = Column(String)
    order_id = Column(String)
    is_verified = Column(Boolean)
    review_topics = Column(JSON)                    # 平台话题标签
    sentiment = Column(String)                      # NLP：positive/negative/neutral
    sentiment_score = Column(Float)                 # NLP：情感得分 -1.0~1.0
    category_l1 = Column(String)                    # NLP：一级分类
    category_l2 = Column(String)                    # NLP：二级标签
    nlp_topics = Column(JSON)                       # NLP：主题词
    analyzed_time = Column(DateTime)                # NLP 分析时间
    collected_time = Column(DateTime, default=datetime.utcnow)


class ApiKey(Base):
    """API 密钥 —— 供 AI Agent / 外部系统通过密钥调用数据输出 API。"""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True)
    name = Column(String)                            # 用途备注
    key_prefix = Column(String, index=True)          # sck_xxxx 前缀，展示用
    key_hash = Column(String, index=True)            # 完整 key 的 SHA-256
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used = Column(DateTime)
    request_count = Column(Integer, default=0)
    active = Column(Boolean, default=True)
    scopes = Column(JSON)                            # ["crawler:read", "crawler:scrape", ...]
    monthly_credit_quota = Column(Integer)           # null -> default free quota
    owner_user_id = Column(Integer, ForeignKey("users.id"), index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)


class Keyword(Base):
    """Google Shopping 关键词 —— 模块四，规格 §4.4.1。"""

    __tablename__ = "keywords"

    id = Column(Integer, primary_key=True)
    keyword = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_crawled = Column(DateTime)
    result_count = Column(Integer, default=0)


class ShoppingResult(Base):
    """Google Shopping 搜索结果商品 —— 模块四，规格 §4.4.4 的 15 字段。"""

    __tablename__ = "shopping_results"

    id = Column(Integer, primary_key=True)
    keyword = Column(String, index=True)
    position = Column(Integer)
    product_title = Column(String)
    product_image = Column(String)
    price = Column(Float)
    currency = Column(String)
    merchant = Column(String, index=True)
    merchant_url = Column(String)
    product_sku = Column(String)
    review_count = Column(Integer)
    rating = Column(Float)
    shipping_info = Column(String)
    promotion_label = Column(String)
    product_url = Column(String)
    crawled_time = Column(DateTime, default=datetime.utcnow, index=True)


class CrawlJob(Base):
    """采集任务 —— 同时充当采集队列（C-030 任务看板）。

    状态机：pending（入队）→ running（worker 领取）→ success / failed
    """

    __tablename__ = "crawl_jobs"

    id = Column(Integer, primary_key=True)
    site = Column(String, index=True)
    status = Column(String, default="pending", index=True)
    trigger = Column(String, default="manual")       # manual / scheduled
    requested_by_workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), index=True)
    assigned_node = Column(String, index=True)       # NAS 预分配的执行节点
    assigned_at = Column(DateTime)
    assigned_by = Column(String)
    worker = Column(String)                          # 领取该任务的 worker 标识
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    heartbeat_at = Column(DateTime, index=True)
    finished_at = Column(DateTime)
    products_count = Column(Integer, default=0)
    total_product_count = Column(Integer)
    new_count = Column(Integer, default=0)
    promotion_count = Column(Integer, default=0)
    success_rate = Column(Float)
    duration_sec = Column(Float)
    error = Column(Text)
    failure_code = Column(String, index=True)
    failure_stage = Column(String, index=True)
    failure_detail = Column(Text)
    retryable = Column(Boolean)
    suggested_action = Column(Text)


class CrawlUrl(Base):
    """URL Frontier —— 记录发现、抓取、解析生命周期，支撑增量和失败诊断。"""

    __tablename__ = "crawl_urls"
    __table_args__ = (UniqueConstraint("site", "url_hash",
                                       name="uq_crawl_url_site_hash"),)

    id = Column(Integer, primary_key=True)
    site = Column(String, index=True)
    url_hash = Column(String, index=True)
    url = Column(Text)
    kind = Column(String, index=True)                # sitemap/category/product/search
    source = Column(String, index=True)              # sitemap/robots/homepage/search/api
    status = Column(String, default="pending", index=True)
    http_status = Column(Integer)
    failure_code = Column(String, index=True)
    failure_stage = Column(String, index=True)
    failure_detail = Column(Text)
    retryable = Column(Boolean)
    attempts = Column(Integer, default=0)
    priority = Column(Integer, default=100, index=True)
    first_seen_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_fetched_at = Column(DateTime)
    next_retry_at = Column(DateTime, index=True)
    final_url = Column(Text)
    fetcher = Column(String)
    content_hash = Column(String, index=True)


class CrawlFailure(Base):
    """结构化失败事件 —— 让页面能展示失败原因分布和下一步动作。"""

    __tablename__ = "crawl_failures"

    id = Column(Integer, primary_key=True)
    site = Column(String, index=True)
    job_id = Column(Integer, ForeignKey("crawl_jobs.id"), index=True)
    url = Column(Text)
    stage = Column(String, index=True)
    code = Column(String, index=True)
    detail = Column(Text)
    retryable = Column(Boolean)
    suggested_action = Column(Text)
    http_status = Column(Integer)
    fetcher = Column(String)
    proxy_tier = Column(String)
    occurred_at = Column(DateTime, default=datetime.utcnow, index=True)


class ProxyHealth(Base):
    """代理健康状态 —— 持久化代理连通性和失败类型。

    健康度是 (proxy_hash, node) 的属性：同一 IP 在不同出口节点可用性不同。
    """

    __tablename__ = "proxy_health"
    __table_args__ = (
        UniqueConstraint("proxy_hash", "node", name="uq_proxy_health_hash_node"),
    )

    id = Column(Integer, primary_key=True)
    proxy_hash = Column(String, index=True)
    node = Column(String, index=True, default="nas")   # 出口节点：nas / US-macmini1 ...
    proxy_redacted = Column(String)
    tier = Column(String, index=True)
    status = Column(String, default="unknown", index=True)  # healthy/degraded/blocked/down
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    consecutive_failures = Column(Integer, default=0)
    last_success_at = Column(DateTime)
    last_failure_at = Column(DateTime)
    last_checked_at = Column(DateTime, index=True)
    last_failure_code = Column(String, index=True)
    last_failure_detail = Column(Text)
    blocked_until = Column(DateTime, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ProxyEndpoint(Base):
    """后台可控代理端点。文件代理只作为首次导入/兜底来源。"""

    __tablename__ = "proxy_endpoints"
    __table_args__ = (UniqueConstraint("proxy_hash", name="uq_proxy_endpoint_hash"),)

    id = Column(Integer, primary_key=True)
    name = Column(String)
    proxy_hash = Column(String, index=True)
    proxy_url = Column(Text)                         # 仅服务端使用,API 永不回传明文
    proxy_redacted = Column(String)
    endpoint_type = Column(String, default="datacenter", index=True)
    scheme = Column(String)
    host = Column(String, index=True)
    port = Column(Integer)
    provider = Column(String)
    country = Column(String, index=True)
    active = Column(Boolean, default=True, index=True)
    exclude_sites = Column(JSON)                     # ["amazon", "etsy"]
    tags = Column(JSON)
    max_concurrency = Column(Integer, default=1)
    source = Column(String, default="admin")         # admin/file/env
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ProxyLease(Base):
    """单次请求代理租约，控制同一出口 IP 的并发占用。"""

    __tablename__ = "proxy_leases"
    __table_args__ = (UniqueConstraint("lease_token", name="uq_proxy_lease_token"),)

    id = Column(Integer, primary_key=True)
    endpoint_id = Column(Integer, ForeignKey("proxy_endpoints.id"), index=True)
    site = Column(String, index=True)
    job_id = Column(Integer, ForeignKey("crawl_jobs.id"), index=True)
    worker = Column(String, index=True)
    lease_token = Column(String, index=True)
    expires_at = Column(DateTime, index=True)
    released_at = Column(DateTime, index=True)
    success = Column(Boolean)
    failure_code = Column(String, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ProxyPoolConfig(Base):
    """逻辑代理池。抓取策略引用 slug,成员可在后台调整。"""

    __tablename__ = "proxy_pools"

    id = Column(Integer, primary_key=True)
    slug = Column(String, unique=True, index=True)
    name = Column(String)
    pool_type = Column(String, default="datacenter", index=True)
    active = Column(Boolean, default=True, index=True)
    fallback_pool_slug = Column(String)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ProxyPoolMember(Base):
    """代理池成员关系,支持权重和优先级扩展。"""

    __tablename__ = "proxy_pool_members"
    __table_args__ = (UniqueConstraint("pool_id", "endpoint_id",
                                       name="uq_proxy_pool_endpoint"),)

    id = Column(Integer, primary_key=True)
    pool_id = Column(Integer, ForeignKey("proxy_pools.id"), index=True)
    endpoint_id = Column(Integer, ForeignKey("proxy_endpoints.id"), index=True)
    active = Column(Boolean, default=True, index=True)
    weight = Column(Integer, default=1)
    priority = Column(Integer, default=100, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ProxyRule(Base):
    """站点到代理策略的后台规则。用于逐步替代代码/配置里的固定 tier。"""

    __tablename__ = "proxy_rules"

    id = Column(Integer, primary_key=True)
    site_pattern = Column(String, index=True)         # exact/contains 由 match_type 控制
    match_type = Column(String, default="contains")  # exact / contains / prefix
    proxy_mode = Column(String, default="pool")      # none / datacenter / residential / pool
    pool_slug = Column(String, index=True)
    fallback_pool_slug = Column(String)
    priority = Column(Integer, default=100, index=True)
    enabled = Column(Boolean, default=True, index=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class OnDemandJob(Base):
    """按需抓取任务记录 —— 每次 fetch(url) 一条。

    摘要入库;详情(listing/评论)按 item_skus 现查 Product/Review。
    status: queued / running / success / partial / failed。
    批量上传时同批共享 batch_id;失败重试原地复用本行(status 回 queued)。
    """

    __tablename__ = "ondemand_jobs"

    id = Column(Integer, primary_key=True)
    url = Column(Text)
    platform = Column(String, index=True)            # mercadolibre / lazada / shopee
    kind = Column(String)                            # product / listing
    listing_count = Column(Integer, default=0)
    review_count = Column(Integer, default=0)
    status = Column(String, index=True)              # queued/running/success/partial/failed
    notes = Column(JSON)                             # res.notes(失败原因/截断)
    item_skus = Column(JSON)                         # 本次抓到的 sku 列表
    batch_id = Column(String, index=True)            # 同批共享;单条抓取也分配一个
    max_items = Column(Integer, default=100)         # 原始抓取参数(重试复跑用)
    review_limit = Column(Integer, default=100)      # 原始抓取参数(重试复跑用)
    attempts = Column(Integer, default=0)            # 执行次数,worker 每跑一次 +1
    error = Column(Text)                             # 最后一次失败的简短原因
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    created_by = Column(String)                      # 发起用户 username
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    finished_at = Column(DateTime)                   # 进入终态(success/partial/failed)的时间


class Usage(Base):
    """按 record 计费 · 记录每个 API key 的调用量。

    用于：
    · 海尔大数据湖项目 · 资源池按订单付费对接
    · API key 维度月度账单（$1.5 / 1k records 基础档）
    · 按 endpoint 分组用量统计（/api/sites, /mcp/, /api/export/products...）
    """

    __tablename__ = "usage_records"

    id = Column(Integer, primary_key=True)
    api_key_id = Column(Integer, ForeignKey("api_keys.id"), index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    endpoint = Column(String, index=True)            # /api/sites, /mcp/, /api/export/products...
    record_count = Column(Integer, default=0)        # 该次调用返回的 records 数
    credits_used = Column(Integer, default=0)        # 该次调用消耗的 credits
    bytes_returned = Column(Integer, default=0)      # 返回字节数（用于带宽计费选项）
    duration_ms = Column(Integer)                    # 调用耗时（用于 SLA 监控）
    api_calls = Column(Integer, nullable=False, default=0)        # 成功的 HTTP 请求次数
    browser_opens = Column(Integer, nullable=False, default=0)    # 成功的浏览器渲染次数
    pages_fetched = Column(Integer, nullable=False, default=0)    # 成功抓取页面数 = 前两者之和
    occurred_at = Column(DateTime, default=datetime.utcnow, index=True)


class RateLimitEvent(Base):
    """持久化限流事件。

    用于 NAS / 多 worker 部署下共享 v2 API 限流窗口。默认保留短窗口数据，
    调用时顺手清理过期记录。
    """

    __tablename__ = "rate_limit_events"

    id = Column(Integer, primary_key=True)
    bucket_key = Column(String, index=True)
    path = Column(String, index=True)
    occurred_at = Column(DateTime, default=datetime.utcnow, index=True)


class AgentCache(Base):
    """Short-lived Agent memory for MCP / v2 crawler calls."""

    __tablename__ = "agent_cache"

    id = Column(Integer, primary_key=True)
    agent_key = Column(String, index=True)
    tool = Column(String, index=True)
    cache_key = Column(String, index=True)
    response = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, index=True)


class RawSnapshot(Base):
    """Raw 层 —— 原始抓取的元数据;正文 gzip 在磁盘(snapshot.py)。"""

    __tablename__ = "raw_snapshots"

    id = Column(Integer, primary_key=True)
    url = Column(Text, index=True)
    canonical_url = Column(Text, index=True)
    content_hash = Column(String, index=True)        # sha256(正文)
    fetched_at = Column(DateTime, index=True, default=datetime.utcnow)
    status_code = Column(Integer)
    etag = Column(String)
    last_modified = Column(String)
    content_type = Column(String)
    body_path = Column(String)                        # data/snapshots/*.gz
    fetch_mode = Column(String)                       # live / advanced
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Dataset(Base):
    """View 层入口 —— 命名数据集。"""

    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("workspace_id", "slug",
                                       name="uq_dataset_ws_slug"),)

    id = Column(Integer, primary_key=True)
    name = Column(String, index=True)
    slug = Column(String, index=True)
    entity_type = Column(String)                      # 默认实体类型
    description = Column(Text)
    source_kind = Column(String)                      # custom_url / ecommerce_template
    freshness_ttl_sec = Column(Integer, default=86400)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    created_by = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class ExtractedRecord(Base):
    """Normalized 层 —— 任意 schema 的结构化结果 + 完整 provenance。"""

    __tablename__ = "extracted_records"
    __table_args__ = (UniqueConstraint("dataset_id", "record_key",
                                       name="uq_record_dataset_key"),)

    id = Column(Integer, primary_key=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), index=True)
    snapshot_id = Column(Integer, ForeignKey("raw_snapshots.id"), nullable=True)
    source_url = Column(Text, index=True)
    canonical_url = Column(Text, index=True)
    entity_type = Column(String, index=True)
    data = Column(JSON)
    record_key = Column(String, index=True)
    content_hash = Column(String)                     # sha256(规整 data)
    confidence = Column(Float)
    extraction_method = Column(String)
    recipe_id = Column(Integer, nullable=True)        # SP3 用
    quality_status = Column(String, index=True)       # main / staging / quarantine
    fetched_at = Column(DateTime, index=True)
    extracted_at = Column(DateTime, default=datetime.utcnow)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)


class SpineJob(Base):
    """Spine 异步抓取队列 —— 任意 URL 入队,worker 消费走 spine.resolve 落库。

    状态机:pending(入队/待重试)→ running(worker 领取)→ success / failed
    与电商 crawl_jobs 完全独立。
    """

    __tablename__ = "spine_jobs"

    id = Column(Integer, primary_key=True)
    url = Column(Text)
    dataset = Column(String, index=True)  # dataset 名(slug),worker 消费时 get_or_create,非外键
    entity_type = Column(String, default="generic")
    save_policy = Column(String, default="promote_if_valid")  # promote_if_valid / main / staging / quarantine
    force_live = Column(Boolean, default=False)
    status = Column(String, default="pending", index=True)  # pending / running / success / failed
    retries = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    next_attempt_at = Column(DateTime, index=True, default=datetime.utcnow)  # 重试退避到期时间,claim 只领 <= now 的
    worker = Column(String)
    result_record_id = Column(Integer, nullable=True)
    error = Column(Text)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    api_key_id = Column(Integer, ForeignKey("api_keys.id"), index=True, nullable=True)  # 计费归属,enqueue 持久化
    heartbeat_at = Column(DateTime, index=True, nullable=True)  # worker 续约时间戳,reclaim 判据
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)


class WebhookConfig(Base):
    """Workspace webhook 配置。当前工作区内的任务事件会投递到这里。"""

    __tablename__ = "webhook_configs"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"),
                          unique=True, index=True)
    url = Column(String, nullable=False)
    secret = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class WebhookDelivery(Base):
    """一次 webhook 投递记录。任务路径只登记记录，后台循环负责发送。"""

    __tablename__ = "webhook_deliveries"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    config_id = Column(Integer, ForeignKey("webhook_configs.id"), index=True)
    event_type = Column(String, index=True)
    job_kind = Column(String, index=True)
    job_id = Column(Integer, index=True)
    payload = Column(JSON)
    status = Column(String, default="pending", index=True)
    retries = Column(Integer, default=0)
    max_retries = Column(Integer, default=5)
    next_retry_at = Column(DateTime, index=True)
    http_status = Column(Integer)
    response_snippet = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    finished_at = Column(DateTime)


class AmazonVocJobFields:
    id = Column(Integer, primary_key=True)
    task_id = Column(String, unique=True, index=True)
    tenant_id = Column(String, index=True)
    app_id = Column(String, index=True)
    req_ssn = Column(String, index=True)
    job_type = Column(String, index=True)            # AmazonReviewJob / AmazonListingJob
    market = Column(String, index=True)
    asin = Column(String, index=True)
    priority = Column(Integer, default=100, index=True)
    priority_label = Column(String)
    biz_source = Column(String, index=True)
    sla = Column(Integer)
    payload = Column(JSON)
    raw_request = Column(JSON)
    callback_url = Column(Text)
    callback_status = Column(String, default="none", index=True)  # none/pending/success/failed
    callback_attempts = Column(Integer, default=0)
    callback_last_error = Column(Text)
    callback_updated_at = Column(DateTime)
    status = Column(String, default="queued", index=True)  # queued/running/completed/failed/partial
    worker = Column(String)
    result_count = Column(Integer, default=0)
    result_data = Column(JSON)
    result_url = Column(Text)
    oss_object_key = Column(String)
    snapshot_html = Column(Text)
    snapshot_url = Column(Text)
    snapshot_object_key = Column(String)
    error_msg = Column(Text)
    fail_reason = Column(String, index=True)
    retries = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    next_attempt_at = Column(DateTime, default=datetime.utcnow, index=True)
    sla_deadline = Column(DateTime, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    started_at = Column(DateTime)
    heartbeat_at = Column(DateTime, index=True)
    completed_at = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.utcnow)


class AmazonVocJob(AmazonVocJobFields, Base):
    """Legacy generic Amazon VOC task table.

    New tasks are written to the type-specific tables below. This model remains
    mapped so old rows can still be queried or drained during migration.
    """

    __tablename__ = "amazon_voc_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "app_id", "req_ssn", name="uq_amazon_voc_req"),
        Index("ix_amazon_voc_claim", "status", "next_attempt_at", "priority", "id"),
    )


class AmazonReviewJob(AmazonVocJobFields, Base):
    """Amazon review crawl task table."""

    __tablename__ = "amazon_review_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "app_id", "req_ssn", name="uq_amazon_review_req"),
        Index("ix_amazon_review_claim", "status", "next_attempt_at", "priority", "id"),
    )


class AmazonListingJob(AmazonVocJobFields, Base):
    """Amazon listing/detail crawl task table."""

    __tablename__ = "amazon_listing_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "app_id", "req_ssn", name="uq_amazon_listing_req"),
        Index("ix_amazon_listing_claim", "status", "next_attempt_at", "priority", "id"),
    )


class AmazonJobIndex(Base):
    """Routing index for Amazon jobs stored in type-specific tables."""

    __tablename__ = "amazon_job_index"
    __table_args__ = (
        UniqueConstraint("tenant_id", "app_id", "req_ssn", "job_type", name="uq_amazon_job_index_req_type"),
        Index("ix_amazon_job_index_job_ref", "job_type", "job_pk"),
    )

    id = Column(Integer, primary_key=True)
    task_id = Column(String, unique=True, index=True, nullable=False)
    tenant_id = Column(String, index=True, nullable=False)
    app_id = Column(String, index=True, nullable=False)
    req_ssn = Column(String, index=True, nullable=False)
    job_type = Column(String, index=True, nullable=False)
    job_pk = Column(Integer, nullable=False)
    table_name = Column(String, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


class AmazonCrawlerAccount(Base):
    """Amazon crawler account/proxy slot stored in Postgres.

    The shape intentionally mirrors the legacy crawler_accounts table so the
    migrated Amazon crawler can keep the old account scheduling behavior.
    """

    __tablename__ = "amazon_crawler_accounts"
    __table_args__ = (
        UniqueConstraint("platform", "username", name="uq_amazon_account_platform_username"),
        Index("idx_amazon_account_platform_state_country", "platform", "state", "country"),
    )

    id = Column(Integer, primary_key=True)
    platform = Column(String, default="amazon", index=True)
    username = Column(String, index=True)
    password = Column(Text)
    country = Column(String, index=True)
    cookies = Column(JSON)
    proxy_config = Column("proxy_", JSON)          # legacy Account.proxy_
    fingerprint_id = Column(String, index=True)
    static_ip = Column(String, index=True)           # listing/detail static proxy URL
    totp_secret = Column(String)
    state = Column(Integer, default=1, index=True)  # 1=enabled, 0=disabled
    is_used = Column(Boolean, default=False, index=True)
    last_used_time = Column(Float, default=0.0, index=True)
    fail_count = Column(Integer, default=0, index=True)
    cooldown_until = Column(Float, default=0.0, index=True)
    city = Column(String)
    user_agent = Column(Text)
    refresh_time = Column(String)
    create_time = Column(String, default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    update_time = Column(String, default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    quota_factor = Column(Float, default=1.0)
    label = Column(String, index=True)


class AmazonCrawlerAccountImportJob(Base):
    """Async account import job consumed by remote BitBrowser nodes."""

    __tablename__ = "amazon_crawler_account_import_jobs"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_amazon_account_import_job_id"),
        Index("idx_amazon_account_import_status", "status", "created_at"),
        Index("idx_amazon_account_import_node", "node_id", "status"),
    )

    id = Column(BIGINT_PK, primary_key=True)
    job_id = Column(String(64), nullable=False, index=True)
    status = Column(Integer, nullable=False, default=0)  # 0=pending 1=running 2=done 3=failed
    node_id = Column(String(128), nullable=False, default="")
    account_type = Column(String(10), nullable=False, default="")
    target_country = Column(String(10), nullable=False, default="")
    proxy_strategy = Column(String(24), nullable=False, default="")
    static_ip_count = Column(Integer, nullable=False, default=0)
    static_ip_pool = Column(JSON)
    limit_count = Column(Integer, nullable=False, default=0)
    source_rows = Column(Integer, nullable=False, default=0)
    queued_rows = Column(Integer, nullable=False, default=0)
    attempted_rows = Column(Integer, nullable=False, default=0)
    success_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    existing_browser_count = Column(Integer, nullable=False, default=0)
    file_proxy_count = Column(Integer, nullable=False, default=0)
    created_usernames = Column(JSON)
    failed_items = Column(JSON)
    error_msg = Column(Text)
    created_by = Column(String(64), nullable=False, default="admin")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)


class AmazonCrawlerAccountImportItem(Base):
    """Single Excel row belonging to an account import job."""

    __tablename__ = "amazon_crawler_account_import_items"
    __table_args__ = (
        Index("idx_amazon_account_import_item_job", "job_id", "id"),
        Index("idx_amazon_account_import_item_status", "job_id", "status"),
    )

    id = Column(BIGINT_PK, primary_key=True)
    job_id = Column(String(64), nullable=False, index=True)
    row_no = Column(Integer, nullable=False, default=0)
    username = Column(String(128), nullable=False, default="")
    password = Column(String(256), nullable=False, default="")
    totp_secret = Column(String(256), nullable=False, default="")
    country = Column(String(10), nullable=False, default="")
    browser_id = Column(String(128), nullable=False, default="")
    had_browser_id = Column(Boolean, nullable=False, default=False)
    proxy = Column(Text)
    status = Column(Integer, nullable=False, default=0)  # 0=pending 1=running 2=done 3=failed
    node_id = Column(String(128), nullable=False, default="")
    error_msg = Column(String(512), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class CrawlerEventLog(Base):
    """Amazon crawler structured event log.

    Ported from the legacy crawler_event_log table. Worker code emits these
    events through Redis; the Amazon daemon consumes them into Postgres for
    ban analysis and account daily aggregation.
    """

    __tablename__ = "crawler_event_log"
    __table_args__ = (
        Index("idx_el_event_type", "event_type"),
        Index("idx_el_username", "username"),
        Index("idx_el_asin", "asin"),
        Index("idx_el_country", "country"),
        Index("idx_el_created_at", "created_at"),
        Index("idx_el_username_date", "username", "created_at"),
    )

    id = Column(BIGINT_PK, primary_key=True)
    event_type = Column(String(32), nullable=False, default="")
    username = Column(String(64), nullable=False, default="")
    asin = Column(String(32), nullable=False, default="")
    country = Column(String(10), nullable=False, default="")
    page = Column(Integer, nullable=False, default=0)
    http_status = Column(Integer, nullable=False, default=0)
    daily_pages = Column(Integer, nullable=False, default=0)
    session_seq = Column(Integer, nullable=False, default=0)
    worker_id = Column(String(128), nullable=False, default="")
    proxy = Column(String(512), nullable=False, default="")
    error_msg = Column(Text)
    extra = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class AccountUsageLog(Base):
    """Per-task account usage log, kept for daily aggregation."""

    __tablename__ = "account_usage_log"
    __table_args__ = (
        Index("idx_ul_username", "username"),
        Index("idx_ul_asin", "asin"),
        Index("idx_ul_country", "country"),
        Index("idx_ul_success", "success"),
        Index("idx_ul_created_at", "created_at"),
        Index("idx_ul_username_created", "username", "created_at"),
    )

    id = Column(BIGINT_PK, primary_key=True)
    task_id = Column(String(64), nullable=False, default="")
    asin = Column(String(32), nullable=False, default="")
    country = Column(String(10), nullable=False, default="")
    username = Column(String(64), nullable=False, default="")
    success = Column(Boolean, nullable=False, default=False)
    review_count = Column(Integer, nullable=False, default=0)
    expected_count = Column(Integer, nullable=False, default=0)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    duration_seconds = Column(Integer, nullable=False, default=0)
    retry_count = Column(Integer, nullable=False, default=0)
    error_msg = Column(Text)
    worker_id = Column(String(128), default="")
    ip = Column(String(128), default="")
    task_type = Column(String(32), default="review")
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)


class ReviewsError(Base):
    """Raw review parsing/fetch error material for later manual diagnosis."""

    __tablename__ = "reviews_error"
    __table_args__ = (
        Index("idx_reviews_error_asin", "asin"),
        Index("idx_reviews_error_status", "status"),
        Index("idx_reviews_error_created_at", "created_at"),
    )

    id = Column(BIGINT_PK, primary_key=True)
    asin = Column(String(32), nullable=False, default="")
    country = Column(String(10), nullable=False, default="")
    resp = Column(Text, nullable=False, default="")
    review_data = Column(JSON)
    task_info = Column(JSON)
    error_msg = Column(Text)
    status = Column(Integer, default=0, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)


class AccountDailySummary(Base):
    """Daily account aggregate used by long-term risk scoring."""

    __tablename__ = "account_daily_summary"
    __table_args__ = (
        UniqueConstraint("username", "date", "country", name="uq_account_daily_user_date_country"),
        Index("idx_ads_date", "date"),
        Index("idx_ads_country", "country"),
        Index("idx_ads_error_rate", "error_rate"),
    )

    id = Column(BIGINT_PK, primary_key=True)
    username = Column(String(64), nullable=False)
    date = Column(Date, nullable=False)
    country = Column(String(10), nullable=False, default="")
    total_tasks = Column(Integer, nullable=False, default=0)
    success_tasks = Column(Integer, nullable=False, default=0)
    failed_tasks = Column(Integer, nullable=False, default=0)
    total_pages = Column(Integer, nullable=False, default=0)
    total_reviews = Column(Integer, nullable=False, default=0)
    captcha_count = Column(Integer, nullable=False, default=0)
    ban_count = Column(Integer, nullable=False, default=0)
    login_redirect_count = Column(Integer, nullable=False, default=0)
    robot_check_count = Column(Integer, nullable=False, default=0)
    proxy_rotate_count = Column(Integer, nullable=False, default=0)
    avg_duration_seconds = Column(Float, nullable=False, default=0.0)
    total_duration_seconds = Column(Integer, nullable=False, default=0)
    session_count = Column(Integer, nullable=False, default=0)
    active_hour_distribution = Column(JSON)
    request_interval_stddev = Column(Float)
    last_fresh_login_at = Column(DateTime)
    cookie_age_hours = Column(Float)
    distinct_ips = Column(Integer, nullable=False, default=0)
    distinct_asins = Column(Integer, nullable=False, default=0)
    error_rate = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AccountRiskProfile(Base):
    """Long-term account risk profile generated from AccountDailySummary."""

    __tablename__ = "account_risk_profile"
    __table_args__ = (
        UniqueConstraint("username", "country", name="uq_account_risk_user_country"),
        Index("idx_arp_risk_level", "risk_level"),
        Index("idx_arp_risk_score", "risk_score"),
    )

    id = Column(BIGINT_PK, primary_key=True)
    username = Column(String(64), nullable=False)
    country = Column(String(10), nullable=False, default="")
    risk_score = Column(Float, nullable=False, default=0.0)
    risk_level = Column(String(16), nullable=False, default="low")
    avg_daily_error_rate_7d = Column(Float, default=0.0)
    avg_daily_ban_count_7d = Column(Float, default=0.0)
    avg_daily_captcha_count_7d = Column(Float, default=0.0)
    total_days_active_30d = Column(Integer, default=0)
    trend_direction = Column(String(16), default="stable")
    recommended_daily_budget = Column(Integer)
    recommended_page_budget = Column(Integer)
    recommended_rest_minutes = Column(Integer)
    last_analyzed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AccountIpLog(Base):
    """Account to proxy/IP usage history."""

    __tablename__ = "account_ip_log"
    __table_args__ = (
        UniqueConstraint("username", "ip", name="uq_account_ip_user_ip"),
        Index("idx_ail_ip", "ip"),
        Index("idx_ail_last_seen", "last_seen_at"),
    )

    id = Column(BIGINT_PK, primary_key=True)
    username = Column(String(64), nullable=False)
    ip = Column(String(512), nullable=False)
    country = Column(String(10), nullable=False, default="")
    first_seen_at = Column(DateTime, nullable=False)
    last_seen_at = Column(DateTime, nullable=False)
    request_count = Column(Integer, nullable=False, default=1)


class CrawlerQueueDepthSnapshot(Base):
    """Redis queue depth samples for Grafana/ops checks."""

    __tablename__ = "crawler_queue_depth_snapshot"
    __table_args__ = (
        Index("idx_qds_queue_time", "queue_name", "created_at"),
        Index("idx_qds_created_at", "created_at"),
    )

    id = Column(BIGINT_PK, primary_key=True)
    queue_name = Column(String(64), nullable=False)
    redis_key = Column(String(256), nullable=False, default="")
    depth = Column(Integer, nullable=False, default=-1)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class CrawlerRuntimeStatus(Base):
    """Latest heartbeat/status row per crawler component."""

    __tablename__ = "crawler_runtime_status"

    component = Column(String(128), primary_key=True)
    status = Column(String(16), nullable=False, default="ok")
    message = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class AdminAuditLog(Base):
    """超管后台写操作审计 —— 谁在何时对什么做了什么。"""

    __tablename__ = "admin_audit_logs"

    id = Column(Integer, primary_key=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=True)
    actor_name = Column(String, index=True)
    action = Column(String, index=True)
    target_type = Column(String, index=True)
    target_id = Column(String, index=True)
    detail = Column(JSON)
    ip = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
