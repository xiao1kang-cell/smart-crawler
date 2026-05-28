"""Generate the 数据校准报告 HTML by querying live /api/coverage."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from urllib import request as urlreq

# Run from inside the smart-crawler container; the API is on localhost.
API_BASE = os.environ.get("SC_API_BASE", "http://localhost:8077")
USERNAME = os.environ.get("SC_USER", "")
PASSWORD = os.environ.get("SC_PASS", "")
OUTPUT = os.environ.get("OUTPUT_PATH", "/app/deliverables/calibration_report.html")


def _post_json(path: str, body: dict) -> dict:
    import json
    req = urlreq.Request(
        f"{API_BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlreq.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_json(path: str, token: str, params: dict | None = None) -> dict:
    import json
    from urllib.parse import urlencode
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urlencode(params)
    req = urlreq.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlreq.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_coverage(include_hidden: bool = False) -> dict:
    login = _post_json("/api/login", {"username": USERNAME, "password": PASSWORD})
    token = login["token"]
    return _get_json("/api/coverage", token, {"include_hidden": "true" if include_hidden else "false"})


STATUS_LABEL = {
    "healthy": ("健康", "#86efac", "rgba(74,222,128,.12)", "rgba(74,222,128,.35)"),
    "warning": ("部分", "#fcd34d", "rgba(251,191,36,.12)", "rgba(251,191,36,.35)"),
    "critical": ("异常", "#fca5a5", "rgba(248,113,113,.12)", "rgba(248,113,113,.35)"),
    "empty": ("待抓取", "#a78bfa", "rgba(167,139,250,.10)", "rgba(167,139,250,.30)"),
}


def render(cov_visible: dict, cov_all: dict) -> str:
    visible = cov_visible["sites"]
    summary_v = cov_visible["summary"]
    all_sites = cov_all["sites"]
    visible_codes = {s["site"] for s in visible}
    hidden = [s for s in all_sites if s["site"] not in visible_codes]

    def fmt_dt(iso: str | None) -> str:
        if not iso:
            return "—"
        try:
            return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return iso

    groups: dict[str, list] = {"healthy": [], "warning": [], "critical": [], "empty": []}
    for s in visible:
        groups.setdefault(s.get("status") or "empty", []).append(s)

    def row(s: dict) -> str:
        label, color, bg, br = STATUS_LABEL.get(s.get("status") or "empty", STATUS_LABEL["empty"])
        cov_pct = s.get("coverage_pct")
        cov_display = f"{cov_pct}%" if cov_pct is not None else "—"
        bar_w = min(cov_pct or 0, 100)
        return f"""
        <tr>
          <td><b>{s['site']}</b></td>
          <td>{s.get('brand', '')}</td>
          <td>{s.get('country', '')}</td>
          <td>{s.get('platform', '')}</td>
          <td style="text-align:right">{int(s.get('estimated_full') or 0):,}</td>
          <td style="text-align:right"><b>{int(s.get('current') or 0):,}</b></td>
          <td>
            <div style="display:flex;align-items:center;gap:8px">
              <div style="flex:1;height:6px;background:#1d1730;border-radius:3px;overflow:hidden;min-width:80px">
                <div style="width:{bar_w}%;height:100%;background:linear-gradient(90deg,{color},{color})"></div>
              </div>
              <span style="font-size:.8rem;color:#fff;min-width:42px;text-align:right">{cov_display}</span>
            </div>
          </td>
          <td><span style="background:{bg};color:{color};border:1px solid {br};padding:2px 8px;border-radius:9px;font-size:.7rem;font-weight:700">{label}</span></td>
          <td style="color:#8888a0;font-size:.78rem">{fmt_dt(s.get('last_crawled'))}</td>
        </tr>
        """

    def section(title: str, key: str, color: str) -> str:
        rows = "".join(row(s) for s in sorted(groups[key], key=lambda x: -(x.get("current") or 0)))
        if not rows:
            return ""
        n = len(groups[key])
        return f"""
        <section>
          <h2 style="color:{color};border-bottom:1px solid #29213a;padding-bottom:10px;margin-bottom:14px">
            {title} <span style="color:#52527a;font-weight:400;font-size:.8em">· {n} 站</span>
          </h2>
          <div style="overflow-x:auto">
            <table>
              <thead><tr>
                <th>站点</th><th>品牌</th><th>国家</th><th>平台</th>
                <th style="text-align:right">应抓 SKU</th>
                <th style="text-align:right">实抓 SKU</th>
                <th>覆盖率</th><th>状态</th><th>最近抓取</th>
              </tr></thead>
              <tbody>{rows}</tbody>
            </table>
          </div>
        </section>
        """

    hidden_rows = "".join(f"""
      <tr>
        <td><b>{h['site']}</b></td>
        <td>{h.get('brand', '')}</td>
        <td>{h.get('country', '')}</td>
        <td>{h.get('platform', '')}</td>
        <td>头部反爬平台 · 需 residential 代理 + 持续投入</td>
      </tr>""" for h in hidden)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_visible = summary_v.get("total_sites", 0)
    total_current = summary_v.get("total_current_sku", 0)
    total_est = summary_v.get("total_estimated_full", 0)
    overall_pct = summary_v.get("overall_coverage_pct", 0)
    healthy_n = summary_v.get("healthy_count", 0)
    warning_n = summary_v.get("warning_count", 0)
    critical_n = summary_v.get("critical_count", 0)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>smart-crawler · 数据校准报告 · {generated_at}</title>
<style>
  body{{background:#0d0a18;color:#e0dff0;font-family:-apple-system,"PingFang SC","Segoe UI",sans-serif;margin:0;padding:32px;line-height:1.5}}
  .wrap{{max-width:1240px;margin:0 auto}}
  h1{{font-size:1.8rem;font-weight:900;color:#fff;margin:0 0 6px}}
  .sub{{color:#8888a0;font-size:.95rem;margin-bottom:24px}}
  .summary{{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:32px}}
  .stat{{background:#13111f;border:1px solid #29213a;border-radius:11px;padding:14px}}
  .stat .lbl{{font-size:.66rem;color:#52527a;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px}}
  .stat .val{{font-size:1.5rem;font-weight:900;color:#fff}}
  .stat .delta{{font-size:.75rem;color:#a78bfa;margin-top:2px}}
  table{{width:100%;border-collapse:collapse;background:#13111f;border:1px solid #29213a;border-radius:11px;overflow:hidden}}
  th{{text-align:left;padding:11px 14px;background:#181024;color:#8888a0;font-size:.74rem;letter-spacing:.5px;text-transform:uppercase;border-bottom:1px solid #29213a}}
  td{{padding:11px 14px;border-bottom:1px solid #1d1730;font-size:.86rem}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#181024}}
  section{{margin-bottom:34px}}
  .note{{background:#13111f;border:1px solid #29213a;border-left:3px solid #a78bfa;padding:14px 18px;border-radius:8px;margin-bottom:24px;color:#c4b5fd;font-size:.88rem;line-height:1.7}}
</style>
</head>
<body>
<div class="wrap">
  <h1>smart-crawler · 数据校准报告</h1>
  <div class="sub">生成时间 {generated_at} · 共 {total_visible} 个 dashboard 可见站点 · 隐藏 {len(hidden)} 个头部反爬平台</div>

  <div class="note">
    <b>口径说明：</b><br>
    1) <b>应抓 SKU</b> = 当前架构下 24 小时窗口内能稳定产出的上限（不是站点理论总量）。Vidaxl 系列受住宅代理 + 反爬限流约束，单站上限设定为 6,000–12,000；如未来接入官方 Dropshipping API 可大幅上调。<br>
    2) <b>覆盖率</b> = 实抓 / 应抓。健康 ≥ 50% · 部分 5–50% · 异常 &lt; 5% · 待抓取 = 0 SKU。<br>
    3) <b>隐藏站点</b>：10 个头部反爬平台（aliexpress / wayfair / ebay / target / etsy / allegro / otto / bestbuy / walmart / ikea）已从 dashboard 移除，需到位住宅代理 + 平台特化采集器后再上架。
  </div>

  <div class="summary">
    <div class="stat"><div class="lbl">可见站点</div><div class="val">{total_visible}</div></div>
    <div class="stat"><div class="lbl">实抓 SKU 累计</div><div class="val">{total_current:,}</div></div>
    <div class="stat"><div class="lbl">应抓 SKU 累计</div><div class="val">{total_est:,}</div></div>
    <div class="stat"><div class="lbl">整体覆盖率</div><div class="val" style="color:#86efac">{overall_pct}%</div></div>
    <div class="stat"><div class="lbl">健康 / 部分 / 异常</div><div class="val" style="font-size:1.1rem">{healthy_n} / {warning_n} / {critical_n}</div></div>
    <div class="stat"><div class="lbl">隐藏（规划中）</div><div class="val" style="color:#64647a">{len(hidden)}</div></div>
  </div>

  {section('健康站点 · Healthy', 'healthy', '#86efac')}
  {section('部分覆盖 · Warning', 'warning', '#fcd34d')}
  {section('异常 · Critical', 'critical', '#fca5a5')}
  {section('待抓取 · Empty', 'empty', '#a78bfa')}

  <section>
    <h2 style="color:#64647a;border-bottom:1px solid #29213a;padding-bottom:10px;margin-bottom:14px">
      隐藏站点 · 暂不展示 <span style="color:#52527a;font-weight:400;font-size:.8em">· {len(hidden)} 站</span>
    </h2>
    <div style="overflow-x:auto">
      <table>
        <thead><tr><th>站点</th><th>品牌</th><th>国家</th><th>平台</th><th>原因</th></tr></thead>
        <tbody>{hidden_rows}</tbody>
      </table>
    </div>
  </section>

  <div style="text-align:center;color:#52527a;font-size:.78rem;margin-top:32px">
    smart-crawler · smartcrawler.io · 数据校准 v1 · {generated_at}
  </div>
</div>
</body>
</html>
"""


def main():
    if not USERNAME or not PASSWORD:
        print("ERROR: SC_USER / SC_PASS env required", file=sys.stderr)
        sys.exit(1)
    cov_visible = fetch_coverage(include_hidden=False)
    cov_all = fetch_coverage(include_hidden=True)
    html = render(cov_visible, cov_all)
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"OK: {OUTPUT} ({len(html):,} bytes, {len(cov_visible['sites'])} visible, {len(cov_all['sites']) - len(cov_visible['sites'])} hidden)")


if __name__ == "__main__":
    main()
