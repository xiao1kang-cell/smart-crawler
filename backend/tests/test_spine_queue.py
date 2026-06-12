"""Spine 异步队列测试。"""
from datetime import datetime, timedelta

from sqlalchemy import inspect

from app.db import engine, init_db


def test_spine_jobs_table_exists():
    init_db()
    insp = inspect(engine)
    assert insp.has_table("spine_jobs"), "缺表 spine_jobs"
    cols = {c["name"] for c in insp.get_columns("spine_jobs")}
    for c in ("id", "url", "dataset", "entity_type", "save_policy",
              "force_live", "status", "retries", "max_retries",
              "next_attempt_at", "worker", "result_record_id", "error",
              "workspace_id", "created_at", "started_at", "finished_at"):
        assert c in cols, f"spine_jobs 缺列 {c}"


from app.db import SessionLocal


def _clear_pending():
    """清空残留 pending,保证 claim/run_loop 测试领到的是本测试入队的 job。

    队列 claim 是全局领最旧到期 pending;测试共享文件 DB,故 claim/loop 类
    测试入队前必须清场,否则会领到别的测试残留的 job。
    """
    from app.models import SpineJob
    s = SessionLocal()
    s.query(SpineJob).filter(SpineJob.status == "pending").delete()
    s.commit(); s.close()


def test_enqueue_creates_pending_job():
    init_db(); s = SessionLocal()
    from app.spine_queue import enqueue
    jid = enqueue(s, "https://x.com/p/1", "q-set", entity_type="product",
                  workspace_id=None)
    s.commit()
    from app.models import SpineJob
    job = s.get(SpineJob, jid)
    assert job.status == "pending" and job.url == "https://x.com/p/1"
    assert job.dataset == "q-set" and job.retries == 0 and job.max_retries == 3
    assert job.next_attempt_at is not None
    s.close()


def test_claim_job_optimistic_lock_single_winner():
    init_db()
    _clear_pending()  # 清场,保证 claim 领到本测试入队的那条
    from app.models import SpineJob
    s = SessionLocal()
    from app.spine_queue import enqueue, claim_job
    jid = enqueue(s, "https://x.com/p/2", "claim-set", workspace_id=None)
    s.commit(); s.close()
    # 两个 worker 抢同一个最旧 job:只有一个领到
    first = claim_job("worker-A")
    second = claim_job("worker-B")
    assert first == jid
    assert second is None  # 已被领走,无其他 pending
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    assert job.status == "running" and job.worker == "worker-A"
    s2.close()


def test_claim_job_empty_returns_none():
    init_db()
    _clear_pending()  # 清场后无 pending
    from app.spine_queue import claim_job
    assert claim_job("worker-X") is None


from unittest.mock import patch

from app.models import SpineJob


def _scrape_stub(db, url, **kw):
    return {"scrape_id": "scr_q", "url": url,
            "data": {"title": "QueuedItem", "confidence": 0.95},
            "metadata": {"canonical": None}, "html": "<html>q</html>",
            "warnings": [], "usage": {"source": "live", "credits_used": 2}}


def test_execute_job_success_sets_record_id():
    init_db(); s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, execute_job
    jid = enqueue(s, "https://x.com/p/ok", "exec-set", entity_type="product",
                  save_policy="main", workspace_id=None)
    s.commit(); s.close()
    assert claim_job("w1") == jid
    with patch("app.spine._do_scrape", side_effect=_scrape_stub):
        out = execute_job(jid)
    assert out["status"] == "success"
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    assert job.status == "success" and job.result_record_id is not None
    assert job.finished_at is not None
    s2.close()


def test_execute_job_failure_retries_with_backoff():
    init_db(); s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, execute_job
    jid = enqueue(s, "https://x.com/p/fail", "fail-set", workspace_id=None)
    s.commit(); s.close()
    claim_job("w1")
    def boom(db, url, **kw):
        raise RuntimeError("scrape exploded")
    with patch("app.spine._do_scrape", side_effect=boom):
        out = execute_job(jid)
    assert out["status"] == "pending"  # 还能重试 → 回 pending
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    assert job.status == "pending" and job.retries == 1
    assert job.next_attempt_at > datetime.utcnow()  # 退避到未来
    s2.close()


