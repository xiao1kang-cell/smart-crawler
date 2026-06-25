from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db import SessionLocal, init_db
from app.models import CrawlJob, Site

pytestmark = pytest.mark.unit

_SITES = [
    "dist_unassigned",
    "dist_direct",
    "dist_existing",
    "dist_a",
    "dist_b",
    "dist_c",
    "dist_w1",
    "dist_w2",
    "dist_w3",
    "dist_w4",
    "dist_cap_existing",
    "dist_cap_a",
    "dist_cap_b",
    "dist_cap_c",
    "dist_stale",
]


def _clean(s):
    s.query(CrawlJob).filter(CrawlJob.status.in_(["pending", "running"])).delete(
        synchronize_session=False)
    s.query(Site).filter(Site.site.in_(_SITES)).delete(synchronize_session=False)
    s.commit()


def _site(s, name: str) -> None:
    s.add(Site(site=name, url=f"https://{name}.com", platform="generic",
               proxy_tier="none"))


def test_assigned_only_claims_only_matching_node():
    from app.runner import claim_job

    init_db()
    s = SessionLocal()
    try:
        _clean(s)
        _site(s, "dist_unassigned")
        s.flush()
        job = CrawlJob(site="dist_unassigned", status="pending",
                       trigger="manual", assigned_node="US-macmini4")
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()

    assert claim_job("mini1-worker", assigned_node="US-macmini1",
                     assigned_only=True) is None
    assert claim_job("mini4-worker", assigned_node="US-macmini4",
                     assigned_only=True) == job_id


def test_node_aware_direct_mode_still_claims_unassigned_jobs():
    from app.runner import claim_job

    init_db()
    s = SessionLocal()
    try:
        _clean(s)
        _site(s, "dist_direct")
        s.flush()
        job = CrawlJob(site="dist_direct", status="pending", trigger="manual")
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()

    assert claim_job("nas-worker", assigned_node="nas",
                     assigned_only=False) == job_id


def test_assign_pending_jobs_balances_against_existing_backlog():
    from app.runner import assign_pending_jobs

    init_db()
    now = datetime.utcnow()
    s = SessionLocal()
    try:
        _clean(s)
        for site_name in ("dist_existing", "dist_a", "dist_b", "dist_c"):
            _site(s, site_name)
        s.flush()
        s.add(CrawlJob(site="dist_existing", status="pending",
                       trigger="scheduled", assigned_node="US-macmini1",
                       assigned_at=now))
        for site_name in ("dist_a", "dist_b", "dist_c"):
            s.add(CrawlJob(site=site_name, status="pending",
                           trigger="scheduled", created_at=now))
        s.commit()
    finally:
        s.close()

    assigned = assign_pending_jobs("nas-distributor",
                                   ("US-macmini1", "US-macmini4"),
                                   batch_size=3)
    assert assigned == 3

    s = SessionLocal()
    try:
        counts = {
            node: s.query(CrawlJob)
            .filter(CrawlJob.site.in_(_SITES),
                    CrawlJob.status == "pending",
                    CrawlJob.assigned_node == node)
            .count()
            for node in ("US-macmini1", "US-macmini4")
        }
        assert counts == {"US-macmini1": 2, "US-macmini4": 2}
    finally:
        s.close()


def test_assign_pending_jobs_respects_node_weights():
    from app.runner import assign_pending_jobs

    init_db()
    now = datetime.utcnow()
    s = SessionLocal()
    try:
        _clean(s)
        for site_name in ("dist_w1", "dist_w2", "dist_w3", "dist_w4"):
            _site(s, site_name)
        s.flush()
        for site_name in ("dist_w1", "dist_w2", "dist_w3", "dist_w4"):
            s.add(CrawlJob(site=site_name, status="pending",
                           trigger="scheduled", created_at=now))
        s.commit()
    finally:
        s.close()

    assigned = assign_pending_jobs("nas-distributor",
                                   ("nas:3", "US-macmini1:1"),
                                   batch_size=4)
    assert assigned == 4

    s = SessionLocal()
    try:
        counts = {
            node: s.query(CrawlJob)
            .filter(CrawlJob.site.in_(_SITES),
                    CrawlJob.status == "pending",
                    CrawlJob.assigned_node == node)
            .count()
            for node in ("nas", "US-macmini1")
        }
        assert counts == {"nas": 3, "US-macmini1": 1}
    finally:
        s.close()


def test_assign_pending_jobs_respects_node_caps():
    from app.runner import assign_pending_jobs

    init_db()
    now = datetime.utcnow()
    s = SessionLocal()
    try:
        _clean(s)
        for site_name in ("dist_cap_existing", "dist_cap_a", "dist_cap_b", "dist_cap_c"):
            _site(s, site_name)
        s.flush()
        s.add(CrawlJob(site="dist_cap_existing", status="running",
                       trigger="scheduled", assigned_node="US-macmini1",
                       started_at=now, heartbeat_at=now))
        for site_name in ("dist_cap_a", "dist_cap_b", "dist_cap_c"):
            s.add(CrawlJob(site=site_name, status="pending",
                           trigger="scheduled", created_at=now))
        s.commit()
    finally:
        s.close()

    assigned = assign_pending_jobs("nas-distributor",
                                   ("US-macmini1:1:1", "US-macmini4:1:3"),
                                   batch_size=3)
    assert assigned == 3

    s = SessionLocal()
    try:
        assert (s.query(CrawlJob)
                .filter(CrawlJob.site.in_(_SITES),
                        CrawlJob.assigned_node == "US-macmini1")
                .count()) == 1
        assert (s.query(CrawlJob)
                .filter(CrawlJob.site.in_(_SITES),
                        CrawlJob.assigned_node == "US-macmini4")
                .count()) == 3
    finally:
        s.close()


def test_assign_pending_jobs_recycles_stale_assignment():
    from app.runner import assign_pending_jobs

    init_db()
    s = SessionLocal()
    try:
        _clean(s)
        _site(s, "dist_stale")
        s.flush()
        job = CrawlJob(
            site="dist_stale",
            status="pending",
            trigger="scheduled",
            assigned_node="dead-node",
            assigned_at=datetime.utcnow() - timedelta(seconds=600),
        )
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()

    assert assign_pending_jobs("nas-distributor", ("US-macmini1",),
                               stale_after_sec=300) == 1

    s = SessionLocal()
    try:
        job = s.get(CrawlJob, job_id)
        assert job.assigned_node == "US-macmini1"
        assert job.assigned_by == "nas-distributor"
    finally:
        s.close()
