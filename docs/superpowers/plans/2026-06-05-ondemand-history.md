# 按需抓取历史记录 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为「按需抓取」(ondemand)新增历史记录:每次 `fetch()` 落一条 `OnDemandJob`,在「🔗 按需抓取」Tab 内列表展示,可点开看该次抓到的 listing+评论,可删除单条/清空。

**Architecture:** 新增 `OnDemandJob` 表(摘要 + `item_skus` 列表)。改造 `POST /api/ondemand/fetch` 在抓取后落一条 job(带 workspace/user)。新增 4 个端点:列表 / 详情 / 删单条 / 清空。详情按 `item_skus` 精确查 Product/Review(绕过 workspace 的 site 过滤)。前端在 ondemand Tab 加历史列表 + 展开详情 + 删除。

**Tech Stack:** SQLAlchemy(模型)· FastAPI(端点)· pytest(unit,in-memory sqlite + TestClient)· Vue3 单文件前端。

**对应设计文档:** `docs/superpowers/specs/2026-06-05-ondemand-history-design.md`

---

## 文件结构

| 文件 | 改动 | 职责 |
|------|------|------|
| `backend/app/models.py` | 新增 `OnDemandJob` 类 | 任务记录表 |
| `backend/app/api/ondemand_jobs.py` | **新建** | job 记录写入 helper + 4 个端点的纯逻辑(列表/详情/删/清),保持 routes.py 不膨胀 |
| `backend/app/api/routes.py` | 改 `ondemand_fetch`;挂 4 个新路由 | 端点注册 + fetch 后落 job |
| `backend/tests/test_ondemand_jobs.py` | **新建** | 记录写入 + 4 端点单测 |
| `frontend/index.html` | 改 ondemand Tab | 历史列表 + 展开详情 + 删除 |

**关键约定:**
- `OnDemandJob.item_skus` 存本次抓到的 listing 的 sku 列表(JSON 数组)。详情端点按这批 sku 查 `Product`(`site LIKE 'ondemand_%' AND sku IN (...)`)和 `Review`(`platform LIKE 'ondemand_%' AND sku IN (...)`),**不走 workspace 的 allowed_sites 过滤**。
- status 判定:`failed`=listing_count==0 且 review_count==0;`partial`=有数据但 notes 非空;`success`=有数据且 notes 空。
- 写入逻辑放 `ondemand_jobs.py` 的 `record_job(db, ws_id, username, url, result)`,端点逻辑也放该文件(`list_jobs_logic`/`job_detail_logic`/`delete_job_logic`/`clear_jobs_logic`),routes.py 只做薄路由声明。

---

## Task 1: OnDemandJob 模型

**Files:**
- Modify: `backend/app/models.py`(在 `CrawlJob` 类之后新增)
- Test: `backend/tests/test_ondemand_jobs.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_ondemand_jobs.py`:

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_ondemand_jobs.py::test_ondemand_job_model_columns -v`
Expected: FAIL — `ImportError: cannot import name 'OnDemandJob'`

- [ ] **Step 3: 写模型**

在 `backend/app/models.py` 的 `CrawlJob` 类定义之后(`class Usage` 之前)新增:

```python
class OnDemandJob(Base):
    """按需抓取任务记录 —— 每次 fetch(url) 一条。

    摘要入库;详情(listing/评论)按 item_skus 现查 Product/Review。
    status: success / partial / failed。
    """

    __tablename__ = "ondemand_jobs"

    id = Column(Integer, primary_key=True)
    url = Column(Text)
    platform = Column(String, index=True)            # mercadolibre / lazada / shopee
    kind = Column(String)                            # product / listing
    listing_count = Column(Integer, default=0)
    review_count = Column(Integer, default=0)
    status = Column(String, index=True)              # success / partial / failed
    notes = Column(JSON)                             # res.notes(失败原因/截断)
    item_skus = Column(JSON)                         # 本次抓到的 sku 列表
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), index=True)
    created_by = Column(String)                      # 发起用户 username
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
```

(`Column/Integer/String/Text/JSON/DateTime/ForeignKey/datetime` 在 models.py 顶部已 import,无需新增。)

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_ondemand_jobs.py::test_ondemand_job_model_columns -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/models.py backend/tests/test_ondemand_jobs.py
git commit -m "feat(ondemand-history): add OnDemandJob model"
```

