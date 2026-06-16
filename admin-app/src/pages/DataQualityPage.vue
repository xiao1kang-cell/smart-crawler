<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { crawlEnqueue, dataQuality, tenants } from '../api/admin'
import { fmtDate, fmtNumber } from '../api/client'
import StatCard from '../components/common/StatCard.vue'
import StatusBadge from '../components/common/StatusBadge.vue'

const rows = ref<Record<string, any>[]>([])
const summary = ref<Record<string, any>>({})
const loading = ref(false)
const error = ref('')
const tenantRows = ref<Record<string, any>[]>([])
const tenantId = ref('')
const includeHidden = ref(false)
const rerunBusy = ref<Record<string, boolean>>({})
const rerunMessage = ref<Record<string, string>>({})

const sortedRows = computed(() => rows.value.slice().sort((a, b) => {
  const rank: Record<string, number> = { critical: 0, warning: 1, healthy: 2 }
  return (rank[a.status] ?? 9) - (rank[b.status] ?? 9) || String(a.site).localeCompare(String(b.site))
}))
const criticalSites = computed(() => sortedRows.value
  .filter((row) => row.status === 'critical')
  .map((row) => row.site)
  .filter(Boolean))

function queueBadges(row: Record<string, any>) {
  const q = row.crawl_queue || {}
  return [
    { key: 'pending', label: '待', value: q.pending || 0 },
    { key: 'stale', label: '久排', value: q.stale_pending || 0 },
    { key: 'running', label: '跑', value: q.running || 0 },
    { key: 'stuck', label: '卡', value: q.stuck || 0 },
    { key: 'failed', label: '败', value: q.failed || 0 },
  ].filter((item) => item.value > 0)
}

