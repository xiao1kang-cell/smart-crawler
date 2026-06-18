"""页面 / API 路由回归测试 · 在 NAS 容器内跑 · pytest 兼容亦可独立运行。
用法（容器内）：
  docker exec smart-crawler python -m tests.test_routes_smoke
"""
from __future__ import annotations

import sys
import urllib.request
import urllib.error
import json
from urllib.parse import quote

BASE = "http://localhost:8077"


def _token() -> str:
    """临时签一个 admin token."""
    from app.auth import make_token
    return make_token("admin")


def _req(path: str, method: str = "GET", headers=None, body=None, expect=200):
    headers = headers or {}
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BASE}{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _ok(path: str, **kw):
    code, body = _req(path, **kw)
    if code != kw.get("expect", 200):
        return f"FAIL {path} → {code} (expected {kw.get('expect', 200)})"
    return f"OK {path} [{code}, {len(body)}B]"


def _has(path: str, must_contain: str, headers=None):
    code, body = _req(path, headers=headers or {})
    if code != 200:
        return f"FAIL {path} → {code}"
    if must_contain.encode() not in body:
        return f"FAIL {path} 缺关键文本 '{must_contain}'"
    return f"OK {path} 含 '{must_contain}'"


def main() -> int:
    results = []
    auth_h = {"Authorization": f"Bearer {_token()}"}

    # === 公共路由（无需鉴权）===
    results.append(_has("/", "smart-crawler"))
    results.append(_has("/app", "smart-crawler"))
    results.append(_has("/login", "smart-crawler"))
    results.append(_has("/report?site=songmics_us", "站点报表"))
    results.append(_ok("/health"))
    results.append(_ok("/favicon.svg"))

    # === API（需要鉴权）===
    results.append(_ok("/api/sites", headers=auth_h))
    results.append(_ok("/api/coverage", headers=auth_h))
    results.append(_ok("/api/jobs?limit=10", headers=auth_h))
    results.append(_ok("/api/proxy/status", headers=auth_h))
    results.append(_ok("/api/sites/songmics_us/overview", headers=auth_h))
    results.append(_ok("/api/products?site=songmics_us&page_size=5", headers=auth_h))
    results.append(_ok("/api/promotions?site=songmics_us&page_size=5", headers=auth_h))
    results.append(_ok("/api/keys", headers=auth_h))
    results.append(_ok("/api/billing/usage", headers=auth_h))
    results.append(_ok("/api/daily-delta/latest", headers=auth_h))

    # === API 鉴权失败 ===
    results.append(_ok("/api/sites", expect=401))   # 无 token

    # === 已删的 PDF 路由必须 404 ===
    results.append(_ok("/api/reports/list", headers=auth_h, expect=404))
    results.append(_ok("/api/reports/generate?site=songmics_us", method="POST",
                       headers=auth_h, expect=404))

    # === overview 数据形状 ===
    code, body = _req("/api/sites/songmics_us/overview", headers=auth_h)
    if code == 200:
        d = json.loads(body)
        cards = d.get("cards") or {}
        if "sku_count" in cards and "new_product_count" in cards:
            results.append(f"OK overview cards 字段齐 (sku={cards['sku_count']})")
        else:
            results.append(f"FAIL overview cards 缺字段: {list(cards)}")
    else:
        results.append(f"FAIL overview status={code}")

    # === sites 数量 ===
    code, body = _req("/api/sites", headers=auth_h)
    sites = json.loads(body) if code == 200 else []
    expected_min = 60
    if len(sites) >= expected_min:
        results.append(f"OK /api/sites 返回 {len(sites)} 站 (≥{expected_min})")
    else:
        results.append(f"FAIL /api/sites 仅 {len(sites)} 站 (期望 ≥{expected_min})")

    # === 输出 ===
    failed = [r for r in results if r.startswith("FAIL")]
    for r in results:
        print(r)
    print(f"\n========= {len(results) - len(failed)}/{len(results)} OK =========")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
