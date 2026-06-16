# Acceptance Data Rerun Plan

This checklist is derived from `/Users/wangxiaokang/Desktop/标杆平台验收报告.xlsx`.
It is meant for the post-deploy data pass, using the admin **数据质量** page as
the source of truth.

## Use The Admin Page

1. Open `/admin/data-quality`.
2. Keep `全部 workspace` selected unless validating one tenant.
3. Sort by status. Start with `critical`, then `warning`.
4. Click `重跑需重跑(...)` to enqueue all critical sites currently visible.
5. Use `/admin/queue` to watch `运行中站点`, `卡住站点`, `失败站点`, and failure codes.
6. Return to `/admin/data-quality` after worker completion. A site is not closed
   until SKU/SPU, promotion count, sales signal, revenue signal, and latest job
   all look sane.

The rerun action reuses existing pending/running crawl jobs, so repeated clicks
should not flood the queue.

## Highest Priority

These are explicit spreadsheet gaps or high-business-value rows that should be
validated immediately after deployment:

- `vidaxl_us`
- `vidaxl_ca`
- `vidaxl_de`
- `vidaxl_uk`
- `vidaxl_fr`
- `vidaxl_es`
- `vidaxl_it`
- `vidaxl_nl`
- `vidaxl_pl`
- `vidaxl_pt`
- `vidaxl_ro`
- `vidaxl_ie`
- `songmics_us`
- `songmics_de`
- `songmics_uk`
- `songmics_fr`
- `songmics_es`
- `songmics_it`
- `flexispot_us`
- `flexispot_de`
- `flexispot_uk`
- `flexispot_ca`
- `flexispot_fr`
- `flexispot_it`
- `flexispot_es`
- `flexispot_nl`
- `flexispot_pl`

## Second Priority

These rows have benchmark count discrepancies or previously missing local data:

- `costway_us`
- `costway_ca`
- `costway_de`
- `costway_uk`
- `costway_fr`
- `costway_es`
- `costway_it`
- `costway_nl`
- `costway_pl`
- `homary_us`
- `homary_de`
- `homary_uk`
- `homary_fr`
- `homary_es`
- `idealo_de`
- `bol_nl`
- `cdiscount_fr`
- `article_us`
- `bcp_us`
- `cratebarrel_us`
- `overstock_us`
- `westelm_us`
- `woltu_de`
- `vonhaus_uk`
- `yaheetech_us`
- `yaheetech_uk`
- `ikea_us`

## Parked

- `etsy_us`:需求已确认先搁置。不要把 Etsy 的验收缺口混进本轮闭环。

## Local Evidence Caveat

The local SQLite database at `data/smart_crawler.db` is not representative of
production data. In the local check, every spreadsheet site except `songmics_us`
had zero product rows, and `songmics_us` had products/promotions but no sales
signal rows. This is useful as a smoke signal only; final acceptance must be
verified against production after deployment and reruns.

