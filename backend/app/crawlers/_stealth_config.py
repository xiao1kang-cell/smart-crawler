"""共享 StealthyFetcher 调用配置 —— 统一反爬参数，避免每个 crawler 重复。

Scrapling 0.4+ StealthyFetcher.fetch() 的反爬参数集中在此。
- 升级前：仅 headless/network_idle/timeout/proxy（4 参数）
- 升级后：含 solve_cloudflare/hide_canvas/real_chrome/locale 等（11+ 参数）

参考 deliverables/scrapling_design_research.html 第 6 节完整参数表。
"""
from __future__ import annotations

from typing import Any


def stealth_kwargs(
    proxy: str | None = None,
    country: str | None = None,
    *,
    real_chrome: bool = False,           # 默认 False，True 需 host 装 Chrome（NAS Debian 没装）
    solve_cloudflare: bool = True,       # 自动过 Turnstile
    network_idle: bool = True,
    timeout_ms: int = 60000,
    persist_profile_key: str | None = None,  # per-site cookie 持久化
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成 StealthyFetcher.fetch() 的反爬全套参数。

    Args:
        proxy: 代理 URL
        country: 站点国家代码（用于 locale/timezone 推断）
        real_chrome: 是否用本机 Chrome（NAS 容器内没装，保持 False）
        solve_cloudflare: 自动解 Turnstile
        network_idle: 等网络空闲
        timeout_ms: 总超时
        persist_profile_key: 同 site 持久化 cookie 目录 key（用 site code）
        extra: 调用方追加自定义参数（如 page_action / wait_selector）

    Returns:
        kwargs dict，直接 ** 展开给 StealthyFetcher.fetch()
    """
    kw: dict[str, Any] = {
        "headless": True,
        "network_idle": network_idle,
        "timeout": timeout_ms,
        "solve_cloudflare": solve_cloudflare,
        "hide_canvas": True,
        "block_webrtc": True,
        "dns_over_https": True,
        "block_ads": True,
        "google_search": True,
        "allow_webgl": True,  # 关 WebGL 易被检测，保持默认
        "real_chrome": real_chrome,
    }

    # 按 country 配 locale/timezone（让 Cloudflare 看上去更"本地化"）
    locale_map = {
        "US": ("en-US", "America/New_York"),
        "CA": ("en-CA", "America/Toronto"),
        "UK": ("en-GB", "Europe/London"),
        "DE": ("de-DE", "Europe/Berlin"),
        "FR": ("fr-FR", "Europe/Paris"),
        "IT": ("it-IT", "Europe/Rome"),
        "ES": ("es-ES", "Europe/Madrid"),
        "NL": ("nl-NL", "Europe/Amsterdam"),
        "PL": ("pl-PL", "Europe/Warsaw"),
        "PT": ("pt-PT", "Europe/Lisbon"),
        "RO": ("ro-RO", "Europe/Bucharest"),
        "IE": ("en-IE", "Europe/Dublin"),
        "BR": ("pt-BR", "America/Sao_Paulo"),
        "MX": ("es-MX", "America/Mexico_City"),
        # 美客多其余拉美站点（locale 必须与目标域名 ccTLD 对齐，否则反爬易弹验证页）
        "AR": ("es-AR", "America/Argentina/Buenos_Aires"),
        "CL": ("es-CL", "America/Santiago"),
        "CO": ("es-CO", "America/Bogota"),
        "UY": ("es-UY", "America/Montevideo"),
        "PE": ("es-PE", "America/Lima"),
        "EC": ("es-EC", "America/Guayaquil"),
        "VE": ("es-VE", "America/Caracas"),
        "JP": ("ja-JP", "Asia/Tokyo"),
        "ID": ("id-ID", "Asia/Jakarta"),
    }
    if country and country.upper() in locale_map:
        loc, tz = locale_map[country.upper()]
        kw["locale"] = loc
        kw["timezone_id"] = tz

    # 持久化 cookie · 让 Cloudflare 看作"老客户"
    if persist_profile_key:
        from pathlib import Path
        base = Path("/app/data/stealth_profiles") if Path("/app").exists() else Path("./data/stealth_profiles")
        profile_dir = base / persist_profile_key
        profile_dir.mkdir(parents=True, exist_ok=True)
        kw["user_data_dir"] = str(profile_dir)

    if proxy:
        kw["proxy"] = proxy

    if extra:
        kw.update(extra)

    return kw
