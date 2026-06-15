from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Usage

pytestmark = pytest.mark.unit


def test_usage_has_count_columns():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as s:
        u = Usage(endpoint="/crawl/job", record_count=5,
                  api_calls=3, browser_opens=2, pages_fetched=5)
        s.add(u)
        s.commit()
        row = s.query(Usage).first()
        assert row.api_calls == 3
        assert row.browser_opens == 2
        assert row.pages_fetched == 5


def test_usage_count_columns_default_zero():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as s:
        u = Usage(endpoint="/crawl/job")
        s.add(u)
        s.commit()
        row = s.query(Usage).first()
        assert row.api_calls == 0
        assert row.browser_opens == 0
        assert row.pages_fetched == 0
