"""REST API —— 满足规格 §10 数据接口需求（API-001 ~ API-008）。"""
from __future__ import annotations

import io
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..apikey import generate as gen_key, hash_key, short as key_short
from ..auth import make_token, verify_password, verify_token
from ..db import get_db
from ..export import export_workbook
from ..models import (ApiKey, Category, CrawlJob, Keyword, PriceHistory,
                      Product, Promotion, Review, ShoppingResult, Site,
                      Trend, User)
from ..proxy import pool_status
from ..runner import enqueue


# ---------- 鉴权依赖：接受 Bearer Token 或 X-API-Key ----------
def require_user(authorization: str = Header(default=""),
                 x_api_key: str = Header(default="", alias="X-API-Key"),
                 db: Session = Depends(get_db)) -> str:
    """校验登录 Token 或 API 密钥，返回调用者标识；失败 401。"""
    token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    username = verify_token(token)
    if username:
        return username
    if x_api_key:
        k = (db.query(ApiKey)
             .filter(ApiKey.key_hash == hash_key(x_api_key),
                     ApiKey.active.is_(True)).first())
        if k:
            k.last_used = datetime.utcnow()
            k.request_count = (k.request_count or 0) + 1
            db.commit()
            return f"apikey:{k.name}"
    raise HTTPException(401, "未登录或 API 密钥无效")


# 公开路由（登录，不需鉴权）
public_router = APIRouter(prefix="/api")
# 数据路由（全部需登录）
router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


@public_router.post("/login")
def login(payload: dict, db: Session = Depends(get_db)):
    """账号登录 —— 返回 Token。"""
    username = (payload or {}).get("username", "").strip()
    password = (payload or {}).get("password", "")
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(401, "账号或密码错误")
    user.last_login = datetime.utcnow()
    db.commit()
    return {"token": make_token(username), "username": username,
            "display_name": user.display_name, "role": user.role}


@router.get("/me")
def me(user: str = Depends(require_user), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.username == user).first()
    return {"username": user, "display_name": u.display_name if u else user,
            "role": u.role if u else "viewer"}


