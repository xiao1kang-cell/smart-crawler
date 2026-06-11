"""平台探测单测（mock 网络，不真实请求）。"""
from unittest.mock import patch

from app.crawlers.detect import detect_platform


class _Resp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


def test_normalizes_base_to_scheme_host():
    with patch("app.crawlers.detect._get", return_value=_Resp(404)):
        _, base = detect_platform("https://shop.example.com/collections/all?page=2")
    assert base == "https://shop.example.com"


def test_detects_shopify_via_products_json():
    def fake_get(url, **kw):
        if url.endswith("/products.json?limit=1"):
            return _Resp(200, json_data={"products": [{"id": 1}]})
        return _Resp(404)
    with patch("app.crawlers.detect._get", side_effect=fake_get):
        platform, _ = detect_platform("https://shop.example.com")
    assert platform == "shopify"


def test_detects_generic_via_sitemap():
    def fake_get(url, **kw):
        if url.endswith("/products.json?limit=1"):
            return _Resp(404)
        if url.endswith("/sitemap.xml"):
            return _Resp(200, text="<urlset><url><loc>x</loc></url></urlset>")
        return _Resp(404)
    with patch("app.crawlers.detect._get", side_effect=fake_get):
        platform, _ = detect_platform("https://store.example.com")
    assert platform == "generic"


def test_returns_none_when_undetectable():
    with patch("app.crawlers.detect._get", return_value=_Resp(404)):
        platform, _ = detect_platform("https://static.example.com")
    assert platform is None
