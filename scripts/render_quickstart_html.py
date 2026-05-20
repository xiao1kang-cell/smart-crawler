#!/usr/bin/env python3
"""把 MCP_QUICKSTART.md 渲染成一个可发同事的 HTML。"""
import re, html
from pathlib import Path
from datetime import datetime

SRC = Path("deliverables/MCP_QUICKSTART.md")
OUT = Path("deliverables/MCP_QUICKSTART.html")

md = SRC.read_text(encoding="utf-8")

# Strip h1 (we render our own hero)
md = re.sub(r"^# .*?\n.*?\n.*?\n", "", md, count=1, flags=re.DOTALL)

# Minimal markdown → HTML
def render(md_text):
    # protect code blocks
    blocks = {}
    def stash_block(m):
        key = f"__BLOCK_{len(blocks)}__"
        lang = m.group(1) or ""
        code = html.escape(m.group(2))
        blocks[key] = f'<pre class="codeblock" data-lang="{lang}"><code>{code}</code></pre>'
        return key
    md_text = re.sub(r"```(\w*)\n(.*?)```", stash_block, md_text, flags=re.DOTALL)

    inlines = {}
    def stash_inline(m):
        key = f"__INL_{len(inlines)}__"
        inlines[key] = f'<code>{html.escape(m.group(1))}</code>'
        return key
    md_text = re.sub(r"`([^`]+)`", stash_inline, md_text)

    # tables
    def render_table(m):
        lines = [l for l in m.group(0).strip().split("\n") if l.strip()]
        rows = [[c.strip() for c in l.strip("|").split("|")] for l in lines]
        head, _sep, *body = rows
        thead = "".join(f"<th>{h}</th>" for h in head)
        tbody = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in body)
        return f'<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>'
    md_text = re.sub(r"(\|[^\n]+\|\n\|[ :|\-]+\|\n(?:\|[^\n]+\|\n?)+)", render_table, md_text)

    # headings
    md_text = re.sub(r"^### (.+)$", r"<h3>\1</h3>", md_text, flags=re.M)
    md_text = re.sub(r"^## (.+)$", r"<h2>\1</h2>", md_text, flags=re.M)
    md_text = re.sub(r"^# (.+)$", r"<h1>\1</h1>", md_text, flags=re.M)

    # blockquote
    md_text = re.sub(r"^> (.+)$", r"<blockquote>\1</blockquote>", md_text, flags=re.M)

    # bold + italic + link
    md_text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", md_text)
    md_text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank">\1</a>', md_text)

    # bullets
    md_text = re.sub(r"^(\d+)\. (.+)$", r"<li>\2</li>", md_text, flags=re.M)
    md_text = re.sub(r"^- (.+)$", r"<li>\1</li>", md_text, flags=re.M)
    md_text = re.sub(r"(<li>.*?</li>(?:\n<li>.*?</li>)+)", r"<ul>\1</ul>", md_text, flags=re.DOTALL)

    # hr
    md_text = re.sub(r"^---+$", "<hr>", md_text, flags=re.M)

    # paragraphs
    parts = re.split(r"\n\n+", md_text)
    parts = [p if p.lstrip().startswith(("<", "__BLOCK")) else f"<p>{p}</p>" for p in parts if p.strip()]
    md_text = "\n".join(parts)

    # restore code
    for k, v in inlines.items():
        md_text = md_text.replace(k, v)
    for k, v in blocks.items():
        md_text = md_text.replace(f"<p>{k}</p>", v).replace(k, v)
    return md_text

body = render(md)
now = datetime.now().strftime("%Y-%m-%d")

html_out = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>smart-crawler MCP — 5 分钟接入指南</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html{{scroll-behavior:smooth}}
body{{background:#fafafa;color:#1f2937;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,sans-serif;
     line-height:1.65;padding:0;font-size:15px}}