def test_execute_job_exhausts_retries_to_failed():
    init_db(); s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, execute_job
    jid = enqueue(s, "https://x.com/p/dead", "dead-set", max_retries=1,
                  workspace_id=None)
    s.commit(); s.close()
    def boom(db, url, **kw):
        raise RuntimeError("always fails")
    claim_job("w1")
    with patch("app.spine._do_scrape", side_effect=boom):
        execute_job(jid)  # retries 0→1
    # 第 1 次后 retries=1 == max_retries=1 → 直接 failed(不再回 pending)
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    assert job.status == "failed" and job.retries == 1
    assert "always fails" in (job.error or "")
    s2.close()


def test_claim_skips_jobs_in_backoff_window():
    init_db()
    _clear_pending()  # 清场,保证 claim 只可能领到本测试入队的 job
    s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, execute_job
    jid = enqueue(s, "https://x.com/p/backoff", "bo-set", workspace_id=None)
    s.commit(); s.close()
    claim_job("w1")
    def boom(db, url, **kw):
        raise RuntimeError("fail once")
    with patch("app.spine._do_scrape", side_effect=boom):
        execute_job(jid)  # → pending,next_attempt_at = now + 30s
    # 退避窗口内,claim 领不到
    assert claim_job("w2") is None
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    assert job.status == "pending" and job.next_attempt_at > datetime.utcnow()
    s2.close()


def _clear_running():
    """清空残留 running,保证 reclaim 全局计数只覆盖本测试入队的 job。

    reclaim_stale_jobs 是全局回收所有超时 running(生产期望行为);测试共享
    文件 DB,前序测试(claim 乐观锁 / fresh-running)会留下 running 残留,其
    started_at 一旦老过 600s 就会被计入,污染 `assert n == 1`。故 reclaim 类
    测试入队前必须清场。
    """
    from app.models import SpineJob
    s = SessionLocal()
    s.query(SpineJob).filter(SpineJob.status == "running").delete()
    s.commit(); s.close()


def test_reclaim_stale_running_job_to_pending():
    init_db()
    _clear_pending()
    _clear_running()
    s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, reclaim_stale_jobs
    from datetime import timedelta
    jid = enqueue(s, "https://x.com/p/stale", "stale-set", workspace_id=None)
    s.commit(); s.close()
    claim_job("dead-worker")  # → running
    # 人为把 heartbeat_at 推老,模拟 worker 崩在 running 态(心跳停了)
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    job.heartbeat_at = datetime.utcnow() - timedelta(seconds=99999)
    s2.commit(); s2.close()
    # 回收:超 600s 的 running 重置为 pending
    n = reclaim_stale_jobs(running_timeout_sec=600)
    assert n == 1
    s3 = SessionLocal()
    job = s3.get(SpineJob, jid)
    assert job.status == "pending" and job.worker is None
    s3.close()


def test_reclaim_leaves_fresh_running_untouched():
    init_db()
    _clear_pending()
    s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, reclaim_stale_jobs
    jid = enqueue(s, "https://x.com/p/fresh", "fresh-set", workspace_id=None)
    s.commit(); s.close()
    claim_job("live-worker")  # → running,started_at=now(新鲜)
    # 新鲜 running(未超时)不应被回收
    n = reclaim_stale_jobs(running_timeout_sec=600)
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    assert job.status == "running"  # 仍 running
    s2.close()


def test_run_loop_consumes_one_job_then_stops():
    init_db()
    _clear_pending()  # 清场,保证 run_loop 消费的是本测试入队的 job
    s = SessionLocal()
    from app.spine_queue import enqueue
    jid = enqueue(s, "https://x.com/p/loop", "loop-set", entity_type="product",
                  save_policy="main", workspace_id=None)
    s.commit(); s.close()
    import app.spine_worker as sw
    # should_continue:第一轮 True,之后 False —— 只消费一轮
    calls = {"n": 0}
    def once():
        calls["n"] += 1
        return calls["n"] <= 1
    with patch("app.spine._do_scrape", side_effect=_scrape_stub):
        sw.run_loop(poll_interval=0, should_continue=once)
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    assert job.status == "success"
    s2.close()


