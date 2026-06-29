# Aosen 生产完成 Runbook

> 更新时间：2026-06-28  
> 范围：遨森字段质量、促销、30 天销量 / 营收闭环  
> 默认排除：`vidaxl_us`、`vidaxl_ca`

## 当前结论

本地代码已经补齐促销解析、字段修正、SKU 目标、销量营收、评论历史导入与 Aosen 验收端点。当前生产 `http://192.168.1.80:8077` 已能访问新的 Aosen 专用端点，但 strict gate 仍未通过：

- `/api/admin/spine/acceptance/aosen/action-plan` 线上返回 `200`，`/field-quality` 返回 `200`。
- 按线上 `tenant=1` strict gate 复核：`action_plan_status=blocked`、`fail=50`、`needs_refresh=4`。
- Homary / VidaXL / VonHaus 重点站点仍触发 `focus_promotions_missing`，线上促销重建后新增促销为 `0`。
- `tenant=1` 是当前 `aosen` 登录用户的 `Internal Workspace`，包含 Amazon / Article 等混合站点；生成客户补齐模板时必须再加 `--site-prefix` 过滤需求站点范围。

因此现在不能对客户说“已完成”。完成标准必须以生产严格验收通过为准。

## 完成标准

必须同时满足：

| 项目 | 完成证据 |
|---|---|
| 新 Aosen 验收端点已上线 | `post_deploy_verify.py` 中 `aosen_field_quality_endpoint` 和 `aosen_action_plan_endpoint` 为 OK |
| 字段质量硬失败清零 | Aosen action-plan `summary.fail == 0` |
| 促销缺口清零 | Aosen action-plan `summary.needs_refresh == 0`，且 Homary / VidaXL / VonHaus 重点站 `promotion_count > 0` |
| 30 天销量 / 营收闭环 | Aosen action-plan `summary.needs_business_data == 0`，或外部 sales CSV / review-history CSV 已导入并重算 |
| SKU/SPU 口径完成 | `sku_targets` 模板没有未处理的 `sku_deviation_high / coverage_low` 行 |
| 最终严格验收 | 生产 strict gate 退出码为 `0`；混合 workspace 下需显式加 `--tenant 1 --site-prefix homary --site-prefix vidaxl --site-prefix vonhaus` |

## 1. 部署 Aosen 定向改动

在有 NAS SSH 权限的机器执行：

```bash
AOSEN_TENANT=1 AOSEN_SITE_PREFIXES="homary vidaxl vonhaus" \
APPLY=1 bash scripts/deploy_aosen_to_nas.sh
```

如果 NAS 只能通过 iMac 跳板访问：

```bash
AOSEN_TENANT=1 AOSEN_SITE_PREFIXES="homary vidaxl vonhaus" \
APPLY=1 JUMP_HOST=siliconno3@192.168.1.87 bash scripts/deploy_aosen_to_nas.sh
```

该脚本会：

- 本地运行 Aosen 相关测试和 admin build。
- 只同步 Aosen 相关后端、爬虫、admin 页面和脚本文件。
- 在 NAS 上备份被覆盖文件。
- 重启 `smart-crawler` 与 worker 服务。
- 运行 post-deploy verification。
- 在容器内执行 Aosen remediation dry-run，导出模板到 `/app/data/exports/aosen_after_deploy_<timestamp>/`。

本机没有 NAS SSH 权限时，可以先生成部署包：

```bash
AOSEN_TENANT=1 AOSEN_SITE_PREFIXES="homary vidaxl vonhaus" \
APPLY=0 bash scripts/deploy_aosen_to_nas.sh
```

生成的包位于 `data/exports/aosen_deploy_<timestamp>.tar.gz`。

## 2. 部署后验证新端点

部署完成后先跑：

```bash
SMARTCRAWLER_BASE_URL=http://192.168.1.80:8077 \
python3 backend/scripts/post_deploy_verify.py
```

必须看到：

```text
[OK] aosen_field_quality_endpoint
[OK] aosen_action_plan_endpoint
```

如果任一项为 `404`，说明新代码没有真正上线，不要继续宣布完成。

## 3. 生成线上缺口模板

执行：

```bash
SMARTCRAWLER_BASE_URL=http://192.168.1.80:8077 \
python3 backend/scripts/aosen_online_remediate.py \
  --tenant 1 \
  --site-prefix homary \
  --site-prefix vidaxl \
  --site-prefix vonhaus \
  --skip-product-samples \
  --template-limit 500 \
  --out-dir data/exports/aosen_after_deploy
```

会生成：

| 文件 | 用途 |
|---|---|
| `product_field_fixes_preview.csv` | 修正标题、币种、价格、类目、图片、SPU |
| `sku_targets_preview.csv` | 修正 workspace 目标 SKU 数口径 |
| `promotion_signals_preview.csv` | 导入外部促销信号，补 coupon / bundle / free shipping |
| `sales_signals_preview.csv` | 导入外部 30 天销量 / 营收 |
| `review_history_preview.csv` | 导入同 SKU 多次评论历史，用于评论增量推算销量 |
| `site_gaps.csv` | 站点级缺口汇总 |

这些 CSV 是生产当前状态生成的预览。导入前必须人工补齐空字段。

如果需要预填 SKU 样例，可以去掉 `--skip-product-samples`；但当前生产大体量 VidaXL 查询较慢，建议先用站点级模板收集外部促销和销量数据。

## 4. 重算促销与销量

如果新促销解析已部署，先对线上缺促销站点重算：

```bash
SMARTCRAWLER_BASE_URL=http://192.168.1.80:8077 \
python3 backend/scripts/aosen_online_remediate.py \
  --tenant 1 \
  --site-prefix homary \
  --site-prefix vidaxl \
  --site-prefix vonhaus \
  --apply \
  --template-limit 500
```

