<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { dataQuality, listCoverage } from '../api/coverage'
import { asList, fmtDate, fmtNumber } from '../api/client'
import DataLoadingPanel from '../components/common/DataLoadingPanel.vue'
import PageLoading from '../components/common/PageLoading.vue'
import { useJobTrigger } from '../composables/useJobTrigger'

const rows = ref<Record<string, any>[]>([])
const summary = ref<Record<string, any>>({})
const quality = ref<Record<string, any>[]>([])
const qualitySummary = ref<Record<string, any>>({})
const loading = ref(false)
const error = ref('')
const jobTrigger = useJobTrigger({ onDone: () => load() })
const batchBusy = ref(false)
const batchMessage = ref('')

const rerunnableIssueSet = new Set([
  'no_products',
  'coverage_low',
  'sku_deviation_high',
  'title_weak',
  'price_missing',
  'promotions_missing',
  'latest_job_failed',
  'never_crawled',
  'empty_sitemap',
  'proxy_unavailable',
  'proxy_auth_failed',
  'anti_bot_blocked',
])

const sortedRows = computed(() => rows.value.slice().sort((a, b) => normalizedPct(b) - normalizedPct(a)))
const totalSku = computed(() => Number(summary.value.total_current_sku ?? rows.value.reduce((sum, row) => sum + currentCount(row), 0)))
const totalEstimated = computed(() => Number(summary.value.total_estimated_full ?? rows.value.reduce((sum, row) => sum + Number(row.estimated_full || 0), 0)))
const coveragePct = computed(() => {
  if (summary.value.overall_coverage_pct != null) return Number(summary.value.overall_coverage_pct)
  if (!totalEstimated.value) return 0
  return Math.round((totalSku.value / totalEstimated.value) * 100)
})
const healthySites = computed(() => Number(summary.value.healthy_count ?? rows.value.filter((row) => normalizedPct(row) >= 90).length))
const warningSites = computed(() => rows.value.filter((row) => {
  if (summary.value.warning_count != null) return false
  const pct = normalizedPct(row)
  return pct >= 50 && pct < 90
}).length)
const warningCount = computed(() => Number(summary.value.warning_count ?? warningSites.value))
const criticalSites = computed(() => Number(summary.value.critical_count ?? rows.value.filter((row) => normalizedPct(row) < 50).length))
const highDeviationCount = computed(() => Number(summary.value.high_deviation_count ?? rows.value.filter((row) => Math.abs(Number(row.sku_deviation_pct || 0)) > 50).length))
const qualityBySite = computed(() => new Map(quality.value.map((row) => [String(row.site), row])))
const rerunCandidateRows = computed(() => sortedRows.value.filter((row) => isRerunCandidate(row)))
const rerunCandidateSites = computed(() => Array.from(new Set(rerunCandidateRows.value.map(siteKey).filter(Boolean))))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [data, qualityData] = await Promise.all([listCoverage(), dataQuality()])
    rows.value = asList(data, ['sites', 'items', 'coverage'])
    summary.value = data?.summary || {}
    quality.value = asList(qualityData, ['items'])
    qualitySummary.value = qualityData?.summary || {}
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function trigger(site: string) {
  await jobTrigger.trigger(site)
}

async function triggerRiskSites() {
  const sites = rerunCandidateSites.value
  if (!sites.length || batchBusy.value) return
  batchBusy.value = true
  batchMessage.value = `准备提交 ${sites.length} 个风险站点`
  let submitted = 0
  try {
    for (const site of sites) {
      if (!site || jobTrigger.isBusy(site)) continue
      submitted += 1
      batchMessage.value = `正在提交 ${submitted}/${sites.length}: ${site}`
      await jobTrigger.trigger(site)
    }
    batchMessage.value = submitted
      ? `已提交 ${submitted} 个站点，页面会持续同步队列状态`
      : '风险站点已有任务在运行或排队'
  } catch (err) {
    batchMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    batchBusy.value = false
  }
}

function siteKey(row: Record<string, any>) {
  return String(row.site || row.name || '')
}

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

function qualityFor(row: Record<string, any>) {
  return qualityBySite.value.get(siteKey(row)) || {}
}

function issuesFor(row: Record<string, any>) {
  const q = qualityFor(row)
  return Array.isArray(q.issues) ? q.issues.map(String) : []
}