def test_run_loop_empty_queue_no_crash():
    init_db()
    _clear_pending()  # 空队列
    import app.spine_worker as sw
    calls = {"n": 0}
    def once():
        calls["n"] += 1
        return calls["n"] <= 1
    # 空队列:领不到 job,sleep(poll_interval=0)一拍,should_continue 转 False 退出
    sw.run_loop(poll_interval=0, should_continue=once)  # 不抛异常即通过


def test_reclaim_recovers_running_with_null_heartbeat():
    init_db()
    _clear_running()  # 清场全局 running
    s = SessionLocal()
    from app.spine_queue import enqueue, reclaim_stale_jobs
    jid = enqueue(s, "https://x.com/p/nullhb", "nullhb-set", workspace_id=None)
    s.commit()
    # 人为造一个 running 但 heartbeat_at=None 的脏状态
    from app.models import SpineJob
    job = s.get(SpineJob, jid)
    job.status = "running"; job.heartbeat_at = None; job.worker = "ghost"
    s.commit(); s.close()
    n = reclaim_stale_jobs(running_timeout_sec=600)
    assert n == 1
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    assert job.status == "pending" and job.worker is None
    s2.close()


def test_spine_jobs_has_billing_and_heartbeat_cols():
    from sqlalchemy import inspect
    from app.db import engine
    init_db()
    cols = {c["name"] for c in inspect(engine).get_columns("spine_jobs")}
    assert "api_key_id" in cols, "spine_jobs 缺列 api_key_id"
    assert "heartbeat_at" in cols, "spine_jobs 缺列 heartbeat_at"


def test_enqueue_persists_api_key_id():
    init_db(); s = SessionLocal()
    from app.spine_queue import enqueue
    jid = enqueue(s, "https://x.com/p/bill", "bill-set", api_key_id=42,
                  workspace_id=None)
    s.commit()
    job = s.get(SpineJob, jid)
    assert job.api_key_id == 42
    s.close()


def test_claim_sets_heartbeat():
    init_db()
    _clear_pending()
    s = SessionLocal()
    from app.spine_queue import enqueue, claim_job
    jid = enqueue(s, "https://x.com/p/hb", "hb-set", workspace_id=None)
    s.commit(); s.close()
    assert claim_job("w-hb") == jid
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    assert job.heartbeat_at is not None  # 领取即设首次心跳
    s2.close()


def test_execute_success_records_usage():
    init_db(); s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, execute_job
    from app.models import Usage
    jid = enqueue(s, "https://x.com/p/billok", "billok-set", entity_type="product",
                  save_policy="main", api_key_id=7, workspace_id=None)
    s.commit(); s.close()
    claim_job("w1")
    before = SessionLocal()
    n_before = before.query(Usage).filter(Usage.endpoint == "/spine/worker/execute").count()
    before.close()
    with patch("app.spine._do_scrape", side_effect=_scrape_stub):
        execute_job(jid)
    after = SessionLocal()
    rows = (after.query(Usage)
            .filter(Usage.endpoint == "/spine/worker/execute", Usage.api_key_id == 7)
            .all())
    after_count = after.query(Usage).filter(Usage.endpoint == "/spine/worker/execute").count()
    after.close()
    assert after_count == n_before + 1  # 成功记一行
    assert any(r.api_key_id == 7 for r in rows)


def test_execute_failure_records_no_usage():
    init_db(); s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, execute_job
    from app.models import Usage
    jid = enqueue(s, "https://x.com/p/billfail", "billfail-set", api_key_id=8,
                  workspace_id=None)
    s.commit(); s.close()
    claim_job("w1")
    before = SessionLocal()
    n_before = before.query(Usage).filter(Usage.endpoint == "/spine/worker/execute").count()
    before.close()
    def boom(db, url, **kw):
        raise RuntimeError("fail no bill")
    with patch("app.spine._do_scrape", side_effect=boom):
        execute_job(jid)
    after = SessionLocal()
    n_after = after.query(Usage).filter(Usage.endpoint == "/spine/worker/execute").count()
    after.close()
    assert n_after == n_before  # 失败不记账