这会调用：

- `/api/admin/spine/promotions/rebuild`
- `/api/admin/spine/analytics/recompute`

如果站点页面本身无法稳定解析促销，需要继续走 `promotion_signals_preview.csv` 外部导入。

## 4.1 无外部数据时的后续闭环

如果没有业务侧 CSV 可导入，不能伪造促销、销量或营收。此时完成路径是：

1. 部署最新爬虫解析增强。
2. 对 Homary / VidaXL / VonHaus 重点站点重新抓取 PDP / 分类入口。
3. 抓取完成后执行 `promotions/rebuild` 与 `analytics/recompute`。
4. 至少等到第二个自然日再次抓取同一批 SKU，使 `PriceHistory` 形成两个不同日期的 review_count 快照。
5. 第二次抓取后再次执行 `analytics/recompute`，用评论增量生成 30 天销量 / 营收估算。

本轮代码已增强后续采集：

- Homary：补 JSON-LD breadcrumb/category、rating、review_count 解析。
- VidaXL：分类页发现商品时保留 category hint，PDP 缺类目时兜底写入；同时从 PDP HTML 文案补 coupon / bundle / free shipping / sale 信号。
- VonHaus：补 JSON-LD / 页面 review_count 解析。

注意：第一次重抓只能证明“未来历史快照开始积累”；销量 / 营收必须等第二个不同日期快照后才可如实估算。

## 5. 导入补齐后的 CSV

示例：

```bash
SMARTCRAWLER_BASE_URL=http://192.168.1.80:8077 \
python3 backend/scripts/aosen_online_remediate.py \
  --tenant 1 \
  --site-prefix homary \
  --site-prefix vidaxl \
  --site-prefix vonhaus \
  --apply \
  --import-product-field-fixes data/exports/aosen_after_deploy/product_field_fixes_ready.csv \
  --import-sku-targets data/exports/aosen_after_deploy/sku_targets_ready.csv \
  --import-promotion-signals data/exports/aosen_after_deploy/promotion_signals_ready.csv \
  --import-sales-signals data/exports/aosen_after_deploy/sales_signals_ready.csv \
  --import-review-history data/exports/aosen_after_deploy/review_history_ready.csv
```

脚本会先调用 validate 端点；CSV 校验失败时不会继续导入。

可按缺口分批导入。例如只有促销和销量数据可用时：

```bash
SMARTCRAWLER_BASE_URL=http://192.168.1.80:8077 \
python3 backend/scripts/aosen_online_remediate.py \
  --tenant 1 \
  --site-prefix homary \
  --site-prefix vidaxl \
  --site-prefix vonhaus \
  --apply \
  --import-promotion-signals data/exports/aosen_after_deploy/promotion_signals_ready.csv \
  --import-sales-signals data/exports/aosen_after_deploy/sales_signals_ready.csv
```

## 6. 最终严格验收

最终只认 strict gate：

```bash
SMARTCRAWLER_BASE_URL=http://192.168.1.80:8077 \
python3 backend/scripts/aosen_online_acceptance.py \
  --tenant 1 \
  --site-prefix homary \
  --site-prefix vidaxl \
  --site-prefix vonhaus \
  --strict \
  --template-limit 20
```

退出码含义：

| 退出码 | 含义 |
|---:|---|
| `0` | Aosen 生产验收通过 |
| `2` | 线上仍无专用 Aosen 端点，脚本只能 fallback 到旧数据质量接口 |
| `3` | 专用端点可用，但 strict gate 仍有 blocker |
| `1` | 登录、接口或请求失败 |

只有退出码为 `0` 时，才能对客户说本轮 Aosen 需求已完成。

## 7. 常见 blocker

| blocker | 处理 |
|---|---|
| `action_plan_status=blocked` | 看 action-plan groups，按字段 / 促销 / 业务数据分组处理 |
| `fail=N` | 先处理字段硬失败：标题、币种、价格、类目、图片、SKU 目标 |
| `needs_refresh=N` | 先重算促销；仍缺则导入 `promotion_signals` |
| `needs_business_data=N` | 导入 `sales_signals` 或补 `review_history` 后重算 |
| `focus_promotions_missing=...` | Homary / VidaXL / VonHaus 重点站仍没有促销，不能验收 |
| `missing_templates=...` | 部署不完整，重新部署后端和 admin 脚本 |
| `Connection refused` / strict gate 退出 `1` | 生产 `8077` 服务不可达；先在 NAS 上检查 `docker compose ps`、`docker compose logs smart-crawler`，恢复服务后再验收 |

## 8. 当前不可关闭原因

截至 2026-06-28 当前机器复核：

- NAS 直连 SSH：`Permission denied (publickey,password)`。
- iMac 跳板：连接超时。
- 生产 `http://192.168.1.80:8077` 当前可达，Aosen 专用 endpoint 已上线。
- `tenant=1` strict gate 返回退出码 `3`：`fail=50`、`needs_refresh=4`，重点站 `homary_* / vidaxl_* / vonhaus_uk` 促销仍缺。
- 已对 Homary / VidaXL / VonHaus 重点站执行线上促销重建，但 `created=0`，说明现有生产商品缺少可重建的促销 / coupon / bundle / free shipping 原始信号，需要重新抓取 PDP/活动页或导入外部 `promotion_signals`。
- 线上 `tenant=1` 是 Internal Workspace，包含混合站点；客户模板导出必须使用 `--site-prefix homary --site-prefix vidaxl --site-prefix vonhaus` 过滤。

所以当前状态是“代码和线上端点已到位，但生产数据仍缺字段修正与促销/业务信号闭环”。Goal 不能关闭。
