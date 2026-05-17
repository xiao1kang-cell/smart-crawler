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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="smart-crawler")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库 + 初始化站点")

    pc = sub.add_parser("crawl", help="采集")
    pc.add_argument("--site")
    pc.add_argument("--brand")

    pe = sub.add_parser("export", help="导出 Excel")
    pe.add_argument("--out", required=True)
    pe.add_argument("--site")

    args = parser.parse_args(argv)
    init_db()

    if args.cmd == "init":
        print("✓ 数据库已初始化，站点已载入。")
        return 0

    if args.cmd == "crawl":
        if args.site:
            results = [run_site(args.site)]
        elif args.brand:
            results = run_brand(args.brand)
        else:
            print("需指定 --site 或 --brand", file=sys.stderr)
            return 2
        for r in results:
            if r["status"] == "success":
                recompute(r["site"])
                print(f"✓ {r['site']}: {r['products']} 商品 / "
                      f"{r['new']} 新品 / {r['promotions']} 促销 "
                      f"/ {r['duration_sec']}s")
                for n in r.get("notes", []):
                    print(f"    {n}")
            else:
                print(f"✗ {r['site']}: {r.get('error')}")
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
