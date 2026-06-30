#!/usr/bin/env bash
set -euo pipefail

BATCH_SIZE="${BATCH_SIZE:-10000}"
SLEEP_SEC="${SLEEP_SEC:-0.2}"
LOG="${LOG:-/tmp/aosen_review_zero_backfill.log}"
SKIP_BUSY_SITES="${SKIP_BUSY_SITES:-1}"

if [[ ! "$BATCH_SIZE" =~ ^[0-9]+$ ]] || [ "$BATCH_SIZE" -lt 1 ]; then
  echo "invalid BATCH_SIZE=$BATCH_SIZE" >&2
  exit 2
fi

psql_prod() {
  printf '%s\n' "${SUDO_PASS:-}" \
    | sudo -S -p '' docker exec smart-crawler-pg \
        psql -U smart_crawler -d smart_crawler "$@"
}

run_batch_for_site() {
  local table="$1"
  local site="$2"
  local date_predicate="$3"
  if [[ ! "$site" =~ ^[a-z0-9_]+$ ]]; then
    echo "0"
    return
  fi
  psql_prod -At -c "
with batch as (
  select t.id
  from ${table} t
  where t.site = '$site'
    and t.review_count is null
    ${date_predicate}
  order by t.id
  limit $BATCH_SIZE
  for update skip locked
),
upd as (
  update ${table} t
  set review_count = 0
  from batch
  where t.id = batch.id
  returning 1
)
select count(*) from upd;
"
}

{
  echo "started_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ') batch_size=$BATCH_SIZE"

  psql_prod -At -c "
with active_sites as (
  select ws.site
  from workspace_sites ws
  join workspaces w on w.id = ws.workspace_id
  where ws.enabled is true
    and coalesce(ws.hidden, false) is false
    and w.status = 'active'
    and ws.site not in ('vidaxl_us', 'vidaxl_ca')
  group by ws.site
)
select site from active_sites order by site;
" > /tmp/aosen_backfill_sites.txt

if [ "$SKIP_BUSY_SITES" = "1" ]; then
  psql_prod -At -c "
select distinct site
from crawl_jobs
where status in ('pending', 'running')
order by site;
" > /tmp/aosen_backfill_busy_sites.txt
else
  : > /tmp/aosen_backfill_busy_sites.txt
fi

is_busy_site() {
  local site="$1"
  [ "$SKIP_BUSY_SITES" = "1" ] \
    && grep -Fxq "$site" /tmp/aosen_backfill_busy_sites.txt
}

  total_products=0
  while IFS= read -r site; do
    [ -n "$site" ] || continue
    if is_busy_site "$site"; then
      echo "products_site=$site skipped_busy at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
      continue
    fi
    site_total=0
    while true; do
      n="$(run_batch_for_site products "$site" "")"
      n="${n//$'\n'/}"
      echo "products_site=$site batch=$n site_before=$site_total total_before=$total_products at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
      if [ "${n:-0}" -eq 0 ]; then
        break
      fi
      site_total=$((site_total + n))
      total_products=$((total_products + n))
      sleep "$SLEEP_SEC"
    done
  done < /tmp/aosen_backfill_sites.txt

  total_history=0
  while IFS= read -r site; do
    [ -n "$site" ] || continue
    if is_busy_site "$site"; then
      echo "price_history_today_site=$site skipped_busy at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
      continue
    fi
    site_total=0
    while true; do
      n="$(run_batch_for_site price_history "$site" "and t.date = current_date")"
      n="${n//$'\n'/}"
      echo "price_history_today_site=$site batch=$n site_before=$site_total total_before=$total_history at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
      if [ "${n:-0}" -eq 0 ]; then
        break
      fi
      site_total=$((site_total + n))
      total_history=$((total_history + n))
      sleep "$SLEEP_SEC"
    done
  done < /tmp/aosen_backfill_sites.txt

  echo "finished_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ') products_updated=$total_products price_history_today_updated=$total_history"
} >> "$LOG" 2>&1
