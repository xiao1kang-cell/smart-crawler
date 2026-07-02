"""TDD: flexispot 统一入口收编验证。

验证三段计数：
- sitemap GET → make_fetcher().get()  → api_calls += 1
- playwright token → count_browser_fetch(_do_bootstrap) → browser_opens += 1
- 批量 POST API → make_fetcher().post() → api_calls += 1 (per slug)
"""
from __future__ import annotations

import json

import pytest

from app.fetching import FetchResult
from app.models import Site

pytestmark = pytest.mark.unit

_SITEMAP_XML = (
    "<urlset>"
    "<url><loc>https://www.flexispot.com/standing-desk-pro-series</loc></url>"
    "<url><loc>https://www.flexispot.com/ergonomic-chair-ultra</loc></url>"
    "</urlset>"
)

_ITEM_JSON = {
    "data": {
        "itemRenderTO": {
            "id": 9001,
            "itemName": "Standing Desk Pro",
            "mainImage": "https://cdn.flexispot.com/img/pro.jpg",
            "itemCode": "EF1PRO",
        },
        "frontCategoryList": [{"name": "Desks"}, {"name": "Standing Desks"}],
        "shopSkuList": [
            {
                "skuCode": "EF1PRO-BLK",
                "skuId": 11111,
                "name": "Standing Desk Pro Black",
                "image": "https://cdn.flexispot.com/img/pro-blk.jpg",
                "salePrc": {"value": 299.99},
                "originalPrc": {"value": 399.99},
                "outOfStock": False,
                "skuStatusDict": "ENABLED",
            }
        ],
    }
}


def _site():
    return Site(
        site="flexispot",
        url="https://www.flexispot.com",
        country="US",
        proxy_tier="none",
        platform="flexispot",
    )


def test_flexispot_counts_browser_and_api(monkeypatch):
    """crawl() 完成后：browser_opens==1, api_calls>=2(sitemap+至少1次POST), 有SKU行。"""
    from app.crawlers.flexispot import FlexispotCrawler

    crawler = FlexispotCrawler(_site())
    # 只 mock _do_bootstrap，让 count_browser_fetch 正常执行（否则 browser_opens 不计）
    monkeypatch.setattr(crawler, "_do_bootstrap", lambda: ("Bearer tok-abc", "10001"))

    api_call_log: list[str] = []

    def fake_get(url, **kw):
        api_call_log.append(f"GET {url}")
        crawler.counter.api_calls += 1
        return FetchResult(
            ok=True,
            url=url,
            status=200,
            text=_SITEMAP_XML,
            final_url=url,
            fetcher="curl_cffi",
        )

    def fake_post(url, **kw):
        api_call_log.append(f"POST {url}")
        crawler.counter.api_calls += 1
        return FetchResult(
            ok=True,
            url=url,
            status=200,
            text=json.dumps(_ITEM_JSON),
            final_url=url,
            fetcher="curl_cffi",
        )

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

        def post(self, url, **kw):
            return fake_post(url, **kw)

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())
    # 只抓 1 个 slug 即可
    crawler.limit = 1

    result = crawler.crawl()

    # browser_opens == 1：_do_bootstrap 经 count_browser_fetch 包裹，返回非空 token
    assert crawler.counter.browser_opens == 1, (
        f"expected browser_opens=1 got {crawler.counter.browser_opens}"
    )
    # api_calls >= 2：sitemap GET(1) + POST(>=1)
    assert crawler.counter.api_calls >= 2, (
        f"expected api_calls>=2 got {crawler.counter.api_calls}, log={api_call_log}"
    )
    # 解析出 SKU 行
    assert isinstance(result.products, list), "result.products 应为 list"
    assert len(result.products) >= 1, (
        f"expected >=1 SKU rows, got {len(result.products)}"
    )
    assert result.total_product_count == 2
    assert result.coverage_complete is False
    assert result.coverage_code == "incomplete_detail_parse"
    # 具体字段
    row = result.products[0]
    assert row["sku"] == "EF1PRO-BLK"
    assert row["sale_price"] == 299.99
    assert row["currency"] == "USD"


def test_flexispot_no_token_returns_early(monkeypatch):
    """若 _do_bootstrap 返回 (None, None)，crawl() 应提前返回，browser_opens==0。"""
    from app.crawlers.flexispot import FlexispotCrawler

    crawler = FlexispotCrawler(_site())
    monkeypatch.setattr(crawler, "_do_bootstrap", lambda: (None, None))

    def fake_get(url, **kw):
        crawler.counter.api_calls += 1
        return FetchResult(
            ok=True, url=url, status=200, text=_SITEMAP_XML,
            final_url=url, fetcher="curl_cffi",
        )

    class _FakeFetcher:
        def get(self, url, **kw):
            return fake_get(url, **kw)

        def post(self, url, **kw):
            pytest.fail("不应到达 POST：token 未取到就应提前返回")

    monkeypatch.setattr(crawler, "make_fetcher", lambda **kw: _FakeFetcher())
    crawler.limit = 1

    result = crawler.crawl()

    # token 为 None → success 判定为 False → browser_opens 不增
    assert crawler.counter.browser_opens == 0
    assert len(result.products) == 0


