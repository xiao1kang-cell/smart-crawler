"""命令行入口 —— 采集 / 分析 / 导出。

用法：
  python -m app.cli init
  python -m app.cli crawl --site songmics_us
  python -m app.cli crawl --brand SONGMICS
  python -m app.cli export --out ../deliverables/report.xlsx [--site songmics_us]
"""
from __future__ import annotations

import argparse
import sys

from .analytics import recompute
from .db import SessionLocal, init_db
from .export import export_workbook
from .runner import run_brand, run_site


def _report(r: dict) -> None:
    if r["status"] == "success":
        recompute(r["site"])
        print(f"✓ {r['site']}: {r['products']} 商品 / {r['new']} 新品 / "
              f"{r['promotions']} 促销 / {r['duration_sec']}s")
        for n in r.get("notes", []):
            print(f"    {n}")
    else:
        print(f"✗ {r['site']}: {r.get('error')}")


def _crawl_many(names: list) -> int:
    """顺序采集多个站点，单站失败不影响其余。"""
    import time as _t
    total = len(names)
    for i, name in enumerate(names, start=1):
        print(f"\n[{i}/{total}] {name}  {_t.strftime('%H:%M:%S')}", flush=True)
        try:
            _report(run_site(name))
        except Exception as exc:                    # 兜底，保证继续
            print(f"✗ {name}: 未捕获异常 {exc}")
    print(f"\n=== 全部完成：{total} 站 ===")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="smart-crawler")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库 + 初始化站点")

    pc = sub.add_parser("crawl", help="采集")
    pc.add_argument("--site")
    pc.add_argument("--brand")
    pc.add_argument("--all", action="store_true", help="采集全部 46 站")

    pe = sub.add_parser("export", help="导出 Excel")
    pe.add_argument("--out", required=True)
    pe.add_argument("--site")

    pr = sub.add_parser("reviews", help="采集口碑评论（模块二）")
    pr.add_argument("--site", help="单个评论渠道，如 aosom_us")
    pr.add_argument("--platform", help="整个平台，如 trustpilot")

    args = parser.parse_args(argv)
    init_db()

    if args.cmd == "init":
        print("✓ 数据库已初始化，站点已载入。")
        return 0

    if args.cmd == "crawl":
        if args.all:
            from .db import SessionLocal
            from .models import Site
            s = SessionLocal()
            names = [r.site for r in s.query(Site).order_by(Site.id).all()]
            s.close()
            return _crawl_many(names)
        if args.site:
            return _crawl_many([args.site])
        if args.brand:
            results = run_brand(args.brand)
            for r in results:
                _report(r)
            return 0
        print("需指定 --site / --brand / --all", file=sys.stderr)
        return 2

    if args.cmd == "reviews":
        from .review_runner import run_review_channel, run_review_platform
        if args.site:
            results = [run_review_channel(args.site)]
        elif args.platform:
            results = run_review_platform(args.platform)
        else:
            print("需指定 --site 或 --platform", file=sys.stderr)
            return 2
        for r in results:
            if r.get("error"):
                print(f"✗ {r['site']}: {r['error']}")
            else:
                print(f"✓ {r['site']}（{r.get('platform')}）: "
                      f"采集 {r.get('fetched',0)} / 新增 {r.get('inserted',0)} "
                      f"/ 更新 {r.get('updated',0)}")
                for n in r.get("notes", []):
                    print(f"    {n}")
        return 0

    if args.cmd == "export":
        s = SessionLocal()
        try:
            data = export_workbook(s, args.site)
        finally:
            s.close()
        with open(args.out, "wb") as f:
            f.write(data)
        print(f"✓ 已导出 {args.out}（{len(data)} 字节）")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
