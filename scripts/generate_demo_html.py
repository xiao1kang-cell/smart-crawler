#!/usr/bin/env python3
"""把 reddit_demo.json 渲染成图文并茂的 HTML 展示页。"""
import json
from pathlib import Path
from datetime import datetime

SRC = Path(__file__).resolve().parent.parent / "deliverables" / "reddit_demo.json"
OUT = Path(__file__).resolve().parent.parent / "deliverables" / "reddit_demo.html"

data = json.load(open(SRC, encoding="utf-8"))
gen_time = datetime.fromisoformat(data["generated_at"]).strftime("%Y-%m-%d %H:%M")

RANK_COLORS = ["#f4c430", "#b0b8c1", "#cd7f32"]
RANK_LABELS = ["🥇 #1", "🥈 #2", "🥉 #3"]
SUB_COLORS  = [
    "#ff6b35","#4ecdc4","#45b7d1","#96ceb4","#ffeaa7",
    "#dda0dd","#98d8c8","#f7dc6f","#82e0aa","#f1948a",
]

def avatar(username: str, color: str) -> str:
    letter = (username[0] if username and username[0].isalpha() else "?").upper()
    return (
        f'<div class="avatar" style="background:{color}">{letter}</div>'
    )

def karma_bar(value: int, max_val: int, color: str) -> str:
    pct = min(100, round(value / max(1, max_val) * 100))
    return (
        f'<div class="bar-wrap">'
        f'<div class="bar-fill" style="width:{pct}%;background:{color}"></div>'
        f'</div>'
    )

def age_badge(days: int) -> str:
    if days == 0:
        return '<span class="badge badge-gray">未知</span>'
    yrs = days // 365
    mos = (days % 365) // 30
    if yrs >= 5:
        cls = "badge-gold"
    elif yrs >= 2:
        cls = "badge-blue"
    else:
        cls = "badge-gray"
    label = f"{yrs}y {mos}m" if yrs else f"{mos}m"
    return f'<span class="badge {cls}">{label}</span>'

def score_fmt(n: int) -> str:
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)

def contributor_card(c: dict, rank: int, max_karma: int, max_score: int, color: str) -> str:
    username = c["username"]
    karma    = c.get("total_karma") or c.get("link_karma", 0) + c.get("comment_karma", 0)
    score    = c.get("post_total_score", 0)
    posts    = c.get("posts_in_sub", 0)
    url      = c.get("profile_url", f"https://reddit.com/u/{username}")
    rank_col = RANK_COLORS[rank]

    return f"""
<div class="contributor">
  <div class="contributor-header">
    {avatar(username, rank_col)}
    <div class="contributor-meta">
      <a class="username" href="{url}" target="_blank">u/{username}</a>
      <div class="rank-label" style="color:{rank_col}">{RANK_LABELS[rank]}</div>
    </div>
    {age_badge(c.get("reddit_age_days", 0))}
  </div>
  <div class="stats-grid">
    <div class="stat">
      <span class="stat-label">Karma</span>
      <span class="stat-val">{score_fmt(karma)}</span>
      {karma_bar(karma, max_karma, rank_col)}
    </div>
    <div class="stat">
      <span class="stat-label">帖子得分</span>
      <span class="stat-val">{score_fmt(score)}</span>
      {karma_bar(score, max_score, rank_col)}
    </div>
    <div class="stat">
      <span class="stat-label">发帖数</span>
      <span class="stat-val">{posts}</span>
    </div>
  </div>
</div>"""