# ---------- 序列化 ----------
def site_dict(s: Site) -> dict:
    return {"site": s.site, "brand": s.brand, "country": s.country,
            "url": s.url, "platform": s.platform, "proxy_tier": s.proxy_tier,
            "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None}


def product_dict(p: Product) -> dict:
    return {
        "id": p.id, "sku": p.sku, "spu": p.spu, "title": p.title,
        "image": (p.image_urls or [None])[0], "image_urls": p.image_urls,
        "category_path": p.category_path, "sale_price": p.sale_price,
        "original_price": p.original_price, "currency": p.currency,
        "attributes": p.attributes, "ratings": p.ratings,
        "review_count": p.review_count, "thirty_day_sales": p.thirty_day_sales,
        "thirty_day_revenue": p.thirty_day_revenue, "status": p.status,
        "inventory": p.inventory, "has_video": p.has_video,
        "has_free_shipping": p.has_free_shipping, "label": p.label,
        "tags": p.tags, "product_url": p.product_url,
        "product_type": p.product_type, "is_new": p.is_new,
        "is_bestseller": p.is_bestseller,
        "created_time": p.created_time.isoformat() if p.created_time else None,
        "updated_time": p.updated_time.isoformat() if p.updated_time else None,
        "site": p.site, "brand": p.brand,
    }


# ---------- 站点概览（R-001 / R-002 / §14.2）----------
@router.get("/sites")
def list_sites(db: Session = Depends(get_db)):
    out = []
    for s in db.query(Site).all():
        d = site_dict(s)
        d["sku_count"] = db.query(Product).filter(Product.site == s.site).count()
        d["spu_count"] = (db.query(Product.spu)
                          .filter(Product.site == s.site).distinct().count())
        d["category_count"] = db.query(Category).filter(
            Category.site == s.site).count()
        d["promotion_count"] = db.query(Promotion).filter(
            Promotion.site == s.site).count()
        out.append(d)
    return out


@router.get("/sites/{site}/overview")
def site_overview(site: str, db: Session = Depends(get_db)):
    """6 个指标卡 + 趋势序列。"""
    if not db.query(Site).filter(Site.site == site).first():
        raise HTTPException(404, "站点不存在")
    sku_count = db.query(Product).filter(Product.site == site).count()
    new_count = db.query(Product).filter(
        Product.site == site, Product.is_new.is_(True)).count()
    sales, revenue = db.query(
        func.coalesce(func.sum(Product.thirty_day_sales), 0),
        func.coalesce(func.sum(Product.thirty_day_revenue), 0.0),
    ).filter(Product.site == site).first()
    trends = [{"date": t.date.isoformat(), "sku_count": t.sku_count,
               "new_product_count": t.new_product_count,
               "estimated_sales": t.estimated_sales,
               "estimated_revenue": t.estimated_revenue}
              for t in db.query(Trend).filter(Trend.site == site)
              .order_by(Trend.date).all()]
    return {
        "cards": {
            "sku_count": sku_count, "new_product_count": new_count,
            "thirty_day_sales": int(sales or 0),
            "thirty_day_revenue": round(revenue or 0, 2),
            "traffic": None, "conversion_rate": None,
        },
        "trends": trends,
    }


# ---------- 商品分析（R-010 / §14.3 / API-002）----------
@router.get("/products")
def list_products(
    site: str | None = None,
    tab: str = Query("all", pattern="^(all|bestseller|new)$"),
    search: str | None = None,
    status: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
):
    q = db.query(Product)
    if site:
        q = q.filter(Product.site == site)
    if tab == "bestseller":
        q = q.filter(Product.is_bestseller.is_(True))
    elif tab == "new":
        q = q.filter(Product.is_new.is_(True))
    if search:
        like = f"%{search}%"
        q = q.filter((Product.title.ilike(like)) | (Product.sku.ilike(like)))
    if status:
        q = q.filter(Product.status == status)
    if min_price is not None:
        q = q.filter(Product.sale_price >= min_price)
    if max_price is not None:
        q = q.filter(Product.sale_price <= max_price)
    total = q.count()
    rows = (q.order_by(Product.id)
            .offset((page - 1) * page_size).limit(page_size).all())
    return {"total": total, "page": page, "page_size": page_size,
            "items": [product_dict(p) for p in rows]}


@router.get("/products/{pid}")
def get_product(pid: int, db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "商品不存在")
    return product_dict(p)


@router.get("/products/{pid}/price-history")
def price_history(pid: int, db: Session = Depends(get_db)):
    """单 SKU 价格曲线 —— R-012。"""
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "商品不存在")
    rows = (db.query(PriceHistory)
            .filter(PriceHistory.site == p.site, PriceHistory.sku == p.sku)
            .order_by(PriceHistory.date).all())
    return [{"date": r.date.isoformat(), "sale_price": r.sale_price,
             "original_price": r.original_price,
             "review_count": r.review_count} for r in rows]


# ---------- 促销分析（§14.4 / API-005）----------
@router.get("/promotions")
def list_promotions(site: str | None = None, page: int = 1,
                    page_size: int = 50, db: Session = Depends(get_db)):
    q = db.query(Promotion)
    if site:
        q = q.filter(Promotion.site == site)
    total = q.count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [{
        "id": r.id, "sku": r.sku, "site": r.site,
        "promotion_type": r.promotion_type, "promotion_name": r.promotion_name,
        "original_price": r.original_price, "promotion_price": r.promotion_price,
        "discount_percent": r.discount_percent, "threshold": r.threshold,
        "product_title": r.product_title, "product_image": r.product_image,
        "detected_time": r.detected_time.isoformat() if r.detected_time else None,
    } for r in rows]}


# ---------- 趋势 / 分类（API-004）----------
@router.get("/trends")
def list_trends(site: str, db: Session = Depends(get_db)):
    return [{"date": t.date.isoformat(), "sku_count": t.sku_count,
             "new_product_count": t.new_product_count,
             "estimated_sales": t.estimated_sales,
             "estimated_revenue": t.estimated_revenue}
            for t in db.query(Trend).filter(Trend.site == site)
            .order_by(Trend.date).all()]


@router.get("/categories")
def list_categories(site: str, db: Session = Depends(get_db)):
    rows = db.query(Category).filter(Category.site == site).all()
    return [{"category_id": c.category_id, "name": c.category_name,
             "url": c.category_url, "level": c.level,
             "product_count": c.product_count} for c in rows]


# ---------- 采集任务看板（C-030 / C-003）----------
@router.get("/jobs")
def list_jobs(limit: int = 30, db: Session = Depends(get_db)):
    rows = db.query(CrawlJob).order_by(CrawlJob.id.desc()).limit(limit).all()
    return [{
        "id": j.id, "site": j.site, "status": j.status,
        "products_count": j.products_count, "new_count": j.new_count,
        "promotion_count": j.promotion_count, "success_rate": j.success_rate,
        "duration_sec": round(j.duration_sec, 1) if j.duration_sec else None,
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        "error": j.error,
    } for j in rows]


