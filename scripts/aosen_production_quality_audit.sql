-- Production-only Aosen crawl/data quality audit.
-- Run inside the production Postgres container, for example:
--   docker exec smart-crawler-pg psql -U smart_crawler -d smart_crawler \
--     -f /tmp/aosen_production_quality_audit.sql

\pset pager off
\timing on

select now() as audited_at;

-- 1) Queue/worker state: shows whether jobs are moving, and where they run.
select
  status,
  coalesce(assigned_node, '<unassigned>') as assigned_node,
  coalesce(worker, '<none>') as worker,
  count(*) as jobs,
  min(created_at) as oldest_created_at,
  min(started_at) as oldest_started_at,
  max(heartbeat_at) as newest_heartbeat_at,
  max(now() - heartbeat_at) filter (where status = 'running') as max_running_heartbeat_age
from crawl_jobs
where status in ('pending', 'running')
group by status, coalesce(assigned_node, '<unassigned>'), coalesce(worker, '<none>')
order by status, assigned_node, worker;

select
  j.id,
  j.site,
  s.platform,
  j.status,
  j.assigned_node,
  j.worker,
  j.products_count,
  j.total_product_count,
  j.created_at,
  j.started_at,
  j.heartbeat_at,
  now() - j.heartbeat_at as heartbeat_age,
  left(coalesce(j.failure_code, ''), 80) as failure_code,
  left(coalesce(j.error, j.failure_detail, ''), 180) as error
from crawl_jobs j
left join sites s on s.site = j.site
where j.status in ('pending', 'running')
order by j.status, j.id;

-- 2) Aosen active site scope. Default acceptance excludes deferred vidaXL US/CA.
with active_sites as (
  select
    ws.site,
    max(ws.target_sku_count) as target_sku_count
  from workspace_sites ws
  join workspaces w on w.id = ws.workspace_id
  where ws.enabled is true
    and coalesce(ws.hidden, false) is false
    and w.status = 'active'
    and ws.site not in ('vidaxl_us', 'vidaxl_ca')
  group by ws.site
),
latest_job as (
  select *
  from (
    select
      j.*,
      row_number() over (partition by j.site order by j.id desc) as rn
    from crawl_jobs j
    join active_sites a on a.site = j.site
  ) ranked
  where rn = 1
),
latest_success as (
  select *
  from (
    select
      j.*,
      row_number() over (partition by j.site order by j.finished_at desc nulls last, j.id desc) as rn
    from crawl_jobs j
    join active_sites a on a.site = j.site
    where j.status = 'success'
  ) ranked
  where rn = 1
),
review_history as (
  select
    h.site,
    count(*) filter (where snapshot_days >= 2) as skus_with_2_review_snapshots,
    count(*) filter (where snapshot_days >= 1) as skus_with_any_review_snapshot
  from (
    select
      ph.site,
      ph.sku,
      count(distinct ph.date) filter (where ph.review_count is not null) as snapshot_days
    from price_history ph
    join active_sites a on a.site = ph.site
    where ph.date >= current_date - interval '30 days'
    group by ph.site, ph.sku
  ) h
  group by h.site
),
product_quality as (
  select
    p.site,
    count(*) as products,
    count(distinct coalesce(nullif(p.spu, ''), p.sku)) as spu_count,
    count(*) filter (
      where coalesce(p.sale_price, p.original_price, 0) > 0
    ) as price_present,
    count(*) filter (
      where coalesce(p.sale_price, p.original_price, 0) <= 0
    ) as price_missing,
    count(*) filter (where p.review_count is not null) as review_count_present,
    count(*) filter (where p.review_count is null) as review_count_null,
    count(*) filter (where coalesce(p.review_count, 0) = 0) as review_count_zero_or_null,
    count(*) filter (where coalesce(p.review_count, 0) > 0) as review_count_gt0,
    count(*) filter (
      where length(trim(coalesce(p.category_path, ''))) = 0
    ) as category_missing,
    count(*) filter (
      where trim(coalesce(p.image_urls::text, '')) in ('', '[]', 'null')
    ) as image_missing,
    count(*) filter (where length(trim(coalesce(p.currency, ''))) = 0) as currency_missing,
    max(p.updated_time) as latest_product_updated_at
  from products p
  join active_sites a on a.site = p.site
  group by p.site
),
promotion_quality as (
  select site, count(*) as promotions
  from promotions
  group by site
)
select
  a.site,
  s.brand,
  s.country,
  s.platform,
  a.target_sku_count,
  coalesce(pq.products, 0) as products,
  coalesce(pq.spu_count, 0) as spu_count,
  round(coalesce(pq.products, 0) * 100.0 / nullif(a.target_sku_count, 0), 2) as target_coverage_pct,
  coalesce(pq.price_present, 0) as price_present,
  coalesce(pq.price_missing, 0) as price_missing,
  round(coalesce(pq.price_present, 0) * 100.0 / nullif(pq.products, 0), 2) as price_present_pct,
  coalesce(pq.review_count_present, 0) as review_count_present,
  coalesce(pq.review_count_null, 0) as review_count_null,
  round(coalesce(pq.review_count_present, 0) * 100.0 / nullif(pq.products, 0), 2) as review_count_present_pct,
  coalesce(pq.review_count_gt0, 0) as review_count_gt0,
  coalesce(rh.skus_with_2_review_snapshots, 0) as skus_with_2_review_snapshots,
  coalesce(pq.category_missing, 0) as category_missing,
  coalesce(pq.image_missing, 0) as image_missing,
  coalesce(pq.currency_missing, 0) as currency_missing,
  coalesce(pr.promotions, 0) as promotions,
  pq.latest_product_updated_at,
  lj.id as latest_job_id,
  lj.status as latest_job_status,
  lj.assigned_node as latest_job_node,
  lj.products_count as latest_job_products,
  lj.total_product_count as latest_job_total,
  lj.finished_at as latest_job_finished_at,
  ls.id as latest_success_job_id,
  ls.products_count as latest_success_products,
  ls.total_product_count as latest_success_total,
  ls.finished_at as latest_success_finished_at,
  case
    when coalesce(pq.products, 0) = 0 then 'no_products'
    when coalesce(pq.price_missing, 0) > 0 then 'price_missing_rows'
    when coalesce(pq.review_count_null, 0) > 0 then 'review_count_null_rows'
    when coalesce(pq.category_missing, 0) > 0 then 'category_missing_rows'
    when coalesce(pq.image_missing, 0) > 0 then 'image_missing_rows'
    when coalesce(pr.promotions, 0) = 0 then 'promotions_missing'
    when coalesce(pq.review_count_gt0, 0) > 0
      and coalesce(rh.skus_with_2_review_snapshots, 0) < 2
      then 'review_history_insufficient_for_sales'
    else 'ok'
  end as strict_status
