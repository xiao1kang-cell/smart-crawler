<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { asList, fmtDate } from '../../api/client'
import { crawlDiagnostics, listFailedProducts, listJobs, retryFailedProducts, retryJob } from '../../api/jobs'
import DataLoadingPanel from '../common/DataLoadingPanel.vue'
import PageLoading from '../common/PageLoading.vue'
import PathLoader from '../common/PathLoader.vue'
import StatusBadge from '../common/StatusBadge.vue'
import { useToastStore } from '../../stores/toast'

defineProps<{
  embedded?: boolean
}>()

const jobs = ref<Record<string, any>[]>([])
const diagnostics = ref<Record<string, any> | null>(null)
const jobSummary = ref<Record<string, any>>({})
const siteScope = ref<Record<string, any>>({})
const jobTotal = ref(0)
const error = ref('')
const loading = ref(false)
const retryingId = ref<number | string | null>(null)
const failedDrawerOpen = ref(false)
const failedLoading = ref(false)
const failedRetrying = ref(false)
const failedJob = ref<Record<string, any> | null>(null)
const failedRows = ref<Record<string, any>[]>([])
const failedTotal = ref(0)
const failedPage = ref(1)
const failedPageSize = ref(50)
const failedFailureCode = ref('')
const failedSummary = ref<Record<string, any>>({})
const selectedFailedUrls = ref<Set<string>>(new Set())
const statusFilter = ref('all')
const timeFilter = ref('today')
const customDateFrom = ref('')
const customDateTo = ref('')
const pageSize = ref(20)
const page = ref(1)
const statusItems = [
  { label: '全部状态', value: 'all' },
  { label: '排队中', value: 'queued,pending' },
  { label: '采集中', value: 'running' },
  { label: '成功', value: 'success,completed' },
  { label: '失败', value: 'failed' },
  { label: '阻断', value: 'blocked' },
  { label: '跳过', value: 'skipped' },
]
const pageSizeItems = [
  { label: '20 条/页', value: 20 },
  { label: '40 条/页', value: 40 },
  { label: '60 条/页', value: 60 },
  { label: '100 条/页', value: 100 },
]
const timeItems = [
  { label: '今天', value: 'today' },
  { label: '昨天', value: 'yesterday' },
  { label: '近 7 天', value: '7d' },
  { label: '近 30 天', value: '30d' },
  { label: '全部时间', value: 'all' },
  { label: '自定义', value: 'custom' },
]
const toast = useToastStore()

function localDayRange(offsetDays = 0) {
  const start = new Date()
  start.setHours(0, 0, 0, 0)
  start.setDate(start.getDate() + offsetDays)
  const end = new Date(start)
  end.setDate(end.getDate() + 1)
  end.setMilliseconds(end.getMilliseconds() - 1)
  return { start: start.toISOString(), end: end.toISOString() }
}

function localDateBoundary(value: string, endOfDay = false) {
  if (!value) return undefined
  const parts = value.split('-').map((part) => Number(part))
  if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) return undefined
  const [year, month, day] = parts
  const dt = new Date(year, month - 1, day)
  if (endOfDay) dt.setHours(23, 59, 59, 999)
  else dt.setHours(0, 0, 0, 0)
  return dt.toISOString()
}

