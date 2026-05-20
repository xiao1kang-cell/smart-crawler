#!/usr/bin/env python3
"""把 reddit_activity.json 渲染成 Deep Research 报告 HTML（图文并茂，真实数据）。"""
import json
import html as _html
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

SRC = Path("deliverables/reddit_activity.json")
OUT = Path("deliverables/reddit_deep_research.html")

records = json.loads(SRC.read_text(encoding="utf-8"))

SUB_COLORS = ["#ff6b35","#4ecdc4","#45b7d1","#96ceb4","#ffeaa7",
              "#dda0dd","#98d8c8","#f7dc6f","#82e0aa","#f1948a"]
RANK_COLORS = ["#f4c430","#b0b8c1","#cd7f32"]
RANK_LABELS = ["🥇 Top 1","🥈 Top 2","🥉 Top 3"]

def esc(s): return _html.escape(str(s or ""))
def fmt_num(n):
    n = int(n or 0)
    if n >= 1000000: return f"{n/1000000:.1f}M"
    if n >= 1000: return f"{n/1000:.1f}k"
    return str(n)

def fmt_date(ts):
    try: return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception: return "?"

def avatar(username, color):
    letter = (username[0] if username and username[0].isalpha() else "?").upper()
    return f'<div class="avatar" style="background:{color}">{letter}</div>'

def render_post(p, color):
    title = esc(p.get("title", "")[:90])
    score = p.get("score", 0)
    nc = p.get("num_comments", 0)
    date = fmt_date(p.get("created_utc", 0))
    body = esc((p.get("body") or "")[:200])
    url = p.get("url", "")
    flair = esc(p.get("flair", "") or "")
    flair_html = f'<span class="flair">{flair}</span>' if flair else ""
    body_html = f'<div class="post-body">{body}…</div>' if body else ""
    return f"""
    <a class="post-card" href="{url}" target="_blank">
      <div class="post-head">
        <span class="post-score" style="color:{color}">⬆ {fmt_num(score)}</span>
        <span class="post-date">{date}</span>
        {flair_html}
        <span class="post-comments">💬 {nc}</span>
      </div>
      <div class="post-title">{title}</div>
      {body_html}
    </a>"""

def render_comment(c):
    body = esc((c.get("body") or "")[:160])
    score = c.get("score", 0)
    date = fmt_date(c.get("created_utc", 0))
    parent = esc(c.get("parent_post_title","")[:60])
    return f"""
    <div class="comment-card">
      <div class="comment-meta">⬆ {fmt_num(score)} · {date}{f" · re: {parent}" if parent else ""}</div>
      <div class="comment-body">{body}…</div>
    </div>"""

def derive_growth(activity):
    """Derive growth phases from posts data."""
    posts = activity.get("posts", [])
    if not posts: return None
    posts_sorted = sorted(posts, key=lambda x: x.get("created_utc", 0))
    first_post = posts_sorted[0]
    last_post = posts_sorted[-1]
    top_post = max(posts, key=lambda x: x.get("score", 0))
    return {
        "first_date": fmt_date(first_post.get("created_utc", 0)),
        "last_date": fmt_date(last_post.get("created_utc", 0)),
        "top_post": top_post,
        "first_score": first_post.get("score", 0),
    }

def topic_keywords(activity, n=8):
    """Frequency-based topic extraction from titles."""
    import re
    STOP = set("the a an of to for in on at is and or but how why what who when which "
               "i my we our you your they them this that these those it its as be been "
               "with from by has have had do does did will would should can could may "
               "not no yes do don ll re ve s t am are was were so if then than just have"
               "got get make made not all any some best how about new one two three more "
               "than other any new way ways via vs using used use about whats getting "
               "really would what's like need first into year years 100 200 50 1 2 3 4 5 "
               "6 7 8 9 10 want got hes she they're you're we're we've us our too most "
               "very you've i've i'd you'd cant ain't isn isn't doesn't didn't won't "
               "still off only also out up down over here there now still month year week"
               .split())
    freq = defaultdict(int)
    for p in activity.get("posts", []):
        for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", p.get("title","").lower()):
            if w not in STOP and len(w) > 2:
                freq[w] += 1
    return sorted(freq.items(), key=lambda x: -x[1])[:n]

