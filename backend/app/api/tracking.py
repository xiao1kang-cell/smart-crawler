"""标杆网站维护面板 API —— /api/tracking*。

扩展 Site 表的追踪元数据 CRUD + 贴 URL 探测建站 + 触发抓取。
写操作 admin 门控,读列表登录即可,均限当前 workspace 作用域。
"""
from __future__ import annotations

from datetime import datetime

import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Product, Site, Workspace, WorkspaceMember, WorkspaceSite
from ..crawlers.detect import detect_platform
from ..runner import enqueue
from .routes import (require_user, _current_workspace, _require_dashboard_user,
                     _is_super_admin, _workspace_site_names, public_router,
                     _user_from_token, _currency_for_site)

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


def _empty_metrics() -> dict:
    return {
        "products": 0,
        "sku_count": 0,
        "thirty_day_sales": 0,
        "thirty_day_revenue": 0,
        "sales_signal_count": 0,
        "sales_available": False,
        "last_product_updated": None,
    }


def _metrics_for_sites(db: Session, sites: list[str]) -> dict[str, dict]:
    """批量算站点指标,避免列表页 N+1 查询。

    products 使用和站点/覆盖页一致的 distinct coalesce(spu, sku) 口径;
    sku_count 保留原始行数,便于排查站点内 SKU 展开情况。
    """
    site_codes = sorted(set(sites))
    if not site_codes:
        return {}
    rows = db.query(
        Product.site,
        func.count(Product.id),
        func.count(func.distinct(func.coalesce(Product.spu, Product.sku))),
        func.coalesce(func.sum(Product.thirty_day_sales), 0),
        func.coalesce(func.sum(Product.thirty_day_revenue), 0.0),
        func.count(Product.id).filter(func.coalesce(Product.thirty_day_sales, 0) > 0),
        func.max(Product.updated_time),
    ).filter(Product.site.in_(site_codes)).group_by(Product.site).all()
    out = {site: _empty_metrics() for site in site_codes}
    for site, sku_count, products, sales, revenue, sales_signal_count, last_updated in rows:
        out[site] = {
            "products": int(products or 0),
            "sku_count": int(sku_count or 0),
            "thirty_day_sales": int(sales or 0),
            "thirty_day_revenue": round(revenue or 0, 2),
            "sales_signal_count": int(sales_signal_count or 0),
            "sales_available": bool(sales_signal_count),
            "last_product_updated": last_updated.isoformat() if last_updated else None,
        }
    return out


def _metrics(db: Session, site: str) -> dict:
    return _metrics_for_sites(db, [site]).get(site, _empty_metrics())


def _apply_tracking_filters(q, search: str | None, market: str | None,
                            brand: str | None, status: str | None):
    if search:
        like = f"%{search.strip()}%"
        q = q.filter(or_(Site.url.ilike(like), Site.brand.ilike(like),
                         Site.site.ilike(like)))
    if market:
        q = q.filter(func.upper(Site.country) == market.strip().upper())
    if brand:
        q = q.filter(Site.brand.ilike(f"%{brand.strip()}%"))
    if status:
        q = q.filter(Site.track_status == status.strip().lower())
    return q


def _tracking_order(q):
    return q.order_by(desc(func.coalesce(Site.last_crawled, Site.updated_at, Site.created_at)).nullslast(),
                      Site.brand.asc().nullslast(),
                      Site.country.asc().nullslast(),
                      Site.site.asc())


def _tracking_facets(db: Session, allowed: list[str]) -> dict:
    rows = db.query(Site.country, Site.brand, Site.track_status).filter(Site.site.in_(allowed)).all()
    markets = sorted({(country or "").upper() for country, _, _ in rows if country})
    brands = sorted({brand for _, brand, _ in rows if brand}, key=lambda x: x.lower())
    statuses = sorted({(status or "tracking").lower() for _, _, status in rows})
    return {"markets": markets, "brands": brands, "statuses": statuses}