const timeRange = computed(() => {
  if (timeFilter.value === 'today') {
    return { ...localDayRange(0), label: '今天', ranged: true }
  }
  if (timeFilter.value === 'yesterday') {
    return { ...localDayRange(-1), label: '昨天', ranged: true }
  }
  if (timeFilter.value === '7d' || timeFilter.value === '30d') {
    const days = timeFilter.value === '7d' ? 7 : 30
    const end = new Date()
    end.setHours(23, 59, 59, 999)
    const start = new Date()
    start.setHours(0, 0, 0, 0)
    start.setDate(start.getDate() - days + 1)
    return {
      start: start.toISOString(),
      end: end.toISOString(),
      label: `近 ${days} 天`,
      ranged: true,
    }
  }
  if (timeFilter.value === 'custom') {
    const start = localDateBoundary(customDateFrom.value)
    const end = localDateBoundary(customDateTo.value, true)
    return {
      start,
      end,
      label: customDateFrom.value || customDateTo.value ? '自定义时间' : '自定义',
      ranged: Boolean(start || end),
    }
  }
  return { start: undefined, end: undefined, label: '全部时间', ranged: false }
})
const summaryScope = computed(() => jobSummary.value?.all_statuses || jobSummary.value)
const runningCount = computed(() => Number(summaryScope.value.running ?? jobs.value.filter((j) => j.status === 'running').length))
const successCount = computed(() => Number(summaryScope.value.success ?? jobs.value.filter((j) => ['success', 'completed'].includes(j.status)).length))
const activeCount = computed(() => Number(summaryScope.value.active ?? runningCount.value + Number(summaryScope.value.queued ?? 0)))
const scopedJobTotal = computed(() => Number(jobSummary.value?.total_all_statuses ?? jobTotal.value))
const jobTotalText = computed(() => `${timeRange.value.label}${timeRange.value.ranged ? '已建' : '任务'} ${scopedJobTotal.value} 条`)
const trackableSiteCount = computed(() => Number(siteScope.value?.trackable ?? siteScope.value?.total ?? 0))
const totalPages = computed(() => Math.max(1, Math.ceil(jobTotal.value / Number(pageSize.value || 20))))
const pageStart = computed(() => jobs.value.length ? (page.value - 1) * Number(pageSize.value || 20) + 1 : 0)
const pageEnd = computed(() => jobs.value.length ? pageStart.value + jobs.value.length - 1 : 0)
const failedTotalPages = computed(() => Math.max(1, Math.ceil(failedTotal.value / Number(failedPageSize.value || 50))))
const failedFailureItems = computed(() => {
  const counts = failedSummary.value?.by_failure || {}
  return [
    { label: '全部失败码', value: '' },
    ...Object.entries(counts).map(([code, count]) => ({
      label: `${code} · ${count}`,
      value: code,
    })),
  ]
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const range = timeRange.value
    const jobData = await listJobs({
      limit: pageSize.value,
      page: page.value,
      status: statusFilter.value === 'all' ? undefined : statusFilter.value,
      all_workspaces: 1,
      created_from: range.start,
      created_to: range.end,
      include_live_progress: statusFilter.value === 'running',
    })
    jobs.value = asList(jobData, ['jobs', 'items'])
    jobTotal.value = Number(jobData?.total ?? jobs.value.length)
    page.value = Math.min(Math.max(1, Number(jobData?.page ?? page.value)), totalPages.value)
    jobSummary.value = {
      ...(jobData?.summary || {}),
      all_statuses: jobData?.summary_all_statuses || jobData?.summary || {},
      total_all_statuses: jobData?.total_all_statuses,
    }
    siteScope.value = jobData?.site_scope || {}
    crawlDiagnostics({
      limit: 8,
      created_from: range.start,
      created_to: range.end,
    })
      .then((data) => {
        diagnostics.value = data
      })
      .catch(() => {
        diagnostics.value = null
      })
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function canRetry(job: Record<string, any>) {
  return job.retryable === true
}

async function retry(job: Record<string, any>) {
  if (!canRetry(job) || retryingId.value) return
  retryingId.value = job.id
  error.value = ''
  try {
    const res = await retryJob(job.id)
    toast.success(`已重新入队 #${res?.job_id ?? '-'}`)
    await load()
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err))
  } finally {
    retryingId.value = null
  }
}

function canOpenFailedProducts(job: Record<string, any>) {
  return Boolean(job.site)
}

async function openFailedProducts(job: Record<string, any>) {
  failedJob.value = job
  failedDrawerOpen.value = true
  failedPage.value = 1
  failedFailureCode.value = ''
  selectedFailedUrls.value = new Set()
  await loadFailedProducts()
}