---

## Task 2: record_job 写入逻辑 + status 判定

**Files:**
- Create: `backend/app/api/ondemand_jobs.py`
- Test: `backend/tests/test_ondemand_jobs.py`(追加)

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_ondemand_jobs.py` 末尾追加:

```python
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
                     url="https://x/p/MLA1?wid=MLA2", result=res)
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_ondemand_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api.ondemand_jobs'`

- [ ] **Step 3: 写实现**

创建 `backend/app/api/ondemand_jobs.py`:

```python
"""按需抓取历史(OnDemandJob)—— 记录写入 + 列表/详情/删除 的纯逻辑。

routes.py 只做薄路由声明,业务逻辑集中在此,避免 routes.py 膨胀。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import OnDemandJob, Product, Review
from ..ondemand.registry import classify_url, detect_platform


def _status_of(listing_count: int, review_count: int, notes: list) -> str:
    if listing_count == 0 and review_count == 0:
        return "failed"
    if notes:
        return "partial"
    return "success"


def record_job(session: Session, *, ws_id: int | None, username: str | None,
               url: str, result) -> OnDemandJob:
    """把一次 fetch 的 OnDemandResult 落成一条 OnDemandJob。"""
    skus = [l.get("sku") for l in result.listings if l.get("sku")]
    listing_count = len(result.listings)
    review_count = len(result.reviews)
    notes = list(result.notes or [])
    job = OnDemandJob(
        url=url,
        platform=detect_platform(url),
        kind=classify_url(url),
        listing_count=listing_count,
        review_count=review_count,
        status=_status_of(listing_count, review_count, notes),
        notes=notes,
        item_skus=skus,
        workspace_id=ws_id,
        created_by=username,
    )
    session.add(job)
    session.flush()
    return job


def _job_dict(job: OnDemandJob) -> dict:
    return {
        "id": job.id,
        "url": job.url,
        "platform": job.platform,
        "kind": job.kind,
        "listing_count": job.listing_count,
        "review_count": job.review_count,
        "status": job.status,
        "notes": job.notes or [],
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_ondemand_jobs.py -v`
Expected: PASS(3 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/ondemand_jobs.py backend/tests/test_ondemand_jobs.py
git commit -m "feat(ondemand-history): add record_job + status logic"
```

---

## Task 3: 列表 + 详情 + 删除的纯逻辑函数

**Files:**
- Modify: `backend/app/api/ondemand_jobs.py`(追加 4 个逻辑函数)
- Test: `backend/tests/test_ondemand_jobs.py`(追加)

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_ondemand_jobs.py` 末尾追加:

```python
def _seed_jobs(s):
    from app.api.ondemand_jobs import record_job
    # ws1 两条,ws2 一条
    record_job(s, ws_id=1, username="u1",
               url="https://x/p/MLA1", result=_result(
                   [{"sku": "MLA1", "title": "椅子", "site": "ondemand_mercadolibre",
                     "sale_price": 100, "original_price": 120}], [], []))
    record_job(s, ws_id=1, username="u1",
               url="https://www.lazada.com.my/products/x-i2.html",
               result=_result([{"sku": "2", "title": "桌", "site": "ondemand_lazada"}], [], []))
    record_job(s, ws_id=2, username="u2",
               url="https://x/p/MLA9", result=_result([], [], ["失败"]))
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_ondemand_jobs.py -v`
Expected: FAIL — `ImportError: cannot import name 'list_jobs_logic'`

- [ ] **Step 3: 写实现**

在 `backend/app/api/ondemand_jobs.py` 末尾追加:

```python
def list_jobs_logic(session: Session, *, ws_id: int | None,
                    platform: str | None, page: int, page_size: int) -> dict:
    q = session.query(OnDemandJob).filter(OnDemandJob.workspace_id == ws_id)
    if platform:
        q = q.filter(OnDemandJob.platform == platform)
    total = q.count()
    rows = (q.order_by(OnDemandJob.created_at.desc(), OnDemandJob.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {"total": total, "page": page, "page_size": page_size,
            "jobs": [_job_dict(r) for r in rows]}


def job_detail_logic(session: Session, *, ws_id: int | None,
                     job_id: int) -> dict | None:
    """返回 job + listings + reviews;job 不存在或不属于 ws_id 时返回 None。"""
    job = session.get(OnDemandJob, job_id)
    if job is None or job.workspace_id != ws_id:
        return None
    skus = list(job.item_skus or [])
    listings, reviews = [], []
    if skus:
        prods = (session.query(Product)
                 .filter(Product.site.like("ondemand_%"),
                         Product.sku.in_(skus)).all())
        listings = [{"sku": p.sku, "title": p.title, "sale_price": p.sale_price,
                     "original_price": p.original_price, "currency": p.currency,
                     "image_urls": p.image_urls or [], "product_url": p.product_url}
                    for p in prods]
        revs = (session.query(Review)
                .filter(Review.platform.like("ondemand_%"),
                        Review.sku.in_(skus)).all())
        reviews = [{"review_id": r.review_id, "rating": r.rating,
                    "content": r.content, "review_date":
                    r.review_date.isoformat() if r.review_date else None}
                   for r in revs]
    return {"job": _job_dict(job), "listings": listings, "reviews": reviews}


def delete_job_logic(session: Session, *, ws_id: int | None,
                     job_id: int) -> bool:
    """删单条;不存在或不属于 ws_id 返回 False。只删记录,不删 Product/Review。"""
    job = session.get(OnDemandJob, job_id)
    if job is None or job.workspace_id != ws_id:
        return False
    session.delete(job)
    return True


def clear_jobs_logic(session: Session, *, ws_id: int | None) -> int:
    """清空本 workspace 的记录,返回删除条数。只删记录,不删 Product/Review。"""
    rows = session.query(OnDemandJob).filter(
        OnDemandJob.workspace_id == ws_id).all()
    n = len(rows)
    for r in rows:
        session.delete(r)
    return n
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_ondemand_jobs.py -v`
Expected: PASS(全部 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/ondemand_jobs.py backend/tests/test_ondemand_jobs.py
git commit -m "feat(ondemand-history): add list/detail/delete/clear logic"
```

---

## Task 4: fetch 端点改造 — 抓取后落 job

**Files:**
- Modify: `backend/app/api/routes.py`(改 `ondemand_fetch`)
- Test: `backend/tests/test_ondemand_jobs.py`(追加端点测试)

注意:现有 `ondemand_fetch` **没有** workspace/db 依赖。改造后要加上 `x_workspace_id`/`db`/`_current_user` 并在 fetch 后落 job。fetch 仍走 `from .. import ondemand; ondemand.fetch(...)`(模块属性访问,保持测试可 monkeypatch)。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_ondemand_jobs.py` 末尾追加(端点级,用 TestClient + dependency_overrides):

```python
def test_fetch_endpoint_records_job(monkeypatch):
    from fastapi.testclient import TestClient
    import app.api.routes as routes
    from app.main import app
    from app.ondemand.base import OnDemandResult
    from app.db import SessionLocal, init_db
    from app.models import OnDemandJob

    init_db()

    def fake_fetch(url, *, max_items, review_limit):
        r = OnDemandResult()
        r.add_listing({"sku": "LZ1", "title": "t", "site": "ondemand_lazada",
                       "product_url": url, "sale_price": 9.9})
        r.add_reviews([{"review_id": "rv", "sku": "LZ1", "rating": 4, "content": "ok"}])
        return r

    import app.ondemand as od
    monkeypatch.setattr(od, "fetch", fake_fetch)
    # 绕过登录 + 工作区(返回固定 ws_id=1)
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
    after_sess = SessionLocal()
    jobs = after_sess.query(OnDemandJob).order_by(OnDemandJob.id.desc()).all()
    assert len(jobs) == before + 1
    assert jobs[0].platform == "lazada"
    assert jobs[0].listing_count == 1
    assert jobs[0].item_skus == ["LZ1"]
    after_sess.close()
    app.dependency_overrides.clear()
```

> 注:该测试用真实 `init_db()`(默认 sqlite 文件库),落到真实库表。这是端点级 smoke-ish 单测,验证「fetch 后确有 job 落库」。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_ondemand_jobs.py::test_fetch_endpoint_records_job -v`
Expected: FAIL — job 数量没增加(当前 fetch 端点不落 job),或 `AttributeError`(端点未取 db/ws)

- [ ] **Step 3: 改 ondemand_fetch 端点**

把 `backend/app/api/routes.py` 的 `ondemand_fetch` 整段替换为:

```python
@router.post("/ondemand/fetch")
def ondemand_fetch(payload: dict, user: str = Depends(require_user),
                   x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                   db: Session = Depends(get_db)):
    """按需抓取:指定 URL → listing + VOC,并落一条历史记录。

    payload: {"url": "...", "max_items"?: int, "review_limit"?: int}
    """
    from .. import ondemand
    from .ondemand_jobs import record_job

    url = (payload or {}).get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url 必填")
    max_items = int(payload.get("max_items", 100))
    review_limit = int(payload.get("review_limit", 100))
    res = ondemand.fetch(url, max_items=max_items, review_limit=review_limit)

    # 落历史记录(workspace/user 隔离)
    try:
        ws = _current_workspace(user, db, x_workspace_id)
        u = _current_user(user, db)
        record_job(db, ws_id=ws.id, username=(u.username if u else user),
                   url=url, result=res)
        db.commit()
    except Exception:
        db.rollback()   # 记录失败不影响抓取结果返回

    return {
        "url": url,
        "listings": res.listings,
        "listings_count": len(res.listings),
        "reviews": res.reviews,
        "reviews_count": len(res.reviews),
        "notes": res.notes,
    }
```

(`Header`/`Depends`/`get_db`/`Session`/`_current_workspace`/`_current_user` 在 routes.py 已可用。)

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_ondemand_jobs.py::test_fetch_endpoint_records_job -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/routes.py backend/tests/test_ondemand_jobs.py
git commit -m "feat(ondemand-history): record job on fetch"
```

---

## Task 5: 4 个新路由端点(列表/详情/删/清)

**Files:**
- Modify: `backend/app/api/routes.py`(新增 4 个路由,紧挨 `ondemand_fetch` 之后)
- Test: `backend/tests/test_ondemand_jobs.py`(追加端点测试)

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_ondemand_jobs.py` 末尾追加:

```python
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
    # 造一条 ws=777 的 job(用独特 ws 避免与其它测试数据混)
    s = SessionLocal()
    job = record_job(s, ws_id=777, username="tester", url="https://x/p/MLA1",
                     result=_result([{"sku": "EP1", "title": "t",
                                      "site": "ondemand_mercadolibre"}], [], []))
    from app.models import Product
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
    s = SessionLocal()
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_ondemand_jobs.py::test_jobs_endpoints_crud -v`
Expected: FAIL — 404(端点不存在)

- [ ] **Step 3: 加 4 个路由**

在 `backend/app/api/routes.py` 的 `ondemand_fetch` 端点之后新增:

```python
@router.get("/ondemand/jobs")
def ondemand_jobs_list(platform: str | None = None, page: int = 1,
                       page_size: int = 20,
                       user: str = Depends(require_user),
                       x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                       db: Session = Depends(get_db)):
    from .ondemand_jobs import list_jobs_logic
    ws = _current_workspace(user, db, x_workspace_id)
    return list_jobs_logic(db, ws_id=ws.id, platform=platform,
                           page=page, page_size=page_size)


@router.get("/ondemand/jobs/{job_id}")
def ondemand_job_detail(job_id: int, user: str = Depends(require_user),
                        x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                        db: Session = Depends(get_db)):
    from .ondemand_jobs import job_detail_logic
    ws = _current_workspace(user, db, x_workspace_id)
    detail = job_detail_logic(db, ws_id=ws.id, job_id=job_id)
    if detail is None:
        # 区分 404(不存在)与 403(越权):存在但不属于本 ws → 403
        from ..models import OnDemandJob
        exists = db.get(OnDemandJob, job_id)
        raise HTTPException(status_code=403 if exists else 404,
                            detail="无权访问" if exists else "记录不存在")
    return detail


@router.delete("/ondemand/jobs/{job_id}")
def ondemand_job_delete(job_id: int, user: str = Depends(require_user),
                        x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                        db: Session = Depends(get_db)):
    from .ondemand_jobs import delete_job_logic
    from ..models import OnDemandJob
    ws = _current_workspace(user, db, x_workspace_id)
    ok = delete_job_logic(db, ws_id=ws.id, job_id=job_id)
    if not ok:
        exists = db.get(OnDemandJob, job_id)
        raise HTTPException(status_code=403 if exists else 404,
                            detail="无权删除" if exists else "记录不存在")
    db.commit()
    return {"deleted": True, "id": job_id}


@router.delete("/ondemand/jobs")
def ondemand_jobs_clear(user: str = Depends(require_user),
                        x_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
                        db: Session = Depends(get_db)):
    from .ondemand_jobs import clear_jobs_logic
    ws = _current_workspace(user, db, x_workspace_id)
    n = clear_jobs_logic(db, ws_id=ws.id)
    db.commit()
    return {"deleted": n}
```

> 注意路由顺序:`GET /ondemand/jobs`(列表)必须在 `GET /ondemand/jobs/{job_id}` 之前声明,否则 `jobs` 会被当成 `{job_id}`。本步骤按列表→详情→删→清的顺序写,满足要求。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_ondemand_jobs.py -v`
Expected: 全 PASS

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd backend && .venv/bin/python -m pytest -m "not smoke" -q`
Expected: 全 PASS

```bash
git add backend/app/api/routes.py backend/tests/test_ondemand_jobs.py
git commit -m "feat(ondemand-history): add jobs list/detail/delete/clear endpoints"
```

---

## Task 6: 前端历史列表 + 展开详情 + 删除

**Files:**
- Modify: `frontend/index.html`(ondemand Tab 内)

前端为单文件 Vue3 Composition API,无自动化测试。沿用现有 `authH()`、`.inf-panel`、`.btn-prim`、`.inf-err` 等。

- [ ] **Step 1: 加响应式状态**

在 `frontend/index.html` 的 `setup()` 中,ondemand 现有 ref(`odUrl` 等,约 709-713 行)之后追加:

```javascript
      const odJobs = ref([]);
      const odJobsLoading = ref(false);
      const expandedJobId = ref(null);
      const odJobDetail = ref(null);

      async function loadOndemandJobs() {
        odJobsLoading.value = true;
        try {
          const r = await fetch('/api/ondemand/jobs?page_size=50', { headers: authH() });
          if (r.ok) { const d = await r.json(); odJobs.value = d.jobs || []; }
        } catch (e) {} finally { odJobsLoading.value = false; }
      }

      async function toggleJobDetail(id) {
        if (expandedJobId.value === id) { expandedJobId.value = null; odJobDetail.value = null; return; }
        expandedJobId.value = id; odJobDetail.value = null;
        try {
          const r = await fetch('/api/ondemand/jobs/' + id, { headers: authH() });
          if (r.ok) odJobDetail.value = await r.json();
        } catch (e) {}
      }

      async function deleteOndemandJob(id) {
        if (!confirm('删除这条抓取记录?(不会删除已入库的商品/评论数据)')) return;
        try {
          const r = await fetch('/api/ondemand/jobs/' + id, { method: 'DELETE', headers: authH() });
          if (r.ok) {
            odJobs.value = odJobs.value.filter(j => j.id !== id);
            if (expandedJobId.value === id) { expandedJobId.value = null; odJobDetail.value = null; }
          }
        } catch (e) {}
      }

      async function clearOndemandJobs() {
        if (!confirm('清空本工作区的全部抓取历史?(不会删除已入库的商品/评论数据)')) return;
        try {
          const r = await fetch('/api/ondemand/jobs', { method: 'DELETE', headers: authH() });
          if (r.ok) { odJobs.value = []; expandedJobId.value = null; odJobDetail.value = null; }
        } catch (e) {}
      }
```

- [ ] **Step 2: 抓取成功后刷新列表**

找到现有 `runOndemand` 函数里设置 `odResult.value = await r.json();` 的那行(约 727 行),在其后补一行:

```javascript
          odResult.value = await r.json();
          loadOndemandJobs();
```

- [ ] **Step 3: 暴露到 setup return**

找到 ondemand 现有的 return 行(约 1120 行 `odUrl, odMaxItems, odLoading, odError, odResult, runOndemand`),改为:

```javascript
      odUrl, odMaxItems, odLoading, odError, odResult, runOndemand,
      odJobs, odJobsLoading, expandedJobId, odJobDetail,
      loadOndemandJobs, toggleJobDetail, deleteOndemandJob, clearOndemandJobs,
```

- [ ] **Step 4: 加历史列表 HTML**

在 ondemand Tab 的抓取结果块(`odResult` 那个 `inf-panel`)之后、该 Tab 的页面 `</div>` 之前,插入历史列表面板:

```html
        <div class="inf-panel" style="margin-top:16px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <h3>抓取历史</h3>
            <button class="btn-prim" style="background:rgba(248,113,113,.18);color:#fca5a5"
                    @click="clearOndemandJobs" v-if="odJobs.length">清空历史</button>
          </div>
          <div v-if="odJobsLoading" class="inf-empty-note">加载中…</div>
          <div v-else-if="!odJobs.length" class="inf-empty-note">暂无抓取记录</div>
          <table v-else style="width:100%;margin-top:10px;border-collapse:collapse;font-size:0.82rem">
            <thead><tr style="text-align:left;color:var(--ui-muted)">
              <th style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">时间</th>
              <th style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">平台</th>
              <th style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">URL</th>
              <th style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">listing</th>
              <th style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">评论</th>
              <th style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">状态</th>
              <th style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">操作</th>
            </tr></thead>
            <tbody>
              <template v-for="j in odJobs" :key="j.id">
                <tr style="cursor:pointer" @click="toggleJobDetail(j.id)">
                  <td style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">{{ (j.created_at||'').replace('T',' ').slice(0,16) }}</td>
                  <td style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">{{ j.platform }}</td>
                  <td style="padding:6px 8px;border-bottom:1px solid var(--ui-border);max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ j.url }}</td>
                  <td style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">{{ j.listing_count }}</td>
                  <td style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">{{ j.review_count }}</td>
                  <td style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">
                    <span :style="{color: j.status==='success'?'#86efac':(j.status==='partial'?'#fcd34d':'#fca5a5')}">
                      {{ j.status==='success'?'成功':(j.status==='partial'?'部分':'失败') }}</span>
                  </td>
                  <td style="padding:6px 8px;border-bottom:1px solid var(--ui-border)">
                    <button class="btn-prim" style="padding:2px 8px;font-size:0.72rem;background:rgba(248,113,113,.18);color:#fca5a5"
                            @click.stop="deleteOndemandJob(j.id)">删除</button>
                  </td>
                </tr>
                <tr v-if="expandedJobId===j.id">
                  <td colspan="7" style="padding:10px;background:var(--ui-card,#13111f)">
                    <div v-if="!odJobDetail" class="inf-empty-note">加载详情中…</div>
                    <div v-else>
                      <ul v-if="odJobDetail.job.notes && odJobDetail.job.notes.length" style="margin:0 0 8px;padding-left:18px;color:var(--ui-muted);font-size:0.8rem">
                        <li v-for="(n,i) in odJobDetail.job.notes" :key="i">{{ n }}</li>
                      </ul>
                      <table v-if="odJobDetail.listings.length" style="width:100%;font-size:0.8rem;margin-bottom:8px">
                        <thead><tr style="color:var(--ui-muted)"><th align="left">SKU</th><th align="left">标题</th><th align="left">售价</th><th align="left">原价</th></tr></thead>
                        <tbody><tr v-for="p in odJobDetail.listings" :key="p.sku">
                          <td>{{ p.sku }}</td><td>{{ p.title }}</td><td>{{ p.sale_price }}</td><td>{{ p.original_price }}</td>
                        </tr></tbody>
                      </table>
                      <table v-if="odJobDetail.reviews.length" style="width:100%;font-size:0.8rem">
                        <thead><tr style="color:var(--ui-muted)"><th align="left" style="width:60px">评分</th><th align="left">评论</th></tr></thead>
                        <tbody><tr v-for="(r,i) in odJobDetail.reviews" :key="i">
                          <td>{{ r.rating }}</td><td>{{ r.content }}</td>
                        </tr></tbody>
                      </table>
                      <div v-if="!odJobDetail.listings.length && !odJobDetail.reviews.length" class="inf-empty-note">本次未抓到数据</div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
```

- [ ] **Step 5: Tab 切换时加载历史**

找到现有切 Tab 的逻辑(`setTab` 或 watch tab),在切到 `ondemand` 时调用 `loadOndemandJobs()`。
Run 先定位:`cd /Users/wangxiaokang/Documents/github/smart-crawler && grep -nE "function setTab|watch\(tab|tab\.value ?===|loadProducts\(\)" frontend/index.html | head`
若有 `setTab` 函数,在其中加:`if (t === 'ondemand') loadOndemandJobs();`(t 为切换目标 tab 变量名,按实际函数签名调整)。若是 watch 模式,在 watch 回调里加同样判断。

- [ ] **Step 6: 校验语法 + 标签平衡**

Run:
```bash
cd /Users/wangxiaokang/Documents/github/smart-crawler && python3 - <<'PY'
import re
html=open('frontend/index.html',encoding='utf-8').read()
scripts=re.findall(r'<script[^>]*>(.*?)</script>',html,re.S)
main=max(scripts,key=len)
open('/tmp/od_hist_main.js','w').write(main)
print('主脚本行数:',main.count(chr(10)))
PY
node --check /tmp/od_hist_main.js && echo SYNTAX_OK
```
Expected: `SYNTAX_OK`。同时人工核对新增 HTML 的 `<template>`/`<tr>`/`<table>` 标签闭合。

- [ ] **Step 7: 提交**

```bash
git add frontend/index.html
git commit -m "feat(ondemand-history): add history list, detail expand, delete to console"
```

---

## Task 7: 部署到生产

**Files:** 无代码改动,部署操作。

> 复用既有部署脚本(`/tmp/DEPLOY.sh` 已在 NAS),它会:双备份→覆盖代码→重启容器(触发 `_migrate()` 自动建 `ondemand_jobs` 表)→验证。`ondemand_jobs` 是新表,`Base.metadata.create_all` 会自动建,无需手写迁移。

- [ ] **Step 1: 全量回归确认**

Run: `cd backend && .venv/bin/python -m pytest -m "not smoke" -q`
Expected: 全 PASS

- [ ] **Step 2: 重新打包 + 暂存到 NAS**

按 NAS 部署 skill 重新打包后端 app + 前端 index.html,scp 到 NAS,解包到 `/tmp/sc_deploy_staging`(覆盖旧暂存)。

- [ ] **Step 3: 执行部署**

在 NAS 上跑 `bash /tmp/DEPLOY.sh`(会自动建 `ondemand_jobs` 表)。盯输出确认:health=200、表数从 23→24、products 行数无损。

- [ ] **Step 4: 部署后验证**

- `ondemand_jobs` 表已建:`docker exec smart-crawler-pg psql ... -c "select to_regclass('public.ondemand_jobs');"` → 非空
- 公网 `GET /api/ondemand/jobs` → 401(需登录,已注册)
- 登录控制台 → 按需抓取 Tab → 抓一条 Lazada → 历史列表出现新记录 → 点开看 listing+评论 → 删除生效

---

## 自检对照(spec coverage)

| spec 要求 | 对应任务 |
|-----------|----------|
| OnDemandJob 表(摘要 + item_skus) | Task 1 |
| status 三态判定 | Task 2(`_status_of`) |
| fetch 后落 job | Task 4 |
| GET /jobs 列表(workspace 过滤/倒序/分页/platform) | Task 3 逻辑 + Task 5 路由 |
| GET /jobs/{id} 详情(按 sku 查,绕 workspace 过滤) | Task 3 逻辑 + Task 5 路由 |
| 越权 403 / 不存在 404 | Task 5 路由(区分 exists) |
| DELETE /jobs/{id} 删单条(不删商品数据) | Task 3 `delete_job_logic` + Task 5 |
| DELETE /jobs 清空(不删商品数据) | Task 3 `clear_jobs_logic` + Task 5 |
| 前端历史列表 + 展开详情 | Task 6 |
| 前端删除单条 + 清空 | Task 6 |
| workspace 隔离 | Task 3/5(ws_id 过滤)+ 测试覆盖 |
| 部署建表 | Task 7 |
| 验收标准 1-7 | Task 5 单测(2-5,7)+ Task 6 前端(1)+ Task 7 验证(6) |
