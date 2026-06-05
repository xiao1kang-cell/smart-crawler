"""真实网络 smoke —— 上线前手动跑:pytest -m smoke tests/test_ondemand_smoke.py

需要可用网络(Shopee/Lazada 可能需 RESIDENTIAL_PROXY)。任一平台失败不代表代码错误,
可能是平台风控/接口变更,据 notes 排查。
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.smoke

# 上线时替换为真实有效的单品 URL。env 名与平台一一对应,与下方 skip 提示一致。
_ENV = {
    "mercadolibre": "SMOKE_MERCADOLIBRE_URL",
    "lazada": "SMOKE_LAZADA_URL",
    "shopee": "SMOKE_SHOPEE_URL",
}


@pytest.mark.parametrize("platform", list(_ENV))
def test_fetch_real_product(platform):
    url = os.environ.get(_ENV[platform], "")
    if not url:
        pytest.skip(f"未设置 {_ENV[platform]}")
    from app import ondemand

    res = ondemand.fetch(url, max_items=5, review_limit=10)
    # 至少抓到 listing,或给出明确 notes
    assert res.listings or res.notes, f"{platform}: 无 listing 且无 notes"
    if res.listings:
        p = res.listings[0]
        for k in ("sku", "title", "product_url", "site"):
            assert p.get(k), f"{platform}: listing 缺字段 {k}"
