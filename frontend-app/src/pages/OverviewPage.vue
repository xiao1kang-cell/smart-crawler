<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { listJobs } from '../api/jobs'
import { listSites } from '../api/products'
import { listCoverage } from '../api/coverage'
import { proxyStatus } from '../api/settings'
import { asList, fmtNumber, proxyAvailable } from '../api/client'
import PageLoading from '../components/common/PageLoading.vue'
import { useJobTrigger } from '../composables/useJobTrigger'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const loading = ref(false)
const error = ref('')
const sites = ref<Record<string, any>[]>([])
const coverage = ref<Record<string, any>[]>([])
const coverageSummary = ref<Record<string, any>>({})
const jobs = ref<Record<string, any>[]>([])
const proxy = ref<Record<string, any> | null>(null)
const jobTrigger = useJobTrigger({ onDone: () => load() })

const healthySites = computed(() => Number(coverageSummary.value.healthy_count ?? coverage.value.filter((x) => normalizedPct(x) >= 50).length))
const warningSites = computed(() => Number(coverageSummary.value.warning_count ?? coverage.value.filter((x) => {
  const pct = normalizedPct(x)
  return pct >= 5 && pct < 50
}).length))
const criticalSites = computed(() => Number(coverageSummary.value.critical_count ?? coverage.value.filter((x) => normalizedPct(x) < 5).length))
const runningJobs = computed(() => jobs.value.filter((x) => ['queued', 'running'].includes(String(x.status))).length)
const successJobs = computed(() => jobs.value.filter((x) => ['success', 'completed'].includes(String(x.status))).length)
const sortedCoverage = computed(() => coverage.value.slice().sort((a, b) => normalizedPct(b) - normalizedPct(a)))
const totalSku = computed(() => Number(coverageSummary.value.total_current_sku ?? coverage.value.reduce((sum, row) => sum + currentCount(row), 0)))
const coveragePct = computed(() => Number(coverageSummary.value.overall_coverage_pct ?? fallbackCoveragePct.value))
const fallbackCoveragePct = computed(() => {
  const totalEstimated = coverage.value.reduce((sum, row) => sum + Number(row.estimated_full || 0), 0)
  return totalEstimated ? Math.round((totalSku.value / totalEstimated) * 100) : 0
})

function normalizedPct(row: Record<string, any>) {
  const raw = Number(row.coverage_pct ?? row.coverage ?? 0)
  const pct = Math.min(100, raw <= 1 ? raw * 100 : raw)
  return Math.round(pct * 100) / 100
}

function width(row: Record<string, any>) {
  return `${normalizedPct(row)}%`
}

function coverageLabel(row: Record<string, any>) {
  return `${normalizedPct(row)}%`
}

function currentCount(row: Record<string, any>) {
  return Number(row.current ?? row.sku_count ?? row.products ?? row.count ?? 0)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [siteData, coverageData, jobData, proxyData] = await Promise.all([
      listSites(),
      listCoverage(),
      listJobs({ limit: 60 }),
      proxyStatus().catch(() => null)
    ])
    sites.value = asList(siteData, ['sites'])
    coverage.value = asList(coverageData, ['sites', 'items', 'coverage'])
    coverageSummary.value = coverageData?.summary || {}
    jobs.value = asList(jobData, ['jobs', 'items'])
    proxy.value = proxyData
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function trigger(site?: string) {
  await jobTrigger.trigger(site)
}

function siteKey(row: Record<string, any>) {
  return String(row.site || row.name || '')
}

function hasReportData(row: Record<string, any>) {
  return currentCount(row) > 0
}

function reportHref(row: Record<string, any>) {
  const params = new URLSearchParams({ site: siteKey(row) })
  if (auth.workspaceId) params.set('workspace_id', auth.workspaceId)
  return `/report?${params.toString()}`
}

onMounted(load)
</script>

<template>
  <section class="overview-page">
    <div class="lead">系统总览 · 实时数据</div>
    <div class="sub">smart-crawler 控制台 · 客户视角统一看板</div>
    <UAlert v-if="error" color="error" variant="soft" :title="error" />
    <PageLoading v-if="loading && !sites.length && !coverage.length && !jobs.length" title="加载系统总览..." note="正在同步站点、覆盖率、任务和代理池" />

    <div v-else class="stats-hero">
      <div class="stat"><div class="lbl">商品总数</div><div class="val">{{ fmtNumber(totalSku) }}</div><div class="delta">{{ coveragePct }}% 覆盖</div></div>
      <div class="stat"><div class="lbl">健康站点</div><div class="val">{{ healthySites }} / {{ sites.length }}</div><div class="delta">≥ 50% 覆盖</div></div>
      <div class="stat"><div class="lbl">警告</div><div class="val">{{ warningSites }}</div><div class="delta warn">5-50%</div></div>
      <div class="stat"><div class="lbl">关键缺口</div><div class="val">{{ criticalSites }}</div><div class="delta bad">&lt; 5%</div></div>
      <div class="stat"><div class="lbl">采集进程</div><div class="val">{{ runningJobs }}</div><div class="delta">{{ successJobs }} 成功</div></div>
      <div class="stat"><div class="lbl">代理池</div><div class="val">{{ proxyAvailable(proxy) }}/{{ proxy?.total || 0 }}</div><div class="delta">健康</div></div>
    </div>

    <RouterLink v-if="!loading || coverage.length" class="entry-row" to="/app/reports">
      <div class="entry-card">
        <div class="entry-icon">📊</div>
        <div class="entry-meta">
          <div class="entry-title">站点报表</div>
          <div class="entry-sub">交互式网页报表 · 可自定义模块、列和时间范围 · 点站点打开</div>
        </div>
        <div class="entry-arrow">→</div>
      </div>
    </RouterLink>

    <div v-if="!loading || coverage.length" class="overview-section">
      <h3 class="section-title">站点覆盖率前 20</h3>
      <div class="cov-grid">
        <div v-for="row in sortedCoverage.slice(0, 20)" :key="row.site || row.name" class="cov-tile" :class="row.status">
          <h6>{{ row.site || row.name }}</h6>
          <div class="country">{{ row.brand || '—' }} · {{ row.country || '—' }}</div>
          <div class="num">{{ fmtNumber(currentCount(row)) }}</div>
          <div class="pct">{{ coverageLabel(row) }} · 满 {{ fmtNumber(row.estimated_full || 0) }}</div>
          <div class="bar"><div :style="{ width: width(row) }" /></div>
          <a v-if="hasReportData(row)" class="cov-action cov-report-link" :href="reportHref(row)" target="_blank" rel="noopener">查看报告</a>
          <button v-else class="cov-action" :class="jobTrigger.classFor(siteKey(row))" :disabled="jobTrigger.isBusy(siteKey(row))" @click="trigger(siteKey(row))">{{ jobTrigger.labelFor(siteKey(row), '触发抓取') }}</button>
          <div v-if="jobTrigger.detailFor(siteKey(row))" class="trigger-note" :class="jobTrigger.classFor(siteKey(row))">{{ jobTrigger.detailFor(siteKey(row)) }}</div>
        </div>
      </div>
      <div v-if="!loading && !coverage.length" class="empty-state">
        <b>暂无覆盖率数据</b>
        可以先在设置里加入站点，或从覆盖率页面触发抓取。
      </div>
    </div>
  </section>
</template>
