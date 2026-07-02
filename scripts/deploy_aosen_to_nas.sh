#!/usr/bin/env bash
# Targeted Aosen deployment helper.
#
# Default mode is safe: run local checks, build the admin SPA, and create a
# payload tarball. Set APPLY=1 to rsync the selected Aosen files to the NAS,
# restart the web/worker services, and run the post-deploy verifier.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NAS_USER="${NAS_USER:-solvea}"
NAS_HOST="${NAS_HOST:-192.168.1.80}"
NAS_PATH="${NAS_PATH:-/volume1/docker/smart-crawler/app}"
JUMP_HOST="${JUMP_HOST:-}"
SSH_TARGET="${NAS_USER}@${NAS_HOST}"
SSH_OPTS="${SSH_OPTS:-}"
APPLY="${APPLY:-0}"
BUILD_ADMIN="${BUILD_ADMIN:-1}"
BUILD_FRONTEND="${BUILD_FRONTEND:-1}"
RUN_LOCAL_TESTS="${RUN_LOCAL_TESTS:-1}"
VERIFY_STRICT="${VERIFY_STRICT:-0}"
REMOTE_TEMPLATE_LIMIT="${REMOTE_TEMPLATE_LIMIT:-100}"
RUN_REMOTE_REMEDIATION_DRY_RUN="${RUN_REMOTE_REMEDIATION_DRY_RUN:-1}"
AOSEN_TENANT="${AOSEN_TENANT:-}"
AOSEN_SITE_PREFIXES="${AOSEN_SITE_PREFIXES:-}"
AOSEN_SKIP_PRODUCT_SAMPLES="${AOSEN_SKIP_PRODUCT_SAMPLES:-0}"
STAMP="$(date -u +%Y%m%d_%H%M%S)"
PAYLOAD_DIR="${PAYLOAD_DIR:-data/exports/aosen_deploy_${STAMP}}"
PAYLOAD_TAR="${PAYLOAD_TAR:-${PAYLOAD_DIR}.tar.gz}"

AOSEN_FILES=(
  backend/sites.yaml
  backend/app/api/admin_spine.py
  backend/app/api/routes.py
  backend/app/api/tracking.py
  backend/app/crawlers/flexispot.py
  backend/app/crawlers/generic.py
  backend/app/crawlers/homary.py
  backend/app/crawlers/magento.py
  backend/app/crawlers/shoper.py
  backend/app/crawlers/shopify.py
  backend/app/crawlers/vidaxl.py
  backend/app/crawlers/vonhaus.py
  backend/app/crawlers/westelm.py
  backend/app/db.py
  backend/app/export.py
  backend/app/models.py
  backend/app/pipeline.py
  backend/app/product_quality.py
  backend/app/runner.py
  backend/app/site_metrics.py
  backend/scripts/aosen_online_acceptance.py
  backend/scripts/aosen_online_remediate.py
  backend/scripts/product_field_completeness.py
  backend/scripts/post_deploy_verify.py
  admin-app/src/api/admin.ts
  admin-app/src/pages/DataQualityPage.vue
  frontend-app/src/api/coverage.ts
  frontend-app/src/pages/CoveragePage.vue
  frontend-app/src/pages/OverviewPage.vue
  frontend-app/src/pages/SiteReportPage.vue
  frontend-app/src/pages/TrackingPage.vue
  frontend/report.html
)
AOSEN_ARTIFACT_FILES=(
  backend/tests/test_admin_sales_signals.py
  backend/tests/test_aosen_online_acceptance.py
  backend/tests/test_aosen_online_remediate.py
  backend/tests/test_generic_discovery.py
  backend/tests/test_onboard_flexispot.py
  backend/tests/test_post_deploy_verify.py
  backend/tests/test_pipeline_promo.py
  backend/tests/test_onboard_homary.py
  backend/tests/test_onboard_magento.py
  backend/tests/test_onboard_shopify.py
  backend/tests/test_onboard_vidaxl.py
  backend/tests/test_onboard_vonhaus.py
  backend/tests/test_onboard_westelm.py
  backend/tests/test_tracking_api.py
  backend/tests/test_workspace_tenancy.py
  deliverables/aosen_production_completion_runbook_2026-06-28.md
)
if [ -n "$JUMP_HOST" ]; then
  SSH_OPTS="-J ${JUMP_HOST} ${SSH_OPTS}"
fi
RSYNC_SSH=()
if [ -n "$SSH_OPTS" ]; then
  RSYNC_SSH=(-e "ssh ${SSH_OPTS}")
fi