from active_sites a
left join sites s on s.site = a.site
left join product_quality pq on pq.site = a.site
left join promotion_quality pr on pr.site = a.site
left join review_history rh on rh.site = a.site
left join latest_job lj on lj.site = a.site
left join latest_success ls on ls.site = a.site
order by strict_status, a.site;

-- 3) Sites with missing price rows: concrete samples.
select
  p.site,
  p.sku,
  left(coalesce(p.title, ''), 90) as title,
  p.sale_price,
  p.original_price,
  p.currency,
  p.review_count,
  left(coalesce(p.product_url, ''), 180) as product_url,
  p.updated_time
from products p
where p.site in (
    select ws.site
    from workspace_sites ws
    join workspaces w on w.id = ws.workspace_id
    where ws.enabled is true
      and coalesce(ws.hidden, false) is false
      and w.status = 'active'
      and ws.site not in ('vidaxl_us', 'vidaxl_ca')
  )
  and coalesce(p.sale_price, p.original_price, 0) <= 0
order by p.site, p.updated_time desc nulls last
limit 80;

-- 4) Sites with NULL review_count rows: concrete samples. Zero is not counted here.
select
  p.site,
  p.sku,
  left(coalesce(p.title, ''), 90) as title,
  p.sale_price,
  p.original_price,
  p.currency,
  p.review_count,
  left(coalesce(p.product_url, ''), 180) as product_url,
  p.updated_time
from products p
where p.site in (
    select ws.site
    from workspace_sites ws
    join workspaces w on w.id = ws.workspace_id
    where ws.enabled is true
      and coalesce(ws.hidden, false) is false
      and w.status = 'active'
      and ws.site not in ('vidaxl_us', 'vidaxl_ca')
  )
  and p.review_count is null
order by p.site, p.updated_time desc nulls last
limit 80;

-- 5) Recent anti-bot / zero-product failures that still need non-code mitigation.
select
  j.site,
  s.platform,
  j.id,
  j.status,
  j.products_count,
  j.total_product_count,
  j.failure_code,
  j.failure_stage,
  left(coalesce(j.failure_detail, j.error, ''), 240) as failure_detail,
  j.finished_at
from crawl_jobs j
left join sites s on s.site = j.site
where j.id in (
  select max(id)
  from crawl_jobs
  group by site
)
and (
  j.failure_code is not null
  or j.status in ('failed', 'blocked', 'partial')
  or coalesce(j.products_count, 0) = 0
)
order by j.finished_at desc nulls last, j.site;
