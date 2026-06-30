#!/usr/bin/env bash
set -euo pipefail

LOG="${LOG:-/tmp/aosen_post_price_review_history_backfill.log}"
BATCH_SIZE="${BATCH_SIZE:-5000}"
SLEEP_SEC="${SLEEP_SEC:-30}"
BUSY_SLEEP_SEC="${BUSY_SLEEP_SEC:-60}"

psql_prod() {
  docker exec smart-crawler-pg \
    psql -q -t -A -U smart_crawler -d smart_crawler "$@"
}

active_site_sql="
  select ws.site
  from workspace_sites ws
  join workspaces w on w.id = ws.workspace_id
  where ws.enabled is true
    and coalesce(ws.hidden, false) is false
    and w.status = 'active'
    and ws.site not in ('vidaxl_us', 'vidaxl_ca')
  group by ws.site
"

count_nulls() {
  local busy_filter="$1"
  PGOPTIONS='-c statement_timeout=60s' psql_prod -v ON_ERROR_STOP=1 -c "
with active_sites as (${active_site_sql}),
busy as (
  select distinct site from crawl_jobs where status in ('pending', 'running')
)
select count(*)
from price_history h
join active_sites a on a.site = h.site
left join busy b on b.site = h.site
where h.date = current_date
  and h.review_count is null
  ${busy_filter};
"
}

update_non_busy_batch() {
  PGOPTIONS='-c statement_timeout=60s' psql_prod -v ON_ERROR_STOP=1 -c "
with active_sites as (${active_site_sql}),
busy as (
  select distinct site from crawl_jobs where status in ('pending', 'running')
),
batch as (
  select h.id
  from price_history h
  join active_sites a on a.site = h.site
  left join busy b on b.site = h.site
  where h.date = current_date
    and h.review_count is null
    and b.site is null
  order by h.site, h.id
  limit ${BATCH_SIZE}
  for update skip locked
),
upd as (
  update price_history h
  set review_count = 0
  from batch
  where h.id = batch.id
  returning 1
)
select count(*) from upd;
"
}

{
  echo "wait_started_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

  while pgrep -f '[a]osen_cratebarrel_price_backfill.py|[a]osen_magento_price_backfill.py' >/dev/null 2>&1; do
    echo "waiting_price_backfills at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    sleep "$SLEEP_SEC"
  done

  echo "price_backfills_done_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

  while true; do
    while true; do
      out="$(update_non_busy_batch)"
      n="$(printf '%s\n' "$out" | tail -n 1 | tr -d '[:space:]')"
      n="${n:-0}"
      echo "history_non_busy_batch=$n at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
      [ "$n" -eq 0 ] && break
      sleep 0.2
    done

    total_out="$(count_nulls "")"
    busy_out="$(count_nulls "and b.site is not null")"
    total_null="$(printf '%s\n' "$total_out" | tail -n 1 | tr -d '[:space:]')"
    busy_null="$(printf '%s\n' "$busy_out" | tail -n 1 | tr -d '[:space:]')"
    total_null="${total_null:-0}"
    busy_null="${busy_null:-0}"
    echo "history_remaining_null=$total_null busy_null=$busy_null at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

    if [ "$total_null" -eq 0 ]; then
      break
    fi

    if [ "$busy_null" -gt 0 ]; then
      sleep "$BUSY_SLEEP_SEC"
    else
      sleep 10
    fi
  done

  echo "finished_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
} >> "$LOG" 2>&1
