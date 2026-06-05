from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Product, Review
from app.ondemand.base import BaseOnDemand, OnDemandResult

pytestmark = pytest.mark.unit


class FakeCrawler(BaseOnDemand):
    platform = "fake"
    proxy_tier = "none"

    @staticmethod
    def parse_item_id(url):
        return "IT1"

    @staticmethod
    def parse_listing(data, url):
        return {"sku": "IT1", "title": "Fake Chair", "site": "ondemand_fake",
                "product_url": url, "sale_price": 10.0}

    @staticmethod
    def parse_reviews(data, item_id, url):
        return [{"review_id": "rv1", "platform": "ondemand_fake",
                 "site": "ondemand_fake", "rating": 5, "content": "ok"}]

    def fetch_listing(self, item_id, url, proxy=None):
        return self.parse_listing({}, url)

    def fetch_reviews(self, item_id, url, limit=100, proxy=None):
        return self.parse_reviews({}, item_id, url)

    def enumerate_listing(self, url, max_items=100, proxy=None):
        return ["IT1", "IT2"]


def test_fetch_single_product_collects_listing_and_reviews():
    from app.ondemand.runner import fetch

    res = fetch("https://x/IT1", crawler=FakeCrawler(), kind="product",
                do_persist=False)
    assert isinstance(res, OnDemandResult)
    assert len(res.listings) == 1
    assert res.listings[0]["sku"] == "IT1"
    assert len(res.reviews) == 1


def test_fetch_listing_enumerates_multiple(monkeypatch):
    from app.ondemand.runner import fetch

    # listing 路径走 enumerate_listing(["IT1","IT2"]),fetch_listing 固定返回 sku=IT1
    res = fetch("https://x/shop", crawler=FakeCrawler(), kind="listing",
                max_items=2, do_persist=False)
    assert len(res.listings) == 2


def test_persist_writes_product_and_review():
    from app.ondemand import runner

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    res = OnDemandResult()
    res.add_listing({"sku": "IT1", "title": "Fake Chair", "site": "ondemand_fake",
                     "product_url": "https://x/IT1", "sale_price": 10.0})
    res.add_reviews([{"review_id": "rv1", "platform": "ondemand_fake",
                      "site": "ondemand_fake", "rating": 5, "content": "ok"}])

    sess = TestSession()
    stats = runner.persist(res, session=sess)
    sess.commit()

    assert sess.query(Product).filter_by(sku="IT1").count() == 1
    assert sess.query(Review).filter_by(review_id="rv1").count() == 1
    assert stats["listings"]["inserted"] == 1
    assert stats["reviews"]["inserted"] == 1
    sess.close()