def test_flexispot_category_fallback_covers_desktops_and_recliners():
    from app.crawlers.flexispot import FlexispotCrawler

    assert (
        FlexispotCrawler._category_from_slug(
            "home-office-100-60cmworktop",
            "Home Office 100x60cm Worktop",
        )
        == "Desktops & Worktops"
    )
    assert (
        FlexispotCrawler._category_from_slug("l-shaped-desktop", "L shaped desktop")
        == "Desktops & Worktops"
    )
    assert (
        FlexispotCrawler._category_from_slug(
            "power-swivel-rocker-recliner-with-waffle-back",
            "Power Swivel Rocker Recliner with Waffle Back",
        )
        == "Chairs"
    )


def test_flexispot_category_fallback_covers_polish_gaps():
    from app.crawlers.flexispot import FlexispotCrawler

    cases = {
        ("blat-w-ksztalcie-litery-l", "Blat w Kształcie Litery L"): (
            "Desktops & Worktops"
        ),
        ("biurko-z-regulacja-wysokosci-e7", "Biurko Stojące Premium"): "Desks",
        ("fotel-relaksacyjny-xp1", "Relaksujący Fotel z Funkcją Rozkładania"): (
            "Chairs"
        ),
        ("prane-dywany-z-miekkiego-polaru", "Prane dywany z miękkiego polaru"): (
            "Rugs"
        ),
        ("zestaw-do-zarzadzania-okablowaniem", "Zestaw do zarzadzania okablowaniem"): (
            "Standing Desks/Desk Bundles"
        ),
    }

    for (slug, title), category in cases.items():
        assert FlexispotCrawler._category_from_slug(slug, title) == category


def test_flexispot_category_fallback_covers_nl_and_ca_gaps():
    from app.crawlers.flexispot import FlexispotCrawler

    cases = {
        ("cable-management-tray-cmp051", "CMP051-Black"): "Cable Management",
        ("standing-desk-anti-fatigue-mat-mt1", "MT1B"): "Floor Mats",
        ("clamp-power-with-usb-ps015", "PS015B"): "Power Accessories",
        ("slim-under-desk-storage-drawer-s07", "S07L-Black"): "Desk Storage",
        ("kleurrijke-metalen-bijzettafel", "FH2BG-40"): "Tables/Side Tables",
        ("alles-in-een-zit-sta-bureau-e7-flow", "Ultiem zit-sta bureau"): (
            "Standing Desks/Desk Systems"
        ),
        ("verstelbare-massief-houten-bureaus", "Zit-sta bureau met bureaublad"): (
            "Desktops & Worktops"
        ),
    }

    for (slug, title), category in cases.items():
        assert FlexispotCrawler._category_from_slug(slug, title) == category


def test_flexispot_category_fallback_covers_multilingual_batch_gaps():
    from app.crawlers.flexispot import FlexispotCrawler

    cases = {
        ("multifunktionale-belueftete-telefonbox-p1s", "Telefonbox P1S"): (
            "Office Pods"
        ),
        ("cabina-insonorizada-m1", "Cabina Insonorizada M1"): "Office Pods",
        ("cabine-acoustique-p1f", "Cabine acoustique pour 1 personne"): "Office Pods",
        ("cabine-telefoniche-da-ufficio-p1s", "Cabine Telefoniche da Ufficio"): (
            "Office Pods"
        ),
        ("set-completo-tutto-in-uno-per-home-office", "E7LB-Set1-EU"): (
            "Standing Desks/Desk Systems"
        ),
        ("set-todo-en-uno-para-oficina-en-casa", "E7LB-Set1-EU"): (
            "Standing Desks/Desk Systems"
        ),
        ("tabla-equilibrio-bb01", "BB01-EU"): "Balance Boards",
        ("bandeja-para-teclado-ergonomico-kt1", "KT1B"): "Keyboard Trays",
        ("ruote-per-sedia-da-ufficio", "W4B"): "Chairs",
        ("l-shaped-standing-desk-with-storage", "L-Shaped Standing Desk with Storage"): (
            "Desk Storage"
        ),
    }

    for (slug, title), category in cases.items():
        assert FlexispotCrawler._category_from_slug(slug, title) == category