def tracking_row(db: Session, s: Site, metrics: dict | None = None) -> dict:
    m = metrics if metrics is not None else _metrics(db, s.site)
    display_updated = (
        s.last_crawled.isoformat() if s.last_crawled else
        m.get("last_product_updated") or
        (s.updated_at.isoformat() if s.updated_at else None)
    )
    return {
        "site": s.site, "brand": s.brand, "country": s.country,
        "url": s.url, "platform": s.platform,
        "currency": _currency_for_site(s.site),
        "track_status": s.track_status or "tracking",
        "source": s.source or "yaml", "creator": s.creator,
        "review_rate": s.review_rate,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None,
        "display_updated_at": display_updated,
        **m,
    }


def _require_workspace_admin(user: str, db: Session, ws: Workspace):
    u = _require_dashboard_user(user, db)
    if _is_super_admin(u):
        return u
    member = (db.query(WorkspaceMember)
              .filter(WorkspaceMember.workspace_id == ws.id,
                      WorkspaceMember.user_id == u.id,
                      WorkspaceMember.status == "active")
              .first())
    if not member or (member.role or "") not in {"owner", "admin"}:
        raise HTTPException(403, "需要当前 workspace 管理员权限")
    return u


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
    q = _apply_tracking_filters(q, search, market, brand, status)
    total = q.count()
    rows = (_tracking_order(q)
            .offset((page - 1) * page_size).limit(page_size).all())
    metrics = _metrics_for_sites(db, [s.site for s in rows])
    return {"total": total, "page": page, "page_size": page_size,
            "facets": _tracking_facets(db, allowed),
            "items": [tracking_row(db, s, metrics.get(s.site)) for s in rows]}


@public_router.get("/tracking/export")
def export_tracking(
    token: str,
    search: str | None = None, market: str | None = None,
    brand: str | None = None, status: str | None = None,
    workspace_id: int | None = None,
    db: Session = Depends(get_db),
):
    import io
    from openpyxl import Workbook
    from fastapi.responses import StreamingResponse

    u = _user_from_token(db, token)
    ws = _current_workspace(u.username, db, str(workspace_id) if workspace_id else None)
    allowed = _workspace_site_names(db, ws.id, include_hidden=True)
    q = db.query(Site).filter(Site.site.in_(allowed))
    q = _apply_tracking_filters(q, search, market, brand, status)
    rows = _tracking_order(q).all()
    metrics = _metrics_for_sites(db, [s.site for s in rows])

    wb = Workbook(); sh = wb.active; sh.title = "Tracking"
    headers = ["Market", "Brand", "URL", "Status", "Products", "SKU Rows",
               "30-Day Sales", "30-Day Revenue", "Updated Time",
               "Created Time", "Creator", "Source"]
    sh.append(headers)
    for s in rows:
        m = metrics.get(s.site, _empty_metrics())
        display_updated = (
            s.last_crawled.isoformat() if s.last_crawled else
            m.get("last_product_updated") or
            (s.updated_at.isoformat() if s.updated_at else "")
        )
        source = "手动" if (s.source or "yaml") == "user" else "种子"
        currency = _currency_for_site(s.site) or ""
        revenue = ""
        if m["sales_available"]:
            revenue = f"{currency} {m['thirty_day_revenue']}".strip()
        sh.append([s.country, s.brand, s.url, s.track_status or "tracking", m["products"],
                   m["sku_count"],
                   m["thirty_day_sales"] if m["sales_available"] else "",
                   revenue,
                   display_updated,
                   s.created_at.isoformat() if s.created_at else "",
                   s.creator or "系统",
                   source])
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=tracking.xlsx"})


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
    ws = _current_workspace(user, db, x_workspace_id)
    _require_workspace_admin(user, db, ws)
    raw_url = (payload.get("url") or "").strip()
    if not raw_url:
        raise HTTPException(400, "url 不能为空")
    if len(raw_url) > 150:
        raise HTTPException(400, "URL 上限 150 字符")
    brand = (payload.get("brand") or "").strip()[:50] or None
    country = (payload.get("country") or "").strip()[:8] or None

    platform, base = detect_platform(raw_url)
    if platform is None:
        platform = "generic"

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
    except Exception as exc:
        logger.warning("tracking add: enqueue 失败 site=%s: %s", code, exc)  # 不阻断建站

    return tracking_row(db, site)


