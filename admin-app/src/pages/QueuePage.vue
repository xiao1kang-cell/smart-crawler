<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { enqueueJob, jobDetail, jobStats, listJobs, queueMaintenance, retryJob } from '../api/queue'
import { fmtDate, fmtNumber } from '../api/client'
import StatCard from '../components/common/StatCard.vue'
import StatusBadge from '../components/common/StatusBadge.vue'

const POLL_MS = 5000
const route = useRoute()

const stats = ref<Record<string, any>>({})
const items = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

const statusFilter = ref('')
const sourceFilter = ref('all')
const targetFilter = ref('')
const failureCodeFilter = ref('')
const page = ref(1)
const size = ref(20)
const detailRow = ref<Record<string, any> | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const maintenanceBusy = ref('')
const maintenanceMsg = ref('')
const maintenanceResult = ref<Record<string, any> | null>(null)

const polling = ref(true)
let timer: ReturnType<typeof setInterval> | null = null

const enqForm = ref({ url: '', dataset: '' })
const enqBusy = ref(false)
const enqMsg = ref('')
const ALL_STATUS = '__all_status__'

const sourceFilterItems = [
  { label: '全部来源', value: 'all' },
  { label: '站点采集', value: 'crawl' },
  { label: '通用抓取', value: 'spine' },
  { label: '按需抓取', value: 'ondemand' }
]
const statusFilterItems = [
  { label: '全部状态', value: ALL_STATUS },
  { label: '待处理', value: 'pending' },
  { label: '久排', value: 'stale_pending' },
  { label: '采取中', value: 'running' },
  { label: '卡住', value: 'stuck' },
  { label: '成功', value: 'success' },
  { label: '部分成功', value: 'partial' },
  { label: '失败', value: 'failed' },
  { label: '阻断', value: 'blocked' },
  { label: '跳过', value: 'skipped' }
]
const statusSelect = computed({
  get: () => statusFilter.value || ALL_STATUS,
  set: (value: string) => {
    statusFilter.value = value === ALL_STATUS ? '' : value
  }
})
const pageSizeItems = [
  { label: '20 / 页', value: 20 },
  { label: '50 / 页', value: 50 },
  { label: '100 / 页', value: 100 }
]
const queueColumns = [
  { accessorKey: 'id', header: 'ID' },
  { accessorKey: 'source', header: '来源' },
  { accessorKey: 'status', header: '状态' },
  { accessorKey: 'target', header: '目标' },
  { accessorKey: 'url', header: 'URL / 批次' },
  { accessorKey: 'products_count', header: '已抓取/总量' },
  { accessorKey: 'failure_code', header: '失败码 / 阶段' },
  { accessorKey: 'retryable', header: '可重试' },
  { accessorKey: 'attempts', header: '执行次数' },
  { accessorKey: 'error', header: '错误' },
  { accessorKey: 'duration', header: '耗时 / 活跃' },
  { accessorKey: 'finished_at', header: '完成时间' },
  { accessorKey: 'created_at', header: '创建时间' },
  { id: 'actions', header: '' }
]

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size.value)))
const detailJson = computed(() => {
  if (!detailRow.value) return ''
  try {
    return JSON.stringify(detailRow.value, null, 2)
  } catch {
    return String(detailRow.value)
  }
})

const statCards = computed(() => [
  { key: 'pending', label: '待处理', value: stats.value.pending ?? 0 },
  { key: 'stale_pending', label: '久排', value: stats.value.stale_pending ?? 0 },
  { key: 'running', label: '采取中', value: stats.value.running ?? 0 },
  { key: 'stuck', label: '卡住', value: stats.value.stuck ?? 0 },
  { key: 'success', label: '成功', value: stats.value.success ?? 0 },
  { key: 'partial', label: '部分成功', value: stats.value.partial ?? 0 },
  { key: 'failed', label: '失败', value: stats.value.failed ?? 0 },
  { key: 'blocked', label: '阻断', value: stats.value.blocked ?? 0 },
  { key: 'skipped', label: '跳过', value: stats.value.skipped ?? 0 }
])

