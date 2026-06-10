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


def _seed_jobs(s):
    from app.api.ondemand_jobs import record_job
    # ws1 两条,ws2 一条
    record_job(s, ws_id=1, username="u1",
               url="https://articulo.mercadolibre.com.ar/MLA-1", result=_result(
                   [{"sku": "MLA1", "title": "椅子", "site": "ondemand_mercadolibre",
                     "sale_price": 100, "original_price": 120}], [], []))
    record_job(s, ws_id=1, username="u1",
               url="https://www.lazada.com.my/products/x-i2.html",
               result=_result([{"sku": "2", "title": "桌", "site": "ondemand_lazada"}], [], []))
    record_job(s, ws_id=2, username="u2",
               url="https://articulo.mercadolibre.com.ar/MLA-9", result=_result([], [], ["失败"]))
    s.commit()


def test_list_jobs_logic_filters_by_workspace():
    from app.api.ondemand_jobs import list_jobs_logic

    s = _session()
    _seed_jobs(s)
    out = list_jobs_logic(s, ws_id=1, platform=None, page=1, page_size=20)
    assert out["total"] == 2
    # 倒序:最新(lazada)在前
    assert out["jobs"][0]["platform"] == "lazada"
    assert all(j["status"] for j in out["jobs"])
    # platform 过滤
    out2 = list_jobs_logic(s, ws_id=1, platform="lazada", page=1, page_size=20)
    assert out2["total"] == 1
    s.close()


def test_job_detail_logic_returns_listings_and_reviews():
    from app.api.ondemand_jobs import job_detail_logic
    from app.models import Product, Review

    s = _session()
    _seed_jobs(s)
    # 造该 job 的 Product/Review 数据(详情按 sku 现查)
    s.add(Product(site="ondemand_mercadolibre", sku="MLA1", title="椅子",
                  sale_price=100.0, product_url="u"))
    s.add(Review(platform="ondemand_mercadolibre", review_id="r1", sku="MLA1",
                 content="好", rating=5))
    s.commit()
    job = list_first_ml_job(s)
    detail = job_detail_logic(s, ws_id=1, job_id=job.id)
    assert detail["job"]["id"] == job.id
    assert len(detail["listings"]) == 1
    assert detail["listings"][0]["sku"] == "MLA1"
    assert len(detail["reviews"]) == 1
    assert detail["reviews"][0]["content"] == "好"
    s.close()


def list_first_ml_job(s):
    from app.models import OnDemandJob
    return (s.query(OnDemandJob)
            .filter(OnDemandJob.platform == "mercadolibre",
                    OnDemandJob.workspace_id == 1).first())


def test_job_detail_logic_cross_workspace_returns_none():
    from app.api.ondemand_jobs import job_detail_logic

    s = _session()
    _seed_jobs(s)
    ws2_job = _session_ws2_job(s)
    # ws1 访问 ws2 的 job → None(端点据此返回 403)
    assert job_detail_logic(s, ws_id=1, job_id=ws2_job.id) is None
    s.close()


def _session_ws2_job(s):
    from app.models import OnDemandJob
    return s.query(OnDemandJob).filter(OnDemandJob.workspace_id == 2).first()


def test_delete_job_logic():
    from app.api.ondemand_jobs import delete_job_logic
    from app.models import OnDemandJob

    s = _session()
    _seed_jobs(s)
    job = list_first_ml_job(s)
    # 越权删 → False
    assert delete_job_logic(s, ws_id=2, job_id=job.id) is False
    # 正常删 → True
    assert delete_job_logic(s, ws_id=1, job_id=job.id) is True
    s.commit()
    assert s.query(OnDemandJob).filter_by(id=job.id).first() is None
    s.close()


def test_clear_jobs_logic():
    from app.api.ondemand_jobs import clear_jobs_logic
    from app.models import OnDemandJob

    s = _session()
    _seed_jobs(s)
    n = clear_jobs_logic(s, ws_id=1)
    s.commit()
    assert n == 2
    assert s.query(OnDemandJob).filter_by(workspace_id=1).count() == 0
    # ws2 不受影响
    assert s.query(OnDemandJob).filter_by(workspace_id=2).count() == 1
    s.close()