async function loadFailedProducts() {
  const job = failedJob.value
  if (!job?.site) return
  failedLoading.value = true
  error.value = ''
  try {
    const data = await listFailedProducts({
      site: job.site,
      job_id: job.id,
      failure_code: failedFailureCode.value || undefined,
      page: failedPage.value,
      page_size: failedPageSize.value,
    })
    failedRows.value = asList(data, ['items'])
    failedTotal.value = Number(data?.total ?? failedRows.value.length)
    failedSummary.value = data?.summary || {}
    failedPage.value = Math.min(Math.max(1, Number(data?.page ?? failedPage.value)), failedTotalPages.value)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    failedLoading.value = false
  }
}

function closeFailedDrawer() {
  failedDrawerOpen.value = false
  failedJob.value = null
  failedRows.value = []
  selectedFailedUrls.value = new Set()
}

function toggleFailedUrl(url?: string, checked?: boolean) {
  if (!url) return
  const next = new Set(selectedFailedUrls.value)
  if (checked) next.add(url)
  else next.delete(url)
  selectedFailedUrls.value = next
}

function toggleFailedUrlFromEvent(url: string | undefined, event: Event) {
  toggleFailedUrl(url, Boolean((event.target as HTMLInputElement | null)?.checked))
}

function selectAllVisible(checked: boolean) {
  const next = new Set(selectedFailedUrls.value)
  for (const row of failedRows.value) {
    if (!row.url) continue
    if (checked) next.add(row.url)
    else next.delete(row.url)
  }
  selectedFailedUrls.value = next
}

function selectAllVisibleFromEvent(event: Event) {
  selectAllVisible(Boolean((event.target as HTMLInputElement | null)?.checked))
}

async function retryFailed(scope: 'selected' | 'filter') {
  const job = failedJob.value
  if (!job?.site || failedRetrying.value) return
  const urls = Array.from(selectedFailedUrls.value)
  if (scope === 'selected' && !urls.length) {
    toast.show({ title: '请先勾选要重抓的失败 URL', tone: 'warning' })
    return
  }
  failedRetrying.value = true
  error.value = ''
  try {
    const res = await retryFailedProducts({
      site: job.site,
      job_id: job.id,
      failure_code: scope === 'filter' ? failedFailureCode.value || undefined : undefined,
      urls: scope === 'selected' ? urls : undefined,
      limit: 500,
    })
    toast.success(
      `已创建失败商品重抓任务 #${res?.job_id ?? '-'}`,
      `选中 ${res?.selected_count ?? (urls.length || 0)} 条`
    )
    selectedFailedUrls.value = new Set()
    await Promise.all([loadFailedProducts(), load()])
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err))
  } finally {
    failedRetrying.value = false
  }
}

async function copyUrl(url?: string) {
  if (!url) return
  try {
    await navigator.clipboard.writeText(url)
    toast.success('URL 已复制')
  } catch {
    toast.show({ title: '复制失败', description: url, tone: 'warning', timeout: 0 })
  }
}

function resetPageAndLoad() {
  page.value = 1
  load()
}

function resetFailedPageAndLoad() {
  failedPage.value = 1
  selectedFailedUrls.value = new Set()
  loadFailedProducts()
}

function changeFailedPage(delta: number) {
  const next = Math.min(failedTotalPages.value, Math.max(1, failedPage.value + delta))
  if (next === failedPage.value) return
  failedPage.value = next
  selectedFailedUrls.value = new Set()
  loadFailedProducts()
}

function changePage(delta: number) {
  const next = Math.min(totalPages.value, Math.max(1, page.value + delta))
  if (next === page.value) return
  page.value = next
  load()
}

function setPage(next: number) {
  if (next === page.value) return
  page.value = next
  load()
}

function fmtJobTime(value?: string | null) {
  const out = fmtDate(value)
  return out === '-' ? '—' : out
}