a{{color:#0066cc;text-decoration:none}}
a:hover{{text-decoration:underline}}

.hero{{background:linear-gradient(135deg,#1a1a2e 0%,#0f3460 100%);color:#fff;padding:56px 32px 40px;text-align:center}}
.hero .badge{{display:inline-block;padding:5px 14px;background:rgba(78,205,196,.15);
              border:1px solid #4ecdc4;border-radius:20px;font-size:0.78rem;color:#4ecdc4;margin-bottom:18px;
              letter-spacing:1.5px;text-transform:uppercase}}
.hero h1{{font-size:2.6rem;font-weight:800;letter-spacing:-1px;line-height:1.15}}
.hero h1 .grad{{background:linear-gradient(90deg,#ff6b35,#f7b731);
                -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.hero .sub{{color:#8a9bb5;font-size:1.05rem;margin-top:12px;max-width:600px;margin-left:auto;margin-right:auto}}
.hero .meta{{margin-top:14px;font-size:0.78rem;color:#566;letter-spacing:1px}}

main{{max-width:880px;margin:0 auto;padding:48px 32px 64px;background:#fff;
      box-shadow:0 0 0 1px #e5e7eb;border-radius:0}}

h1{{font-size:1.6rem;font-weight:700;margin:36px 0 12px;letter-spacing:-0.3px}}
h2{{font-size:1.35rem;font-weight:700;margin:36px 0 14px;padding-bottom:8px;border-bottom:2px solid #f3f4f6;
   letter-spacing:-0.2px;color:#111827}}
h3{{font-size:1.05rem;font-weight:700;margin:24px 0 10px;color:#1f2937}}
p{{margin-bottom:12px;color:#374151}}
strong{{color:#111827;font-weight:600}}

ul{{margin:8px 0 12px 0;padding-left:24px}}
li{{margin-bottom:4px;color:#374151}}

blockquote{{background:#fef9e7;border-left:4px solid #f7b731;
            padding:10px 16px;border-radius:0 6px 6px 0;color:#5a4500;font-style:italic;margin:14px 0}}

table{{border-collapse:collapse;margin:14px 0 20px;width:100%;font-size:0.92rem;
       box-shadow:0 1px 3px rgba(0,0,0,.05);border-radius:8px;overflow:hidden}}
thead{{background:linear-gradient(135deg,#f9fafb,#f3f4f6)}}
th{{text-align:left;padding:10px 14px;border-bottom:1px solid #e5e7eb;font-weight:700;color:#111827;font-size:0.88rem}}
td{{padding:10px 14px;border-bottom:1px solid #f3f4f6;vertical-align:top}}
tbody tr:hover{{background:#fafbfc}}
tbody tr:last-child td{{border-bottom:none}}

code{{font-family:'Fira Code','SF Mono',monospace;font-size:0.86em;
      background:#f1f5f9;color:#1e40af;padding:1px 6px;border-radius:4px}}
.codeblock{{background:#0d1117;color:#e6edf3;padding:18px 22px;border-radius:10px;
            overflow-x:auto;margin:12px 0 20px;font-size:0.84rem;line-height:1.7;
            border:1px solid #1f2937;box-shadow:0 4px 14px rgba(0,0,0,.08)}}
.codeblock code{{background:transparent;color:inherit;padding:0;font-size:inherit}}

hr{{border:none;border-top:1px solid #e5e7eb;margin:28px 0}}

footer{{text-align:center;padding:32px;color:#6b7280;font-size:0.85rem;background:#f9fafb;border-top:1px solid #e5e7eb}}
footer a{{color:#0066cc}}

@media(max-width:760px){{
  main{{padding:32px 20px}}
  .hero h1{{font-size:1.9rem}}
  table{{font-size:0.82rem}}
}}
</style>
</head>
<body>

<div class="hero">
  <div class="badge">SMART-CRAWLER · MCP</div>
  <h1>5 分钟接入 <span class="grad">12 个 Agent 工具</span></h1>
  <p class="sub">把竞品情报 + 亚马逊 VOC + Reddit 社区分析 直接接入到 Claude / Cursor / Cline</p>
  <div class="meta">更新于 {now}</div>
</div>

<main>
{body}
</main>

<footer>
  smart-crawler · <a href="https://smartcrawler.io">smartcrawler.io</a> ·
  <a href="mailto:mcp@smartcrawler.io">mcp@smartcrawler.io</a> ·
  <a href="https://github.com/mguozhen/smart-crawler">GitHub</a>
</footer>

</body>
</html>"""

OUT.write_text(html_out, encoding="utf-8")
print(f"✅ {OUT} ({OUT.stat().st_size//1024} KB)")
