"""数据模型 —— 对齐《需求规格说明书》§4 字段规格。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from .db import Base


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
    worker = Column(String)                          # 领取该任务的 worker 标识
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    products_count = Column(Integer, default=0)
    new_count = Column(Integer, default=0)
    promotion_count = Column(Integer, default=0)
    success_rate = Column(Float)
    duration_sec = Column(Float)
    error = Column(Text)


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
