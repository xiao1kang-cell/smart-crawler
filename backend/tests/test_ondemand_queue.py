from __future__ import annotations

import pytest

from app.db import SessionLocal, init_db
from app.models import OnDemandJob

pytestmark = pytest.mark.unit


def _mk_job(s, *, status="queued", url="https://www.lazada.com.my/products/x-i1.html",
            ws=1, batch="b1"):
    job = OnDemandJob(url=url, platform="lazada", kind="product",
                      status=status, batch_id=batch, max_items=20,
                      review_limit=50, attempts=0, workspace_id=ws,
                      created_by="tester")
    s.add(job)
    s.commit()
    return job.id


def _fake_result(listings, reviews, notes):
    from app.ondemand.base import OnDemandResult
    r = OnDemandResult()
    for l in listings:
        r.add_listing(l)
    r.add_reviews(reviews)
    for n in notes:
        r.note(n)
    return r


def test_process_one_success(monkeypatch):
    from app.ondemand import queue as q
    from app.ondemand import runner

    init_db()
    s = SessionLocal()
    jid = _mk_job(s)

    captured = {}

    def fake_fetch(url, *, max_items, review_limit, do_persist=True):
        captured["args"] = (url, max_items, review_limit, do_persist)
        return _fake_result(
            [{"sku": "LZ1", "title": "t", "site": "ondemand_lazada"}],
            [{"review_id": "r1", "sku": "LZ1"}], [])

    monkeypatch.setattr(runner, "fetch", fake_fetch)
    q.process_one(jid)

    s.expire_all()
    job = s.get(OnDemandJob, jid)
    assert job.status == "success"
    assert job.attempts == 1
    assert job.listing_count == 1
    assert job.review_count == 1
    assert job.item_skus == ["LZ1"]
    assert job.error in (None, "")
    # 原始参数被透传给 fetch
    assert captured["args"] == (job.url, 20, 50, True)
    s.close()


def test_process_one_no_data_is_failed(monkeypatch):
    from app.ondemand import queue as q
    from app.ondemand import runner

    init_db()
    s = SessionLocal()
    jid = _mk_job(s)
    monkeypatch.setattr(runner, "fetch",
                        lambda url, **k: _fake_result([], [], ["多次被封放弃"]))
    q.process_one(jid)
    s.expire_all()
    job = s.get(OnDemandJob, jid)
    assert job.status == "failed"
    assert job.attempts == 1
    s.close()


def test_process_one_exception_sets_error(monkeypatch):
    from app.ondemand import queue as q
    from app.ondemand import runner

    init_db()
    s = SessionLocal()
    jid = _mk_job(s)

    def boom(url, **k):
        raise RuntimeError("render crashed")

    monkeypatch.setattr(runner, "fetch", boom)
    q.process_one(jid)
    s.expire_all()
    job = s.get(OnDemandJob, jid)
    assert job.status == "failed"
    assert job.attempts == 1
    assert "render crashed" in (job.error or "")
    s.close()


def test_requeue_pending_resets_and_enqueues(monkeypatch):
    from app.ondemand import queue as q

    init_db()
    s = SessionLocal()
    # requeue_pending 是全局扫描(无 ws 过滤,符合设计),先清掉其它测试残留的
    # queued/running,保证计数确定。
    s.query(OnDemandJob).filter(
        OnDemandJob.status.in_(("queued", "running"))).delete(
        synchronize_session=False)
    s.commit()
    q1 = _mk_job(s, status="queued", batch="rq")
    r1 = _mk_job(s, status="running", batch="rq")
    done = _mk_job(s, status="success", batch="rq")

    enqueued = []
    monkeypatch.setattr(q, "enqueue", lambda jid: enqueued.append(jid))
    n = q.requeue_pending()

    s.expire_all()
    assert set(enqueued) == {q1, r1}
    assert n == 2
    # running 被重置回 queued;success 不动
    assert s.get(OnDemandJob, r1).status == "queued"
    assert s.get(OnDemandJob, done).status == "success"
    s.close()


def test_worker_drains_queue_serially(monkeypatch):
    """通过真实 worker 线程入队多条,_q.join() 等待,验证全部被串行处理。"""
    from app.ondemand import queue as q
    from app.ondemand import runner

    init_db()
    s = SessionLocal()
    ids = [_mk_job(s, batch="serial") for _ in range(3)]

    order = []

    def fake_fetch(url, **k):
        order.append(url)
        return _fake_result(
            [{"sku": "S", "title": "t", "site": "ondemand_lazada"}], [], [])

    monkeypatch.setattr(runner, "fetch", fake_fetch)
    for jid in ids:
        q.enqueue(jid)
    q._q.join()  # 等队列清空

    s.expire_all()
    for jid in ids:
        assert s.get(OnDemandJob, jid).status == "success"
    assert len(order) == 3
    s.close()