function isRerunCandidate(row: Record<string, any>) {
  const pct = normalizedPct(row)
  const deviation = Math.abs(Number(row.sku_deviation_pct || 0))
  const issues = issuesFor(row)
  return pct < 50
    || deviation > 50
    || issues.some((issue) => rerunnableIssueSet.has(issue))
}

function issueLabel(issue: string) {
  return ({
    no_products: '无商品',
    coverage_low: '覆盖低',
    sku_deviation_high: 'SKU偏差高',
    title_weak: '标题弱',
    price_missing: '价格缺失',
    sales_missing: '销量缺失',
    revenue_missing: '收入缺失',
    traffic_missing: '流量缺失',
    conversion_missing: '转化缺失',
    promotions_missing: '促销缺失',
    latest_job_failed: '任务失败',
    job_in_progress: '运行中',
    never_crawled: '未采集',
  } as Record<string, string>)[issue] || issue
}

onMounted(load)
</script>

<template>
  <section>
    <div class="lead">数据覆盖率 · 全 {{ rows.length }} 站</div>
    <div class="sub">商品总数 {{ fmtNumber(totalSku) }} / 预计 {{ fmtNumber(totalEstimated) }} = {{ coveragePct }}%</div>
    <UAlert v-if="error" color="error" variant="soft" :title="error" class="mb-4" />

    <PageLoading v-if="loading && !rows.length" title="加载覆盖率数据..." note="正在读取当前工作区的站点覆盖率" />

    <div v-else class="stats-hero">
      <div class="stat"><div class="lbl">健康</div><div class="val">{{ healthySites }}</div><div class="delta">≥ 90%</div></div>
      <div class="stat"><div class="lbl">警告</div><div class="val">{{ warningCount }}</div><div class="delta warn">50-90%</div></div>
      <div class="stat"><div class="lbl">关键</div><div class="val">{{ criticalSites }}</div><div class="delta bad">&lt; 50%</div></div>
      <div class="stat"><div class="lbl">SKU偏差</div><div class="val">{{ highDeviationCount }}</div><div class="delta bad">&gt; 50%</div></div>
      <div class="stat"><div class="lbl">需重跑</div><div class="val">{{ qualitySummary.needs_rerun || 0 }}</div><div class="delta bad">任务/覆盖风险</div></div>
      <div class="stat"><div class="lbl">缺销量</div><div class="val">{{ qualitySummary.missing_sales || 0 }}</div><div class="delta warn">需销量估算</div></div>
      <div class="stat"><div class="lbl">缺促销</div><div class="val">{{ qualitySummary.missing_promotions || 0 }}</div><div class="delta warn">需促销采集</div></div>
    </div>

    <div v-if="!loading || rows.length" class="coverage-actions">
      <div>
        <b>风险站点 {{ rerunCandidateSites.length }}</b>
        <span>包含覆盖低、SKU 偏差高、缺商品/价格/促销、最近任务失败或反爬代理类问题。</span>
      </div>
      <button class="batch-rerun" :disabled="loading || batchBusy || !rerunCandidateSites.length" @click="triggerRiskSites">
        {{ batchBusy ? '提交中...' : `批量重跑风险站点(${rerunCandidateSites.length})` }}
      </button>
    </div>
    <div v-if="batchMessage" class="batch-note">{{ batchMessage }}</div>

    <DataLoadingPanel v-if="!loading || rows.length" class="cov-grid" :loading="loading" :has-data="rows.length > 0" label="正在更新覆盖率">
      <div v-for="row in sortedRows" :key="row.site || row.name" class="cov-tile" :class="row.status">
        <h6>{{ row.site || row.name }}</h6>
        <div class="country">{{ row.brand || '—' }} · {{ row.country || '—' }}</div>
        <div class="num">{{ fmtNumber(currentCount(row)) }}</div>
        <div class="pct">{{ coverageLabel(row) }} · 满 {{ fmtNumber(row.estimated_full || 0) }}</div>
        <div v-if="row.target_sku_count" class="target-line">
          目标 SKU {{ fmtNumber(row.target_sku_count) }} · 偏差 {{ row.sku_deviation_pct }}%
          <span v-if="row.target_sku_source === 'acceptance'">验收口径</span>
          <span v-else-if="row.target_sku_source === 'workspace'">工作区配置</span>
        </div>
        <div class="bar"><div :style="{ width: width(row) }" /></div>
        <div class="quality-metrics">
          <span>SKU {{ fmtNumber(qualityFor(row).sku_count ?? row.current ?? 0) }}</span>
          <span>SPU {{ fmtNumber(qualityFor(row).spu_count ?? 0) }}</span>
          <span>促销 {{ fmtNumber(qualityFor(row).promotion_count ?? 0) }}</span>
          <span>销量 {{ qualityFor(row).sales_signal_pct ?? 0 }}%</span>
        </div>
        <div v-if="qualityFor(row).issues?.length" class="issue-list">
          <span v-for="issue in qualityFor(row).issues" :key="issue">{{ issueLabel(issue) }}</span>
        </div>
        <div v-else class="issue-list ok"><span>质量正常</span></div>
        <div class="quality-foot">
          <span>{{ qualityFor(row).latest_job ? `任务 #${qualityFor(row).latest_job.id} ${qualityFor(row).latest_job.status}` : '暂无任务' }}</span>
          <span>{{ fmtDate(qualityFor(row).latest_job?.finished_at || qualityFor(row).last_product_updated || qualityFor(row).last_crawled) }}</span>
        </div>
        <div class="quality-action">{{ qualityFor(row).suggested_action || '—' }}</div>
        <button :class="jobTrigger.classFor(siteKey(row))" :disabled="loading || jobTrigger.isBusy(siteKey(row))" @click="trigger(siteKey(row))">{{ jobTrigger.labelFor(siteKey(row), '触发抓取') }}</button>
        <div v-if="jobTrigger.detailFor(siteKey(row))" class="trigger-note" :class="jobTrigger.classFor(siteKey(row))">{{ jobTrigger.detailFor(siteKey(row)) }}</div>
      </div>
    </DataLoadingPanel>
    <div v-if="!loading && !rows.length" class="empty-state">
      <b>当前工作区还没有覆盖率数据</b>
      请先在设置里加入站点，或切换到已有站点的工作区。
    </div>
  </section>