def test_fetch_endpoint_enqueues_job(monkeypatch):
    """单条 fetch 改异步:建一条 queued job 并入队,立即返回(不再同步抓取)。"""
    from fastapi.testclient import TestClient
    import app.api.routes as routes
    from app.main import app
    from app.db import SessionLocal, init_db
    from app.models import OnDemandJob
    import app.api.ondemand_jobs as oj

    init_db()

    # 拦截入队,避免真起 worker 抓网络
    enqueued = []
    monkeypatch.setattr(oj, "enqueue", lambda jid: enqueued.append(jid))

    app.dependency_overrides[routes.require_user] = lambda: "tester"
    monkeypatch.setattr(routes, "_current_workspace",
                        lambda user, db, x=None: type("W", (), {"id": 1})())
    monkeypatch.setattr(routes, "_current_user",
                        lambda user, db: type("U", (), {"username": "tester"})())

    client = TestClient(app)
    before = SessionLocal().query(OnDemandJob).count()
    resp = client.post("/api/ondemand/fetch",
                       json={"url": "https://www.lazada.com.my/products/x-i1.html"})
    assert resp.status_code == 200
    assert resp.json().get("queued") == 1

    after_sess = SessionLocal()
    jobs = after_sess.query(OnDemandJob).order_by(OnDemandJob.id.desc()).all()
    assert len(jobs) == before + 1
    assert jobs[0].platform == "lazada"
    assert jobs[0].status == "queued"          # 异步:入队待跑,尚未抓取
    assert jobs[0].id in enqueued              # 已入队
    after_sess.close()
    app.dependency_overrides.clear()


def _override_ws(routes, app, monkeypatch, ws_id):
    app.dependency_overrides[routes.require_user] = lambda: "tester"
    monkeypatch.setattr(routes, "_current_workspace",
                        lambda user, db, x=None: type("W", (), {"id": ws_id})())


def test_jobs_endpoints_crud(monkeypatch):
    from fastapi.testclient import TestClient
    import app.api.routes as routes
    from app.main import app
    from app.db import SessionLocal, init_db
    from app.api.ondemand_jobs import record_job

    init_db()
    from app.models import Product, OnDemandJob
    s = SessionLocal()
    # 清理上次运行可能残留的数据(测试用持久库,需自清以保证幂等)
    s.query(OnDemandJob).filter(OnDemandJob.workspace_id == 777).delete()
    s.query(Product).filter_by(site="ondemand_mercadolibre", sku="EP1").delete()
    s.commit()
    # 造一条 ws=777 的 job(用独特 ws 避免与其它测试数据混)
    job = record_job(s, ws_id=777, username="tester", url="https://x/p/MLA1",
                     result=_result([{"sku": "EP1", "title": "t",
                                      "site": "ondemand_mercadolibre"}], [], []))
    s.add(Product(site="ondemand_mercadolibre", sku="EP1", title="t",
                  sale_price=5.0, product_url="u"))
    s.commit()
    job_id = job.id
    s.close()

    _override_ws(routes, app, monkeypatch, 777)
    client = TestClient(app)

    # 列表
    r = client.get("/api/ondemand/jobs")
    assert r.status_code == 200
    body = r.json()
    assert any(j["id"] == job_id for j in body["jobs"])

    # 详情
    r = client.get(f"/api/ondemand/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["listings"][0]["sku"] == "EP1"

    # 删除
    r = client.delete(f"/api/ondemand/jobs/{job_id}")
    assert r.status_code == 200
    # 删后详情 404
    r = client.get(f"/api/ondemand/jobs/{job_id}")
    assert r.status_code == 404

    app.dependency_overrides.clear()


def test_jobs_detail_cross_workspace_403(monkeypatch):
    from fastapi.testclient import TestClient
    import app.api.routes as routes
    from app.main import app
    from app.db import SessionLocal, init_db
    from app.api.ondemand_jobs import record_job

    init_db()
    from app.models import OnDemandJob
    s = SessionLocal()
    s.query(OnDemandJob).filter(OnDemandJob.workspace_id == 888).delete()
    s.commit()
    job = record_job(s, ws_id=888, username="other", url="https://x/p/MLA2",
                     result=_result([{"sku": "Z1", "title": "t",
                                      "site": "ondemand_mercadolibre"}], [], []))
    s.commit(); job_id = job.id; s.close()

    # 当前用户在 ws=999,访问 ws=888 的 job
    _override_ws(routes, app, monkeypatch, 999)
    client = TestClient(app)
    r = client.get(f"/api/ondemand/jobs/{job_id}")
    assert r.status_code == 403
    r = client.delete(f"/api/ondemand/jobs/{job_id}")
    assert r.status_code == 403
    app.dependency_overrides.clear()