const sourceCards = computed(() => {
  const byQueue = (stats.value.by_queue || {}) as Record<string, any>
  return [
    { key: 'all', label: '全部', value: stats.value.total ?? 0 },
    { key: 'crawl', label: '站点采集', value: byQueue.crawl?.total ?? 0 },
    { key: 'spine', label: '通用抓取', value: byQueue.spine?.total ?? 0 },
    { key: 'ondemand', label: '按需抓取', value: byQueue.ondemand?.total ?? 0 }
  ]
})

const breakdowns = computed(() => (stats.value.breakdowns || {}) as Record<string, any[]>)
const statusMeta = computed(() => (stats.value.status_meta || {}) as Record<string, any>)
const queueCountNote = computed(() => String(stats.value.status_count_note || ''))
const maintenanceJson = computed(() => {
  if (!maintenanceResult.value) return ''
  try {
    return JSON.stringify(maintenanceResult.value, null, 2)
  } catch {
    return String(maintenanceResult.value)
  }
})

const breakdownCards = computed(() => [
  {
    key: 'crawl_failed_by_site',
    title: '失败站点',
    status: 'failed',
    source: 'crawl',
    rows: breakdowns.value.crawl_failed_by_site || []
  },
  {
    key: 'crawl_running_by_site',
    title: '采取中站点',
    status: 'running',
    source: 'crawl',
    rows: breakdowns.value.crawl_running_by_site || []
  },
  {
    key: 'crawl_stuck_by_site',
    title: '卡住站点',
    status: 'stuck',
    source: 'crawl',
    rows: breakdowns.value.crawl_stuck_by_site || []
  },
  {
    key: 'crawl_stale_pending_by_site',
    title: '久排站点',
    status: 'stale_pending',
    source: 'crawl',
    rows: breakdowns.value.crawl_stale_pending_by_site || []
  },
  {
    key: 'crawl_blocked_by_site',
    title: '阻断站点',
    status: 'blocked',
    source: 'crawl',
    rows: breakdowns.value.crawl_blocked_by_site || []
  },
  {
    key: 'crawl_skipped_by_site',
    title: '跳过站点',
    status: 'skipped',
    source: 'crawl',
    rows: breakdowns.value.crawl_skipped_by_site || []
  },
  {
    key: 'crawl_failure_codes',
    title: '失败码',
    status: 'failed,blocked',
    source: 'crawl',
    failureCode: true,
    rows: breakdowns.value.crawl_failure_codes || []
  },
  {
    key: 'spine_failed_by_dataset',
    title: '通用失败',
    status: 'failed',
    source: 'spine',
    rows: breakdowns.value.spine_failed_by_dataset || []
  },
  {
    key: 'spine_running_by_dataset',
    title: '通用采取中',
    status: 'running',
    source: 'spine',
    rows: breakdowns.value.spine_running_by_dataset || []
  },
  {
    key: 'spine_stuck_by_dataset',
    title: '通用卡住',
    status: 'stuck',
    source: 'spine',
    rows: breakdowns.value.spine_stuck_by_dataset || []
  },
  {
    key: 'ondemand_running_by_platform',
    title: '按需采取中',
    status: 'running',
    source: 'ondemand',
    rows: breakdowns.value.ondemand_running_by_platform || []
  },
  {
    key: 'ondemand_stuck_by_platform',
    title: '按需卡住',
    status: 'stuck',
    source: 'ondemand',
    rows: breakdowns.value.ondemand_stuck_by_platform || []
  },
  {
    key: 'ondemand_failed_by_platform',
    title: '按需失败',
    status: 'failed',
    source: 'ondemand',
    rows: breakdowns.value.ondemand_failed_by_platform || []
  }
])

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [s, jobs] = await Promise.all([
      jobStats(),
      listJobs({
        status: statusFilter.value,
        source: sourceFilter.value,
        dataset: targetFilter.value,
        failure_code: failureCodeFilter.value,
        page: page.value,
        size: size.value
      })
    ])
    stats.value = s || {}
    items.value = jobs?.items ?? []
    total.value = jobs?.total ?? 0
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function startPolling() {
  stopPolling()
  if (polling.value) timer = setInterval(load, POLL_MS)
}

