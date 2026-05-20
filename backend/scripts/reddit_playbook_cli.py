#!/usr/bin/env python3
"""Reddit Playbook CLI — 从命令行生成 subreddit 贡献者 playbook。

用法:
  # 全流程（找 top 3 → 生成 playbook）
  python reddit_playbook_cli.py entrepreneur

  # 指定 top N
  python reddit_playbook_cli.py learnprogramming --top 5

  # 只找贡献者不生成 playbook
  python reddit_playbook_cli.py entrepreneur --contributors-only

  # 只分析某个特定用户
  python reddit_playbook_cli.py entrepreneur --user some_username

  # 指定输出路径
  python reddit_playbook_cli.py entrepreneur --output /tmp/playbook.md

  # 从 MCP 服务端运行（需环境变量 SMARTCRAWLER_API_KEY）
  SC_KEY=sck_xxx python reddit_playbook_cli.py entrepreneur --via-mcp
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 让 CLI 在 backend/ 下运行
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))


def cmd_contributors(args):
    """只输出 top N 贡献者列表。"""
    from app.crawlers.reddit import get_top_contributors
    print(f"🔍 Fetching top {args.top} contributors from r/{args.subreddit}...")
    result = get_top_contributors(args.subreddit, top_n=args.top)
    print(f"\n{'='*60}")
    print(f"r/{args.subreddit} — Top {len(result)} Contributors")
    print('='*60)
    for i, c in enumerate(result, 1):
        karma = c.get('total_karma', c.get('link_karma', 0) + c.get('comment_karma', 0))
        age = c.get('reddit_age_days', 0)
        print(f"\n#{i} u/{c['username']}")
        print(f"   Posts in sub : {c['posts_in_sub']}")
        print(f"   Post score   : {c['post_total_score']}")
        print(f"   Total karma  : {karma:,}")
        print(f"   Account age  : {age//365}y {(age%365)//30}m")
        print(f"   Profile      : {c['profile_url']}")


def cmd_user_playbook(args):
    """为单个用户生成 playbook。"""
    from app.reddit_playbook import generate_user_playbook
    print(f"🔍 Analyzing u/{args.user} in r/{args.subreddit}...")
    result = generate_user_playbook(args.user, subreddit=args.subreddit)
    _write_output(args, result["markdown"],
                  f"playbook_{args.subreddit}_{args.user}.md")
    _print_summary_single(result)


def cmd_subreddit_playbook(args):
    """全流程：top N → playbook for each。"""
    from app.reddit_playbook import generate_subreddit_playbook
    print(f"🚀 Generating playbook for r/{args.subreddit} (top {args.top})...")
    print("   Steps: ① find top contributors → ② fetch activity → ③ LLM analysis")
    print(f"   Estimated time: ~{args.top * 4}-{args.top * 6} minutes\n")
    result = generate_subreddit_playbook(args.subreddit, top_n=args.top)
    _write_output(args, result["combined_markdown"],
                  f"playbook_{args.subreddit}_top{args.top}.md")
    # Also write JSON
    json_path = _default_output_dir() / f"playbook_{args.subreddit}_top{args.top}.json"
    json_path.write_text(
        json.dumps({k: v for k, v in result.items() if k != "combined_markdown"},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n📄 JSON data  → {json_path}")
    _print_summary_multi(result)


def cmd_via_mcp(args):
    """通过 smartcrawler.io MCP 接口调用（需要 API Key）。"""
    import urllib.request
    import urllib.parse

    key = os.environ.get("SC_KEY") or os.environ.get("SMARTCRAWLER_API_KEY")
    if not key:
        print("❌ 需要设置 SC_KEY 或 SMARTCRAWLER_API_KEY 环境变量")
        sys.exit(1)

    tool = "reddit_subreddit_playbook"
    payload = json.dumps({
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": {"subreddit": args.subreddit, "top_n": args.top},
        },
    }).encode()

    req = urllib.request.Request(
        "https://smartcrawler.io/mcp",
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    print(f"📡 Calling {tool} via MCP at smartcrawler.io ...")
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())

    # Parse MCP response
    content = data.get("result", {}).get("content", [{}])
    text = content[0].get("text", json.dumps(data, ensure_ascii=False, indent=2))
    try:
        result = json.loads(text)
        md = result.get("combined_markdown", text)
    except Exception:
        md = text

    _write_output(args, md, f"playbook_{args.subreddit}_top{args.top}_mcp.md")
    print(md[:500] + "\n...")


def _default_output_dir() -> Path:
    d = Path.home() / "smart-crawler-output"
    d.mkdir(exist_ok=True)
    return d


def _write_output(args, content: str, default_name: str):
    if args.output:
        out = Path(args.output)
    else:
        out = _default_output_dir() / default_name
    out.write_text(content, encoding="utf-8")
    print(f"\n✅ Playbook saved → {out}")
    print("   Open with: open " + str(out))


def _print_summary_single(result: dict):
    pb = result.get("playbook", {})
    print(f"\n{'='*60}")
    print(f"u/{result['username']} Playbook Summary")
    print('='*60)
    print(f"Headline : {pb.get('playbook_headline', '-')}")
    print(f"Standing : {pb.get('community_standing', '-')}")
    print(f"Tags     : {', '.join(pb.get('expertise_tags', []))}")
    print(f"\nTL;DR: {pb.get('tldr', '-')}")


def _print_summary_multi(result: dict):
    print(f"\n{'='*60}")
    print(f"r/{result['subreddit']} Playbook — {len(result['contributors'])} users")
    print('='*60)
    for c in result["contributors"]:
        pb = c.get("playbook", {})
        print(f"\n#{c['rank']} u/{c['username']}")
        print(f"   {pb.get('playbook_headline', '-')}")
        print(f"   Tags: {', '.join(pb.get('expertise_tags', []))}")


def main():
    parser = argparse.ArgumentParser(
        description="Reddit Playbook CLI — smart-crawler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("subreddit", help="Subreddit name (without r/)")
    parser.add_argument("--top", "-n", type=int, default=3,
                        help="Top N contributors (default: 3)")
    parser.add_argument("--user", "-u", default=None,
                        help="Analyze a specific user instead of top N")
    parser.add_argument("--contributors-only", action="store_true",
                        help="Only find contributors, skip playbook generation")
    parser.add_argument("--output", "-o", default=None,
                        help="Output path for Markdown (default: ~/smart-crawler-output/)")
    parser.add_argument("--via-mcp", action="store_true",
                        help="Call via smartcrawler.io MCP API (needs SC_KEY env)")
    parser.add_argument("--json-only", action="store_true",
                        help="Output raw JSON instead of Markdown")

    args = parser.parse_args()

    try:
        if args.via_mcp:
            cmd_via_mcp(args)
        elif args.contributors_only:
            cmd_contributors(args)
        elif args.user:
            cmd_user_playbook(args)
        else:
            cmd_subreddit_playbook(args)
    except KeyboardInterrupt:
        print("\n⚠ Interrupted.")
        sys.exit(1)
    except Exception as exc:
        print(f"\n❌ Error: {exc}")
        if os.environ.get("DEBUG"):
            import traceback; traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