@router.post("/jobs/trigger")
def trigger(site: str | None = None, brand: str | None = None,
            db: Session = Depends(get_db)):
    """手动触发采集 —— C-003。入队任务，由 worker 执行。"""
    if not site and not brand:
        raise HTTPException(400, "需指定 site 或 brand")
    if brand:
        names = [r.site for r in db.query(Site).filter(Site.brand == brand)]
        if not names:
            raise HTTPException(404, "品牌不存在")
    else:
        if not db.query(Site).filter(Site.site == site).first():
            raise HTTPException(404, "站点不存在")
        names = [site]
    job_ids = [enqueue(n, trigger="manual") for n in names]
    return {"status": "queued", "jobs": job_ids, "count": len(job_ids),
            "queued_at": datetime.utcnow().isoformat()}


_PLATFORM_METHOD = {
    "shopify": "Shopify /products.json 接口直拉，无需浏览器",
    "vue_spa": "Vue SPA /api/* JSON 接口直连",
    "nuxt": "Nuxt SSR：sitemap + 商品页 JSON-LD 解析",
    "generic": "sitemap 发现 + JSON-LD/OpenGraph 多策略解析",
    "flexispot": "Playwright 取会话 token → /sapi 接口批量调",
    "vidaxl": "官方 Dropshipping API / sitemap + JSON-LD（欧洲站）",
    "vonhaus": "sitemap 扫描判别 + OpenGraph meta 解析",
}
_REVIEW_METHOD = {
    "trustpilot": "Scrapling 隐身浏览器突破 WAF + __NEXT_DATA__ 解析",
    "reviews_io": "Reviews.io 公开商家 API 直连",
    "google_map": "Scrapling 渲染商家页 + 滚动加载评论",
}


@router.get("/datasources")
def datasources(db: Session = Depends(get_db)):
    """数据源总览 —— 每个源的平台/获取方式/状态/计数（看板「数据源」Tab）。"""
    out = []
    for s in db.query(Site).all():
        sku = db.query(Product).filter(Product.site == s.site).count()
        out.append({
            "type": "product", "id": s.site,
            "name": f"{s.brand} · {s.country}", "platform": s.platform,
            "method": _PLATFORM_METHOD.get(s.platform, "—"),
            "count": sku, "unit": "SKU",
            "status": "online" if sku > 0 else "idle",
            "freq": "每日 02:00",
            "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None,
            "url": s.url,
        })
    for plat, method in _REVIEW_METHOD.items():
        n = db.query(Review).filter(Review.platform == plat).count()
        out.append({
            "type": "review", "id": f"review_{plat}",
            "name": {"trustpilot": "Trustpilot", "reviews_io": "Reviews.io",
                     "google_map": "Google Maps"}[plat],
            "platform": plat, "method": method, "count": n, "unit": "评论",
            "status": "online" if n > 0 else "idle", "freq": "每周一",
            "last_crawled": None, "url": None,
        })
    sr = db.query(ShoppingResult).count()
    out.append({
        "type": "shopping", "id": "google_shopping", "name": "Google Shopping",
        "platform": "google_shopping",
        "method": "Scrapling 渲染 udm=28 购物结果页",
        "count": sr, "unit": "结果", "status": "online" if sr > 0 else "idle",
        "freq": "每周一", "last_crawled": None, "url": None,
    })
    return out


@router.get("/proxy-status")
def proxy_status():
    """代理池状态 —— C-010。"""
    return pool_status()


# ---------- API 密钥管理（仅登录用户，供 Agent 接入数据 API）----------
@router.get("/keys")
def list_keys(user: str = Depends(require_user), db: Session = Depends(get_db)):
    if user.startswith("apikey:"):
        raise HTTPException(403, "API 密钥不能管理密钥")
    rows = db.query(ApiKey).order_by(ApiKey.id.desc()).all()
    return [{
        "id": k.id, "name": k.name, "key_prefix": k.key_prefix + "…",
        "active": k.active, "request_count": k.request_count,
        "created_at": k.created_at.isoformat() if k.created_at else None,
        "last_used": k.last_used.isoformat() if k.last_used else None,
    } for k in rows]