echo "== Aosen targeted NAS deploy =="
echo "target: ${SSH_TARGET}:${NAS_PATH}"
echo "jump:   ${JUMP_HOST:-none}"
echo "apply:  ${APPLY}"
echo "tenant: ${AOSEN_TENANT:-all visible workspaces}"
echo "scope:  ${AOSEN_SITE_PREFIXES:-all sites}"
echo "sample: ${AOSEN_SKIP_PRODUCT_SAMPLES}"

for path in "${AOSEN_FILES[@]}"; do
  if [ ! -f "$path" ]; then
    echo "FAIL: required file missing: $path" >&2
    exit 1
  fi
done
for path in "${AOSEN_ARTIFACT_FILES[@]}"; do
  if [ ! -f "$path" ]; then
    echo "FAIL: required artifact missing: $path" >&2
    exit 1
  fi
done

echo "-- Python syntax check"
python3 -m py_compile \
  backend/app/api/admin_spine.py \
  backend/app/api/routes.py \
  backend/app/api/tracking.py \
  backend/app/crawlers/flexispot.py \
  backend/app/crawlers/generic.py \
  backend/app/crawlers/homary.py \
  backend/app/crawlers/magento.py \
  backend/app/crawlers/shoper.py \
  backend/app/crawlers/shopify.py \
  backend/app/crawlers/vidaxl.py \
  backend/app/crawlers/vonhaus.py \
  backend/app/crawlers/westelm.py \
  backend/app/db.py \
  backend/app/models.py \
  backend/app/pipeline.py \
  backend/app/product_quality.py \
  backend/app/runner.py \
  backend/app/site_metrics.py \
  backend/scripts/aosen_online_acceptance.py \
  backend/scripts/aosen_online_remediate.py \
  backend/scripts/product_field_completeness.py \
  backend/scripts/post_deploy_verify.py

if [ "$RUN_LOCAL_TESTS" = "1" ]; then
  echo "-- targeted backend tests"
  PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
  PYTHONPATH=backend "$PYTHON_BIN" -m pytest \
    backend/tests/test_admin_sales_signals.py \
    backend/tests/test_aosen_online_acceptance.py \
    backend/tests/test_aosen_online_remediate.py \
    backend/tests/test_generic_discovery.py \
    backend/tests/test_onboard_flexispot.py \
    backend/tests/test_onboard_homary.py \
    backend/tests/test_onboard_magento.py \
    backend/tests/test_onboard_shopify.py \
    backend/tests/test_onboard_vidaxl.py \
    backend/tests/test_onboard_vonhaus.py \
    backend/tests/test_onboard_westelm.py \
    backend/tests/test_pipeline_promo.py \
    backend/tests/test_post_deploy_verify.py \
    backend/tests/test_tracking_api.py \
    backend/tests/test_workspace_tenancy.py \
    -q
else
  echo "-- targeted backend tests skipped (RUN_LOCAL_TESTS=0)"
fi

if [ "$BUILD_ADMIN" = "1" ]; then
  echo "-- admin app build"
  if [ -f admin-app/pnpm-lock.yaml ] && command -v pnpm >/dev/null 2>&1; then
    (cd admin-app && pnpm install --frozen-lockfile && pnpm run build)
  else
    (cd admin-app && npm run build)
  fi
else
  echo "-- admin app build skipped (BUILD_ADMIN=0)"
fi

if [ "$BUILD_FRONTEND" = "1" ]; then
  echo "-- frontend app build"
  if [ -f frontend-app/pnpm-lock.yaml ] && command -v pnpm >/dev/null 2>&1; then
    (cd frontend-app && pnpm install --frozen-lockfile && pnpm run build)
  else
    (cd frontend-app && npm run build)
  fi
else
  echo "-- frontend app build skipped (BUILD_FRONTEND=0)"
fi

mkdir -p "$PAYLOAD_DIR"
{
  echo "# runtime files"
  printf "%s\n" "${AOSEN_FILES[@]}"
  echo
  echo "# test and runbook artifacts"
  printf "%s\n" "${AOSEN_ARTIFACT_FILES[@]}"
  echo
  echo "# generated admin build"
  echo "admin-app/dist"
  echo
  echo "# generated frontend build"
  echo "frontend-app/dist"
} > "$PAYLOAD_DIR/manifest.txt"
tar -czf "$PAYLOAD_TAR" \
  "${AOSEN_FILES[@]}" \
  "${AOSEN_ARTIFACT_FILES[@]}" \
  admin-app/dist \
  frontend-app/dist \
  "$PAYLOAD_DIR/manifest.txt"
echo "-- payload: $PAYLOAD_TAR"

if [ "$APPLY" != "1" ]; then
  cat <<EOF

