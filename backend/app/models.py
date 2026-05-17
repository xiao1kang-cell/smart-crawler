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


class User(Base):
    """后台账号 —— 登录鉴权。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)
    role = Column(String, default="admin")          # admin / viewer
    display_name = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime)


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


class CrawlJob(Base):
    """采集任务 —— 同时充当采集队列（C-030 任务看板）。

    状态机：pending（入队）→ running（worker 领取）→ success / failed
    """

    __tablename__ = "crawl_jobs"

    id = Column(Integer, primary_key=True)
    site = Column(String, index=True)
    status = Column(String, default="pending", index=True)
    trigger = Column(String, default="manual")       # manual / scheduled
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