def test_execute_records_usage_with_null_api_key():
    init_db(); s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, execute_job
    jid = enqueue(s, "https://x.com/p/nullkey", "nullkey-set", save_policy="main",
                  api_key_id=None, workspace_id=None)
    s.commit(); s.close()
    claim_job("w1")
    with patch("app.spine._do_scrape", side_effect=_scrape_stub):
        out = execute_job(jid)
    assert out["status"] == "success"  # api_key_id=None 记账不崩


def test_start_heartbeat_updates_and_stops():
    init_db()
    _clear_pending()
    s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, _start_heartbeat
    jid = enqueue(s, "https://x.com/p/beat", "beat-set", workspace_id=None)
    s.commit(); s.close()
    claim_job("w-beat")  # heartbeat_at = now
    s2 = SessionLocal(); first = s2.get(SpineJob, jid).heartbeat_at; s2.close()
    import time
    stop, t = _start_heartbeat(jid, interval=0.1)
    time.sleep(0.35)  # 至少续约 2~3 次
    stop.set(); t.join(timeout=2)
    s3 = SessionLocal(); later = s3.get(SpineJob, jid).heartbeat_at; s3.close()
    assert later > first  # 心跳确实更新了 heartbeat_at
    assert not t.is_alive()  # 线程已退出


def test_execute_with_heartbeat_no_state_corruption(monkeypatch):
    """execute 期间心跳并发续约,不污染主状态;返回后线程停。"""
    init_db()
    _clear_pending()
    s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, execute_job
    import app.spine_queue as sq
    jid = enqueue(s, "https://x.com/p/hbexec", "hbexec-set", entity_type="product",
                  save_policy="main", api_key_id=11, workspace_id=None)
    s.commit(); s.close()
    claim_job("w1")
    # 把心跳间隔压到极短,保证 execute 期间至少续约几次
    monkeypatch.setattr(sq, "HEARTBEAT_INTERVAL", 0.02)
    # resolve 故意慢一点,给心跳留出并发窗口
    import time as _t
    def slow_stub(db, url, **kw):
        _t.sleep(0.15)
        return {"scrape_id": "x", "url": url,
                "data": {"title": "HB", "confidence": 0.95},
                "metadata": {"canonical": None}, "html": "<html>x</html>",
                "warnings": [], "usage": {"source": "live", "credits_used": 2}}
    import threading
    before_threads = threading.active_count()
    with patch("app.spine._do_scrape", side_effect=slow_stub):
        out = execute_job(jid)
    # (a) 主状态正确,没被心跳覆盖
    assert out["status"] == "success" and out["record_id"] is not None
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    assert job.status == "success"
    assert job.result_record_id is not None  # 主写未被心跳覆盖
    assert job.heartbeat_at is not None      # 心跳确实写过
    s2.close()
    # (c) execute 返回后心跳线程已停(给 daemon 一点收尾时间)
    _t.sleep(0.1)
    assert threading.active_count() <= before_threads  # 无泄漏


def test_reclaim_uses_heartbeat_not_started_at():
    init_db()
    _clear_pending()  # 清场,保证 claim("w1") 领到的是本测试入队的 job
    _clear_running()
    s = SessionLocal()
    from app.spine_queue import enqueue, claim_job, reclaim_stale_jobs
    jid = enqueue(s, "https://x.com/p/hbreclaim", "hbr-set", workspace_id=None)
    s.commit(); s.close()
    claim_job("w1")  # started_at=now, heartbeat_at=now
    # 关键:started_at 推得很老,但 heartbeat_at 保持新鲜 → 不应回收(活着的长抓)
    s2 = SessionLocal()
    job = s2.get(SpineJob, jid)
    job.started_at = datetime.utcnow() - timedelta(seconds=99999)
    job.heartbeat_at = datetime.utcnow()  # 心跳新鲜
    s2.commit(); s2.close()
    n = reclaim_stale_jobs(running_timeout_sec=600)
    s3 = SessionLocal()
    job = s3.get(SpineJob, jid)
    assert job.status == "running"  # 心跳新鲜,长抓不被误回收
    s3.close()
