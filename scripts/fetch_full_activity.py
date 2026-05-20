#!/usr/bin/env python3
"""为 30 个贡献者抓取完整 activity 数据（不调 LLM），保存为 JSON。"""
import sys, json, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from app.crawlers.reddit import get_user_activity

SRC = Path("deliverables/reddit_demo.json")
OUT = Path("deliverables/reddit_activity.json")

if OUT.exists():
    existing = json.loads(OUT.read_text(encoding="utf-8"))
    done = {(r["subreddit"], r["username"]) for r in existing if "activity" in r}
    existing = [r for r in existing if "activity" in r]
else:
    existing, done = [], set()

demo = json.loads(SRC.read_text(encoding="utf-8"))
total = sum(len(s["contributors"]) for s in demo["subreddits"])
i = 0
for sub_data in demo["subreddits"]:
    sub = sub_data["subreddit"]
    for rank, c in enumerate(sub_data["contributors"], 1):
        i += 1
        username = c["username"]
        if (sub, username) in done:
            print(f"[{i}/{total}] r/{sub} u/{username} — 跳过")
            continue
        print(f"[{i}/{total}] r/{sub} #{rank} u/{username} …", flush=True)
        t0 = time.time()
        try:
            activity = get_user_activity(username, subreddit=sub,
                                         post_limit=100, comment_limit=100)
            existing.append({
                "subreddit": sub, "rank": rank, "username": username,
                "contributor_stats": c,
                "activity": activity,
            })
            print(f"   posts={activity['stats']['total_posts']} "
                  f"comments={activity['stats']['total_comments']} "
                  f"top={activity['stats']['top_post_score']} ({time.time()-t0:.1f}s)")
        except Exception as e:
            print(f"   ❌ {e}")
            existing.append({
                "subreddit": sub, "rank": rank, "username": username,
                "contributor_stats": c, "error": str(e),
            })
        OUT.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"\n✅ {len(existing)} → {OUT}")