function issueLabel(issue: string) {
  return ({
    no_products: '无商品',
    coverage_low: '覆盖低',
    price_missing: '价格缺失',
    sales_missing: '销量缺失',
    revenue_missing: '收入缺失',
    promotions_missing: '促销缺失',
    latest_job_failed: '任务失败',
    job_in_progress: '运行中',
    job_pending_stale: '排队过久',
    never_crawled: '未采集',
  } as Record<string, string>)[issue] || issue
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params: Record<string, any> = { include_hidden: includeHidden.value }
    if (tenantId.value) params.tenant = tenantId.value
    const res = await dataQuality(params)
    rows.value = res?.items || []
    summary.value = res?.summary || {}
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function rerunSites(sites: string[]) {
  if (!sites.length) return
  const key = sites.length === 1 ? sites[0] : '__batch__'
  rerunBusy.value = { ...rerunBusy.value, [key]: true }
  rerunMessage.value = { ...rerunMessage.value, [key]: '' }
  try {
    const res = await crawlEnqueue({ sites })
    const created = (res?.created_jobs || []).length
    const reused = (res?.existing_jobs || []).length
    const msg = `${created} 个新任务，${reused} 个已有任务`
    rerunMessage.value = { ...rerunMessage.value, [key]: msg }
    for (const site of sites) {
      const item = res?.by_site?.[site]
      const label = item?.status === 'queued'
        ? '已入队'
        : item?.status === 'promoted'
          ? '已提升'
          : item?.status === 'already_queued'
            ? '已有高优先级任务'
            : '已有任务'
      if (item) rerunMessage.value = {
        ...rerunMessage.value,
        [site]: `${label} #${item.job_id}`,
      }
    }
    await load()
  } catch (err) {
    rerunMessage.value = { ...rerunMessage.value, [key]: err instanceof Error ? err.message : String(err) }
  } finally {
    rerunBusy.value = { ...rerunBusy.value, [key]: false }
  }
}

async function bootstrap() {
  try {
    const res = await tenants()
    tenantRows.value = res?.items || []
  } finally {
    await load()
  }
}

onMounted(bootstrap)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1 class="page-title">数据质量</h1>
        <p class="page-subtitle">站点级验收明细：覆盖、销量收入、促销、最近任务和失败建议。</p>
      </div>
      <div class="head-actions">
        <select v-model="tenantId" class="ctl" @change="load">
          <option value="">全部 workspace</option>
          <option v-for="tenant in tenantRows" :key="tenant.id" :value="String(tenant.id)">
            {{ tenant.name }} ({{ tenant.site_count || 0 }})
          </option>
        </select>
        <label class="inline-check">
          <input v-model="includeHidden" type="checkbox" @change="load" />
          <span>包含隐藏站点</span>
        </label>
        <button class="btn small primary" :disabled="loading || rerunBusy.__batch__ || !criticalSites.length" @click="rerunSites(criticalSites)">
          {{ rerunBusy.__batch__ ? '入队中...' : `重跑需重跑(${criticalSites.length})` }}
        </button>
        <button class="btn small" :disabled="loading" @click="load">刷新</button>
      </div>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div class="stat-row">
      <StatCard label="站点" :value="fmtNumber(summary.total_sites)" />
      <StatCard label="健康" :value="fmtNumber(summary.healthy)" />
      <StatCard label="需重跑" :value="fmtNumber(summary.needs_rerun)" />
      <StatCard label="缺价格" :value="fmtNumber(summary.missing_prices)" />
      <StatCard label="缺销量" :value="fmtNumber(summary.missing_sales)" />
      <StatCard label="缺促销" :value="fmtNumber(summary.missing_promotions)" />
      <StatCard label="覆盖风险" :value="fmtNumber(summary.coverage_risk)" />
      <StatCard label="待处理任务" :value="fmtNumber(summary.pending_jobs)" />
      <StatCard label="久排任务" :value="fmtNumber(summary.stale_pending_jobs)" />
      <StatCard label="运行任务" :value="fmtNumber(summary.running_jobs)" />
      <StatCard label="卡住任务" :value="fmtNumber(summary.stuck_jobs)" />
      <StatCard label="失败任务" :value="fmtNumber(summary.failed_jobs)" />
    </div>

    <div class="table-wrap">
      <table class="tbl">
        <thead>
          <tr>
            <th>站点</th>
            <th>状态</th>
            <th>SKU / SPU</th>
            <th>覆盖</th>
            <th>促销</th>
            <th>价格 / 销量 / 收入信号</th>
            <th>任务队列</th>
            <th>最近任务</th>
            <th>问题</th>
            <th>建议</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in sortedRows" :key="row.site">
            <td>
              <b>{{ row.site }}</b>
              <small>{{ row.brand || '-' }} · {{ row.country || '-' }}</small>
              <small v-if="row.workspaces?.length">
                {{ row.workspaces.map((w: any) => w.name).join(' / ') }}
              </small>
            </td>
            <td><StatusBadge :status="row.status" /></td>
            <td>{{ fmtNumber(row.sku_count) }} / {{ fmtNumber(row.spu_count) }}</td>
            <td>{{ row.coverage_pct }}% · {{ fmtNumber(row.fetched_count) }}/{{ fmtNumber(row.estimated_full) }}</td>
            <td>{{ fmtNumber(row.promotion_count) }}</td>
            <td>{{ row.price_signal_pct }}% / {{ row.sales_signal_pct }}% / {{ row.revenue_signal_pct }}%</td>
            <td>
              <div v-if="queueBadges(row).length" class="queue-badges">
                <span v-for="item in queueBadges(row)" :key="item.key" :class="['queue-badge', item.key]">
                  {{ item.label }} {{ fmtNumber(item.value) }}
                </span>
              </div>
              <span v-else class="muted">无活跃/失败</span>
              <small v-if="row.crawl_queue?.oldest_active_at">
                最早 {{ fmtDate(row.crawl_queue.oldest_active_at) }}
              </small>
              <RouterLink class="queue-link" :to="{ path: '/queue', query: { source: 'crawl', dataset: row.site } }">
                队列明细
              </RouterLink>
            </td>
            <td>
              <span v-if="row.latest_job">#{{ row.latest_job.id }} {{ row.latest_job.status }}</span>
              <span v-else>-</span>
              <small>{{ fmtDate(row.latest_job?.finished_at || row.last_product_updated || row.last_crawled) }}</small>
            </td>
            <td>
              <div class="issues">
                <span v-for="issue in row.issues" :key="issue">{{ issueLabel(issue) }}</span>
                <span v-if="!row.issues?.length" class="ok">质量正常</span>
              </div>
            </td>
            <td class="suggest">{{ row.suggested_action || '-' }}</td>
            <td>
              <button class="btn small" :disabled="rerunBusy[row.site]" @click="rerunSites([row.site])">
                {{ rerunBusy[row.site] ? '入队中...' : '重跑' }}
              </button>
              <small v-if="rerunMessage[row.site]" class="rerun-msg">{{ rerunMessage[row.site] }}</small>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td colspan="11" class="empty">{{ loading ? '加载中...' : '暂无数据质量明细' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.page { padding:24px; display:flex; flex-direction:column; gap:16px; }
.page-head { display:flex; align-items:center; justify-content:space-between; gap:16px; }
.page-title { font-size:20px; font-weight:600; }
.page-subtitle { margin-top:4px; font-size:12px; opacity:.55; }
.head-actions { display:flex; align-items:center; justify-content:flex-end; gap:10px; flex-wrap:wrap; }
.ctl { min-height:32px; padding:5px 10px; border-radius:7px; border:1px solid var(--ui-border, rgba(255,255,255,.12)); background:var(--ui-bg, rgba(0,0,0,.16)); color:inherit; font-size:12px; }
.inline-check { display:inline-flex; align-items:center; gap:6px; font-size:12px; opacity:.78; white-space:nowrap; }
.stat-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(128px,1fr)); gap:12px; }
.table-wrap { overflow:auto; border:1px solid var(--ui-border, rgba(255,255,255,.08)); border-radius:12px; }
.tbl { width:100%; min-width:1440px; border-collapse:collapse; font-size:13px; }
.tbl th,.tbl td { padding:10px 12px; text-align:left; border-bottom:1px solid var(--ui-border, rgba(255,255,255,.06)); vertical-align:top; }
.tbl th { font-weight:600; opacity:.7; white-space:nowrap; }
.tbl b { display:block; }
.tbl small { display:block; margin-top:3px; opacity:.58; white-space:nowrap; }
.issues { display:flex; flex-wrap:wrap; gap:5px; min-width:190px; }
.issues span { display:inline-flex; padding:2px 7px; border-radius:999px; color:#fca5a5; background:rgba(248,113,113,.13); border:1px solid rgba(248,113,113,.24); font-size:11px; font-weight:700; }
.issues span.ok { color:#86efac; background:rgba(16,185,129,.13); border-color:rgba(16,185,129,.24); }
.queue-badges { display:flex; flex-wrap:wrap; gap:5px; min-width:140px; }
.queue-badge { display:inline-flex; align-items:center; gap:3px; padding:2px 7px; border-radius:999px; font-size:11px; font-weight:700; border:1px solid rgba(148,163,184,.28); background:rgba(148,163,184,.12); color:var(--ui-text, #e5e7eb); }
.queue-badge.pending { color:#fde68a; background:rgba(245,158,11,.14); border-color:rgba(245,158,11,.28); }
.queue-badge.stale { color:#fcd34d; background:rgba(217,119,6,.18); border-color:rgba(217,119,6,.34); }
.queue-badge.running { color:#93c5fd; background:rgba(59,130,246,.14); border-color:rgba(59,130,246,.28); }
.queue-badge.stuck { color:#fca5a5; background:rgba(248,113,113,.15); border-color:rgba(248,113,113,.3); }
.queue-badge.failed { color:#fdba74; background:rgba(249,115,22,.14); border-color:rgba(249,115,22,.3); }
.muted { opacity:.55; white-space:nowrap; }
.queue-link { display:inline-flex; margin-top:4px; font-size:12px; color:#a78bfa; text-decoration:none; }
.queue-link:hover { text-decoration:underline; }
.suggest { max-width:220px; line-height:1.45; }
.btn.small { padding:4px 10px; border-radius:6px; font-size:12px; border:1px solid var(--ui-border, rgba(255,255,255,.12)); background:transparent; color:inherit; cursor:pointer; }
.btn.small.primary { border-color:rgba(139,92,246,.45); color:#fff; background:rgba(139,92,246,.85); }
.btn:disabled { opacity:.55; cursor:not-allowed; }
.rerun-msg { display:block; margin-top:4px; color:var(--ui-muted, #9ca3af); max-width:140px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.error { font-size:13px; color:#ef4444; }
.empty { text-align:center; opacity:.6; padding:24px; }
@media (max-width:1100px) {
  .stat-row { grid-template-columns:repeat(2,minmax(0,1fr)); }
}
</style>