@router.post("/keys")
def create_key(payload: dict, user: str = Depends(require_user),
               db: Session = Depends(get_db)):
    """新建 API 密钥 —— 明文仅此一次返回。"""
    if user.startswith("apikey:"):
        raise HTTPException(403, "API 密钥不能管理密钥")
    raw = gen_key()
    k = ApiKey(name=(payload or {}).get("name") or "未命名",
               key_prefix=key_short(raw), key_hash=hash_key(raw))
    db.add(k)
    db.commit()
    return {"id": k.id, "name": k.name, "key": raw,
            "note": "请立即保存，密钥明文不再展示"}


@router.delete("/keys/{key_id}")
def revoke_key(key_id: int, user: str = Depends(require_user),
               db: Session = Depends(get_db)):
    if user.startswith("apikey:"):
        raise HTTPException(403, "API 密钥不能管理密钥")
    k = db.get(ApiKey, key_id)
    if not k:
        raise HTTPException(404, "密钥不存在")
    k.active = False
    db.commit()
    return {"status": "revoked", "id": key_id}


@router.get("/scheduler")
def scheduler_jobs():
    """定时采集任务列表 —— C-001。"""
    try:
        from ..scheduler import list_scheduled_jobs
        return list_scheduled_jobs()
    except Exception:
        return []


# ---------- Excel 导出（API-006，Token 走 query 参数以支持浏览器直接下载）----------
@public_router.get("/export/products")
def export_products(token: str, site: str | None = None,
                    sites: str | None = None,
                    categories: str | None = None,
                    format: str = "xlsx",
                    include_price_history: bool = False,
                    include_voc: bool = False,
                    include_images: bool = True,
                    split_by_category: bool = False,
                    db: Session = Depends(get_db)):
    """导出产品数据，支持多格式 + 4 个 toggle。
    - site=foo：单站点；sites=a,b,c：多站点（| 或 , 分隔）
    - categories=cat1|cat2：品类过滤（无品类则全站）
    - format=xlsx|csv|json|zip
    - include_price_history / include_voc：xlsx 额外加 sheet
    - include_images：xlsx 全字段表是否含 image_urls 列
    - split_by_category：xlsx 是否按品类拆 sheet
    """
    if not verify_token(token):
        raise HTTPException(401, "未登录或登录已过期")
    site_list = None
    if sites:
        site_list = [s.strip() for s in sites.replace(",", "|").split("|") if s.strip()]
    elif site:
        site_list = [site]
    cat_list = [c.strip() for c in categories.split("|")
                if c.strip()] if categories else None

    from ..export import export_workbook, export_csv, export_json, export_zip
    site_suffix = (site_list[0] if site_list and len(site_list) == 1
                   else f"{len(site_list)}sites" if site_list else "all")
    cat_suffix = "_".join(c.replace("/","-") for c in (cat_list or []))[:40]
    base_name = (f"smart-crawler_{site_suffix}"
                 f"{('_'+cat_suffix) if cat_suffix else ''}_{datetime.now():%Y%m%d}")

    fmt = (format or "xlsx").lower()
    workbook_kwargs = dict(
        include_price_history=include_price_history,
        include_voc=include_voc,
        include_images=include_images,
        split_by_category=split_by_category,
    )

    if fmt == "csv":
        data = export_csv(db, site_list, categories=cat_list)
        return StreamingResponse(
            io.BytesIO(data), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{base_name}.csv"'},
        )
    if fmt == "json":
        data = export_json(db, site_list, categories=cat_list)
        return StreamingResponse(
            io.BytesIO(data), media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{base_name}.json"'},
        )
    if fmt == "zip":
        data = export_zip(db, site_list or [], categories=cat_list,
                          **workbook_kwargs)
        return StreamingResponse(
            io.BytesIO(data), media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{base_name}.zip"'},
        )

    # 默认 xlsx
    data = export_workbook(db, site_list, categories=cat_list, **workbook_kwargs)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{base_name}.xlsx"'},
    )


