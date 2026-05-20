"""Reddit Playbook 生成器 —— 通过 LLM 将用户活动数据提炼为可复制 playbook。

场景：给定一个 subreddit，找 top N 贡献者，为每人生成：
  · 成长时间线（起步→爆发→成熟）
  · 核心内容公式（什么话题/形式/时机有效）
  · 可复制步骤（别人怎么跟着做）

LLM 网关与 nlp.py 一致（flatkey.ai + OpenAI SDK）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .crawlers.reddit import get_top_contributors, get_user_activity

GATEWAY = os.environ.get("LLM_BASE_URL", "https://app.flatkey.ai/v1")
PLAYBOOK_MODEL = os.environ.get("LLM_PLAYBOOK_MODEL",
                                os.environ.get("LLM_MODEL", "gpt-5.4-mini"))


def _llm_client():
    from openai import OpenAI
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("未配置 OPENAI_API_KEY（flatkey.ai 密钥）")
    return OpenAI(base_url=GATEWAY, api_key=key)


_SYSTEM_PLAYBOOK = """\
你是社区运营分析专家。给定一位 Reddit 用户在某 subreddit 的完整发帖/评论数据，
请提炼出这位用户的成长路径和可复制方法论。

只输出 JSON，字段如下：
{
  "profile_headline": "一句话描述此人在该社区的定位（中文，≤30字）",
  "expertise_tags": ["话题标签1","话题标签2","话题标签3"],
  "community_standing": "新手|成长|成熟|KOL（根据 karma+年龄+帖子质量判断）",
  "growth_phases": [
    {
      "phase": "起步期",
      "timeframe": "账号前 X 个月",
      "key_behavior": "此阶段典型行为",
      "karma_milestone": "~X"
    }
    // 共 2-4 个阶段
  ],
  "content_formula": "此人成功内容的核心规律（类型+话题+形式，≤60字）",
  "top_post_analysis": [
    {
      "title": "帖子标题（截断≤40字）",
      "score": 0,
      "why_it_worked": "10字以内原因"
    }
    // 取分数最高的 3 篇
  ],
  "replicable_steps": [
    {
      "step": 1,
      "action": "具体可执行动作",
      "why": "背后逻辑",
      "timeframe": "预期时间"
    }
    // 共 5 步
  ],
  "playbook_headline": "一句话 playbook 标题（英文或中文，有冲击力，≤20字）",
  "tldr": "给同行者的一段话（2-3句，启发性，中文）"
}

分析要求：
- 用数据说话，引用帖子标题或分数作证据
- 时间线要基于帖子的 created_utc 时间推算
- replicable_steps 要具体可操作，不能写"提高内容质量"这种废话
- 如果数据不足（posts < 5），在 tldr 里说明
"""


def _build_user_context(activity: dict) -> str:
    """把 activity dict 压缩成 LLM prompt 友好的文本。"""
    u = activity["username"]
    sub = activity.get("subreddit_filter") or "all"
    profile = activity["profile"]
    stats = activity["stats"]
    top_posts = activity["top_posts"][:5]
    timeline = activity["monthly_post_count"]

    lines = [
        f"用户名: u/{u}",
        f"目标 subreddit: r/{sub}",
        f"账号年龄: {profile['reddit_age_days']} 天",
        f"link karma: {profile['link_karma']}, comment karma: {profile['comment_karma']}",
        f"本次分析帖子数: {stats['total_posts']}, 评论数: {stats['total_comments']}",
        f"平均帖子分: {stats['avg_post_score']}, 最高帖子分: {stats['top_post_score']}",
        "",
        "活跃时间线（年月:发帖数）:",
        json.dumps(timeline, ensure_ascii=False),
        "",
        f"Top {len(top_posts)} 帖子（按分数）:",
    ]
    for i, p in enumerate(top_posts, 1):
        ts = p.get("created_utc", 0)
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m") if ts else "?"
        lines.append(
            f"  [{i}] {p['title'][:60]} | "
            f"score={p['score']} | comments={p['num_comments']} | "
            f"flair={p['flair'] or '-'} | {date}"
        )
        if p.get("body"):
            lines.append(f"      摘要: {p['body'][:100]}")

    # Sample comments
    comments = sorted(activity["comments"], key=lambda c: c["score"], reverse=True)[:5]
    if comments:
        lines += ["", "Top 评论（按分数）:"]
        for c in comments:
            lines.append(f"  score={c['score']} | {c['body'][:80]}")

    return "\n".join(lines)


def generate_user_playbook(username: str, subreddit: str | None = None,
                            activity: dict | None = None,
                            proxy: str | None = None) -> dict:
    """为单个 Reddit 用户生成 playbook。

    activity 可外部传入（避免重复请求）；否则自动抓取。
    返回：{username, subreddit, activity_stats, playbook（LLM 输出）, markdown}
    """
    if activity is None:
        activity = get_user_activity(username, subreddit=subreddit,
                                     proxy=proxy)

    context = _build_user_context(activity)
    client = _llm_client()
    resp = client.chat.completions.create(
        model=PLAYBOOK_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PLAYBOOK},
            {"role": "user", "content": context},
        ],
        response_format={"type": "json_object"},
    )
    playbook = json.loads(resp.choices[0].message.content)

    markdown = _render_markdown(username, subreddit or "all", activity, playbook)
    return {
        "username": username,
        "subreddit": subreddit,
        "activity_stats": activity["stats"],
        "profile": activity["profile"],
        "playbook": playbook,
        "markdown": markdown,
    }


def _render_markdown(username: str, subreddit: str,
                     activity: dict, pb: dict) -> str:
    stats = activity["stats"]
    profile = activity["profile"]
    top_posts = activity["top_posts"][:3]
    age_yrs = profile["reddit_age_days"] // 365
    age_mo = (profile["reddit_age_days"] % 365) // 30

    phases_md = "\n".join(
        f"  - **{ph['phase']}** ({ph['timeframe']}): {ph['key_behavior']}  \n"
        f"    *karma 里程碑: {ph['karma_milestone']}*"
        for ph in pb.get("growth_phases", [])
    )

    steps_md = "\n".join(
        f"{s['step']}. **{s['action']}**  \n"
        f"   *为什么: {s['why']}* · 时间: {s['timeframe']}"
        for s in pb.get("replicable_steps", [])
    )

    top_analysis_md = "\n".join(
        f"- [{t['title']}] ({t.get('score',0)} pts) — {t['why_it_worked']}"
        for t in pb.get("top_post_analysis", [])
    )

    expertise_tags = " · ".join(f"`{t}`" for t in pb.get("expertise_tags", []))

    return f"""# u/{username} — Reddit Playbook in r/{subreddit}

