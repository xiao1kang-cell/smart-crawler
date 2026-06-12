"""通用数据脊柱（SP1）—— 落库 + warehouse-first + 质量门。

复用 agent_crawler.scrape_url 的抓取与提取,只在其后接落库/读路径。
不改任何现有电商表/采集器。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import String, cast, or_
from sqlalchemy.orm import Session

from . import snapshot
from .models import Dataset, ExtractedRecord, RawSnapshot

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "_ga", "ref", "ref_src",
}


def canonical_url(url: str, explicit: str | None = None) -> str:
    """规整 URL 作去重键。explicit(页面 <link rel=canonical>) 优先。"""
    target = explicit or url
    p = urlparse(target if "://" in target else f"https://{target}")
    host = (p.netloc or "").lower()
    path = p.path.rstrip("/") or "/"
    query = urlencode([(k, v) for k, v in parse_qsl(p.query)
                       if k.lower() not in _TRACKING_PARAMS])
    return urlunparse((p.scheme or "https", host, path, "", query, ""))


def content_hash(value) -> str:
    """对 dict/str 算稳定 sha256(dict 按 key 排序,顺序无关)。"""
    if isinstance(value, (bytes, str)):
        raw = value.encode("utf-8") if isinstance(value, str) else value
    else:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


_CONFIDENCE_MIN = 0.6
_REQUIRED_FIELDS = {
    "product": {"title"},
    "review": {"content"},
    "article": {"title"},
    "generic": set(),
}
_BLOCK_MARKERS = ("blocked", "challenge", "captcha", "403", "429")


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "dataset"


def get_or_create_dataset(db: Session, name: str, *, workspace_id: int | None,
                          entity_type: str = "generic",
                          source_kind: str = "custom_url") -> Dataset:
    slug = _slugify(name)
    row = (db.query(Dataset)
           .filter(Dataset.workspace_id == workspace_id, Dataset.slug == slug)
           .first())
    if row:
        return row
    row = Dataset(name=name, slug=slug, entity_type=entity_type,
                  source_kind=source_kind, workspace_id=workspace_id)
    db.add(row); db.commit(); db.refresh(row)
    return row


def quality_check(data: dict, entity_type: str, confidence: float,
                  warnings: list, save_policy: str) -> tuple[str, list[str]]:
    """返回 (quality_status, missing_fields)。"""
    required = _REQUIRED_FIELDS.get(entity_type, set())
    missing = [f for f in required if not (data or {}).get(f)]
    # 被反爬污染 → quarantine,优先级最高
    wtext = " ".join(str(w) for w in (warnings or [])).lower()
    if any(m in wtext for m in _BLOCK_MARKERS):
        return "quarantine", missing
    if save_policy == "quarantine":
        return "quarantine", missing
    if save_policy == "main":
        return "main", missing
    if save_policy == "staging":
        return "staging", missing
    # promote_if_valid(默认)
    if confidence >= _CONFIDENCE_MIN and not missing:
        return "main", missing
    return "staging", missing


def ingest_extraction(db: Session, scrape_result: dict, dataset: Dataset, *,
                      save_policy: str = "promote_if_valid",
                      workspace_id: int | None = None) -> dict:
    """把一次 scrape_url 结果落库:raw_snapshot + extracted_record + 质量门。"""
    data = dict(scrape_result.get("data") or {})
    confidence = float(data.pop("confidence", 0.0) or 0.0)
    meta = scrape_result.get("metadata") or {}
    url = scrape_result.get("url") or ""
    canon = canonical_url(url, explicit=meta.get("canonical"))
    warnings = scrape_result.get("warnings") or []
    entity_type = dataset.entity_type or "generic"
    now = datetime.utcnow()

    # 1) raw_snapshot(正文写盘 + 元数据入表)
    html = scrape_result.get("html") or ""
    body_path = snapshot.save_returning_path(
        dataset.slug, canon.rsplit("/", 1)[-1] or "page", html)
    snap = RawSnapshot(
        url=url, canonical_url=canon, content_hash=content_hash(html),
        fetched_at=now, status_code=(meta.get("status") or 200),
        etag=meta.get("etag"), last_modified=meta.get("last_modified"),
        content_type=meta.get("content_type"), body_path=body_path,
        fetch_mode=(scrape_result.get("usage") or {}).get("source") or "live",
        workspace_id=workspace_id)
    db.add(snap); db.flush()

    # 2) 质量门
    method = "jsonld" if confidence >= 0.9 else "heuristic"
    status, missing = quality_check(data, entity_type, confidence, warnings, save_policy)
    chash = content_hash(data)

    # 3) upsert by (dataset_id, record_key)
    rec = (db.query(ExtractedRecord)
           .filter_by(dataset_id=dataset.id, record_key=canon).first())
    if rec is None:
        rec = ExtractedRecord(dataset_id=dataset.id, record_key=canon,
                              source_url=url, canonical_url=canon,
                              entity_type=entity_type, workspace_id=workspace_id)
        db.add(rec)
    elif rec.content_hash == chash:
        # 内容没变 → 只刷新 fetched_at,不重写 data(SP2 少爬钩子)
        rec.fetched_at = now; rec.snapshot_id = snap.id
        db.commit()
        return _ingest_response(scrape_result, snap, dataset, rec, status, missing,
                                save_policy, canon, url, unchanged=True)
    rec.data = data; rec.content_hash = chash; rec.confidence = confidence
    rec.extraction_method = method; rec.quality_status = status
    rec.snapshot_id = snap.id; rec.fetched_at = now; rec.extracted_at = now
    db.commit(); db.refresh(rec)
    return _ingest_response(scrape_result, snap, dataset, rec, status, missing,
                            save_policy, canon, url, unchanged=False)


def _ingest_response(scrape_result, snap, dataset, rec, status, missing,
                     save_policy, canon, url, *, unchanged) -> dict:
    return {
        "scrape_id": scrape_result.get("scrape_id"),
        "snapshot_id": snap.id, "dataset_id": dataset.id, "record_id": rec.id,
        "confidence": rec.confidence if rec.confidence is not None else 0.0, "quality_status": status,
        "fetch_mode": snap.fetch_mode, "missing_fields": missing,
        "warnings": scrape_result.get("warnings") or [],
        "save_policy": save_policy, "unchanged": unchanged,
        "provenance": {
            "source_url": url, "canonical_url": canon,
            "fetched_at": rec.fetched_at.isoformat() if rec.fetched_at else None,
            "extraction_method": rec.extraction_method,
            "content_hash": rec.content_hash,
        },
    }


_GLOBAL_TTL = 86400


def _do_scrape(db: Session, url: str, *, force_live: bool, mode: str) -> dict:
    """包一层 agent_crawler.scrape_url,便于测试 monkeypatch。"""
    from .agent_crawler import scrape_url as _scrape
    out = _scrape(db, url, force_live=force_live, mode=mode)
    out.setdefault("url", url)
    return out


def resolve(db: Session, url: str, dataset: Dataset, *, workspace_id: int | None,
            force_live: bool = False, max_age_sec: int | None = None,
            save_policy: str = "promote_if_valid", mode: str = "standard") -> dict:
    """warehouse-first 带 TTL。返回 data + source + credits + provenance。"""
    canon = canonical_url(url)
    if not force_live:
        rec = (db.query(ExtractedRecord)
               .filter_by(dataset_id=dataset.id, record_key=canon,
                          quality_status="main").first())
        if rec and rec.fetched_at:
            ttl = max_age_sec if max_age_sec is not None else (dataset.freshness_ttl_sec or _GLOBAL_TTL)
            age = (datetime.utcnow() - rec.fetched_at).total_seconds()
            if age <= ttl:
                return {
                    "source": "warehouse", "credits_used": 0,
                    "dataset_id": dataset.id, "record_id": rec.id,
                    "data": rec.data, "confidence": rec.confidence,
                    "quality_status": rec.quality_status,
                    "age_sec": int(age),
                    "fetch_mode": "warehouse",
                    "save_policy": None,
                    "missing_fields": [],
                    "warnings": [],
                    "unchanged": None,
                    "provenance": {
                        "source_url": rec.source_url,
                        "canonical_url": rec.canonical_url,
                        "fetched_at": rec.fetched_at.isoformat(),
                        "extraction_method": rec.extraction_method,
                        "content_hash": rec.content_hash,
                    },
                }
    # 未命中/过期/force_live → live 抓 + 落库
    scrape_result = _do_scrape(db, url, force_live=True, mode=mode)
    # force_live 默认强抓不污染主库 → staging
    policy = save_policy if not force_live else "staging"
    out = ingest_extraction(db, scrape_result, dataset,
                            save_policy=policy, workspace_id=workspace_id)
    out["source"] = "live"
    out["credits_used"] = (scrape_result.get("usage") or {}).get("credits_used", 2)
    out["data"] = {k: v for k, v in (scrape_result.get("data") or {}).items()
                   if k != "confidence"}
    return out


def query_dataset(db: Session, dataset: Dataset, *, query: str | None = None,
                  entity_type: str | None = None, include_staging: bool = False,
                  limit: int = 20) -> dict:
    """查通用数据集。默认只返 main;include_staging 才带 staging。"""
    q = db.query(ExtractedRecord).filter(ExtractedRecord.dataset_id == dataset.id)
    statuses = ["main"] + (["staging"] if include_staging else [])
    q = q.filter(ExtractedRecord.quality_status.in_(statuses))
    if entity_type:
        q = q.filter(ExtractedRecord.entity_type == entity_type)
    if query:
        like = f"%{query}%"
        q = q.filter(or_(ExtractedRecord.source_url.ilike(like),
                         ExtractedRecord.canonical_url.ilike(like),
                         cast(ExtractedRecord.data, String).ilike(like)))
    total = q.count()
    rows = q.order_by(ExtractedRecord.fetched_at.desc().nullslast(),
                      ExtractedRecord.id.desc()).limit(limit).all()
    return {"total": total, "dataset": dataset.slug, "items": [
        {"record_id": r.id, "entity_type": r.entity_type, "data": r.data,
         "confidence": r.confidence, "quality_status": r.quality_status,
         "source_url": r.source_url,
         "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None}
        for r in rows]}