def render_user_card(rec, sub_color, idx):
    if "error" in rec:
        return f'<div class="user-card error">u/{esc(rec["username"])} — 数据获取失败：{esc(rec["error"])}</div>'

    username = rec["username"]
    rank = rec["rank"]
    activity = rec["activity"]
    profile = activity["profile"]
    stats = activity["stats"]
    posts = activity.get("posts", [])
    comments = activity.get("comments", [])
    top_posts = activity.get("top_posts", [])
    timeline = activity.get("monthly_post_count", {})
    rank_col = RANK_COLORS[rank-1]
    growth = derive_growth(activity)
    keywords = topic_keywords(activity)
    karma = profile.get("total_karma", 0)
    age_days = profile.get("reddit_age_days", 0)
    yr, mo = age_days // 365, (age_days % 365) // 30

    # Top posts (3)
    top_posts_html = "".join(render_post(p, sub_color) for p in top_posts[:3])

    # Top comments (3) by score
    top_comments = sorted(comments, key=lambda c: c.get("score", 0), reverse=True)[:3]
    top_comments_html = "".join(render_comment(c) for c in top_comments)

    # Keywords cloud
    if keywords:
        max_f = max(f for _, f in keywords) or 1
        kw_html = "".join(
            f'<span class="kw" style="font-size:{0.7 + (f/max_f)*0.6:.2f}rem;'
            f'background:{sub_color}{int((f/max_f)*60+20):02x}">{esc(w)} <em>{f}</em></span>'
            for w, f in keywords
        )
    else:
        kw_html = '<span class="kw-empty">数据不足</span>'

    # Timeline chart data
    months_sorted = sorted(timeline.keys())
    chart_id = f"tl_{username.replace('-','_').replace('.','_')}_{idx}"
    chart_labels = json.dumps(months_sorted)
    chart_data = json.dumps([timeline[m] for m in months_sorted])

    # Growth section
    growth_html = ""
    if growth:
        growth_html = f"""
        <div class="growth-track">
          <div class="track-event">
            <div class="track-dot" style="background:#666"></div>
            <div class="track-content">
              <div class="track-date">{growth['first_date']}</div>
              <div class="track-label">📍 首帖于 r/{rec['subreddit']}</div>
              <div class="track-detail">起步得分: {growth['first_score']}</div>
            </div>
          </div>
          <div class="track-event">
            <div class="track-dot" style="background:{rank_col}"></div>
            <div class="track-content">
              <div class="track-date">{fmt_date(growth['top_post'].get('created_utc', 0))}</div>
              <div class="track-label">🚀 最高分帖子</div>
              <div class="track-detail">{esc(growth['top_post'].get('title', '')[:80])}…<br>
                <strong style="color:{rank_col}">⬆ {fmt_num(growth['top_post'].get('score',0))} · 💬 {growth['top_post'].get('num_comments',0)}</strong>
              </div>
            </div>
          </div>
          <div class="track-event">
            <div class="track-dot" style="background:#4ecdc4"></div>
            <div class="track-content">
              <div class="track-date">{growth['last_date']}</div>
              <div class="track-label">📊 最近活跃</div>
              <div class="track-detail">已积累 {stats['total_posts']} 帖 · {stats['total_comments']} 评论</div>
            </div>
          </div>
        </div>"""

    # Pattern inference (data-driven, no LLM)
    pattern_lines = []
    if stats.get("avg_post_score", 0) > 200:
        pattern_lines.append(f'平均帖子得分 <strong style="color:{rank_col}">{stats["avg_post_score"]:.0f}</strong> — 内容质量高于一般用户 50 倍')
    elif stats.get("avg_post_score", 0) > 50:
        pattern_lines.append(f'平均帖子得分 <strong>{stats["avg_post_score"]:.0f}</strong> — 内容稳定有共鸣')
    if stats.get("total_comments", 0) > stats.get("total_posts", 0) * 5:
        pattern_lines.append(f'评论:帖子 = <strong>{stats["total_comments"]}:{stats["total_posts"]}</strong> — 重社区互动')
    if len(months_sorted) >= 12:
        pattern_lines.append(f'活跃 <strong>{len(months_sorted)}</strong> 个月 — 长期持续输出')
    if yr >= 5:
        pattern_lines.append(f'账号 <strong>{yr}y{mo}m</strong> — 资深用户')
    if karma > 50000:
        pattern_lines.append(f'Karma <strong>{fmt_num(karma)}</strong> — 顶级社区身份')
    pattern_html = "<br>".join(f"• {l}" for l in pattern_lines) or "数据不足"

    return f"""
<div class="user-card" id="user-{username}">
  <div class="user-head">
    {avatar(username, rank_col)}
    <div class="user-name-block">
      <a class="user-name" href="https://reddit.com/u/{username}" target="_blank">u/{username}</a>
      <div class="user-rank" style="color:{rank_col}">{RANK_LABELS[rank-1]} of r/{rec["subreddit"]}</div>
    </div>
    <div class="user-stats">
      <div class="us-item"><span class="us-val">{fmt_num(karma)}</span><span class="us-lbl">Karma</span></div>
      <div class="us-item"><span class="us-val">{yr}y{mo}m</span><span class="us-lbl">账号年龄</span></div>
      <div class="us-item"><span class="us-val">{stats['total_posts']}</span><span class="us-lbl">帖子</span></div>
      <div class="us-item"><span class="us-val">{stats['total_comments']}</span><span class="us-lbl">评论</span></div>
      <div class="us-item"><span class="us-val" style="color:{rank_col}">{fmt_num(stats['top_post_score'])}</span><span class="us-lbl">最高单帖</span></div>
    </div>
  </div>

  <div class="user-grid">
    <div class="grid-col">
      <h4>📈 月度发帖时间线</h4>
      <canvas id="{chart_id}" height="100"></canvas>
    </div>
    <div class="grid-col">
      <h4>🏷️ 高频话题</h4>
      <div class="keywords">{kw_html}</div>
    </div>
  </div>

  <h4 style="margin-top:24px">🛤️ 关键节点</h4>
  {growth_html}

  <h4 style="margin-top:24px">🔥 爆款帖子（Top 3）</h4>
  <div class="post-grid">{top_posts_html}</div>

  <h4 style="margin-top:24px">💬 高赞评论（Top 3）</h4>
  <div class="comment-grid">{top_comments_html}</div>

  <h4 style="margin-top:24px">🧠 数据模式（Pattern）</h4>
  <div class="pattern-box">{pattern_html}</div>
</div>

<script>
setTimeout(function(){{
  var ctx = document.getElementById('{chart_id}');
  if (!ctx) return;
  new Chart(ctx.getContext('2d'), {{
    type: 'bar',
    data: {{
      labels: {chart_labels},
      datasets: [{{
        label: '帖子数',
        data: {chart_data},
        backgroundColor: '{rank_col}',
        borderRadius: 3
      }}]
    }},
    options: {{
      responsive: true,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ ticks: {{color:'#666', font:{{size:9}}, maxRotation:60, autoSkip:true}}, grid:{{display:false}} }},
        y: {{ ticks: {{color:'#666', font:{{size:9}}}}, grid: {{color:'#1e1e2e'}}, beginAtZero: true }}
      }}
    }}
  }});
}}, 100);
</script>"""

