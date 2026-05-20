#!/usr/bin/env python3
"""
从 reddit_demo.json 的 30 个贡献者出发，
为每人跑完整流水线：get_user_activity + LLM playbook。
已处理的用户会跳过（可断点续跑）。
"""
import sys, json, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from app.crawlers.reddit import get_user_activity
from app.reddit_playbook import generate_user_playbook

SRC  = Path("deliverables/reddit_demo.json")
OUT  = Path("deliverables/reddit_playbooks.json")

# 加载已有采集结果（支持断点续跑，跳过无 error 的成功记录）
if OUT.exists():
    existing = json.loads(OUT.read_text(encoding="utf-8"))
    # 只跳过有 playbook 的成功记录，error 记录需要重跑
    done = {(r["subreddit"], r["username"]) for r in existing if "playbook" in r}
    # 清掉 error 记录，重跑时重新填
    existing = [r for r in existing if "playbook" in r]
else:
    existing = []
    done = set()

demo = json.loads(SRC.read_text(encoding="utf-8"))

total = sum(len(s["contributors"]) for s in demo["subreddits"])
processed = 0

for sub_data in demo["subreddits"]:
    sub = sub_data["subreddit"]
    for rank, c in enumerate(sub_data["contributors"], 1):
        username = c["username"]
        processed += 1
        tag = f"[{processed}/{total}] r/{sub} #{rank} u/{username}"

        if (sub, username) in done:
            print(f"{tag} — 已完成，跳过")
            continue

        print(f"\n{tag}", flush=True)
        print("  ① 抓取帖子 + 评论…", flush=True)
        t0 = time.time()

        try:
            activity = get_user_activity(
                username, subreddit=sub,
                post_limit=100, comment_limit=100,
            )
            print(f"     posts={activity['stats']['total_posts']} "
                  f"comments={activity['stats']['total_comments']} "
                  f"avg_score={activity['stats']['avg_post_score']} "
                  f"({time.time()-t0:.1f}s)")

            print("  ② LLM 生成 playbook…", flush=True)
            t1 = time.time()
            pb_result = generate_user_playbook(
                username, subreddit=sub, activity=activity,
            )
            print(f"     headline: {pb_result['playbook'].get('playbook_headline','?')[:60]} ({time.time()-t1:.1f}s)")

            existing.append({
                "subreddit": sub,
                "rank": rank,
                "username": username,
                "contributor_stats": c,
                "activity_stats": activity["stats"],
                "profile": activity["profile"],
                "playbook": pb_result["playbook"],
                "markdown": pb_result["markdown"],
                "top_posts": activity["top_posts"][:3],
                "monthly_post_count": activity["monthly_post_count"],
            })
            done.add((sub, username))

            # 每完成一个就写盘（防丢失）
            OUT.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

        except Exception as e:
            print(f"  ❌ {e}")
            existing.append({
                "subreddit": sub, "rank": rank, "username": username,
                "contributor_stats": c, "error": str(e),
            })
            OUT.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"\n✅ 全部完成 → {OUT}  ({len(existing)} 条记录)")