> {pb.get("playbook_headline", "")}

**社区定位**: {pb.get("profile_headline", "")}
**话题标签**: {expertise_tags}
**社区地位**: {pb.get("community_standing", "")}

---

## 档案概览

| 指标 | 数值 |
|---|---|
| 账号年龄 | {age_yrs}年{age_mo}月 |
| Total Karma | {profile.get('total_karma', 0):,} |
| Link Karma | {profile.get('link_karma', 0):,} |
| Comment Karma | {profile.get('comment_karma', 0):,} |
| 分析帖数 | {stats['total_posts']} |
| 平均帖子得分 | {stats['avg_post_score']} |
| 最高单帖 | {stats['top_post_score']} pts |

---

## 成长时间线

{phases_md}

---

## 内容公式

> {pb.get("content_formula", "")}

**爆款帖子分析**:
{top_analysis_md}

---

## 可复制路径 (Replicable Playbook)

{steps_md}

---

## TL;DR

{pb.get("tldr", "")}

---
*Generated by smart-crawler Reddit Playbook · {datetime.now().strftime("%Y-%m-%d")}*
"""


def generate_subreddit_playbook(subreddit: str, top_n: int = 3,
                                 proxy: str | None = None) -> dict:
    """全流程：从 subreddit 找 top N 贡献者，为每人生成 playbook。

    返回：{subreddit, contributors: [{username, rank, stats, playbook, markdown}]}
    """
    contributors = get_top_contributors(subreddit, top_n=top_n, proxy=proxy)
    results = []
    for rank, contrib in enumerate(contributors, 1):
        username = contrib["username"]
        activity = get_user_activity(username, subreddit=subreddit,
                                     post_limit=100, comment_limit=100,
                                     proxy=proxy)
        pb = generate_user_playbook(username, subreddit=subreddit,
                                    activity=activity, proxy=proxy)
        results.append({
            "rank": rank,
            "username": username,
            "contributor_stats": contrib,
            "playbook": pb["playbook"],
            "activity_stats": pb["activity_stats"],
            "markdown": pb["markdown"],
        })

    return {
        "subreddit": subreddit,
        "generated_at": datetime.now().isoformat(),
        "contributors": results,
        "combined_markdown": _combined_md(subreddit, results),
    }


def _combined_md(subreddit: str, results: list[dict]) -> str:
    header = (
        f"# r/{subreddit} — Top {len(results)} 贡献者 Playbook\n\n"
        f"*Generated {datetime.now().strftime('%Y-%m-%d')} by smart-crawler*\n\n"
        "---\n\n"
    )
    sections = []
    for r in results:
        sections.append(
            f"## #{r['rank']} — u/{r['username']}\n\n"
            f"{r['markdown']}\n\n---\n"
        )
    return header + "\n".join(sections)