function stopPolling() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

watch(polling, (on) => (on ? startPolling() : stopPolling()))

watch([statusFilter, sourceFilter, targetFilter, failureCodeFilter, size], () => {
  page.value = 1
  load()
})

watch(() => route.fullPath, () => {
  applyRouteQuery()
  page.value = 1
  load()
})

async function submitEnqueue() {
  if (!enqForm.value.url || !enqForm.value.dataset) {
    enqMsg.value = '请填写 URL 与 dataset'
    return
  }
  enqBusy.value = true
  enqMsg.value = ''
  try {
    const res = await enqueueJob({ url: enqForm.value.url, dataset: enqForm.value.dataset })
    enqMsg.value = `已入队 #${res?.job_id ?? '-'}`
    enqForm.value.url = ''
    await load()
  } catch (err) {
    enqMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    enqBusy.value = false
  }
}

async function doRetry(row: Record<string, any>) {
  const id = Number(row.id)
  try {
    await retryJob(id, row?.source || 'spine')
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function runMaintenance(apply = false) {
  if (apply && !window.confirm('确认恢复当前卡住任务？crawl 卡住任务会按超时失败收尾，通用/按需卡住任务会重新入队。')) {
    return
  }
  maintenanceBusy.value = apply ? 'apply' : 'dry'
  maintenanceMsg.value = ''
  try {
    const res = await queueMaintenance({ apply, sample_limit: 20 })
    maintenanceResult.value = res || {}
    const counts = res?.counts || {}
    maintenanceMsg.value = apply
      ? `已处理 ${res?.total_actionable ?? 0} 个卡住任务`
      : `待处理 ${res?.total_actionable ?? 0} 个卡住任务，久排 ${counts.crawl_stale_pending_observed ?? 0} 个`
    await load()
  } catch (err) {
    maintenanceMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    maintenanceBusy.value = ''
  }
}

function changePage(delta: number) {
  const next = page.value + delta
  if (next < 1 || next > totalPages.value) return
  page.value = next
  load()
}

function setPage(next: number) {
  page.value = next
  load()
}

function applyBreakdown(card: Record<string, any>, row: Record<string, any>) {
  sourceFilter.value = card.source || 'all'
  statusFilter.value = card.status || ''
  page.value = 1
  if (card.failureCode) {
    failureCodeFilter.value = row.key === 'null' ? '' : row.key
  } else {
    targetFilter.value = row.key === 'null' ? '' : row.key
    failureCodeFilter.value = ''
  }
  load()
}

function clearFilters() {
  sourceFilter.value = 'all'
  statusFilter.value = ''
  targetFilter.value = ''
  failureCodeFilter.value = ''
  page.value = 1
  load()
}

function applyStatusCard(status: string) {
  statusFilter.value = status
  sourceFilter.value = 'all'
  targetFilter.value = ''
  failureCodeFilter.value = ''
  page.value = 1
  load()
}

function truncate(s?: string | null, n = 48) {
  const v = String(s || '')
  return v.length > n ? `${v.slice(0, n)}…` : v
}

function targetText(row: Record<string, any>) {
  return row.target || row.site || row.dataset || row.platform || row.url || '-'
}

function metaText(row: Record<string, any>) {
  if (row.failure_code || row.failure_stage) return [row.failure_code, row.failure_stage].filter(Boolean).join(' / ')
  if (row.listing_count || row.review_count) return `${row.listing_count || 0} listings / ${row.review_count || 0} reviews`
  return '-'
}

function retryableText(row: Record<string, any>) {
  if (row.retryable === true) return '可重试'
  if (row.retryable === false) return '不可重试'
  return '-'
}

function attemptText(row: Record<string, any>) {
  return row.attempts ?? row.retries ?? '-'
}

function productProgressText(row: Record<string, any>) {
  const fetched = Number(row.products_count ?? row.product_count ?? row.listing_count ?? 0)
  const total = Number(row.total_product_count ?? row.crawl_total_product_count ?? row.attempted_product_count ?? 0)
  return `${fmtNumber(fetched)}/${total > 0 ? fmtNumber(total) : '-'}`
}

function durationText(row: Record<string, any>) {
  const parts: string[] = []
  const duration = Number(row.duration_sec ?? 0)
  const active = Number(row.active_sec ?? 0)
  const age = Number(row.age_sec ?? 0)
  if (duration > 0) parts.push(`耗时 ${humanSeconds(duration)}`)
  if (!duration && active > 0) parts.push(`活跃 ${humanSeconds(active)}`)
  if (!duration && !active && age > 0) parts.push(`等待 ${humanSeconds(age)}`)
  if (row.is_stale_pending) parts.push('久排')
  return parts.join(' / ') || '-'
}

function humanSeconds(value: number) {
  if (!Number.isFinite(value) || value <= 0) return '0s'
  if (value < 60) return `${Math.round(value)}s`
  if (value < 3600) return `${Math.round(value / 60)}m`
  if (value < 86400) return `${Math.round(value / 3600)}h`
  return `${Math.round(value / 86400)}d`
}

function noteText(row: Record<string, any>) {
  if (!row.notes) return ''
  try {
    return typeof row.notes === 'string' ? row.notes : JSON.stringify(row.notes)
  } catch {
    return String(row.notes)
  }
}

async function openDetail(row: Record<string, any>) {
  detailRow.value = row
  detailLoading.value = true
  detailError.value = ''
  try {
    const detail = await jobDetail(Number(row.id), row?.source || 'spine')
    detailRow.value = { ...row, ...detail }
  } catch (err) {
    detailError.value = err instanceof Error ? err.message : String(err)
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  detailRow.value = null
  detailError.value = ''
  detailLoading.value = false
}

function canRetry(row: Record<string, any>) {
  return !['pending', 'running'].includes(String(row.normalized_status || row.status || ''))
}

function applyRouteQuery() {
  const q = route.query
  sourceFilter.value = String(q.source || sourceFilter.value || 'all')
  statusFilter.value = String(q.status || statusFilter.value || '')
  targetFilter.value = String(q.dataset || q.target || targetFilter.value || '')
  failureCodeFilter.value = String(q.failure_code || failureCodeFilter.value || '')
}

onMounted(() => {
  applyRouteQuery()
  load()
  startPolling()
})

onUnmounted(stopPolling)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1 class="page-title">任务队列</h1>
        <p class="page-subtitle">统一统计站点采集、通用抓取和按需抓取任务。</p>
      </div>
      <label class="poll-toggle">
        <input v-model="polling" type="checkbox" />
        <span>自动刷新 (5s)</span>
      </label>
      <div class="head-actions">
        <button class="btn small" :disabled="!!maintenanceBusy" @click="runMaintenance(false)">
          {{ maintenanceBusy === 'dry' ? '体检中…' : '体检队列' }}
        </button>
        <button class="btn small primary" :disabled="!!maintenanceBusy" @click="runMaintenance(true)">
          {{ maintenanceBusy === 'apply' ? '恢复中…' : '恢复卡住任务' }}
        </button>
      </div>
    </div>

    <div class="stat-row">
      <button
        v-for="c in statCards"
        :key="c.key"
        class="stat-filter"
        :class="{ active: statusFilter === c.key }"
        @click="applyStatusCard(c.key)"
      >
        <StatCard :label="c.label" :value="c.value" />
      </button>
    </div>

    <div class="source-row">
      <button
        v-for="c in sourceCards"
        :key="c.key"
        class="source-chip"
        :class="{ active: sourceFilter === c.key }"
        @click="sourceFilter = c.key"
      >
        <span>{{ c.label }}</span>
        <b>{{ c.value }}</b>
      </button>
    </div>

    <div v-if="queueCountNote" class="queue-note">
      <span>{{ queueCountNote }}</span>
      <b>原始运行 {{ statusMeta.running_raw ?? 0 }}</b>
      <b>有效运行 {{ statusMeta.running_active ?? 0 }}</b>
      <b>卡住 {{ statusMeta.stuck ?? 0 }}</b>
      <b>久排 {{ statusMeta.stale_pending ?? 0 }}</b>
    </div>

    <div v-if="maintenanceMsg || maintenanceResult" class="maintenance-panel">
      <div class="maintenance-head">
        <strong>{{ maintenanceMsg || '队列维护结果' }}</strong>
        <span v-if="maintenanceResult">
          {{ maintenanceResult.applied ? '已执行' : '只读体检' }} · {{ fmtDate(maintenanceResult.checked_at) }}
        </span>
      </div>
      <div v-if="maintenanceResult?.counts" class="maintenance-counts">
        <span>通用重入队 <b>{{ maintenanceResult.counts.spine_requeued ?? 0 }}</b></span>
        <span>采集超时收尾 <b>{{ maintenanceResult.counts.crawl_failed_timeout ?? 0 }}</b></span>
        <span>按需重入队 <b>{{ maintenanceResult.counts.ondemand_requeued ?? 0 }}</b></span>
        <span>久排待诊断 <b>{{ maintenanceResult.counts.crawl_stale_pending_observed ?? 0 }}</b></span>
      </div>
      <details v-if="maintenanceResult" class="maintenance-detail">
        <summary>查看维护明细 JSON</summary>
        <pre>{{ maintenanceJson }}</pre>
      </details>
    </div>

    <div class="toolbar">
      <USelect v-model="sourceFilter" class="select-ctl" :items="sourceFilterItems" value-key="value" />
      <USelect v-model="statusSelect" class="select-ctl" :items="statusFilterItems" value-key="value" />
      <input v-model.trim="targetFilter" class="ctl filter-input" placeholder="站点 / 平台 / 批次 / URL" />
      <input v-model.trim="failureCodeFilter" class="ctl filter-input code-input" placeholder="失败码" />
      <button class="ctl btn" :disabled="loading" @click="load">刷新</button>
      <button class="ctl btn" :disabled="loading" @click="clearFilters">清空</button>
    </div>

    <div class="breakdown-grid">
      <section v-for="card in breakdownCards" :key="card.key" class="breakdown-panel">
        <div class="breakdown-head">
          <h2>{{ card.title }}</h2>
          <span>{{ card.rows.reduce((sum, row) => sum + Number(row.count || 0), 0) }}</span>
        </div>
        <div v-if="card.rows.length" class="breakdown-list">
          <button
            v-for="row in card.rows.slice(0, 8)"
            :key="`${card.key}-${row.key}`"
            class="breakdown-row"
            @click="applyBreakdown(card, row)"
          >
            <span :title="row.key">{{ row.key }}</span>
            <b>{{ row.count }}</b>
          </button>
        </div>
        <div v-else class="breakdown-empty">暂无</div>
      </section>
    </div>

    <div class="enqueue">
      <input v-model="enqForm.url" class="ctl grow" placeholder="URL" />
      <input v-model="enqForm.dataset" class="ctl" placeholder="dataset" />
      <button class="ctl btn primary" :disabled="enqBusy" @click="submitEnqueue">
        {{ enqBusy ? '入队中…' : '入队' }}
      </button>
      <span v-if="enqMsg" class="enqueue-msg">{{ enqMsg }}</span>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div class="table-wrap">
      <UTable class="tbl ui-table" :data="items" :columns="queueColumns" :loading="loading" sticky="header" empty="暂无任务">
        <template #source-cell="{ row }">{{ row.original.source_label || row.original.source || '-' }}</template>
        <template #status-cell="{ row }"><StatusBadge :status="row.original.normalized_status || row.original.status" /></template>
        <template #target-cell="{ row }">{{ targetText(row.original) }}</template>
        <template #url-cell="{ row }">
          <span :title="row.original.url || row.original.dataset || ''">{{ truncate(row.original.url || row.original.dataset) || '-' }}</span>
        </template>
        <template #products_count-cell="{ row }">
          <span :title="row.original.total_product_count_source ? `本次总量来源：${row.original.total_product_count_source}` : '暂无本次总量数据'">
            {{ productProgressText(row.original) }}
          </span>
        </template>
        <template #failure_code-cell="{ row }">
          <span :title="metaText(row.original)">{{ truncate(metaText(row.original), 32) }}</span>
        </template>
        <template #retryable-cell="{ row }">{{ retryableText(row.original) }}</template>
        <template #attempts-cell="{ row }">{{ attemptText(row.original) }}</template>
        <template #error-cell="{ row }">
          <span class="err-cell" :title="row.original.error || ''">{{ truncate(row.original.error, 36) || '-' }}</span>
        </template>
        <template #duration-cell="{ row }">
          <span :title="row.original.stuck_reason || ''">{{ durationText(row.original) }}</span>
        </template>
        <template #finished_at-cell="{ row }">{{ fmtDate(row.original.finished_at) }}</template>
        <template #created_at-cell="{ row }">{{ fmtDate(row.original.created_at) }}</template>
        <template #actions-cell="{ row }">
          <div class="row-actions">
            <button class="btn small" @click="openDetail(row.original)">详情</button>
            <button class="btn small" :disabled="!canRetry(row.original)" @click="doRetry(row.original)">重试</button>
          </div>
        </template>
      </UTable>
    </div>

    <div class="pager">
      <UPagination
        :page="page"
        :total="total"
        :items-per-page="size"
        :disabled="loading || totalPages <= 1"
        size="sm"
        show-edges
        @update:page="setPage"
      />
      <span>第 {{ page }} / {{ totalPages }} 页 · 共 {{ total }} 条</span>
      <USelect v-model="size" class="size-select" :items="pageSizeItems" value-key="value" />
    </div>

    <div v-if="detailRow" class="detail-mask" @click.self="closeDetail">
      <aside class="detail-panel">
        <div class="detail-head">
          <div>
            <p>{{ detailRow.source_label || detailRow.source }}</p>
            <h2>#{{ detailRow.id }} · {{ targetText(detailRow) }}</h2>
          </div>
          <button class="btn small" @click="closeDetail">关闭</button>
        </div>
        <div v-if="detailLoading" class="detail-loading">正在读取最新任务明细…</div>
        <div v-if="detailError" class="detail-error">{{ detailError }}</div>
        <dl class="detail-grid">
          <div>
            <dt>状态</dt>
            <dd><StatusBadge :status="detailRow.normalized_status || detailRow.status" /></dd>
          </div>
          <div>
            <dt>原始状态</dt>
            <dd>{{ detailRow.raw_status || '-' }}</dd>
          </div>
          <div>
            <dt>触发来源</dt>
            <dd>{{ detailRow.trigger || '-' }}</dd>
          </div>
          <div>
            <dt>失败码</dt>
            <dd>{{ detailRow.failure_code || '-' }}</dd>
          </div>
          <div>
            <dt>阶段</dt>
            <dd>{{ detailRow.failure_stage || '-' }}</dd>
          </div>
          <div>
            <dt>可重试</dt>
            <dd>{{ retryableText(detailRow) }}</dd>
          </div>
          <div>
            <dt>执行次数</dt>
            <dd>{{ attemptText(detailRow) }}</dd>
          </div>
          <div>
            <dt>排队时长</dt>
            <dd>{{ detailRow.age_sec ? humanSeconds(Number(detailRow.age_sec)) : '-' }}</dd>
          </div>
          <div>
            <dt>活跃时长</dt>
            <dd>{{ detailRow.active_sec ? humanSeconds(Number(detailRow.active_sec)) : '-' }}</dd>
          </div>
          <div>
            <dt>任务耗时</dt>
            <dd>{{ detailRow.duration_sec ? humanSeconds(Number(detailRow.duration_sec)) : '-' }}</dd>
          </div>
          <div>
            <dt>卡住原因</dt>
            <dd>{{ detailRow.stuck_reason || '-' }}</dd>
          </div>
          <div>
            <dt>创建时间</dt>
            <dd>{{ fmtDate(detailRow.created_at) }}</dd>
          </div>
          <div>
            <dt>开始时间</dt>
            <dd>{{ fmtDate(detailRow.started_at) }}</dd>
          </div>
          <div>
            <dt>完成时间</dt>
            <dd>{{ fmtDate(detailRow.finished_at) }}</dd>
          </div>
          <div>
            <dt>Worker</dt>
            <dd>{{ detailRow.worker || '-' }}</dd>
          </div>
          <div>
            <dt>商品 / 新品 / 促销</dt>
            <dd>{{ detailRow.products_count ?? 0 }} / {{ detailRow.new_count ?? 0 }} / {{ detailRow.promotion_count ?? 0 }}</dd>
          </div>
          <div>
            <dt>Listing / Review</dt>
            <dd>{{ detailRow.listing_count ?? 0 }} / {{ detailRow.review_count ?? 0 }}</dd>
          </div>
        </dl>
        <div class="detail-block">
          <h3>URL / 批次</h3>
          <pre>{{ detailRow.url || detailRow.dataset || detailRow.batch_id || '-' }}</pre>
        </div>
        <div class="detail-block">
          <h3>错误</h3>
          <pre>{{ detailRow.failure_detail || detailRow.error || '-' }}</pre>
        </div>
        <div class="detail-block">
          <h3>建议动作</h3>
          <pre>{{ detailRow.suggested_action || '-' }}</pre>
        </div>
        <div v-if="noteText(detailRow)" class="detail-block">
          <h3>Notes</h3>
          <pre>{{ noteText(detailRow) }}</pre>
        </div>
        <div class="detail-block">
          <h3>原始明细</h3>
          <pre>{{ detailJson }}</pre>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.page-title {
  font-size: 20px;
  font-weight: 600;
}

.page-subtitle {
  margin-top: 4px;
  font-size: 12px;
  opacity: 0.55;
}

.poll-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  opacity: 0.8;
  cursor: pointer;
}

.head-actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.stat-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.stat-filter {
  display: block;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.stat-filter :deep(.stat-card) {
  height: 100%;
  transition: border-color .16s ease, background .16s ease, transform .16s ease;
}

.stat-filter:hover :deep(.stat-card),
.stat-filter.active :deep(.stat-card) {
  border-color: rgba(139, 92, 246, .62);
  background: rgba(139, 92, 246, .12);
}

.source-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.source-chip {
  min-height: 34px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 7px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
  background: transparent;
  color: inherit;
  cursor: pointer;
}

.source-chip.active {
  border-color: rgba(139, 92, 246, 0.55);
  background: rgba(139, 92, 246, 0.14);
}

.source-chip span {
  font-size: 12px;
  opacity: 0.75;
}

.source-chip b {
  font-size: 13px;
}

.queue-note {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 12px;
  border: 1px solid rgba(245, 158, 11, 0.24);
  border-radius: 8px;
  background: rgba(245, 158, 11, 0.08);
  color: var(--ui-muted, #9ca3af);
  font-size: 12px;
  line-height: 1.5;
}

.queue-note span {
  flex: 1;
  min-width: 260px;
}

.queue-note b {
  color: inherit;
  font-weight: 700;
  white-space: nowrap;
}

.toolbar,
.enqueue {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.ctl {
  padding: 8px 12px;
  border-radius: 8px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
  background: var(--ui-bg, rgba(0, 0, 0, 0.15));
  color: inherit;
  font-size: 14px;
}

.filter-input {
  width: 240px;
}

.code-input {
  width: 150px;
}

.breakdown-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}

.breakdown-panel {
  min-height: 180px;
  padding: 12px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.1));
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.025);
}

.breakdown-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.breakdown-head h2 {
  font-size: 13px;
  font-weight: 600;
}

.breakdown-head span {
  font-size: 12px;
  opacity: 0.55;
}

.breakdown-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.breakdown-row {
  width: 100%;
  min-height: 28px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 4px 6px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.breakdown-row:hover {
  background: rgba(139, 92, 246, 0.12);
}

.breakdown-row span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}

.breakdown-row b {
  font-size: 12px;
}

.breakdown-empty {
  padding: 30px 0;
  text-align: center;
  font-size: 12px;
  opacity: 0.45;
}

.grow {
  flex: 1;
  min-width: 200px;
}

.btn {
  cursor: pointer;
}

.btn.primary {
  border: none;
  color: #fff;
  background: var(--ui-color-primary-500, #6366f1);
}

.btn.small {
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 12px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
  background: transparent;
  color: inherit;
}

.btn.small.primary {
  border-color: transparent;
  color: #fff;
  background: var(--ui-color-primary-500, #6366f1);
}

.btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.enqueue-msg {
  font-size: 13px;
  opacity: 0.75;
}

.error {
  font-size: 13px;
  color: #ef4444;
}

.maintenance-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  border: 1px solid rgba(34, 197, 94, 0.24);
  border-radius: 8px;
  background: rgba(34, 197, 94, 0.07);
}

.maintenance-head,
.maintenance-counts {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.maintenance-head strong {
  font-size: 13px;
}

.maintenance-head span,
.maintenance-counts span {
  font-size: 12px;
  color: var(--ui-muted, #9ca3af);
}

.maintenance-counts b {
  color: inherit;
}

.maintenance-detail summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--ui-muted, #9ca3af);
}

.maintenance-detail pre {
  max-height: 260px;
  margin-top: 8px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.table-wrap {
  position: relative;
  z-index: 0;
  overflow-x: auto;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.08));
  border-radius: 12px;
  background: var(--admin-panel, #fff);
}

.tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.tbl th,
.tbl td {
  padding: 10px 12px;
  text-align: left;
  border-bottom: 1px solid var(--ui-border, rgba(255, 255, 255, 0.06));
  white-space: nowrap;
}

.tbl th {
  font-weight: 600;
  opacity: 0.7;
}

.err-cell {
  max-width: 240px;
  color: #ef4444;
}

.row-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.empty {
  text-align: center;
  opacity: 0.6;
  padding: 24px;
}

.pager {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
}

.detail-mask {
  position: fixed;
  inset: 0;
  z-index: 120;
  display: flex;
  justify-content: flex-end;
  background: var(--admin-overlay, rgba(15, 23, 42, 0.42));
  backdrop-filter: blur(4px);
}

.detail-panel {
  width: min(620px, 100vw);
  height: 100%;
  padding: 22px;
  overflow: auto;
  border-left: 1px solid var(--ui-border, rgba(148, 163, 184, 0.32));
  background: var(--ui-panel, #fff);
  color: var(--ui-text, #0f172a);
  box-shadow: -20px 0 60px rgba(15, 23, 42, 0.22);
}

.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.detail-head p {
  margin-bottom: 4px;
  font-size: 12px;
  opacity: 0.55;
}

.detail-head h2 {
  font-size: 18px;
  font-weight: 600;
}

.detail-loading,
.detail-error {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: 13px;
}

.detail-loading {
  border: 1px solid rgba(139, 92, 246, 0.28);
  background: rgba(139, 92, 246, 0.12);
  color: var(--ui-color-primary-600, #6d28d9);
}

.detail-error {
  border: 1px solid rgba(239, 68, 68, 0.28);
  background: rgba(239, 68, 68, 0.1);
  color: var(--admin-danger-text, #b91c1c);
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin: 0 0 16px;
}

.detail-grid div,
.detail-block {
  padding: 10px;
  border: 1px solid var(--ui-border, rgba(148, 163, 184, 0.32));
  border-radius: 8px;
  background: var(--admin-panel-soft, #f8fafc);
}

.detail-grid dt,
.detail-block h3 {
  margin-bottom: 6px;
  font-size: 12px;
  font-weight: 600;
  opacity: 0.6;
}

.detail-grid dd {
  margin: 0;
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 13px;
}

.detail-block {
  margin-top: 10px;
}

.detail-block pre {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  font-size: 13px;
  line-height: 1.5;
}

@media (max-width: 1000px) {
  .stat-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .breakdown-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .page-head {
    align-items: flex-start;
    flex-direction: column;
  }
}

@media (max-width: 640px) {
  .stat-row {
    grid-template-columns: 1fr;
  }

  .breakdown-grid,
  .detail-grid {
    grid-template-columns: 1fr;
  }

  .filter-input,
  .code-input {
    width: 100%;
  }
}
</style>