# ---------- 跨站点品类列表（drawer 用）----------
@router.get("/categories/cross")
def categories_cross(sites: str = "", db: Session = Depends(get_db)):
    """跨站点品类汇总。优先从 Category 表取，缺数据时降级到 Product.category_path 去重。
    返回 {site: [{name, product_count, source, parent_id, level, category_id}], ...}。
    parent_id / level / category_id 用于前端建树（无 Category 表数据时为 null）。
    """
    site_list = [s.strip() for s in sites.replace(",", "|").split("|") if s.strip()]
    if not site_list:
        return {}
    result: dict[str, list] = {}
    for s in site_list:
        cats = db.query(Category).filter(Category.site == s).all()
        if cats:
            result[s] = [{
                "name": c.category_name or "(unnamed)",
                "category_id": c.category_id,
                "parent_id": c.parent_id,
                "level": c.level,
                "product_count": c.product_count or 0,
                "source": "category-tree",
            } for c in cats if c.category_name]
        else:
            rows = db.query(Product.category_path, func.count(Product.id)).filter(
                Product.site == s,
                Product.category_path.isnot(None)).group_by(
                Product.category_path).all()
            result[s] = [{
                "name": p, "category_id": None, "parent_id": None,
                "level": None, "product_count": n, "source": "product-path"
            } for p, n in rows if p]
    return result


# ---------- 导出预览（drawer 实时统计）----------
@public_router.get("/export/preview")
def export_preview(token: str, site: str | None = None,
                   sites: str | None = None,
                   categories: str | None = None,
                   include_price_history: bool = False,
                   include_voc: bool = False,
                   db: Session = Depends(get_db)):
    """轻量 count 查询返回 7 项预览统计。前端实时调用。"""
    if not verify_token(token):
        raise HTTPException(401, "未登录或登录已过期")

    site_list = None
    if sites:
        site_list = [s.strip() for s in sites.replace(",", "|").split("|") if s.strip()]
    elif site:
        site_list = [site]
    cat_list = [c.strip() for c in categories.split("|")
                if c.strip()] if categories else None

    # SKU 查询
    from sqlalchemy import or_
    pq = db.query(Product)
    if site_list:
        pq = pq.filter(Product.site.in_(site_list)) if len(site_list) > 1 \
            else pq.filter(Product.site == site_list[0])
    if cat_list:
        pq = pq.filter(or_(*[Product.category_path.ilike(f"%{c}%")
                             for c in cat_list]))
    sku_count = pq.count()
    skus = [r[0] for r in pq.with_entities(Product.sku).all() if r[0]]

    # 促销
    promo_q = db.query(Promotion)
    if site_list:
        promo_q = promo_q.filter(Promotion.site.in_(site_list))
    if skus:
        promo_q = promo_q.filter(Promotion.sku.in_(skus))
    promo_count = promo_q.count() if skus else 0

    # 品类数
    cq = db.query(Product.category_path).filter(Product.category_path.isnot(None))
    if site_list:
        cq = cq.filter(Product.site.in_(site_list))
    if cat_list:
        cq = cq.filter(or_(*[Product.category_path.ilike(f"%{c}%")
                             for c in cat_list]))
    category_count = cq.distinct().count()

    # 价格历史 / 评论（仅 toggle 开时计数）
    price_history_rows = 0
    review_count = 0
    if include_price_history and skus:
        from datetime import date, timedelta
        cutoff = date.today() - timedelta(days=90)
        price_history_rows = db.query(PriceHistory).filter(
            PriceHistory.date >= cutoff,
            PriceHistory.sku.in_(skus)).count()
    if include_voc and skus:
        review_count = db.query(Review).filter(
            Review.sku.in_(skus)).count()
        # 限 10/sku：实际导出量上限 = sku_count × 10
        review_count = min(review_count, len(skus) * 10)

    # 文件大小估算：每 SKU ~5KB xlsx + price ~80B/行 + review ~1KB/条
    size_bytes = (sku_count * 5_000
                  + price_history_rows * 80
                  + review_count * 1_000)
    file_size_mb = round(size_bytes / 1_000_000, 2)

    # 耗时估算：每 SKU 0.03s
    duration_sec = max(2, round(sku_count * 0.03 + price_history_rows * 0.0005
                                + review_count * 0.01))

    return {
        "category_count": category_count,
        "sku_count": sku_count,
        "promo_count": promo_count,
        "price_history_rows": price_history_rows,
        "review_count": review_count,
        "file_size_mb": file_size_mb,
        "duration_sec": duration_sec,
    }


# ---------- 代理池状态 ----------
@router.get("/proxy/status")
def proxy_status_endpoint():
    """代理池状态：总数 / 可用 / 各代理失败率。"""
    from ..proxy_pool import pool_status
    return pool_status()


@router.post("/proxy/reload")
def proxy_reload():
    """热重载 proxies.txt（添加/删除代理后调用）。"""
    from ..proxy_pool import reload_pool
    reload_pool()
    from ..proxy_pool import pool_status
    return {"reloaded": True, "status": pool_status()}


