#!/usr/bin/env python3
"""采集 10 个示范 subreddit 的 top 3 贡献者，保存为 JSON。"""
import sys, json, time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from app.crawlers.reddit import get_top_contributors

SUBREDDITS = [
    "entrepreneur",
    "ecommerce",
    "AmazonFBA",
    "dropshipping",
    "smallbusiness",
    "startups",
    "marketing",
    "SEO",
    "SideProject",
    "growthhacking",
]

OUT = Path(__file__).resolve().parent.parent / "deliverables" / "reddit_demo.json"
OUT.parent.mkdir(exist_ok=True)

results = []
for i, sub in enumerate(SUBREDDITS):
    print(f"[{i+1}/{len(SUBREDDITS)}] r/{sub} …", flush=True)
    try:
        contributors = get_top_contributors(sub, top_n=3)
        results.append({"subreddit": sub, "contributors": contributors, "error": None})
        print(f"  → {len(contributors)} contributors found")
    except Exception as e:
        results.append({"subreddit": sub, "contributors": [], "error": str(e)})
        print(f"  ❌ {e}")

payload = {
    "generated_at": datetime.now().isoformat(),
    "subreddits": results,
}
OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n✅ Saved → {OUT}")