def subreddit_block(sub_data: dict, idx: int) -> str:
    sub     = sub_data["subreddit"]
    contribs = sub_data["contributors"]
    err     = sub_data.get("error")
    color   = SUB_COLORS[idx % len(SUB_COLORS)]

    if err or not contribs:
        return f"""
<div class="sub-card">
  <div class="sub-header" style="background:{color}">
    <span class="sub-icon">r/</span>
    <span class="sub-name">{sub}</span>
  </div>
  <div class="error-note">❌ 采集失败：{err or "无数据"}</div>
</div>"""

    max_karma = max(
        (c.get("total_karma") or c.get("link_karma", 0) + c.get("comment_karma", 0))
        for c in contribs
    ) or 1
    max_score = max(c.get("post_total_score", 0) for c in contribs) or 1

    cards_html = "".join(
        contributor_card(c, i, max_karma, max_score, color)
        for i, c in enumerate(contribs)
    )

    # mini chart data for Chart.js
    chart_id = f"chart_{sub}"
    labels   = json.dumps([f"u/{c['username'][:12]}" for c in contribs])
    karma_data = json.dumps([
        c.get("total_karma") or c.get("link_karma", 0) + c.get("comment_karma", 0)
        for c in contribs
    ])
    score_data = json.dumps([c.get("post_total_score", 0) for c in contribs])
    bg_colors  = json.dumps(RANK_COLORS[:len(contribs)])

    return f"""
<div class="sub-card">
  <div class="sub-header" style="background:{color}">
    <span class="sub-icon">r/</span>
    <span class="sub-name">{sub}</span>
    <span class="sub-count">{len(contribs)} contributors</span>
  </div>
  <div class="sub-body">
    <div class="contributors-list">
      {cards_html}
    </div>
    <div class="chart-wrap">
      <canvas id="{chart_id}" height="180"></canvas>
    </div>
  </div>
</div>
<script>
(function(){{
  var ctx = document.getElementById('{chart_id}').getContext('2d');
  new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: {labels},
      datasets: [
        {{label:'Total Karma', data:{karma_data}, backgroundColor:{bg_colors}, borderRadius:6}},
        {{label:'Post Score',  data:{score_data}, backgroundColor:{bg_colors}.map(c=>c+'88'), borderRadius:6}}
      ]
    }},
    options: {{
      responsive:true,
      plugins:{{legend:{{labels:{{color:'#ccc',font:{{size:11}}}}}}}},
      scales:{{
        x:{{ticks:{{color:'#aaa'}},grid:{{color:'#2a2a2a'}}}},
        y:{{ticks:{{color:'#aaa'}},grid:{{color:'#2a2a2a'}}}}
      }}
    }}
  }});
}})();
</script>"""

# ── assemble page ──────────────────────────────────────────────────────────────

total_contribs = sum(len(s["contributors"]) for s in data["subreddits"])
body_blocks = "\n".join(
    subreddit_block(s, i) for i, s in enumerate(data["subreddits"])
)

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reddit Intelligence Demo — smart-crawler</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0f0f13;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}}
a{{color:inherit;text-decoration:none}}
a:hover{{text-decoration:underline}}