function fmtDuration(seconds?: number | string | null) {
  const raw = Number(seconds ?? 0)
  if (!Number.isFinite(raw) || raw <= 0) return '—'
  const total = Math.round(raw)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${secs}s`
  return `${secs}s`
}

function nodeText(job: Record<string, any>) {
  return job.assigned_node || job.node || '未分配'
}

function workerText(job: Record<string, any>) {
  return job.worker || (job.status === 'pending' ? '待领取' : '—')
}

function runtimeTitle(job: Record<string, any>) {
  const bits = [
    job.started_at ? `开始：${fmtJobTime(job.started_at)}` : '',
    job.heartbeat_at ? `心跳：${fmtJobTime(job.heartbeat_at)}` : '',
    job.finished_at ? `完成：${fmtJobTime(job.finished_at)}` : '',
  ].filter(Boolean)
  return bits.join('\n')
}

function failureText(job: Record<string, any>) {
  return job.failure_code || (job.error ? 'unknown' : '—')
}

function failedProductCountText() {
  return `${failedRows.value.length}/${failedTotal.value}`
}

function failureTitle(job: Record<string, any>) {
  const bits = [job.failure_code, job.failure_stage].filter(Boolean)
  return bits.length ? bits.join(' · ') : (job.error || '—')
}

function fetchedProductCount(job: Record<string, any>) {
  return Number(
    job.products_count ??
    job.product_count ??
    job.fetched_count ??
    job.listing_count ??
    job.items_count ??
    0
  )
}

function totalProductCount(job: Record<string, any>) {
  const value = Number(
    job.total_product_count ??
    job.crawl_total_product_count ??
    job.run_product_count ??
    job.attempted_product_count ??
    0
  )
  return Number.isFinite(value) && value > 0 ? value : null
}

function productProgressText(job: Record<string, any>) {
  const fetched = fetchedProductCount(job)
  const rawTotal = totalProductCount(job)
  const total = rawTotal == null ? null : Math.max(rawTotal, fetched)
  return `${fetched}/${total ?? '-'}`
}

onMounted(load)
</script>

<template>
  <section :class="{ 'jobs-panel-embedded': embedded }">
    <div v-if="!embedded" class="lead">采集任务 · 进程状态</div>
    <div class="sub">
      {{ jobTotalText }} · 可采集标杆 {{ trackableSiteCount }} 个 ·
      {{ runningCount }} 采集中 · {{ activeCount }} 活跃 · {{ successCount }} 成功 ·
      当前 {{ pageStart }}-{{ pageEnd }} 条 · 第 {{ page }} / {{ totalPages }} 页
    </div>
    <UAlert v-if="error" color="error" variant="soft" :title="error" class="mb-4" />
    <div class="jobs-toolbar">
      <div class="jobs-filter-group">
        <label class="jobs-filter-row">
          <span>状态</span>
          <USelect v-model="statusFilter" class="jobs-select jobs-status-select" :items="statusItems" value-key="value" @update:model-value="resetPageAndLoad" />
        </label>
        <label class="jobs-filter-row">
          <span>时间</span>
          <USelect v-model="timeFilter" class="jobs-select jobs-time-select" :items="timeItems" value-key="value" @update:model-value="resetPageAndLoad" />
        </label>
        <label v-if="timeFilter === 'custom'" class="jobs-filter-row jobs-date-range">
          <span>日期</span>
          <input v-model="customDateFrom" class="jobs-date-input" type="date" @change="resetPageAndLoad" />
          <input v-model="customDateTo" class="jobs-date-input" type="date" @change="resetPageAndLoad" />
        </label>
      </div>
      <button class="btn-go jobs-refresh-btn" :class="{ 'is-loading': loading }" :disabled="loading" @click="load">
        <PathLoader v-if="loading" compact :size="34" />
        <span>{{ loading ? '刷新中' : '刷新' }}</span>
      </button>
    </div>
    <div v-if="diagnostics?.failure_counts && Object.keys(diagnostics.failure_counts).length" class="diag-strip">
      <span class="diag-title">失败分布</span>
      <span v-for="[code, count] in Object.entries(diagnostics.failure_counts).slice(0, 6)" :key="code" class="diag-pill">
        {{ code }} · {{ count }}
      </span>
    </div>
    <DataLoadingPanel v-if="!loading || jobs.length" class="jobs-list" :loading="loading" :has-data="jobs.length > 0" label="正在更新任务列表">
      <div class="job-row head">
        <div class="col-id">#</div>
        <div class="col-site">站点</div>
        <div class="col-status">状态</div>
        <div class="col-node">分配</div>
        <div class="col-worker">执行</div>
        <div class="col-products">已抓取/总量</div>
        <div class="col-started">开始</div>
        <div class="col-duration">已跑/耗时</div>
        <div class="col-failure">失败码</div>
        <div class="col-action">建议动作</div>
        <div class="col-ops">操作</div>
      </div>
      <div v-for="job in jobs" :key="job.id" class="job-row">
        <div class="col-id">{{ job.id }}</div>
        <div class="col-site">{{ job.site || job.brand }}</div>
        <div class="col-status"><StatusBadge :status="job.status" /></div>
        <div class="col-node" :title="job.assigned_by ? `由 ${job.assigned_by} 分配于 ${fmtJobTime(job.assigned_at)}` : ''">{{ nodeText(job) }}</div>
        <div class="col-worker" :title="workerText(job)">{{ workerText(job) }}</div>
        <div class="col-products" :title="job.total_product_count_source ? `本次总量来源：${job.total_product_count_source}` : '暂无本次总量数据'">{{ productProgressText(job) }}</div>
        <div class="col-started">{{ fmtJobTime(job.started_at) }}</div>
        <div class="col-duration" :title="runtimeTitle(job)">{{ fmtDuration(job.duration_sec) }}</div>
        <div :title="failureTitle(job)" class="col-failure failure-code">{{ failureText(job) }}</div>
        <div :title="job.failure_detail || job.error || ''" class="col-action job-action">{{ job.suggested_action || '—' }}</div>
        <div class="col-ops">
          <button class="btn-mini" :disabled="!canOpenFailedProducts(job)" @click="openFailedProducts(job)">
            失败商品
          </button>
          <button class="btn-mini" :disabled="!canRetry(job) || retryingId === job.id" @click="retry(job)">
            {{ retryingId === job.id ? '重试中' : '重试' }}
          </button>
        </div>
      </div>
      <div v-if="!loading && !jobs.length" class="empty-state">
        <b>暂无采集任务</b>
        可以从覆盖率页面触发一个站点抓取。
      </div>
    </DataLoadingPanel>
    <div v-else class="jobs-list jobs-empty-loading">
      <PageLoading compact title="加载采集任务..." note="正在读取最近任务队列" />
    </div>
    <div v-if="jobTotal > 0" class="jobs-footer-pager">
      <div class="jobs-page-size">
        <span>每页条数</span>
        <USelect v-model="pageSize" class="jobs-select jobs-page-size-select" :items="pageSizeItems" value-key="value" @update:model-value="resetPageAndLoad" />
      </div>
      <div class="jobs-pager">
        <UPagination
          :page="page"
          :total="jobTotal"
          :items-per-page="pageSize"
          :disabled="loading || totalPages <= 1"
          size="sm"
          show-edges
          @update:page="setPage"
        />
        <span>{{ page }} / {{ totalPages }}</span>
      </div>
    </div>
    <div v-if="failedDrawerOpen" class="failed-drawer-backdrop" @click.self="closeFailedDrawer">
      <aside class="failed-drawer" aria-label="失败商品明细">
        <div class="failed-drawer-head">
          <div>
            <div class="failed-title">失败商品 URL</div>
            <div class="failed-sub">#{{ failedJob?.id }} · {{ failedJob?.site }} · {{ failedProductCountText() }}</div>
          </div>
          <button class="btn-mini" @click="closeFailedDrawer">关闭</button>
        </div>
        <div class="failed-toolbar">
          <USelect
            v-model="failedFailureCode"
            class="jobs-select failed-code-select"
            :items="failedFailureItems"
            value-key="value"
            @update:model-value="resetFailedPageAndLoad"
          />
          <button class="btn-mini" :disabled="failedRetrying || failedLoading || selectedFailedUrls.size === 0" @click="retryFailed('selected')">
            {{ failedRetrying ? '提交中' : `重抓勾选 ${selectedFailedUrls.size}` }}
          </button>
          <button class="btn-mini primary" :disabled="failedRetrying || failedLoading || failedTotal === 0" @click="retryFailed('filter')">
            重抓当前筛选
          </button>
        </div>
        <div class="failed-table">
          <div class="failed-row failed-head">
            <label class="failed-check">
              <input type="checkbox" :checked="failedRows.length > 0 && failedRows.every((row) => selectedFailedUrls.has(row.url))" @change="selectAllVisibleFromEvent" />
            </label>
            <div>URL</div>
            <div>状态</div>
            <div>失败码</div>
            <div>次数</div>
            <div>下次重试</div>
            <div>操作</div>
          </div>
          <PageLoading v-if="failedLoading" compact title="加载失败商品..." note="正在读取 URL 明细" />
          <template v-else>
            <div v-for="row in failedRows" :key="row.url" class="failed-row">
              <label class="failed-check">
                <input type="checkbox" :checked="selectedFailedUrls.has(row.url)" @change="toggleFailedUrlFromEvent(row.url, $event)" />
              </label>
              <div class="failed-url" :title="row.url">{{ row.url }}</div>
              <div><StatusBadge :status="row.status" /></div>
              <div class="failure-code" :title="row.failure_detail || row.failure_code">{{ row.failure_code || '—' }}</div>
              <div>{{ row.attempts ?? 0 }}</div>
              <div>{{ fmtJobTime(row.next_retry_at) }}</div>
              <div><button class="btn-mini" @click="copyUrl(row.url)">复制</button></div>
            </div>
            <div v-if="!failedRows.length" class="empty-state failed-empty">
              <b>没有失败商品 URL</b>
              当前任务没有可展示的失败明细。
            </div>
          </template>
        </div>
        <div class="failed-footer">
          <button class="btn-mini" :disabled="failedPage <= 1 || failedLoading" @click="changeFailedPage(-1)">上一页</button>
          <span>{{ failedPage }} / {{ failedTotalPages }}</span>
          <button class="btn-mini" :disabled="failedPage >= failedTotalPages || failedLoading" @click="changeFailedPage(1)">下一页</button>
        </div>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.jobs-panel-embedded {
  display: grid;
  gap: 10px;
}
.jobs-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
  padding: 10px 12px;
  border: 1px solid var(--ui-border);
  border-radius: 10px;
  background: var(--ui-card-soft);
  flex-wrap: wrap;
}
.jobs-filter-group {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 1 1 auto;
  min-width: 0;
}
.jobs-filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 36px;
  min-width: 0;
  color: var(--ui-muted);
  font-size: 13px;
}
.jobs-filter-row span {
  flex: 0 0 auto;
}
:global(.jobs-select) {
  flex: 0 0 auto !important;
}
:global(.jobs-status-select) {
  width: 148px !important;
  min-width: 148px !important;
}
:global(.jobs-time-select) {
  width: 120px !important;
  min-width: 120px !important;
}
.jobs-date-range {
  flex-wrap: wrap;
}
.jobs-date-input {
  width: 136px;
  min-height: 34px;
  padding: 0 10px;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  background: var(--ui-card-soft);
  color: var(--ui-heading);
  font-family: inherit;
  font-size: .82rem;
  font-weight: 600;
  line-height: 1;
  outline: none;
  box-shadow: 0 1px 0 rgba(255,255,255,.74),0 8px 18px rgba(37,29,61,.06);
}
.jobs-date-input:hover {
  border-color: var(--ui-purple-line);
  background: var(--ui-card);
}
.jobs-date-input:focus {
  border-color: var(--ui-purple);
  outline: 2px solid rgba(124,58,237,.18);
  outline-offset: 2px;
}
.jobs-refresh-btn {
  margin-left: auto;
  min-height: 36px;
  min-width: 96px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 0 16px;
  line-height: 1;
  white-space: nowrap;
}
.jobs-refresh-btn.is-loading {
  background: linear-gradient(135deg, #c4b5fd, #8b5cf6);
}
.jobs-footer-pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 14px;
  flex-wrap: wrap;
  margin-top: 12px;
  padding: 0 2px;
}
.jobs-page-size {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--ui-muted);
  font-size: 13px;
}
.jobs-page-size-select {
  width: 112px !important;
  min-width: 112px !important;
}
.jobs-pager {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--ui-muted);
  font-size: 13px;
}
.jobs-pager :deep(button) {
  min-height: 34px;
}
.jobs-pager :deep(nav) {
  min-width: 0;
}
.job-row {
  display: grid;
  width: 100%;
  min-width: 0;
  grid-template-columns: 44px minmax(76px, .8fr) 72px minmax(76px, .65fr) minmax(88px, .8fr) 92px minmax(98px, .85fr) 72px minmax(76px, .7fr) minmax(120px, 1.15fr) 136px;
  gap: 8px;
  align-items: center;
  min-height: 42px;
  padding: 9px 14px;
  border-bottom: 1px solid var(--ui-border);
  color: var(--ui-text);
  font-size: .8rem;
}
.job-row.head {
  min-height: 38px;
  background: var(--ui-card-soft);
  color: var(--ui-muted);
  font-size: .62rem;
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.job-row:hover:not(.head) {
  background: #faf5ff;
}
.jobs-list {
  overflow-x: auto;
}
.jobs-empty-loading {
  padding: 14px;
  overflow: hidden;
}
.job-row > div {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.job-row .col-status,
.job-row .col-ops {
  overflow: visible;
}
.col-products,
.col-duration,
.col-node,
.col-ops {
  text-align: center;
}
.col-worker,
.col-started {
  color: var(--ui-muted);
}
.col-ops {
  justify-self: end;
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
}
.diag-strip {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 0 0 10px;
}
.diag-title {
  color: var(--ui-muted);
  font-size: 12px;
}
.diag-pill {
  border: 1px solid var(--ui-border);
  border-radius: 999px;
  padding: 4px 8px;
  background: var(--ui-card-soft);
  color: var(--ui-heading);
  font-size: 12px;
}
.failure-code,
.job-action {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.btn-mini {
  min-height: 30px;
  padding: 0 10px;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
  background: var(--ui-card);
  color: var(--ui-heading);
  font-weight: 700;
  cursor: pointer;
}
.btn-mini:hover:not(:disabled) {
  border-color: rgba(139, 92, 246, .45);
  color: var(--ui-purple-strong);
}
.btn-mini:disabled {
  cursor: not-allowed;
  opacity: .45;
}
.btn-mini.primary {
  border-color: rgba(139, 92, 246, .45);
  background: rgba(139, 92, 246, .16);
  color: var(--ui-purple-strong);
}
.failed-drawer-backdrop {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: flex;
  justify-content: flex-end;
  background: rgba(3, 7, 18, .42);
}
.failed-drawer {
  width: min(920px, 96vw);
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px;
  border-left: 1px solid var(--ui-border);
  background: var(--ui-card);
  box-shadow: -20px 0 50px rgba(0, 0, 0, .28);
}
.failed-drawer-head,
.failed-toolbar,
.failed-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.failed-title {
  font-size: 18px;
  font-weight: 850;
  color: var(--ui-heading);
}
.failed-sub {
  margin-top: 2px;
  color: var(--ui-muted);
  font-size: 12px;
}
.failed-toolbar {
  justify-content: flex-start;
  flex-wrap: wrap;
  padding: 10px 0;
  border-top: 1px solid var(--ui-border);
  border-bottom: 1px solid var(--ui-border);
}
.failed-code-select {
  width: 220px !important;
  min-width: 180px !important;
}
.failed-table {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  border: 1px solid var(--ui-border);
  border-radius: 8px;
}
.failed-row {
  display: grid;
  grid-template-columns: 34px minmax(220px, 1fr) 88px minmax(108px, .55fr) 52px 118px 64px;
  gap: 8px;
  align-items: center;
  min-height: 42px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--ui-border);
  color: var(--ui-heading);
  font-size: 13px;
}
.failed-row > div {
  min-width: 0;
}
.failed-head {
  position: sticky;
  top: 0;
  z-index: 1;
  min-height: 38px;
  background: var(--ui-card-soft);
  color: var(--ui-muted);
  font-size: 12px;
  font-weight: 800;
}
.failed-check {
  display: flex;
  align-items: center;
  justify-content: center;
}
.failed-url {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ui-purple-strong);
}
.failed-empty {
  margin: 16px;
}

:global(html[data-theme="dark"]) .jobs-date-input {
  border-color: var(--ui-border);
  background: var(--ui-card-soft);
  color: var(--ui-heading);
}
:global(html[data-theme="dark"]) .job-row.head {
  background: var(--ui-card-soft);
  color: #9f95b6;
}
:global(html[data-theme="dark"]) .job-row:hover:not(.head) {
  background: rgba(167,139,250,.05);
}

@media (max-width: 900px) {
  .job-row.head {
    display: none;
  }

  .job-row {
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas:
      "site ops"
      "status products"
      "node worker"
      "started duration"
      "failure failure"
      "action action";
    padding: 12px 14px;
    gap: 6px 10px;
  }

  .col-id,
  .job-row.head .col-duration {
    display: none;
  }

  .col-site {
    grid-area: site;
    font-weight: 800;
  }

  .col-status {
    grid-area: status;
  }

  .col-products {
    grid-area: products;
    display: block;
    color: var(--ui-muted);
    font-size: 12px;
    text-align: right;
  }

  .col-node {
    grid-area: node;
    display: block;
    color: var(--ui-muted);
    font-size: 12px;
    text-align: left;
  }

  .job-row:not(.head) .col-node::before {
    content: "分配 ";
  }

  .col-worker {
    grid-area: worker;
    display: block;
    color: var(--ui-muted);
    font-size: 12px;
    text-align: right;
  }

  .job-row:not(.head) .col-worker::before {
    content: "执行 ";
  }

  .col-started {
    grid-area: started;
    color: var(--ui-muted);
    font-size: 12px;
  }

  .job-row:not(.head) .col-started::before {
    content: "开始 ";
  }

  .col-duration {
    grid-area: duration;
    display: block;
    color: var(--ui-muted);
    font-size: 12px;
    text-align: right;
  }

  .job-row:not(.head) .col-duration::before {
    content: "已跑 ";
  }

  .job-row:not(.head) .col-products::before {
    content: "商品 ";
  }

  .col-failure {
    grid-area: failure;
  }

  .col-action {
    grid-area: action;
  }

  .col-ops {
    grid-area: ops;
  }

  .failed-row {
    grid-template-columns: 28px minmax(150px, 1fr) 76px 56px;
    grid-template-areas:
      "check url url op"
      "check status code tries";
  }

  .failed-row > :nth-child(1) { grid-area: check; }
  .failed-row > :nth-child(2) { grid-area: url; }
  .failed-row > :nth-child(3) { grid-area: status; }
  .failed-row > :nth-child(4) { grid-area: code; }
  .failed-row > :nth-child(5) { grid-area: tries; }
  .failed-row > :nth-child(6) { display: none; }
  .failed-row > :nth-child(7) { grid-area: op; }

  .jobs-footer-pager {
    justify-content: flex-start;
  }

  .jobs-toolbar {
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
  }

  .jobs-filter-group {
    flex: 1 1 0;
    width: auto;
  }

  .jobs-filter-row {
    flex: 1 1 auto;
  }

  :global(.jobs-status-select) {
    flex: 0 1 132px !important;
    width: 132px !important;
    min-width: 112px !important;
  }
}
</style>