Dry run complete. To deploy from a machine with NAS SSH access:

  APPLY=1 NAS_USER=${NAS_USER} NAS_HOST=${NAS_HOST} NAS_PATH=${NAS_PATH} \\
    bash scripts/deploy_aosen_to_nas.sh

If the NAS is only reachable through the iMac jump host:

  APPLY=1 JUMP_HOST=siliconno3@192.168.1.87 \\
    bash scripts/deploy_aosen_to_nas.sh

After deploy, run strict acceptance only after importing/refreshing the required
Aosen business data:

  STRICT_AOSEN_ACCEPTANCE=1 SMARTCRAWLER_BASE_URL=http://${NAS_HOST}:8077 \\
    python3 backend/scripts/post_deploy_verify.py

Or use the dedicated Aosen gate:

  SMARTCRAWLER_BASE_URL=http://${NAS_HOST}:8077 \\
    python3 backend/scripts/aosen_online_acceptance.py --strict --template-limit 20
EOF
  exit 0
fi

echo "-- checking SSH"
ssh ${SSH_OPTS} -o BatchMode=yes -o ConnectTimeout=10 "$SSH_TARGET" \
  "test -d '$NAS_PATH' && test -f '$NAS_PATH/docker-compose.yml'"

echo "-- remote backup of selected files"
ssh ${SSH_OPTS} "$SSH_TARGET" "cd '$NAS_PATH' && mkdir -p data/backups && \
  tar -czf 'data/backups/aosen_selected_${STAMP}.tar.gz' \
  ${AOSEN_FILES[*]} admin-app/dist frontend-app/dist 2>/dev/null || true"

echo "-- syncing backend/admin source files"
rsync -avR "${RSYNC_SSH[@]}" \
  "${AOSEN_FILES[@]}" \
  "${SSH_TARGET}:${NAS_PATH}/"

echo "-- syncing built admin dist"
rsync -av --delete "${RSYNC_SSH[@]}" \
  admin-app/dist/ \
  "${SSH_TARGET}:${NAS_PATH}/admin-app/dist/"

echo "-- syncing built frontend dist"
rsync -av --delete "${RSYNC_SSH[@]}" \
  frontend-app/dist/ \
  "${SSH_TARGET}:${NAS_PATH}/frontend-app/dist/"

echo "-- restarting NAS web/worker services"
ssh ${SSH_OPTS} "$SSH_TARGET" "cd '$NAS_PATH' && \
  services=\$(docker compose ps --services --status running | grep -E '^(smart-crawler|worker_)' || true); \
  if [ -n \"\$services\" ]; then docker compose restart \$services; else docker compose up -d smart-crawler worker_1 worker_2; fi"

echo "-- post-deploy verification"
ssh ${SSH_OPTS} "$SSH_TARGET" "cd '$NAS_PATH' && \
  SMARTCRAWLER_BASE_URL='http://127.0.0.1:8077' \
  SKIP_API_KEY_VERIFY='1' \
  STRICT_AOSEN_ACCEPTANCE='${VERIFY_STRICT}' \
  bash scripts/deploy/post_deploy_verify.sh"

if [ "$RUN_REMOTE_REMEDIATION_DRY_RUN" = "1" ]; then
  echo "-- remote Aosen action-plan/remediation dry-run"
  TENANT_ARGS=()
  if [ -n "$AOSEN_TENANT" ]; then
    TENANT_ARGS=(--tenant "$AOSEN_TENANT")
  fi
  SITE_SCOPE_ARGS=()
  if [ -n "$AOSEN_SITE_PREFIXES" ]; then
    for prefix in $AOSEN_SITE_PREFIXES; do
      SITE_SCOPE_ARGS+=(--site-prefix "$prefix")
    done
  fi
  PRODUCT_SAMPLE_ARGS=()
  if [ "$AOSEN_SKIP_PRODUCT_SAMPLES" = "1" ]; then
    PRODUCT_SAMPLE_ARGS=(--skip-product-samples)
  fi
  ssh ${SSH_OPTS} "$SSH_TARGET" "cd '$NAS_PATH' && \
    docker compose exec -T smart-crawler \
      python scripts/aosen_online_remediate.py \
        --base-url http://127.0.0.1:8077 \
        ${TENANT_ARGS[*]} \
        ${SITE_SCOPE_ARGS[*]} \
        ${PRODUCT_SAMPLE_ARGS[*]} \
        --template-limit '${REMOTE_TEMPLATE_LIMIT}' \
        --out-dir '../data/exports/aosen_after_deploy_${STAMP}'"
fi

echo "OK: Aosen targeted deploy finished"
