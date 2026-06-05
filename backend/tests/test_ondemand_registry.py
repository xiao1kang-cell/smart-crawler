from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_ondemand_result_accumulates():
    from app.ondemand.base import OnDemandResult

    r = OnDemandResult()
    r.add_listing({"sku": "X1", "title": "Chair", "site": "ondemand_shopee",
                   "product_url": "u"})
    r.add_reviews([{"review_id": "r1"}, {"review_id": "r2"}])
    r.note("done")

    assert len(r.listings) == 1
    assert len(r.reviews) == 2
    assert r.notes == ["done"]
    assert r.summary()["listings"] == 1
    assert r.summary()["reviews"] == 2


def test_detect_platform_by_domain():
    from app.ondemand.registry import detect_platform

    assert detect_platform("https://articulo.mercadolibre.com.mx/MLM-123") == "mercadolibre"
    assert detect_platform("https://www.lazada.com.my/products/x-i123-s456.html") == "lazada"
    assert detect_platform("https://shopee.com.my/product-i.111.222") == "shopee"
    assert detect_platform("https://example.com/foo") is None


def test_classify_url_product_vs_listing():
    from app.ondemand.registry import classify_url

    # 美客多:商品页含 MLM-/MLB-/MLA- 编码
    assert classify_url("https://articulo.mercadolibre.com.mx/MLM-123456789-chair") == "product"
    # 美客多:店铺/搜索页
    assert classify_url("https://listado.mercadolibre.com.mx/sillas") == "listing"
    # Shopee:单品 i.shopid.itemid
    assert classify_url("https://shopee.com.my/product-i.111.222") == "product"
    # Shopee:店铺页
    assert classify_url("https://shopee.com.my/shop123") == "listing"
    # Lazada:/products/...html 为单品
    assert classify_url("https://www.lazada.com.my/products/x-i123-s456.html") == "product"
    # Lazada:类目页
    assert classify_url("https://www.lazada.com.my/shop/abc/") == "listing"


@pytest.mark.skip(reason="平台类待 Task 3-5 实现")
def test_get_crawler_returns_platform_class():
    from app.ondemand.registry import get_crawler

    assert get_crawler("mercadolibre").platform == "mercadolibre"
    assert get_crawler("lazada").platform == "lazada"
    assert get_crawler("shopee").platform == "shopee"
    with pytest.raises(ValueError):
        get_crawler("unknown")