</template>

<style scoped>
.quality-metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 5px 8px;
  margin: 9px 0 8px;
  font-size: .68rem;
  color: var(--ui-muted);
}
.quality-metrics span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.target-line {
  margin-top: 4px;
  color: var(--ui-amber, #b45309);
  font-size: .72rem;
  font-weight: 700;
}
.issue-list {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  min-height: 24px;
  margin-bottom: 8px;
}
.issue-list span {
  display: inline-flex;
  align-items: center;
  min-height: 21px;
  padding: 2px 7px;
  border-radius: 999px;
  border: 1px solid rgba(248, 113, 113, .28);
  background: rgba(248, 113, 113, .12);
  color: #be123c;
  font-size: .64rem;
  font-weight: 700;
}
.issue-list.ok span {
  border-color: rgba(16, 185, 129, .28);
  background: rgba(16, 185, 129, .12);
  color: #047857;
}
.quality-foot {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  color: var(--ui-muted);
  font-size: .66rem;
  margin-bottom: 6px;
}
.quality-foot span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.quality-action {
  min-height: 30px;
  margin-bottom: 8px;
  color: var(--ui-text);
  font-size: .7rem;
  line-height: 1.35;
}
.coverage-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: -4px 0 14px;
  padding: 11px 12px;
  border: 1px solid var(--ui-border);
  border-radius: 10px;
  background: var(--ui-card);
}
.coverage-actions b {
  display: block;
  color: var(--ui-heading);
  font-size: .84rem;
}
.coverage-actions span {
  display: block;
  margin-top: 3px;
  color: var(--ui-muted);
  font-size: .72rem;
  line-height: 1.4;
}
.batch-rerun {
  flex: 0 0 auto;
  min-height: 34px;
  padding: 0 13px;
  border-radius: 8px;
  border: 1px solid rgba(167, 139, 250, .34);
  background: var(--ui-purple-soft);
  color: var(--ui-purple-strong);
  font-size: .74rem;
  font-weight: 800;
  cursor: pointer;
}
.batch-rerun:disabled {
  opacity: .58;
  cursor: not-allowed;
}
.batch-note {
  margin: -5px 0 10px;
  padding: 7px 9px;
  border-radius: 8px;
  border: 1px solid rgba(167, 139, 250, .24);
  background: var(--ui-purple-soft);
  color: var(--ui-purple-strong);
  font-size: .72rem;
}
@media (max-width: 780px) {
  .coverage-actions {
    align-items: stretch;
    flex-direction: column;
  }
  .batch-rerun {
    width: 100%;
  }
}
</style>
