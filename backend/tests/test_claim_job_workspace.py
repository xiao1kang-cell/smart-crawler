from __future__ import annotations

import pytest

from app.db import SessionLocal, init_db
from app.models import CrawlJob, Site, WorkspaceSite

pytestmark = pytest.mark.unit

# Site names scoped to this test module to avoid collisions with other test files
_SITES = ["ws_test_siteA", "ws_test_siteB", "ws_blk_siteA",
          "ws_sched_siteA", "ws_blk_sched_siteA", "ws_orphan_siteA"]


def _clean(s):
    """Wipe all pending/running CrawlJobs and module-scoped Site rows."""
    s.query(CrawlJob).filter(CrawlJob.status.in_(["pending", "running"])).delete(
        synchronize_session=False)
    s.query(WorkspaceSite).filter(WorkspaceSite.site.in_(_SITES)).delete(
        synchronize_session=False)
    s.query(Site).filter(Site.site.in_(_SITES)).delete(synchronize_session=False)
    s.commit()


def _site(s, name):
    """Insert a Site that passes crawl_preflight_issue (proxy_tier=none, track_status=tracking)."""
    s.add(Site(site=name, url=f"https://{name}.com", platform="generic",
               proxy_tier="none"))


def test_allowlist_only_claims_matching_workspace():
    """mini: workspace_allowlist only claims jobs for the specified workspace (by requested_by_workspace_id)."""
    from app.runner import claim_job
    init_db()
    s = SessionLocal()
    try:
        _clean(s)
        _site(s, "ws_test_siteA")
        _site(s, "ws_test_siteB")
        s.flush()
        job_a = CrawlJob(site="ws_test_siteA", status="pending", trigger="manual",
                         requested_by_workspace_id=7)
        job_b = CrawlJob(site="ws_test_siteB", status="pending", trigger="manual",
                         requested_by_workspace_id=99)
        s.add_all([job_a, job_b])
        s.commit()
        job_a_id = job_a.id
    finally:
        s.close()

    jid = claim_job("mini-1", workspace_allowlist=(7,))
    assert jid == job_a_id

    # ws=99 job should not be claimed by mini with allowlist=(7,)
    assert claim_job("mini-1", workspace_allowlist=(7,)) is None


def test_blocklist_skips_matching_workspace():
    """NAS: workspace_blocklist does not claim jobs for the specified workspace."""
    from app.runner import claim_job
    init_db()
    s = SessionLocal()
    try:
        _clean(s)
        _site(s, "ws_blk_siteA")
        s.flush()
        s.add(CrawlJob(site="ws_blk_siteA", status="pending", trigger="manual",
                       requested_by_workspace_id=7))
        s.commit()
    finally:
        s.close()

    assert claim_job("nas", workspace_blocklist=(7,)) is None


def test_scheduled_null_routed_via_workspace_sites():
    """scheduled job with NULL requested_by_workspace_id is routed via workspace_sites mapping."""
    from app.runner import claim_job
    init_db()
    s = SessionLocal()
    try:
        _clean(s)
        _site(s, "ws_sched_siteA")
        s.flush()
        s.add(WorkspaceSite(workspace_id=7, site="ws_sched_siteA"))
        job = CrawlJob(site="ws_sched_siteA", status="pending", trigger="scheduled",
                       requested_by_workspace_id=None)
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()

    jid = claim_job("mini-1", workspace_allowlist=(7,))
    assert jid is not None
    assert jid == job_id


def test_blocklist_excludes_scheduled_null_via_mapping():
    """NAS blocklist also blocks scheduled NULL jobs via workspace_sites mapping."""
    from app.runner import claim_job
    init_db()
    s = SessionLocal()
    try:
        _clean(s)
        _site(s, "ws_blk_sched_siteA")
        s.flush()
        s.add(WorkspaceSite(workspace_id=7, site="ws_blk_sched_siteA"))
        s.add(CrawlJob(site="ws_blk_sched_siteA", status="pending", trigger="scheduled",
                       requested_by_workspace_id=None))
        s.commit()
    finally:
        s.close()

    assert claim_job("nas", workspace_blocklist=(7,)) is None


def test_orphan_null_job_claimable_under_blocklist():
    """NAS：requested_by_workspace_id=NULL 且 site 无 workspace_sites 映射的孤儿 job，
    在 blocklist 下仍应被领取（NAS 兜底，不被 NULL 传播误排除）。"""
    from app.runner import claim_job
    init_db()
    s = SessionLocal()
    try:
        _clean(s)
        _site(s, "ws_orphan_siteA")
        s.flush()
        job = CrawlJob(site="ws_orphan_siteA", status="pending", trigger="scheduled",
                       requested_by_workspace_id=None)
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()

    jid = claim_job("nas", workspace_blocklist=(7,))
    assert jid is not None
    assert jid == job_id


def test_orphan_null_job_not_claimed_by_allowlist():
    """mini：孤儿 job（NULL + 无 workspace_sites 映射）不属于任何租户，
    allowlist 的 mini 不应领取。"""
    from app.runner import claim_job
    init_db()
    s = SessionLocal()
    try:
        _clean(s)
        _site(s, "ws_orphan_siteA")
        s.flush()
        s.add(CrawlJob(site="ws_orphan_siteA", status="pending", trigger="scheduled",
                       requested_by_workspace_id=None))
        s.commit()
    finally:
        s.close()

    assert claim_job("mini-1", workspace_allowlist=(7,)) is None