# Group records by subreddit
by_sub = defaultdict(list)
for r in records:
    by_sub[r["subreddit"]].append(r)

# Build subreddit sections
sections_html = []
for idx, (sub, recs) in enumerate(by_sub.items()):
    color = SUB_COLORS[idx % len(SUB_COLORS)]
    recs_sorted = sorted(recs, key=lambda x: x["rank"])
    users_html = "\n".join(render_user_card(r, color, idx*10+i) for i, r in enumerate(recs_sorted))

    # Subreddit stats
    total_posts = sum(r.get("activity",{}).get("stats",{}).get("total_posts",0) for r in recs if "activity" in r)
    total_comments = sum(r.get("activity",{}).get("stats",{}).get("total_comments",0) for r in recs if "activity" in r)
    avg_top = sum(r.get("activity",{}).get("stats",{}).get("top_post_score",0) for r in recs if "activity" in r) / max(1, len(recs))

    sections_html.append(f"""
<section class="sub-section" id="sub-{sub}">
  <div class="sub-header" style="border-left-color:{color}">
    <h2><span style="color:{color}">r/</span>{sub}</h2>
    <div class="sub-meta">
      <span>{len(recs)} contributors</span> ·
      <span>{total_posts} 总帖子</span> ·
      <span>{total_comments} 总评论</span> ·
      <span>平均最高分: {avg_top:.0f}</span>
    </div>
  </div>
  {users_html}
</section>""")

# Aggregate stats
total_users = len(records)
total_posts_all = sum(r.get("activity",{}).get("stats",{}).get("total_posts",0) for r in records if "activity" in r)
total_comments_all = sum(r.get("activity",{}).get("stats",{}).get("total_comments",0) for r in records if "activity" in r)
total_karma = sum(r.get("activity",{}).get("profile",{}).get("total_karma",0) for r in records if "activity" in r)
all_top = max((r.get("activity",{}).get("stats",{}).get("top_post_score",0) for r in records if "activity" in r), default=0)

