"""批量导出所有站点的报表 —— 客户交付用。

用法：
  python -m scripts.export_all_sites [--out-dir DIR]

为每个站点生成一份 6-Sheet Excel，按品牌分组到子目录。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.export import export_workbook
from app.models import Site


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", default="../deliverables/reports_bundle")
    args = p.parse_args()
    out_root = Path(args.out_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as s:
        sites = s.query(Site).order_by(Site.brand, Site.country).all()
        for st in sites:
            brand_dir = out_root / (st.brand or "_unknown")
            brand_dir.mkdir(exist_ok=True)
            fname = f"{st.site}.xlsx"
            blob = export_workbook(s, site=st.site)
            (brand_dir / fname).write_bytes(blob)
            print(f"  ✓ {st.brand}/{st.site}: {len(blob)//1024} KB")
        print(f"\n=== 导出完成: {len(sites)} 个站点 -> {out_root} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
