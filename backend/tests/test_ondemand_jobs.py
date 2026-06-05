from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base

pytestmark = pytest.mark.unit


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return Session()


def test_ondemand_job_model_columns():
    from app.models import OnDemandJob

    s = _session()
    job = OnDemandJob(
        url="https://x/p/MLA1", platform="mercadolibre", kind="product",
        listing_count=1, review_count=4, status="success",
        notes=["ok"], item_skus=["MLA1"],
        workspace_id=1, created_by="tester",
    )
    s.add(job)
    s.commit()
    row = s.query(OnDemandJob).first()
    assert row.url == "https://x/p/MLA1"
    assert row.platform == "mercadolibre"
    assert row.listing_count == 1
    assert row.item_skus == ["MLA1"]
    assert row.notes == ["ok"]
    assert row.workspace_id == 1
    assert row.created_by == "tester"
    assert row.created_at is not None
    s.close()


def _result(listings, reviews, notes):
    from app.ondemand.base import OnDemandResult
    r = OnDemandResult()
    for l in listings:
        r.add_listing(l)
    r.add_reviews(reviews)
    for n in notes:
        r.note(n)
    return r


def test_record_job_success():
    from app.api.ondemand_jobs import record_job
    from app.models import OnDemandJob

    s = _session()
    res = _result(
        [{"sku": "MLA1", "title": "t", "site": "ondemand_mercadolibre"}],
        [{"review_id": "r1", "sku": "MLA1"}], [])
    job = record_job(s, ws_id=1, username="u1",
                     url="https://articulo.mercadolibre.com.ar/MLA-123?wid=MLA2", result=res)
    s.commit()
    assert job.platform == "mercadolibre"
    assert job.kind == "product"
    assert job.listing_count == 1
    assert job.review_count == 1
    assert job.status == "success"
    assert job.item_skus == ["MLA1"]
    assert s.query(OnDemandJob).count() == 1
    s.close()


def test_record_job_partial_and_failed():
    from app.api.ondemand_jobs import record_job

    s = _session()
    # 有数据 + notes 非空 → partial
    res1 = _result([{"sku": "A", "title": "t", "site": "ondemand_lazada"}],
                   [], ["列表枚举达上限"])
    j1 = record_job(s, ws_id=1, username="u1",
                    url="https://www.lazada.com.my/products/x-i1.html", result=res1)
    assert j1.status == "partial"
    assert j1.platform == "lazada"

    # 无数据 → failed
    res2 = _result([], [], ["多次被封放弃"])
    j2 = record_job(s, ws_id=1, username="u1",
                    url="https://shopee.sg/x-i.1.2", result=res2)
    assert j2.status == "failed"
    assert j2.listing_count == 0
    s.close()
