<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { asList, fmtDate } from '../../api/client'
import { crawlDiagnostics, listJobs, retryJob } from '../../api/jobs'
import DataLoadingPanel from '../common/DataLoadingPanel.vue'
import PageLoading from '../common/PageLoading.vue'
import StatusBadge from '../common/StatusBadge.vue'

defineProps<{
  embedded?: boolean
}>()

const jobs = ref<Record<string, any>[]>([])
const diagnostics = ref<Record<string, any> | null>(null)
const jobSummary = ref<Record<string, any>>({})
const jobTotal = ref(0)
const error = ref('')
const actionMsg = ref('')
const loading = ref(false)
const retryingId = ref<number | string | null>(null)
const statusFilter = ref('all')
const pageSize = ref(20)
const page = ref(1)
const statusItems = [
  { label: '全部状态', value: 'all' },
  { label: '排队中', value: 'queued,pending' },
  { label: '采取中', value: 'running' },
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
const runningCount = computed(() => Number(jobSummary.value.running ?? jobs.value.filter((j) => j.status === 'running').length))
const successCount = computed(() => Number(jobSummary.value.success ?? jobs.value.filter((j) => ['success', 'completed'].includes(j.status)).length))
const totalPages = computed(() => Math.max(1, Math.ceil(jobTotal.value / Number(pageSize.value || 20))))
const pageStart = computed(() => jobs.value.length ? (page.value - 1) * Number(pageSize.value || 20) + 1 : 0)
const pageEnd = computed(() => jobs.value.length ? pageStart.value + jobs.value.length - 1 : 0)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const jobData = await listJobs({
      limit: pageSize.value,
      page: page.value,
      status: statusFilter.value === 'all' ? undefined : statusFilter.value,
    })
    jobs.value = asList(jobData, ['jobs', 'items'])
    jobTotal.value = Number(jobData?.total ?? jobs.value.length)
    page.value = Math.min(Math.max(1, Number(jobData?.page ?? page.value)), totalPages.value)
    jobSummary.value = jobData?.summary || {}
    diagnostics.value = await crawlDiagnostics({ limit: 8 }).catch(() => null)
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
  actionMsg.value = ''
  try {
    const res = await retryJob(job.id)
    actionMsg.value = `已重新入队 #${res?.job_id ?? '-'}`
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    retryingId.value = null
  }
}

function resetPageAndLoad() {
  page.value = 1
  load()
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

function failureText(job: Record<string, any>) {
  return job.failure_code || (job.error ? 'unknown' : '—')
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
  const total = totalProductCount(job)
  return `${fetched}/${total ?? '-'}`
}

onMounted(load)
</script>

<template>
  <section :class="{ 'jobs-panel-embedded': embedded }">
    <div v-if="!embedded" class="lead">采集任务 · 进程状态</div>
    <div class="sub">
      {{ runningCount }} 采取中 · {{ successCount }} 成功 · 共 {{ jobTotal }} 条 ·
      当前 {{ pageStart }}-{{ pageEnd }} 条 · 第 {{ page }} / {{ totalPages }} 页
    </div>
    <UAlert v-if="error" color="error" variant="soft" :title="error" class="mb-4" />
    <UAlert v-if="actionMsg" color="success" variant="soft" :title="actionMsg" class="mb-4" />
    <div class="jobs-toolbar">
      <div class="jobs-filter-group">
        <label class="jobs-filter-row">
          <span>状态</span>
          <USelect v-model="statusFilter" class="jobs-select jobs-status-select" :items="statusItems" value-key="value" @update:model-value="resetPageAndLoad" />
        </label>
      </div>
      <button class="btn-go jobs-refresh-btn" :disabled="loading" @click="load">{{ loading ? '刷新中...' : '刷新' }}</button>
    </div>
    <div v-if="diagnostics?.failure_counts && Object.keys(diagnostics.failure_counts).length" class="diag-strip">
      <span class="diag-title">失败分布</span>
      <span v-for="[code, count] in Object.entries(diagnostics.failure_counts).slice(0, 6)" :key="code" class="diag-pill">
        {{ code }} · {{ count }}
      </span>
    </div>
    <DataLoadingPanel class="jobs-list" :loading="loading" :has-data="jobs.length > 0" label="正在更新任务列表">
      <div class="job-row head">
        <div class="col-id">#</div>
        <div class="col-site">站点</div>
        <div class="col-status">状态</div>
        <div class="col-products">已抓取/总量</div>
        <div class="col-duration">耗时</div>
        <div class="col-failure">失败码</div>
        <div class="col-action">建议动作</div>
        <div class="col-finished">完成</div>
        <div class="col-ops">操作</div>
      </div>
      <PageLoading v-if="loading && !jobs.length" compact title="加载采集任务..." note="正在读取最近任务队列" />
      <template v-else>
        <div v-for="job in jobs" :key="job.id" class="job-row">
          <div class="col-id">{{ job.id }}</div>
          <div class="col-site">{{ job.site || job.brand }}</div>
          <div class="col-status"><StatusBadge :status="job.status" /></div>
          <div class="col-products" :title="job.total_product_count_source ? `本次总量来源：${job.total_product_count_source}` : '暂无本次总量数据'">{{ productProgressText(job) }}</div>
          <div class="col-duration">{{ job.duration_sec ? Math.round(job.duration_sec) + ' 秒' : '—' }}</div>
          <div :title="failureTitle(job)" class="col-failure failure-code">{{ failureText(job) }}</div>
          <div :title="job.failure_detail || job.error || ''" class="col-action job-action">{{ job.suggested_action || '—' }}</div>
          <div class="col-finished">{{ fmtJobTime(job.finished_at) }}</div>
          <div class="col-ops">
            <button class="btn-mini" :disabled="!canRetry(job) || retryingId === job.id" @click="retry(job)">
              {{ retryingId === job.id ? '重试中' : '重试' }}
            </button>
          </div>
        </div>
      </template>
      <div v-if="!loading && !jobs.length" class="empty-state">
        <b>暂无采集任务</b>
        可以从覆盖率页面触发一个站点抓取。
      </div>
    </DataLoadingPanel>
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
.jobs-select {
  width: 112px;
  min-height: 34px;
  flex: 0 0 auto;
}
:global(.jobs-status-select) {
  width: 148px !important;
  min-width: 148px !important;
}
.jobs-refresh-btn {
  margin-left: auto;
  min-height: 36px;
  padding: 0 18px;
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
  width: 100%;
  min-width: 0;
  grid-template-columns: 44px minmax(76px, .8fr) 72px 92px 62px minmax(78px, .75fr) minmax(132px, 1.35fr) minmax(98px, .85fr) 58px;
  gap: 8px;
}
.jobs-list {
  overflow-x: hidden;
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
.col-ops {
  text-align: center;
}
.col-ops {
  justify-self: end;
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

@media (max-width: 900px) {
  .job-row.head {
    display: none;
  }

  .job-row {
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas:
      "site ops"
      "status products"
      "failure failure"
      "action action"
      "finished finished";
    padding: 12px 14px;
    gap: 6px 10px;
  }

  .col-id,
  .col-duration {
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

  .job-row:not(.head) .col-products::before {
    content: "商品 ";
  }

  .col-failure {
    grid-area: failure;
  }

  .col-action {
    grid-area: action;
  }

  .col-finished {
    grid-area: finished;
    color: var(--ui-muted);
    font-size: 12px;
  }

  .col-ops {
    grid-area: ops;
  }

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
