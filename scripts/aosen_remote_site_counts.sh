#!/usr/bin/env bash
set -euo pipefail

psql_prod() {
  printf '%s\n' "${SUDO_PASS:-}" \
    | sudo -S -p '' docker exec smart-crawler-pg \
        psql -U smart_crawler -d smart_crawler "$@"
}

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
" > /tmp/aosen_sites.txt

echo "site|products|price_missing|review_null|review_zero|review_gt0|latest_updated"
while IFS= read -r site; do
  [ -n "$site" ] || continue
  if [[ ! "$site" =~ ^[a-z0-9_]+$ ]]; then
    echo "$site|ERROR|ERROR|ERROR|ERROR|ERROR|invalid_site_code"
    continue
  fi
  if ! out=$(psql_prod -F '|' -At -c "
set statement_timeout = '8000ms';
select
  '$site',
  count(*),
  count(*) filter (where coalesce(sale_price, original_price, 0) <= 0),
  count(*) filter (where review_count is null),
  count(*) filter (where coalesce(review_count, 0) = 0),
  count(*) filter (where coalesce(review_count, 0) > 0),
  coalesce(max(updated_time)::text, '')
from products
where site = '$site';
" 2>&1); then
    echo "$site|ERROR|ERROR|ERROR|ERROR|ERROR|${out//$'\n'/ }"
    continue
  fi
  echo "$out" | tail -n 1
done < /tmp/aosen_sites.txt