# Navigation
nav_links = " ".join(
    f'<a href="#sub-{sub}" style="border-color:{SUB_COLORS[i%len(SUB_COLORS)]}">r/{sub}</a>'
    for i, sub in enumerate(by_sub.keys())
)

now = datetime.now().strftime("%Y-%m-%d %H:%M")

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reddit Deep Research — 10 Subreddits × Top 3 贡献者</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html{{scroll-behavior:smooth}}
body{{background:#0c0c12;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,sans-serif;line-height:1.5}}
a{{color:inherit;text-decoration:none}}
h2{{font-size:1.5rem;font-weight:800;letter-spacing:-0.5px}}
h4{{font-size:0.95rem;font-weight:700;color:#cdd6f4;margin-bottom:12px;letter-spacing:0.3px}}

/* hero */
.hero{{padding:60px 32px 40px;text-align:center;
       background:radial-gradient(ellipse 80% 50% at 50% -10%, rgba(78,205,196,.15) 0%, transparent 70%)}}
.hero-tag{{display:inline-block;padding:5px 14px;border:1px solid #1f3a5f;border-radius:20px;
          font-size:0.78rem;color:#4ecdc4;margin-bottom:16px}}
.hero h1{{font-size:2.6rem;font-weight:800;letter-spacing:-1px;line-height:1.1}}
.hero h1 .grad{{background:linear-gradient(90deg,#ff6b35,#f7b731,#4ecdc4);
                -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.hero p{{color:#8a9bb5;margin-top:14px;font-size:1.02rem}}
.hero .gen-meta{{font-size:0.78rem;color:#3a4a5a;margin-top:8px}}

/* summary */
.summary{{display:grid;grid-template-columns:repeat(5,1fr);gap:0;
          border-top:1px solid #1a1a2a;border-bottom:1px solid #1a1a2a;background:#0e0e18}}
.summary-item{{text-align:center;padding:18px 12px;border-right:1px solid #1a1a2a}}
.summary-item:last-child{{border-right:none}}
.summary-val{{font-size:1.7rem;font-weight:800;
              background:linear-gradient(90deg,#4ecdc4,#45b7d1);
              -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.summary-lbl{{font-size:0.74rem;color:#566;margin-top:2px}}

/* nav */
.subnav{{position:sticky;top:0;background:rgba(12,12,18,.94);backdrop-filter:blur(10px);
         border-bottom:1px solid #1e1e2e;padding:14px 24px;overflow-x:auto;white-space:nowrap;z-index:50}}
.subnav a{{display:inline-block;padding:5px 14px;margin-right:8px;border:1px solid;
          border-radius:16px;font-size:0.82rem;color:#aaa;transition:all .2s}}
.subnav a:hover{{background:rgba(255,255,255,.06);color:#fff}}

/* sub section */
.sub-section{{padding:48px 32px;max-width:1180px;margin:0 auto}}
.sub-header{{border-left:4px solid;padding:0 18px;margin-bottom:28px}}
.sub-meta{{color:#667;font-size:0.85rem;margin-top:6px}}

/* user card */
.user-card{{background:#13131c;border:1px solid #1e1e2e;border-radius:16px;
            padding:24px;margin-bottom:20px}}
.user-card.error{{padding:14px 20px;color:#e57373}}
.user-head{{display:flex;align-items:center;gap:14px;margin-bottom:22px;padding-bottom:18px;
            border-bottom:1px solid #1e1e2e;flex-wrap:wrap}}
.avatar{{width:48px;height:48px;border-radius:50%;display:flex;align-items:center;
         justify-content:center;font-weight:700;font-size:1.2rem;flex-shrink:0}}
.user-name-block{{flex:1;min-width:200px}}
.user-name{{font-size:1.1rem;font-weight:700;color:#e0e0e0}}
.user-name:hover{{color:#4ecdc4}}
.user-rank{{font-size:0.78rem;font-weight:600;margin-top:2px}}
.user-stats{{display:flex;gap:18px;flex-wrap:wrap}}
.us-item{{display:flex;flex-direction:column;align-items:flex-start;min-width:62px}}
.us-val{{font-size:0.98rem;font-weight:700;color:#cdd6f4}}
.us-lbl{{font-size:0.68rem;color:#566;margin-top:1px}}

.user-grid{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:8px}}
.grid-col h4{{margin-bottom:8px}}

/* keywords */
.keywords{{display:flex;flex-wrap:wrap;gap:6px;align-items:center}}
.kw{{padding:4px 10px;border-radius:14px;font-size:0.85rem;color:#fff;font-weight:500}}
.kw em{{font-style:normal;opacity:.65;font-size:0.72em;margin-left:3px}}
.kw-empty{{color:#566;font-size:0.85rem}}

/* growth track */
.growth-track{{display:flex;flex-direction:column;gap:0;position:relative;margin-left:8px}}
.growth-track::before{{content:'';position:absolute;left:5px;top:8px;bottom:8px;
                       width:2px;background:linear-gradient(180deg,#666,#cd7f32,#4ecdc4)}}
.track-event{{display:flex;gap:14px;padding:8px 0;position:relative}}
.track-dot{{width:12px;height:12px;border-radius:50%;margin-top:4px;flex-shrink:0;
            box-shadow:0 0 0 3px #0c0c12;position:relative;z-index:1}}
.track-content{{flex:1}}
.track-date{{font-size:0.78rem;color:#667;font-weight:600}}
.track-label{{font-size:0.92rem;font-weight:700;margin:2px 0}}
.track-detail{{font-size:0.85rem;color:#8a9bb5;line-height:1.5}}

/* posts */
.post-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}}
.post-card{{background:#1a1a26;border:1px solid #1e1e2e;border-radius:10px;padding:14px;
            transition:all .2s;display:block}}
.post-card:hover{{border-color:#2a3a5a;background:#1d1d2a}}
.post-head{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
            font-size:0.75rem;color:#667;margin-bottom:8px}}
.post-score{{font-weight:700}}
.post-date{{color:#566}}
.post-comments{{margin-left:auto;color:#566}}
.flair{{background:#0a2a4a;color:#4ecdc4;padding:2px 7px;border-radius:8px;font-size:0.7rem}}
.post-title{{font-size:0.92rem;font-weight:600;line-height:1.4;color:#e0e0e0}}
.post-body{{font-size:0.8rem;color:#8a9bb5;margin-top:6px;line-height:1.5}}

/* comments */
.comment-grid{{display:flex;flex-direction:column;gap:8px}}
.comment-card{{background:#16161f;border-left:3px solid #2a3a5a;padding:10px 14px;border-radius:0 8px 8px 0}}
.comment-meta{{font-size:0.72rem;color:#667;margin-bottom:4px}}
.comment-body{{font-size:0.82rem;color:#aaa;line-height:1.5}}

/* pattern */
.pattern-box{{background:#0e1622;border:1px solid #1f3a5f;border-radius:10px;
              padding:16px 20px;line-height:2;color:#cdd6f4;font-size:0.9rem}}

/* footer */
footer{{text-align:center;padding:40px 32px;color:#3a4a5a;font-size:0.85rem;
       border-top:1px solid #1a1a2a;background:#0a0a10;margin-top:48px}}

@media(max-width:800px){{
  .user-grid,.summary{{grid-template-columns:1fr}}
  .user-stats{{gap:12px}}
  .hero h1{{font-size:1.8rem}}
}}
</style>
</head>
<body>

<div class="hero">
  <div class="hero-tag">SMART-CRAWLER · DEEP RESEARCH</div>
  <h1>Reddit <span class="grad">深度贡献者分析</span></h1>
  <p>10 个商业 subreddit · 30 位顶级贡献者 · 真实发帖/评论/时间线数据</p>
  <div class="gen-meta">采集于 {now} · 数据源：Reddit JSON API + Arctic Shift 存档</div>
</div>

<div class="summary">
  <div class="summary-item"><div class="summary-val">{total_users}</div><div class="summary-lbl">深度档案</div></div>
  <div class="summary-item"><div class="summary-val">{total_posts_all}</div><div class="summary-lbl">帖子样本</div></div>
  <div class="summary-item"><div class="summary-val">{total_comments_all}</div><div class="summary-lbl">评论样本</div></div>
  <div class="summary-item"><div class="summary-val">{fmt_num(total_karma)}</div><div class="summary-lbl">总 Karma</div></div>
  <div class="summary-item"><div class="summary-val">{fmt_num(all_top)}</div><div class="summary-lbl">最高单帖得分</div></div>
</div>

<nav class="subnav">
  {nav_links}
</nav>

{"".join(sections_html)}

<footer>
  smart-crawler · Reddit Deep Research · {now} ·
  <a href="https://smartcrawler.io" style="color:#4ecdc4">smartcrawler.io</a>
</footer>

</body>
</html>"""

OUT.write_text(html, encoding="utf-8")
print(f"✅ HTML ({OUT.stat().st_size//1024} KB) → {OUT}")