def _user_site_or_404(db: Session, ws_id: int, code: str) -> Site:
    allowed = set(_workspace_site_names(db, ws_id, include_hidden=True))
    if code not in allowed:
        raise HTTPException(404, "站点不存在或不在当前工作区")
    site = db.query(Site).filter(Site.site == code).first()
    if not site:
        raise HTTPException(404, "站点不存在")
    return site


@router.patch("/tracking/{code}")
def edit_tracking(code: str, payload: dict,
                  user: str = Depends(require_user),
                  x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                  db: Session = Depends(get_db)):
    ws = _current_workspace(user, db, x_workspace_id)
    _require_workspace_admin(user, db, ws)
    site = _user_site_or_404(db, ws.id, code)
    if "brand" in payload:
        site.brand = (payload.get("brand") or "").strip()[:50] or None
    if "country" in payload:
        site.country = (payload.get("country") or "").strip()[:8] or None
    if "review_rate" in payload:
        rr = payload.get("review_rate")
        if rr in (None, ""):
            site.review_rate = None
        else:
            try:
                site.review_rate = float(rr)
            except (TypeError, ValueError):
                raise HTTPException(400, "review_rate 须为数字")
    site.updated_at = datetime.utcnow()
    db.commit(); db.refresh(site)
    return tracking_row(db, site)


def _set_status(db: Session, ws_id: int, code: str, status: str):
    site = _user_site_or_404(db, ws_id, code)
    site.track_status = status
    site.updated_at = datetime.utcnow()
    db.commit(); db.refresh(site)
    return tracking_row(db, site)


@router.post("/tracking/{code}/pause")
def pause_tracking(code: str, user: str = Depends(require_user),
                   x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                   db: Session = Depends(get_db)):
    ws = _current_workspace(user, db, x_workspace_id)
    _require_workspace_admin(user, db, ws)
    return _set_status(db, ws.id, code, "paused")


@router.post("/tracking/{code}/resume")
def resume_tracking(code: str, user: str = Depends(require_user),
                    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                    db: Session = Depends(get_db)):
    ws = _current_workspace(user, db, x_workspace_id)
    _require_workspace_admin(user, db, ws)
    return _set_status(db, ws.id, code, "tracking")


@router.delete("/tracking/{code}")
def delete_tracking(code: str, user: str = Depends(require_user),
                    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                    db: Session = Depends(get_db)):
    ws = _current_workspace(user, db, x_workspace_id)
    _require_workspace_admin(user, db, ws)
    site = _user_site_or_404(db, ws.id, code)
    link = (db.query(WorkspaceSite)
            .filter(WorkspaceSite.workspace_id == ws.id,
                    WorkspaceSite.site == code)
            .first())
    if not link:
        raise HTTPException(404, "站点不存在或不在当前工作区")
    orphaned = db.query(Product).filter(Product.site == code).count()
    db.delete(link)
    remaining_refs = (db.query(WorkspaceSite)
                      .filter(WorkspaceSite.site == code,
                              WorkspaceSite.id != link.id)
                      .count())
    deleted_site = False
    if (site.source or "yaml") == "user" and remaining_refs == 0:
        db.delete(site)
        deleted_site = True
    db.commit()
    return {
        "removed": code,
        "deleted_site": deleted_site,
        "orphaned_products": orphaned if deleted_site else 0,
    }
