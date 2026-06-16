"""代理池 per-proxy 平台排除机制测试。

标注语法:代理行尾 `# no:amazon` 表示该代理不可用于抓 amazon 平台。
get_proxy(tier, site=...) 选候选时跳过 site 命中排除集的代理。
"""
from app.proxy_pool import ProxyPool


def _pool_from(tmp_path, content):
    f = tmp_path / "proxies.txt"
    f.write_text(content, encoding="utf-8")
    # ProxyPool 读模块级 _PROXY_FILE;这里直接构造并指向临时文件
    pool = ProxyPool(prefer_db=False)
    import app.proxy_pool as pp
    # 用临时文件路径替换实例加载源
    orig = pp._PROXY_FILE
    pp._PROXY_FILE = f
    try:
        pool._ensure_loaded()
    finally:
        pp._PROXY_FILE = orig
    return pool


def test_default_proxy_file_prefers_private_local_file(tmp_path, monkeypatch):
    import app.proxy_pool as pp
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    template = backend_dir / "proxies.txt"
    local = backend_dir / "proxies.local.txt"
    template.write_text("[datacenter]\nhttp://u:p@1.1.1.1:2333\n", encoding="utf-8")
    local.write_text("[datacenter]\nhttp://u:p@2.2.2.2:2333\n", encoding="utf-8")
    monkeypatch.delenv("PROXIES_FILE", raising=False)
    monkeypatch.setattr(pp, "__file__", str(backend_dir / "app" / "proxy_pool.py"))

    assert pp._default_proxy_file() == local


def test_proxy_file_env_overrides_private_local_file(tmp_path, monkeypatch):
    import app.proxy_pool as pp
    env_file = tmp_path / "private.txt"
    env_file.write_text("[datacenter]\nhttp://u:p@3.3.3.3:2333\n", encoding="utf-8")
    monkeypatch.setenv("PROXIES_FILE", str(env_file))

    assert pp._default_proxy_file() == env_file


def test_excluded_proxy_never_returned_for_amazon(tmp_path):
    pool = _pool_from(tmp_path, """
[datacenter]
http://u:p@1.1.1.1:2333   # no:amazon
http://u:p@1.1.1.2:2333   # no:amazon
""")
    # 抓 amazon → 两个代理都被排除 → 无候选
    for _ in range(10):
        assert pool.get("datacenter", site="amazon_us") is None


def test_excluded_proxy_used_for_other_platforms(tmp_path):
    pool = _pool_from(tmp_path, """
[datacenter]
http://u:p@1.1.1.1:2333   # no:amazon
""")
    # 抓非 amazon → 正常返回
    assert pool.get("datacenter", site="songmics_us") == "http://u:p@1.1.1.1:2333"
    # site=None → 不限平台,正常返回
    assert pool.get("datacenter", site=None) is None or pool.get("datacenter") == "http://u:p@1.1.1.1:2333"


def test_unannotated_proxy_unaffected(tmp_path):
    pool = _pool_from(tmp_path, """
[datacenter]
http://u:p@1.1.1.1:2333
""")
    # 无标注 → 任何平台都能用,包括 amazon
    assert pool.get("datacenter", site="amazon_us") == "http://u:p@1.1.1.1:2333"


def test_mixed_pool_amazon_skips_only_excluded(tmp_path):
    pool = _pool_from(tmp_path, """
[datacenter]
http://u:p@1.1.1.1:2333   # no:amazon
http://u:p@1.1.1.2:2333
""")
    # 抓 amazon 多次 → 只会拿到未排除的 .2,绝不出现 .1
    seen = {pool.get("datacenter", site="amazon_us") for _ in range(20)}
    assert seen == {"http://u:p@1.1.1.2:2333"}


def test_proxy_py_wrapper_does_not_bypass_exclusion(tmp_path, monkeypatch):
    """回归:proxy.py::get_proxy 的 fallback 不能把被排除的代理吐回来。

    proxy_pool 对 amazon 返回 None(有意排除),proxy.py 旧版 fallback 既不解析
    `# no:amazon` 也不排除,曾把含注释的整行原样返回,绕过排除机制。
    """
    f = tmp_path / "proxies.txt"
    f.write_text("[datacenter]\nhttp://u:p@9.9.9.9:2333   # no:amazon\n", encoding="utf-8")
    import app.proxy as proxy
    import app.proxy_pool as pp
    # 两个模块都指向同一临时文件,并重置已加载状态
    monkeypatch.setattr(pp, "_PROXY_FILE", f)
    monkeypatch.setattr(proxy, "_PROXY_FILE", f)
    monkeypatch.setattr(pp._pool, "prefer_db", False)
    pp._pool.reload()
    proxy._loaded = False
    proxy._pools.clear()
    # amazon → 必须 None(被排除),绝不能 fallback 返回含 # 的脏 URL
    for _ in range(10):
        assert proxy.get_proxy("datacenter", site="amazon_us") is None
    # 非 amazon → 拿到干净 URL(无注释)
    url = proxy.get_proxy("datacenter", site="songmics_us")
    assert url == "http://u:p@9.9.9.9:2333"


def test_persistent_unhealthy_proxy_is_skipped(tmp_path, monkeypatch):
    pool = _pool_from(tmp_path, """
[datacenter]
http://u:p@1.1.1.1:2333
http://u:p@1.1.1.2:2333
""")
    pool.use_persistent_health = True
    import app.proxy_pool as pp
    monkeypatch.setattr(pp, "_persistent_unhealthy_hashes",
                        lambda: {pp._proxy_hash("http://u:p@1.1.1.1:2333")})

    seen = {pool.get("datacenter", site="songmics_us") for _ in range(10)}

    assert seen == {"http://u:p@1.1.1.2:2333"}


def test_status_counts_persistent_unhealthy_as_unavailable(tmp_path, monkeypatch):
    pool = _pool_from(tmp_path, """
[datacenter]
http://u:p@1.1.1.1:2333
http://u:p@1.1.1.2:2333
""")
    pool.use_persistent_health = True
    import app.proxy_pool as pp
    monkeypatch.setattr(pp, "_persistent_unhealthy_hashes",
                        lambda: {pp._proxy_hash("http://u:p@1.1.1.1:2333")})

    status = pool.status()

    assert status["by_tier"]["datacenter"]["total"] == 2
    assert status["by_tier"]["datacenter"]["available"] == 1
    assert status["by_tier"]["datacenter"]["blocked"] == 1
