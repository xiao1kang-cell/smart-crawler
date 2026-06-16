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

const sortedRows = computed(() => rows.value.slice().sort((a, b) => normalizedPct(b) - normalizedPct(a)))
const totalSku = computed(() => Number(summary.value.total_current_sku ?? rows.value.reduce((sum, row) => sum + Number(row.current || row.sku_count || row.products || row.count || 0), 0)))
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
const qualityBySite = computed(() => new Map(quality.value.map((row) => [String(row.site), row])))

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

function siteKey(row: Record<string, any>) {
  return String(row.site || row.name || '')
}

function normalizedPct(row: Record<string, any>) {
  const raw = Number(row.coverage_pct || row.coverage || 0)
  return Math.min(100, raw <= 1 ? raw * 100 : raw)
}

function width(row: Record<string, any>) {
  return `${normalizedPct(row)}%`
}

function qualityFor(row: Record<string, any>) {
  return qualityBySite.value.get(siteKey(row)) || {}
}

function issueLabel(issue: string) {
  return ({
    no_products: '无商品',
    coverage_low: '覆盖低',
    sales_missing: '销量缺失',
    revenue_missing: '收入缺失',
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
      <div class="stat"><div class="lbl">需重跑</div><div class="val">{{ qualitySummary.needs_rerun || 0 }}</div><div class="delta bad">任务/覆盖风险</div></div>
      <div class="stat"><div class="lbl">缺销量</div><div class="val">{{ qualitySummary.missing_sales || 0 }}</div><div class="delta warn">需销量估算</div></div>
      <div class="stat"><div class="lbl">缺促销</div><div class="val">{{ qualitySummary.missing_promotions || 0 }}</div><div class="delta warn">需促销采集</div></div>
    </div>

    <DataLoadingPanel v-if="!loading || rows.length" class="cov-grid" :loading="loading" :has-data="rows.length > 0" label="正在更新覆盖率">
      <div v-for="row in sortedRows" :key="row.site || row.name" class="cov-tile" :class="row.status">
        <h6>{{ row.site || row.name }}</h6>
        <div class="country">{{ row.brand || '—' }} · {{ row.country || '—' }}</div>
        <div class="num">{{ fmtNumber(row.current || row.sku_count || row.products || row.count) }}</div>
        <div class="pct">{{ row.coverage_pct ?? row.coverage ?? '—' }}% · 满 {{ fmtNumber(row.estimated_full || 0) }}</div>
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
</style>
