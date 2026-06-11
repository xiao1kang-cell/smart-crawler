"""标杆网站维护面板 API —— /api/tracking*。

扩展 Site 表的追踪元数据 CRUD + 贴 URL 探测建站 + 触发抓取。
写操作 admin 门控,读列表登录即可,均限当前 workspace 作用域。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Product, Site, WorkspaceSite
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