# ---------- 数据覆盖率（3B 仪表盘）----------
# 估算全量 SKU 数（基于 sitemap 实测或经验）；为 0 表示未知
_FULL_ESTIMATES: dict[str, int] = {
    # Vidaxl: sitemap 实测每站 26 个 product sitemap × ~20k URL ≈ 30-50 万
    "vidaxl_de": 500000, "vidaxl_uk": 450000, "vidaxl_fr": 450000,
    "vidaxl_es": 400000, "vidaxl_it": 400000, "vidaxl_nl": 350000,
    "vidaxl_pl": 350000, "vidaxl_pt": 350000, "vidaxl_ro": 300000,
    "vidaxl_ie": 300000, "vidaxl_us": 300000, "vidaxl_ca": 0,  # 业务暂停
    # SONGMICS: Shopify 一次拉完，已是全量
    # Costway: API 分页采集，已接近全量
    # 其他：缺数据，0 = 不计入覆盖率
}


@router.get("/coverage")
def data_coverage(db: Session = Depends(get_db)):
    """每站点数据覆盖率：当前 SKU / 估算全量。"""
    from ..models import Site as SiteModel
    rows = []
    for s in db.query(SiteModel).all():
        cur = db.query(Product).filter(Product.site == s.site).count()
        est = _FULL_ESTIMATES.get(s.site, 0)
        pct = round(cur / est * 100, 2) if est > 0 else None
        if est == 0:
            # 没有估算时，假定当前就是全量
            est = cur
            pct = 100.0 if cur > 0 else 0.0
        # 健康度分级
        if pct is None or cur == 0:
            status = "empty"
        elif pct < 5:
            status = "critical"
        elif pct < 50:
            status = "warning"
        else:
            status = "healthy"
        rows.append({
            "site": s.site, "brand": s.brand, "country": s.country,
            "url": s.url, "platform": s.platform,
            "current": cur, "estimated_full": est, "coverage_pct": pct,
            "status": status,
            "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None,
        })
    rows.sort(key=lambda x: (x["status"] != "critical", x["coverage_pct"] or 0))

    # 汇总
    total_current = sum(r["current"] for r in rows)
    total_est = sum(r["estimated_full"] for r in rows)
    critical = sum(1 for r in rows if r["status"] == "critical")
    warning = sum(1 for r in rows if r["status"] == "warning")
    healthy = sum(1 for r in rows if r["status"] == "healthy")

    return {
        "sites": rows,
        "summary": {
            "total_sites": len(rows),
            "total_current_sku": total_current,
            "total_estimated_full": total_est,
            "overall_coverage_pct": round(total_current / total_est * 100, 2)
                                   if total_est > 0 else 0,
            "critical_count": critical,
            "warning_count": warning,
            "healthy_count": healthy,
        },
    }


# ---------- 按 record 计费 · 用量查询 ----------
# Schema 就绪 · 中间件层 metering 留给下个迭代（避免影响线上稳定性）
@router.get("/billing/usage")
def billing_usage(days: int = 30, user: str = Depends(require_user),
                  db: Session = Depends(get_db)):
    """当前用户所有 API key 的 N 天用量 + 账单。

    用于：
    · 海尔大数据湖项目 · 资源池按订单付费对接
    · 用户自助查询：调用量 / 字节数 / 账单 / 按 endpoint 分组
    """
    if user.startswith("apikey:"):
        raise HTTPException(403, "API 密钥不能查计费")
    from ..billing import get_usage_summary
    keys = db.query(ApiKey).all()
    return {
        "days": days,
        "keys": [{
            "id": k.id,
            "name": k.name,
            "key_prefix": (k.key_prefix or "") + "…",
            **get_usage_summary(k.id, days),
        } for k in keys],
    }


@router.get("/billing/usage/{api_key_id}")
def billing_usage_detail(api_key_id: int, days: int = 30,
                         user: str = Depends(require_user),
                         db: Session = Depends(get_db)):
    """指定 API key 的 N 天用量明细。"""
    if user.startswith("apikey:"):
        raise HTTPException(403, "API 密钥不能查计费")
    k = db.query(ApiKey).filter(ApiKey.id == api_key_id).first()
    if not k:
        raise HTTPException(404, "API key 不存在")
    from ..billing import get_usage_summary
    return {
        "id": k.id,
        "name": k.name,
        "key_prefix": (k.key_prefix or "") + "…",
        **get_usage_summary(api_key_id, days),
    }
