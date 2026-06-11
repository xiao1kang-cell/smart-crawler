"""标杆网站维护面板 API —— /api/tracking*。

扩展 Site 表的追踪元数据 CRUD + 贴 URL 探测建站 + 触发抓取。
写操作 admin 门控,读列表登录即可,均限当前 workspace 作用域。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Product, Site, WorkspaceSite
from ..crawlers.detect import detect_platform
from ..runner import enqueue
from .routes import (require_user, _current_workspace, _require_admin,
                     _workspace_site_names)

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])

_NEW_DAYS = 30


def _metrics(db: Session, site: str) -> dict:
    """实时算 products(distinct spu)/30天销量/收入,不冗余存储。"""
    products = (db.query(func.count(func.distinct(Product.spu)))
                .filter(Product.site == site).scalar() or 0)
    sales, revenue = db.query(
        func.coalesce(func.sum(Product.thirty_day_sales), 0),
        func.coalesce(func.sum(Product.thirty_day_revenue), 0.0),
    ).filter(Product.site == site).first()
    return {"products": int(products),
            "thirty_day_sales": int(sales or 0),
            "thirty_day_revenue": round(revenue or 0, 2)}


def tracking_row(db: Session, s: Site) -> dict:
    m = _metrics(db, s.site)
    return {
        "site": s.site, "brand": s.brand, "country": s.country,
        "url": s.url, "platform": s.platform,
        "track_status": s.track_status or "tracking",
        "source": s.source or "yaml", "creator": s.creator,
        "review_rate": s.review_rate,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None,
        **m,
    }


@router.get("/tracking")
def list_tracking(
    search: str | None = None,
    market: str | None = None,
    brand: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 10,
    user: str = Depends(require_user),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
    db: Session = Depends(get_db),
):
    ws = _current_workspace(user, db, x_workspace_id)
    allowed = _workspace_site_names(db, ws.id, include_hidden=True)
    q = db.query(Site).filter(Site.site.in_(allowed))
    if search:
        like = f"%{search}%"
        q = q.filter(or_(Site.url.ilike(like), Site.brand.ilike(like),
                         Site.site.ilike(like)))
    if market:
        q = q.filter(Site.country == market)
    if brand:
        q = q.filter(Site.brand == brand)
    if status:
        q = q.filter(Site.track_status == status)
    total = q.count()
    rows = (q.order_by(Site.created_at.desc().nullslast(), Site.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {"total": total, "page": page, "page_size": page_size,
            "items": [tracking_row(db, s) for s in rows]}


def _gen_site_code(db: Session, base: str, country: str | None) -> str:
    """从 host 主域 + country 后缀生成唯一 site 主键(如 newbrand_us)。"""
    host = urlparse(base).netloc.split(":")[0]
    parts = [p for p in host.split(".") if p not in ("www", "com", "co", "shop")]
    stem = re.sub(r"[^a-z0-9]", "", (parts[0] if parts else "site").lower()) or "site"
    suffix = (country or "xx").lower()[:2]
    code = f"{stem}_{suffix}"
    n = 2
    while db.query(Site).filter(Site.site == code).first():
        code = f"{stem}_{suffix}{n}"
        n += 1
    return code


@router.post("/tracking")
def add_tracking(
    payload: dict,
    user: str = Depends(require_user),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
    db: Session = Depends(get_db),
):
    _require_admin(user, db)
    ws = _current_workspace(user, db, x_workspace_id)
    raw_url = (payload.get("url") or "").strip()
    if not raw_url:
        raise HTTPException(400, "url 不能为空")
    if len(raw_url) > 150:
        raise HTTPException(400, "URL 上限 150 字符")
    brand = (payload.get("brand") or "").strip()[:50] or None
    country = (payload.get("country") or "").strip()[:8] or None

    platform, base = detect_platform(raw_url)
    if platform is None:
        raise HTTPException(400, "无法识别平台，请联系技术人员手工配置")

    code = _gen_site_code(db, base, country)
    now = datetime.utcnow()
    site = Site(site=code, brand=brand, country=country, url=base,
                platform=platform, proxy_tier="none",
                track_status="tracking", source="user",
                creator=user, created_at=now, updated_at=now)
    db.add(site)
    db.add(WorkspaceSite(workspace_id=ws.id, site=code,
                         display_name=f"{brand or code} · {country or ''}".strip(" ·"),
                         enabled=True, hidden=False, sort_order=0))
    db.commit()
    db.refresh(site)

    try:
        enqueue(code, trigger="tracking_add", requested_by_workspace_id=ws.id)
    except Exception:
        pass  # 入队失败不阻断建站,站已落库,可后续手动触发

    return tracking_row(db, site)