/* hero */
.hero{{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f3460 100%);
       padding:48px 32px 32px;text-align:center;border-bottom:1px solid #1f3a5f}}
.hero-logo{{display:flex;align-items:center;justify-content:center;gap:12px;margin-bottom:16px}}
.logo-icon{{width:40px;height:40px;background:linear-gradient(135deg,#ff6b35,#f7b731);
           border-radius:10px;display:flex;align-items:center;justify-content:center;
           font-size:20px;font-weight:700}}
.hero h1{{font-size:2rem;font-weight:800;letter-spacing:-0.5px}}
.hero h1 span{{background:linear-gradient(90deg,#ff6b35,#f7b731);
               -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.hero p{{color:#8a9bb5;margin-top:8px;font-size:0.95rem}}
.hero-meta{{color:#566;font-size:0.82rem;margin-top:12px}}

/* summary strip */
.summary{{display:flex;justify-content:center;gap:32px;padding:20px 32px;
          background:#13131a;border-bottom:1px solid #1e1e2e;flex-wrap:wrap}}
.summary-stat{{text-align:center}}
.summary-stat .val{{font-size:1.8rem;font-weight:700;
                    background:linear-gradient(90deg,#4ecdc4,#45b7d1);
                    -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.summary-stat .lbl{{font-size:0.78rem;color:#667;margin-top:2px}}

/* grid */
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(680px,1fr));
       gap:24px;padding:32px;max-width:1600px;margin:0 auto}}

/* card */
.sub-card{{background:#16161f;border:1px solid #222;border-radius:16px;overflow:hidden;
           box-shadow:0 4px 20px rgba(0,0,0,.4)}}
.sub-header{{display:flex;align-items:center;gap:10px;padding:14px 20px}}
.sub-icon{{font-weight:800;font-size:1.1rem;opacity:.7}}
.sub-name{{font-size:1.15rem;font-weight:700;letter-spacing:.5px}}
.sub-count{{margin-left:auto;font-size:0.78rem;background:rgba(0,0,0,.25);
            padding:3px 10px;border-radius:20px}}
.sub-body{{display:grid;grid-template-columns:1fr 260px;gap:0}}
.contributors-list{{padding:16px;border-right:1px solid #1e1e2e}}
.chart-wrap{{padding:16px 12px;display:flex;align-items:center}}

/* contributor */
.contributor{{background:#1c1c28;border-radius:12px;padding:14px;margin-bottom:10px}}
.contributor:last-child{{margin-bottom:0}}
.contributor-header{{display:flex;align-items:center;gap:12px;margin-bottom:12px}}
.avatar{{width:38px;height:38px;border-radius:50%;display:flex;align-items:center;
         justify-content:center;font-weight:700;font-size:1rem;flex-shrink:0}}
.contributor-meta{{flex:1}}
.username{{font-size:0.9rem;font-weight:600;color:#e0e0e0}}
.rank-label{{font-size:0.75rem;margin-top:2px;font-weight:600}}

.stats-grid{{display:grid;grid-template-columns:1fr 1fr auto;gap:10px;align-items:end}}
.stat{{}}
.stat-label{{font-size:0.7rem;color:#667;display:block;margin-bottom:3px}}
.stat-val{{font-size:0.92rem;font-weight:600}}
.bar-wrap{{background:#2a2a38;border-radius:4px;height:5px;margin-top:5px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:4px;transition:width .5s ease}}

/* badge */
.badge{{font-size:0.72rem;padding:3px 9px;border-radius:20px;font-weight:600;white-space:nowrap}}
.badge-gold{{background:#3d2e00;color:#f4c430}}
.badge-blue{{background:#0a2a4a;color:#4ecdc4}}
.badge-gray{{background:#1e1e2e;color:#667}}

.error-note{{padding:20px;color:#e57373;font-size:0.9rem}}

/* cta */
.cta{{background:linear-gradient(135deg,#1a1a2e,#0f3460);
      border:1px solid #1f3a5f;border-radius:16px;
      padding:32px;margin:0 32px 32px;text-align:center}}
.cta h2{{font-size:1.4rem;font-weight:700;margin-bottom:12px}}
.cta h2 span{{color:#f7b731}}
.cta p{{color:#8a9bb5;margin-bottom:20px;line-height:1.6}}
.code-block{{background:#0d0d14;border:1px solid #1e1e3a;border-radius:10px;
             padding:16px 20px;text-align:left;font-family:'Fira Code',monospace;
             font-size:0.82rem;color:#82aaff;line-height:1.8;max-width:700px;margin:0 auto}}
.code-block .comment{{color:#546}}
.code-block .kw{{color:#c792ea}}
.code-block .str{{color:#c3e88d}}
.pill-row{{display:flex;justify-content:center;gap:12px;margin-top:20px;flex-wrap:wrap}}
.pill{{padding:8px 20px;border-radius:24px;font-size:0.82rem;font-weight:600;cursor:pointer}}
.pill-orange{{background:linear-gradient(90deg,#ff6b35,#f7b731);color:#000}}
.pill-outline{{border:1px solid #2a3a5a;color:#8a9bb5}}

/* footer */
.footer{{text-align:center;padding:24px;color:#3a4a5a;font-size:0.8rem;border-top:1px solid #1e1e2e}}

@media(max-width:800px){{
  .sub-body{{grid-template-columns:1fr}}
  .chart-wrap{{border-top:1px solid #1e1e2e;border-right:none}}
  .grid{{grid-template-columns:1fr;padding:16px}}
  .stats-grid{{grid-template-columns:1fr 1fr}}
}}
</style>
</head>
<body>

<div class="hero">
  <div class="hero-logo">
    <div class="logo-icon">SC</div>
    <div>
      <div style="font-size:0.75rem;color:#4ecdc4;letter-spacing:2px;text-transform:uppercase">smart-crawler</div>
      <h1>Reddit <span>Intelligence</span></h1>
    </div>
  </div>
  <p>从 subreddit 自动发现顶级贡献者 · 提炼成长路径 · 生成可复制 Playbook</p>
  <div class="hero-meta">Demo · 采集于 {gen_time} · 数据来源：Reddit JSON API + Arctic Shift</div>
</div>

<div class="summary">
  <div class="summary-stat"><div class="val">{len(data['subreddits'])}</div><div class="lbl">Subreddits</div></div>
  <div class="summary-stat"><div class="val">{total_contribs}</div><div class="lbl">贡献者档案</div></div>
  <div class="summary-stat"><div class="val">0</div><div class="lbl">API Key 需求</div></div>
  <div class="summary-stat"><div class="val">3</div><div class="lbl">MCP Tools</div></div>
  <div class="summary-stat"><div class="val">~2min</div><div class="lbl">Full Playbook 耗时</div></div>
</div>

<div class="grid">
{body_blocks}
</div>

<div class="cta">
  <h2>一键生成完整 <span>Playbook</span></h2>
  <p>smart-crawler 提供 3 个 MCP 工具，让 AI Agent 直接调用 Reddit 情报能力。<br>
     无需 API Key，无需 OAuth，支持历史存档（含已删帖）。</p>
  <div class="code-block">
<span class="comment"># MCP Tool 调用示例（任意 Agent / Claude / GPT）</span>

<span class="kw">reddit_subreddit_playbook</span>(<span class="str">"entrepreneur"</span>, top_n=<span class="str">3</span>)
<span class="comment"># → 自动找 top 3 贡献者 → 抓帖子/评论 → LLM 生成 playbook</span>
<span class="comment"># → 返回成长时间线 / 内容公式 / 5步可复制路径</span>

<span class="kw">reddit_top_contributors</span>(<span class="str">"AmazonFBA"</span>, top_n=<span class="str">5</span>)
<span class="comment"># → 返回 karma / 年龄 / 帖子统计</span>

<span class="kw">reddit_user_activity</span>(<span class="str">"username"</span>, subreddit=<span class="str">"SEO"</span>)
<span class="comment"># → 完整发帖记录 + 月度活跃时间线 + top 5 爆款帖</span>
  </div>
  <div class="pill-row">
    <a class="pill pill-orange" href="https://smartcrawler.io/docs/reddit" target="_blank">查看文档</a>
    <a class="pill pill-outline" href="https://smartcrawler.io/mcp" target="_blank">MCP Endpoint</a>
    <a class="pill pill-outline" href="https://github.com/mguozhen/smart-crawler" target="_blank">GitHub</a>
  </div>
</div>

<div class="footer">
  smart-crawler · Reddit Intelligence · {gen_time} ·
  <a href="https://smartcrawler.io" style="color:#4ecdc4">smartcrawler.io</a>
</div>

</body>
</html>"""

OUT.write_text(html, encoding="utf-8")
print(f"✅ HTML → {OUT}")
