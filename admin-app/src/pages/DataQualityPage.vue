<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { analyticsRecompute, aosenAcceptanceActionPlan, aosenFieldQualityAcceptance, crawlEnqueue, dataQuality, dataQualityProducts, productFieldFixesImport, productFieldFixesTemplate, productFieldFixesValidate, promotionSignalsImport, promotionSignalsTemplate, promotionSignalsValidate, promotionsRebuild, proxyAntiBotApplyRules, reviewHistoryImport, reviewHistoryTemplate, reviewHistoryValidate, salesSignalsImport, salesSignalsTemplate, salesSignalsValidate, siteCrawlerConfig, siteCrawlerConfigTestPriceSource, siteCrawlerConfigUpdate, skuTargetsImport, skuTargetsTemplate, skuTargetsValidate, tenants, thirdPartyMetricsImport, thirdPartyMetricsTemplate, thirdPartyMetricsValidate } from '../api/admin'
import { fmtDate, fmtNumber } from '../api/client'
import StatCard from '../components/common/StatCard.vue'
import StatusBadge from '../components/common/StatusBadge.vue'

const rows = ref<Record<string, any>[]>([])
const summary = ref<Record<string, any>>({})
type PreconditionRow = { issue: string; count: number; sites: string[] }
const loading = ref(false)
const error = ref('')
const tenantRows = ref<Record<string, any>[]>([])
const tenantId = ref('')
const includeHidden = ref(false)
const rerunBusy = ref<Record<string, boolean>>({})
const rerunMessage = ref<Record<string, string>>({})
const promoBusy = ref<Record<string, boolean>>({})
const promoMessage = ref<Record<string, string>>({})
const promoImportOpen = ref(false)
const promoImportText = ref('site,sku,promotion_type,promotion_name,discount_percent,promotion_price,threshold,start_time,end_time\nhomary_us,SKU123,coupon,Save 10% with code HOME10,10,,,2026-06-28,')
const promoImportBusy = ref(false)
const promoImportMessage = ref('')
const promoTemplateBusy = ref(false)
const promoValidateBusy = ref(false)
const promoValidation = ref<Record<string, any> | null>(null)
const fieldFixImportOpen = ref(false)
const fieldFixImportText = ref('site,sku,title,currency,category_path,image_urls,sale_price,original_price,spu,note\nhomary_us,SKU123,Oak storage cabinet,USD,Living Room > Storage,https://example.com/image.jpg,199.99,249.99,SPU123,')
const fieldFixImportBusy = ref(false)
const fieldFixImportMessage = ref('')
const fieldFixTemplateBusy = ref(false)
const fieldFixValidateBusy = ref(false)
const fieldFixValidation = ref<Record<string, any> | null>(null)
const skuTargetImportOpen = ref(false)
const skuTargetImportText = ref('site,workspace_id,target_sku_count,note\nhomary_us,1,1200,client accepted target')
const skuTargetImportBusy = ref(false)
const skuTargetImportMessage = ref('')
const skuTargetTemplateBusy = ref(false)
const skuTargetValidateBusy = ref(false)
const skuTargetValidation = ref<Record<string, any> | null>(null)
const analyticsBusy = ref<Record<string, boolean>>({})
const analyticsMessage = ref<Record<string, string>>({})
const proxyRuleBusy = ref<Record<string, boolean>>({})
const proxyRuleMessage = ref<Record<string, string>>({})
const metricsImportOpen = ref(false)
const metricsImportText = ref('site,date,traffic,conversion_rate\nsongmics_us,2026-06-17,123456,2.5')
const metricsImportBusy = ref(false)
const metricsImportMessage = ref('')
const metricsTemplateBusy = ref(false)
const metricsValidateBusy = ref(false)
const metricsValidation = ref<Record<string, any> | null>(null)
const salesImportOpen = ref(false)
const salesImportText = ref('site,sku,date,thirty_day_sales,thirty_day_revenue\nhomary_us,SKU123,2026-06-28,12,1299.99')
const salesImportBusy = ref(false)
const salesImportMessage = ref('')
const salesTemplateBusy = ref(false)
const salesValidateBusy = ref(false)
const salesValidation = ref<Record<string, any> | null>(null)
const reviewHistoryImportOpen = ref(false)
const reviewHistoryImportText = ref('site,sku,date,review_count,sale_price,original_price\nhomary_us,SKU123,2026-06-01,120,199.99,249.99')
const reviewHistoryImportBusy = ref(false)
const reviewHistoryImportMessage = ref('')
const reviewHistoryTemplateBusy = ref(false)
const reviewHistoryValidateBusy = ref(false)
const reviewHistoryValidation = ref<Record<string, any> | null>(null)
const aosenAcceptance = ref<Record<string, any>>({})
const aosenActionPlan = ref<Record<string, any>>({})
const aosenAcceptanceBusy = ref(false)
const aosenAcceptanceError = ref('')
const configOpen = ref(false)
const configSite = ref('')
const configBusy = ref(false)
const configError = ref('')
const configMessage = ref('')
const configKeys = ref<string[]>([])
const configTestBusy = ref(false)
const configTestResult = ref<Record<string, any> | null>(null)
const configForm = ref<Record<string, string>>({
  proxy_tier: 'residential',
  price_source_type: 'feed',
  price_feed_url: '',
  pdp_price_api_url: '',
  pdp_price_selector: '',
  pdp_title_selector: '',
  price_source_max_items: '50',
  price_source_use_proxy: '',
  price_source_allow_stealth: '',
  price_source_timeout: '30',
  price_source_retries: '1',
  price_feed_sku_field: '',
  price_feed_sale_price_field: '',
  price_feed_original_price_field: '',
  price_feed_currency_field: '',
  price_feed_title_field: '',
  notes: '',
})
const detailSite = ref('')
const detailIssue = ref('')
const detailRows = ref<Record<string, any>[]>([])
const detailMeta = ref<Record<string, any>>({})
const detailLoading = ref(false)
const detailError = ref('')
const detailPage = ref(1)
const detailLimit = ref(50)
const qualityFilter = ref('')
const EMPTY_SELECT = '__empty__'
const detailIssues = [
  { key: '', label: '全部问题' },
  { key: 'no_products', label: '无商品' },
  { key: 'coverage_low', label: '覆盖低' },
  { key: 'sku_deviation_high', label: 'SKU偏差' },
  { key: 'title_weak', label: '弱标题' },
  { key: 'category_missing', label: '缺类目' },
  { key: 'image_missing', label: '缺图片' },
  { key: 'price_missing', label: '缺价格' },
  { key: 'pdp_price_required', label: 'PDP价格源' },
  { key: 'currency_missing', label: '缺币种' },
  { key: 'currency_mismatch', label: '币种错配' },
  { key: 'sales_missing', label: '缺销量' },
  { key: 'revenue_missing', label: '缺收入' },
  { key: 'sales_history_insufficient', label: '销量历史不足' },
  { key: 'traffic_missing', label: '缺流量' },
  { key: 'conversion_missing', label: '缺转化' },
  { key: 'promotions_missing', label: '缺促销' },
  { key: 'partial_crawl', label: '部分采集' },
  { key: 'never_crawled', label: '未采集' },
  { key: 'latest_job_failed', label: '任务失败' },
  { key: 'job_in_progress', label: '运行中' },
  { key: 'job_pending_stale', label: '排队过久' },
  { key: 'proxy_unavailable', label: '代理不可用' },
  { key: 'proxy_auth_failed', label: '代理鉴权' },
  { key: 'anti_bot_blocked', label: '反爬封禁' },
  { key: 'empty_sitemap', label: '空站点图' },
  { key: 'market_paused', label: '市场暂停' },
]
const tenantItems = computed(() => [
  { label: '全部 workspace', value: EMPTY_SELECT },
  ...tenantRows.value.map((tenant) => ({
    label: `${tenant.name} (${tenant.site_count || 0})`,
    value: String(tenant.id)
  }))
])
const tenantSelect = computed({
  get: () => tenantId.value || EMPTY_SELECT,
  set: (value: string) => {
    tenantId.value = value === EMPTY_SELECT ? '' : value
  }
})
const proxyTierItems = [
  { label: 'none', value: 'none' },
  { label: 'datacenter', value: 'datacenter' },
  { label: 'residential', value: 'residential' }
]
const priceSourceTypeItems = [
  { label: '未指定', value: EMPTY_SELECT },
  { label: 'Feed / JSON', value: 'feed' },
  { label: 'API 模板', value: 'api' },
  { label: 'PDP HTML', value: 'pdp' },
  { label: '外部登记', value: 'external' }
]
const booleanModeItems = [
  { label: '默认', value: EMPTY_SELECT },
  { label: '启用', value: 'true' },
  { label: '停用', value: 'false' }
]
const stealthModeItems = [
  { label: '停用', value: EMPTY_SELECT },
  { label: '启用', value: 'true' }
]
const priceSourceTypeSelect = computed({
  get: () => configForm.value.price_source_type || EMPTY_SELECT,
  set: (value: string) => {
    configForm.value.price_source_type = value === EMPTY_SELECT ? '' : value
  }
})
const priceSourceUseProxySelect = computed({
  get: () => configForm.value.price_source_use_proxy || EMPTY_SELECT,
  set: (value: string) => {
    configForm.value.price_source_use_proxy = value === EMPTY_SELECT ? '' : value
  }
})
const priceSourceAllowStealthSelect = computed({
  get: () => configForm.value.price_source_allow_stealth || EMPTY_SELECT,
  set: (value: string) => {
    configForm.value.price_source_allow_stealth = value === EMPTY_SELECT ? '' : value
  }
})
const sortedRows = computed(() => rows.value.slice().sort((a, b) => {
  const rank: Record<string, number> = { critical: 0, warning: 1, healthy: 2 }
  return (rank[a.status] ?? 9) - (rank[b.status] ?? 9) || String(a.site).localeCompare(String(b.site))
}))
const summaryCards = computed(() => [
  { key: '', label: '站点', value: summary.value.total_sites },
  { key: 'healthy', label: '健康', value: summary.value.healthy },
  { key: 'rerunnable', label: '需重跑', value: summary.value.needs_rerun },
  { key: 'rerun_after_setup', label: '修复后重跑', value: summary.value.rerun_after_setup },
  { key: 'external_data_required', label: '需外部数据', value: summary.value.external_data_required },
  { key: 'rerun_blocked', label: '暂不可重跑', value: summary.value.rerun_blocked },
  { key: 'no_products', label: '无商品', value: summary.value.no_products },
  { key: 'never_crawled', label: '未采集', value: summary.value.never_crawled },
  { key: 'sites_without_jobs', label: '无任务记录', value: summary.value.sites_without_jobs },
  { key: 'sku_deviation_high', label: 'SKU偏差', value: summary.value.high_deviation },
  { key: 'title_weak', label: '弱标题', value: summary.value.weak_titles },
  { key: 'category_missing', label: '缺类目', value: summary.value.missing_categories },
  { key: 'image_missing', label: '缺图片', value: summary.value.missing_images },
  { key: 'price_missing', label: '缺价格', value: summary.value.missing_prices },
  { key: 'pdp_price_required', label: 'PDP价格源', value: summary.value.pdp_price_required },
  { key: 'currency_issues', label: '币种问题', value: summary.value.currency_issues },
  { key: 'sales_missing', label: '缺销量', value: summary.value.missing_sales },
  { key: 'sales_history_insufficient', label: '历史不足', value: summary.value.insufficient_sales_history },
  { key: 'traffic_missing', label: '缺流量', value: summary.value.missing_traffic },
  { key: 'conversion_missing', label: '缺转化', value: summary.value.missing_conversion },
  { key: 'promotions_missing', label: '缺促销', value: summary.value.missing_promotions },
  { key: 'partial_crawl', label: '部分采集', value: summary.value.partial_crawls },
  { key: 'coverage_low', label: '覆盖风险', value: summary.value.coverage_risk },
  { key: 'pending_jobs', label: '待处理任务', value: summary.value.pending_jobs },
  { key: 'job_pending_stale', label: '久排任务', value: summary.value.stale_pending_jobs },
  { key: 'running_jobs', label: '运行任务', value: summary.value.running_jobs },
  { key: 'stuck_jobs', label: '卡住任务', value: summary.value.stuck_jobs },
  { key: 'failed_jobs', label: '失败任务', value: summary.value.failed_jobs },
  { key: 'blocked_jobs', label: '阻断任务', value: summary.value.blocked_jobs },
  { key: 'skipped_jobs', label: '跳过任务', value: summary.value.skipped_jobs },
])
const preconditionRows = computed(() => {
  const rowsFromApi: PreconditionRow[] = Array.isArray(summary.value.rerun_preconditions)
    ? summary.value.rerun_preconditions.map((item: Record<string, any>) => ({
      issue: String(item.issue || ''),
      count: Number(item.count || 0),
      sites: Array.isArray(item.sites) ? item.sites.map(String) : [],
    })).filter((item: PreconditionRow) => item.issue)
    : []
  const fallback = new Map<string, PreconditionRow>()
  for (const row of rows.value) {
    for (const issue of row.rerun_preconditions || []) {
      const item = fallback.get(issue) || { issue, count: 0, sites: [] as string[] }
      item.count += 1
      if (item.sites.length < 20) item.sites.push(String(row.site || ''))
      fallback.set(issue, item)
    }
  }
  const source = rowsFromApi.length ? rowsFromApi : Array.from(fallback.values())
  const order = ['traffic_missing', 'conversion_missing', 'anti_bot_blocked', 'proxy_unavailable', 'proxy_auth_failed', 'pdp_price_required', 'sales_history_insufficient']
  return source.slice().sort((a: any, b: any) => (order.indexOf(a.issue) === -1 ? 99 : order.indexOf(a.issue)) - (order.indexOf(b.issue) === -1 ? 99 : order.indexOf(b.issue)))
})
const visibleRows = computed(() => sortedRows.value.filter((row) => qualityFilterMatches(row)))
const rerunSitesList = computed(() => sortedRows.value
  .filter((row) => isRerunnableQualityRow(row))
  .map((row) => row.site)
  .filter(Boolean))
const promotionRebuildSites = computed(() => sortedRows.value
  .filter((row) => shouldRebuildPromotions(row))
  .map((row) => row.site)
  .filter(Boolean))
const analyticsRecomputeSites = computed(() => sortedRows.value
  .filter((row) => shouldRecomputeAnalytics(row))
  .map((row) => row.site)
  .filter(Boolean))
const aosenAcceptanceSummary = computed(() => aosenAcceptance.value?.summary || {})
const aosenAcceptanceItems = computed(() => Array.isArray(aosenAcceptance.value?.items) ? aosenAcceptance.value.items : [])
const aosenActionGroups = computed(() => aosenActionPlan.value?.groups || {})
const aosenFieldFixTemplate = computed(() => aosenActionPlan.value?.templates?.product_field_fixes || {})
const aosenSkuTargetTemplate = computed(() => aosenActionPlan.value?.templates?.sku_targets || {})
const aosenPromotionTemplate = computed(() => aosenActionPlan.value?.templates?.promotion_signals || {})
const aosenSalesTemplate = computed(() => aosenActionPlan.value?.templates?.sales_signals || {})
const aosenReviewHistoryTemplate = computed(() => aosenActionPlan.value?.templates?.review_history || {})
function uniqueSites(raw: unknown): string[] {
  if (!Array.isArray(raw)) return []
  return Array.from(new Set(raw.map((item) => String(item || '').trim()).filter(Boolean)))
}
const aosenPromotionRefreshSites = computed(() => uniqueSites(aosenActionGroups.value?.promotion_refresh?.sites))
const aosenBusinessDataSites = computed(() => uniqueSites(aosenActionGroups.value?.business_data?.sites))
const aosenActionCards = computed(() => [
  { key: 'field_fixes', label: '字段修复', value: aosenActionGroups.value?.field_fixes?.count || 0, issue: 'title_weak' },
  { key: 'promotion_refresh', label: '促销刷新', value: aosenActionGroups.value?.promotion_refresh?.count || 0, issue: 'promotions_missing' },
  { key: 'business_data', label: '业务数据', value: aosenActionGroups.value?.business_data?.count || 0, issue: 'sales_missing' },
])
const aosenAttentionItems = computed(() => aosenAcceptanceItems.value
  .filter((item: Record<string, any>) => item.status !== 'pass')
  .slice(0, 8))
const proxyRuleSites = computed(() => {
  const sites = new Set<string>()
  for (const item of preconditionRows.value) {
    if (!isProxyRulePrecondition(item.issue)) continue
    for (const site of item.sites || []) {
      if (site) sites.add(site)
    }
  }
  if (!sites.size) {
    for (const row of sortedRows.value) {
      if (needsProxyRule(row)) sites.add(String(row.site || ''))
    }
  }
  return Array.from(sites).filter(Boolean)
})
const detailTotalPages = computed(() => Math.max(1, Math.ceil(Number(detailMeta.value?.total || 0) / Number(detailLimit.value || 50))))
const detailStart = computed(() => detailRows.value.length ? (detailPage.value - 1) * Number(detailLimit.value || 50) + 1 : 0)
const detailEnd = computed(() => detailRows.value.length ? detailStart.value + detailRows.value.length - 1 : 0)
const jobDetailIssues = new Set([
  'latest_job_failed',
  'partial_crawl',
  'job_in_progress',
  'job_pending_stale',
  'proxy_unavailable',
  'proxy_auth_failed',
  'anti_bot_blocked',
  'empty_sitemap',
  'market_paused',
])
const siteDetailIssues = new Set([
  'no_products',
  'coverage_low',
  'sku_deviation_high',
  'pdp_price_required',
  'promotions_missing',
  'never_crawled',
])
const detailKind = computed(() => detailMeta.value?.kind || (
  siteDetailIssues.has(detailIssue.value)
    ? 'site'
    : jobDetailIssues.has(detailIssue.value)
    ? 'job'
    : ['traffic_missing', 'conversion_missing'].includes(detailIssue.value)
      ? 'trend'
      : 'product'
))
const detailTitle = computed(() => {
  if (detailKind.value === 'site') return '站点诊断明细'
  if (detailKind.value === 'job') return '问题任务明细'
  if (detailKind.value === 'trend') return '趋势信号明细'
  return '问题商品明细'
})
const qualityColumns = [
  { accessorKey: 'site', header: '站点' },
  { accessorKey: 'status', header: '状态' },
  { accessorKey: 'sku_count', header: 'SKU / SPU' },
  { accessorKey: 'coverage_pct', header: '覆盖 / 目标偏差' },
  { accessorKey: 'promotion_count', header: '促销' },
  { accessorKey: 'signals', header: '标题 / 价格 / 币种 / 销量 / 收入 / 第三方信号' },
  { accessorKey: 'crawl_queue', header: '任务队列' },
  { accessorKey: 'latest_job', header: '最近任务' },
  { accessorKey: 'issues', header: '问题' },
  { accessorKey: 'suggested_action', header: '建议' },
  { id: 'actions', header: '操作' },
]
const detailProductColumns = [
  { accessorKey: 'sku', header: 'SKU / SPU' },
  { accessorKey: 'title', header: '标题 / 类目' },
  { accessorKey: 'sale_price', header: '价格' },
  { accessorKey: 'currency', header: '币种' },
  { accessorKey: 'thirty_day_sales', header: '30日销量 / 收入' },
  { accessorKey: 'status', header: '状态' },
  { accessorKey: 'created_time', header: '创建 / 发布 / 更新' },
  { accessorKey: 'latest_job', header: '最近任务' },
  { accessorKey: 'issues', header: '命中问题' },
]
const detailJobColumns = [
  { accessorKey: 'id', header: 'ID' },
  { accessorKey: 'normalized_status', header: '状态' },
  { accessorKey: 'trigger', header: '触发' },
  { accessorKey: 'failure_code', header: '失败码' },
  { accessorKey: 'failure_stage', header: '阶段' },
  { accessorKey: 'retryable', header: '可重试' },
  { accessorKey: 'products_count', header: '商品/新品/促销' },
  { accessorKey: 'started_at', header: '开始/完成' },
  { accessorKey: 'error', header: '错误' },
  { accessorKey: 'suggested_action', header: '建议' },
]
const detailSiteColumns = [
  { accessorKey: 'site', header: '站点' },
  { accessorKey: 'sku_count', header: 'SKU / SPU' },
  { accessorKey: 'coverage_pct', header: '覆盖' },
  { accessorKey: 'target_sku_count', header: '目标 SKU' },
  { accessorKey: 'sku_deviation_pct', header: '偏差' },
  { accessorKey: 'promotion_count', header: '促销' },
  { accessorKey: 'last_crawled', header: '最近采集' },
  { accessorKey: 'last_product_updated', header: '最近商品更新' },
  { accessorKey: 'latest_job', header: '最近任务' },
  { accessorKey: 'issues', header: '命中问题' },
  { accessorKey: 'suggested_action', header: '建议' },
]
const detailTrendColumns = [
  { accessorKey: 'date', header: '日期' },
  { accessorKey: 'sku_count', header: 'SKU' },
  { accessorKey: 'new_product_count', header: '新品' },
  { accessorKey: 'estimated_sales', header: '估算销量' },
  { accessorKey: 'estimated_revenue', header: '估算收入' },
  { accessorKey: 'traffic', header: '流量' },
  { accessorKey: 'conversion_rate', header: '转化率' },
  { accessorKey: 'issues', header: '命中问题' },
  { accessorKey: 'note', header: '备注' },
]
const activeDetailColumns = computed(() => {
  if (detailKind.value === 'site') return detailSiteColumns
  if (detailKind.value === 'job') return detailJobColumns
  if (detailKind.value === 'trend') return detailTrendColumns
  return detailProductColumns
})
const activeDetailRow = computed(() => rows.value.find((row) => String(row.site || '') === detailSite.value) || null)

function isRerunnableQualityRow(row: Record<string, any>) {
  return row.rerun_recommended === true
}

function shouldRebuildPromotions(row: Record<string, any>) {
  const issues = Array.isArray(row.issues) ? row.issues.map(String) : []
  const skuCount = Number(row.sku_count || 0)
  const promoCount = Number(row.promotion_count || 0)
  return skuCount > 0 && (promoCount <= 0 || issues.includes('promotions_missing'))
}

function shouldRecomputeAnalytics(row: Record<string, any>) {
  const issues = Array.isArray(row.issues) ? row.issues.map(String) : []
  const skuCount = Number(row.sku_count || 0)
  return skuCount > 0 && (
    issues.includes('sales_missing')
    || issues.includes('revenue_missing')
    || issues.includes('sales_history_insufficient')
  )
}

function isProxyRulePrecondition(issue: string) {
  return ['anti_bot_blocked', 'proxy_unavailable', 'proxy_auth_failed'].includes(issue)
}

function needsProxyRule(row: Record<string, any>) {
  const issues = Array.isArray(row.issues) ? row.issues.map(String) : []
  return issues.some(isProxyRulePrecondition)
}

function qualityFilterMatches(row: Record<string, any>) {
  const key = qualityFilter.value
  if (!key) return true
  const issues = Array.isArray(row.issues) ? row.issues.map(String) : []
  const q = row.crawl_queue || {}
  if (key === 'healthy') return row.status === 'healthy'
  if (key === 'rerunnable') return isRerunnableQualityRow(row)
  if (key === 'rerun_after_setup') return row.rerun_after_setup === true
  if (key === 'external_data_required') return row.external_data_required === true
  if (key === 'rerun_blocked') return row.rerun_blocked === true
  if (key === 'currency_issues') return issues.includes('currency_missing') || issues.includes('currency_mismatch')
  if (key === 'pending_jobs') return Number(q.pending || 0) > 0
  if (key === 'running_jobs') return Number(q.running || 0) > 0
  if (key === 'stuck_jobs') return Number(q.stuck || 0) > 0
  if (key === 'failed_jobs') return Number(q.failed || 0) > 0 || issues.includes('latest_job_failed')
  if (key === 'blocked_jobs') return Number(q.blocked || 0) > 0
  if (key === 'skipped_jobs') return Number(q.skipped || 0) > 0
  if (key === 'sites_without_jobs') return Number(q.total || 0) === 0
  return issues.includes(key)
}

function applyQualityFilter(key: string) {
  qualityFilter.value = qualityFilter.value === key ? '' : key
  detailSite.value = ''
  detailRows.value = []
  detailMeta.value = {}
  detailError.value = ''
  detailPage.value = 1
}

function defaultDetailIssue(row: Record<string, any>) {
  const issues = Array.isArray(row.issues) ? row.issues.map(String) : []
  const mappedFilter = ({
    rerunnable: issues[0],
    running_jobs: 'job_in_progress',
    failed_jobs: 'latest_job_failed',
    blocked_jobs: 'latest_job_failed',
    pending_jobs: 'job_in_progress',
    stuck_jobs: 'job_in_progress',
    currency_issues: issues.includes('currency_mismatch') ? 'currency_mismatch' : 'currency_missing',
  } as Record<string, string | undefined>)[qualityFilter.value] || qualityFilter.value
  if (mappedFilter && detailIssues.some((item) => item.key === mappedFilter)) {
    if (!issues.length || issues.includes(mappedFilter) || mappedFilter.startsWith('job_') || mappedFilter === 'latest_job_failed') {
      return mappedFilter
    }
  }
  return issues.find((issue) => detailIssues.some((item) => item.key === issue)) || ''
}

function queueBadges(row: Record<string, any>) {
  const q = row.crawl_queue || {}
  return [
    { key: 'pending', label: '待', value: q.pending || 0 },
    { key: 'stale', label: '久排', value: q.stale_pending || 0 },
    { key: 'running', label: '跑', value: q.running || 0 },
    { key: 'stuck', label: '卡', value: q.stuck || 0 },
    { key: 'failed', label: '败', value: q.failed || 0 },
    { key: 'blocked', label: '阻', value: q.blocked || 0 },
    { key: 'skipped', label: '跳', value: q.skipped || 0 },
  ].filter((item) => item.value > 0)
}

function issueLabel(issue: string) {
  return ({
    no_products: '无商品',
    coverage_low: '覆盖低',
    sku_deviation_high: 'SKU偏差高',
    title_weak: '标题弱',
    category_missing: '类目缺失',
    image_missing: '图片缺失',
    price_missing: '价格缺失',
    pdp_price_required: '需PDP价格源',
    currency_missing: '币种缺失',
    currency_mismatch: '币种错配',
    sales_missing: '销量缺失',
    revenue_missing: '收入缺失',
    sales_history_insufficient: '销量历史不足',
    traffic_missing: '流量缺失',
    conversion_missing: '转化缺失',
    promotions_missing: '促销缺失',
    partial_crawl: '部分采集',
    latest_job_failed: '任务失败',
    job_in_progress: '运行中',
    job_pending_stale: '排队过久',
    never_crawled: '未采集',
    market_paused: '市场暂停',
    empty_sitemap: '空站点地图',
    proxy_unavailable: '代理不可用',
    proxy_auth_failed: '代理鉴权失败',
    anti_bot_blocked: '反爬封禁',
  } as Record<string, string>)[issue] || issue
}

function preconditionAction(issue: string) {
  return ({
    traffic_missing: '导入流量转化',
    conversion_missing: '导入流量转化',
    anti_bot_blocked: '检查代理池/反爬策略',
    proxy_unavailable: '修复代理池',
    proxy_auth_failed: '修复代理鉴权',
    pdp_price_required: '配置PDP价格源',
    sales_history_insufficient: '导入评论历史',
  } as Record<string, string>)[issue] || '查看明细'
}

function preconditionHint(issue: string) {
  return ({
    traffic_missing: '需要 SimilarWeb/GA/BI 等外部指标，抓取重跑不能生成。',
    conversion_missing: '需要外部转化率指标，导入后再刷新报表。',
    anti_bot_blocked: '先让该站走可用住宅代理或浏览器策略，再重跑。',
    proxy_unavailable: '代理池无可用出口，修复健康检查后再重跑。',
    proxy_auth_failed: '代理账号/密码或白名单失败，修复后再重跑。',
    pdp_price_required: '列表页只有商品枚举，价格在 PDP/API/外部价格源。',
    sales_history_insufficient: '评论倒推销量至少需要两次快照；也可以导入明确的外部 30 日销量/营收。',
  } as Record<string, string>)[issue] || '先处理前置条件，再重跑站点。'
}

function resetConfigForm() {
  configForm.value = {
    proxy_tier: 'residential',
    price_source_type: 'feed',
    price_feed_url: '',
    pdp_price_api_url: '',
    pdp_price_selector: '',
    pdp_title_selector: '',
    price_source_max_items: '50',
    price_source_use_proxy: '',
    price_source_allow_stealth: '',
    price_source_timeout: '30',
    price_source_retries: '1',
    price_feed_sku_field: '',
    price_feed_sale_price_field: '',
    price_feed_original_price_field: '',
    price_feed_currency_field: '',
    price_feed_title_field: '',
    notes: '',
  }
  configKeys.value = []
  configError.value = ''
  configMessage.value = ''
  configTestResult.value = null
}

async function openSiteConfig(site: string, issue = '') {
  if (!site) return
  resetConfigForm()
  configSite.value = site
  configOpen.value = true
  configBusy.value = true
  try {
    const res = await siteCrawlerConfig(site)
    const cfg = res?.crawler_config || {}
    configKeys.value = Array.isArray(res?.configured_keys) ? res.configured_keys : []
    configForm.value = {
      proxy_tier: String(res?.proxy_tier || 'residential'),
      price_source_type: String(cfg.price_source_type || (issue === 'pdp_price_required' ? 'feed' : '')),
      price_feed_url: String(cfg.price_feed_url || cfg.feed_url || ''),
      pdp_price_api_url: String(cfg.pdp_price_api_url || cfg.price_api_url || ''),
      pdp_price_selector: String(cfg.pdp_price_selector || cfg.price_selector || ''),
      pdp_title_selector: String(cfg.pdp_title_selector || cfg.title_selector || ''),
      price_source_max_items: String(cfg.price_source_max_items || '50'),
      price_source_use_proxy: String(cfg.price_source_use_proxy ?? ''),
      price_source_allow_stealth: String(cfg.price_source_allow_stealth ?? ''),
      price_source_timeout: String(cfg.price_source_timeout || '30'),
      price_source_retries: String(cfg.price_source_retries || '1'),
      price_feed_sku_field: String(cfg.price_feed_sku_field || ''),
      price_feed_sale_price_field: String(cfg.price_feed_sale_price_field || ''),
      price_feed_original_price_field: String(cfg.price_feed_original_price_field || ''),
      price_feed_currency_field: String(cfg.price_feed_currency_field || ''),
      price_feed_title_field: String(cfg.price_feed_title_field || ''),
      notes: String(cfg.notes || ''),
    }
  } catch (err) {
    configError.value = err instanceof Error ? err.message : String(err)
  } finally {
    configBusy.value = false
  }
}

async function saveSiteConfig() {
  if (!configSite.value) return
  configBusy.value = true
  configError.value = ''
  configMessage.value = ''
  try {
    const res = await siteCrawlerConfigUpdate(configSite.value, {
      proxy_tier: configForm.value.proxy_tier,
      crawler_config: currentCrawlerConfigPayload(),
    })
    configKeys.value = Array.isArray(res?.configured_keys) ? res.configured_keys : []
    configMessage.value = '站点采集配置已保存'
    await load()
  } catch (err) {
    configError.value = err instanceof Error ? err.message : String(err)
  } finally {
    configBusy.value = false
  }
}

function currentCrawlerConfigPayload() {
  return {
    price_source_type: configForm.value.price_source_type,
    price_feed_url: configForm.value.price_feed_url,
    pdp_price_api_url: configForm.value.pdp_price_api_url,
    pdp_price_selector: configForm.value.pdp_price_selector,
    pdp_title_selector: configForm.value.pdp_title_selector,
    price_source_max_items: configForm.value.price_source_max_items,
    price_source_use_proxy: configForm.value.price_source_use_proxy,
    price_source_allow_stealth: configForm.value.price_source_allow_stealth,
    price_source_timeout: configForm.value.price_source_timeout,
    price_source_retries: configForm.value.price_source_retries,
    price_feed_sku_field: configForm.value.price_feed_sku_field,
    price_feed_sale_price_field: configForm.value.price_feed_sale_price_field,
    price_feed_original_price_field: configForm.value.price_feed_original_price_field,
    price_feed_currency_field: configForm.value.price_feed_currency_field,
    price_feed_title_field: configForm.value.price_feed_title_field,
    notes: configForm.value.notes,
  }
}

async function testPriceSourceConfig() {
  if (!configSite.value) return
  configTestBusy.value = true
  configError.value = ''
  configMessage.value = ''
  configTestResult.value = null
  try {
    configTestResult.value = await siteCrawlerConfigTestPriceSource(configSite.value, {
      proxy_tier: configForm.value.proxy_tier,
      sample_limit: 5,
      crawler_config: currentCrawlerConfigPayload(),
    })
  } catch (err) {
    configError.value = err instanceof Error ? err.message : String(err)
  } finally {
    configTestBusy.value = false
  }
}

async function applyPrecondition(item: PreconditionRow) {
  const issue = item.issue
  applyQualityFilter(issue)
  if (issue === 'traffic_missing' || issue === 'conversion_missing') {
    metricsImportOpen.value = true
  } else if (issue === 'coverage_low' || issue === 'sku_deviation_high') {
    skuTargetImportOpen.value = true
  } else if (issue === 'sales_history_insufficient') {
    reviewHistoryImportOpen.value = true
  } else if (['anti_bot_blocked', 'proxy_unavailable', 'proxy_auth_failed'].includes(issue)) {
    await applyRecommendedProxyRules(item.sites || [], issue)
  } else if (issue === 'pdp_price_required' && item.sites?.[0]) {
    await openSiteConfig(item.sites[0], issue)
  }
}

async function applyRecommendedProxyRules(sites: string[] = proxyRuleSites.value, issue = 'anti_bot_blocked') {
  const targetSites = Array.from(new Set((sites || []).map(String).filter(Boolean)))
  if (!targetSites.length) return
  const key = targetSites.length === 1 ? targetSites[0] : '__proxy_rules__'
  proxyRuleBusy.value = { ...proxyRuleBusy.value, [key]: true }
  proxyRuleMessage.value = { ...proxyRuleMessage.value, [key]: '' }
  try {
    const res = await proxyAntiBotApplyRules({
      tenant: tenantId.value || undefined,
      include_hidden: includeHidden.value,
      sites: targetSites,
    })
    const applied = Number(res?.applied_count || 0)
    const total = Number(res?.summary?.total || targetSites.length || 0)
    const unavailable = Number(res?.summary?.with_available_rule || 0)
    const message = applied > 0
      ? `已应用 ${fmtNumber(applied)} 条推荐规则，诊断站点 ${fmtNumber(total)} 个`
      : `暂无可应用规则：请先确认住宅代理池可用（当前可用规则 ${fmtNumber(unavailable)}）`
    proxyRuleMessage.value = { ...proxyRuleMessage.value, [key]: message }
    for (const item of res?.applied || []) {
      if (!item?.site) continue
      proxyRuleMessage.value = {
        ...proxyRuleMessage.value,
        [item.site]: `已绑定 ${item.pool_slug || 'residential'} 规则`,
      }
    }
    await load()
  } catch (err) {
    proxyRuleMessage.value = {
      ...proxyRuleMessage.value,
      [key]: err instanceof Error ? err.message : String(err),
    }
  } finally {
    proxyRuleBusy.value = { ...proxyRuleBusy.value, [key]: false }
  }
}

function detailIssueCount(key: string) {
  const counts = detailMeta.value?.issue_counts || {}
  const value = counts[key || 'all']
  return value === undefined || value === null ? '' : ` ${fmtNumber(value)}`
}

function reportWorkspaceId(row?: Record<string, any>) {
  if (tenantId.value) return String(tenantId.value)
  const workspaces = Array.isArray(row?.workspaces) ? row.workspaces : []
  const first = workspaces.find((item: Record<string, any>) => item?.id)
  return first?.id ? String(first.id) : ''
}

function reportHref(site: string, productId?: number | string, row?: Record<string, any>) {
  const params = new URLSearchParams({ site })
  const workspaceId = reportWorkspaceId(row)
  if (workspaceId) params.set('workspace_id', workspaceId)
  if (productId !== undefined && productId !== null && productId !== '') {
    params.set('pid', String(productId))
    params.set('panel', 'trend')
  }
  return `/report?${params.toString()}`
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
    await loadAosenAcceptance()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function loadAosenAcceptance() {
  aosenAcceptanceBusy.value = true
  aosenAcceptanceError.value = ''
  try {
    const params: Record<string, any> = { include_hidden: includeHidden.value }
    if (tenantId.value) params.tenant = tenantId.value
    const [acceptance, actionPlan] = await Promise.all([
      aosenFieldQualityAcceptance(params),
      aosenAcceptanceActionPlan(params),
    ])
    aosenAcceptance.value = acceptance
    aosenActionPlan.value = actionPlan
  } catch (err) {
    aosenAcceptanceError.value = err instanceof Error ? err.message : String(err)
  } finally {
    aosenAcceptanceBusy.value = false
  }
}

function loadAosenPromotionTemplatePreview() {
  const csv = String(aosenPromotionTemplate.value?.csv || '')
  if (!csv.trim()) {
    promoImportMessage.value = '暂无 Aosen 促销缺口模板'
    return
  }
  promoImportText.value = csv
  promoImportOpen.value = true
  promoValidation.value = null
  const more = aosenPromotionTemplate.value?.has_more ? ' · 仍有更多缺口，可生成完整模板' : ''
  promoImportMessage.value = `已载入 Aosen 促销模板预览 ${fmtNumber(aosenPromotionTemplate.value?.count || 0)} 行${more}`
}

function loadAosenFieldFixTemplatePreview() {
  const csv = String(aosenFieldFixTemplate.value?.csv || '')
  if (!csv.trim()) {
    fieldFixImportMessage.value = '暂无 Aosen 字段修正模板'
    return
  }
  fieldFixImportText.value = csv
  fieldFixImportOpen.value = true
  fieldFixValidation.value = null
  const more = aosenFieldFixTemplate.value?.has_more ? ' · 仍有更多缺口，可生成完整模板' : ''
  fieldFixImportMessage.value = `已载入 Aosen 字段修正模板预览 ${fmtNumber(aosenFieldFixTemplate.value?.count || 0)} 行${more}`
}

function loadAosenSkuTargetTemplatePreview() {
  const csv = String(aosenSkuTargetTemplate.value?.csv || '')
  if (!csv.trim()) {
    skuTargetImportMessage.value = '暂无 Aosen SKU 目标模板'
    return
  }
  skuTargetImportText.value = csv
  skuTargetImportOpen.value = true
  skuTargetValidation.value = null
  const more = aosenSkuTargetTemplate.value?.has_more ? ' · 仍有更多缺口，可生成完整模板' : ''
  skuTargetImportMessage.value = `已载入 Aosen SKU 目标模板预览 ${fmtNumber(aosenSkuTargetTemplate.value?.count || 0)} 行${more}`
}

function loadAosenSalesTemplatePreview() {
  const csv = String(aosenSalesTemplate.value?.csv || '')
  if (!csv.trim()) {
    salesImportMessage.value = '暂无 Aosen 销量营收缺口模板'
    return
  }
  salesImportText.value = csv
  salesImportOpen.value = true
  salesValidation.value = null
  const more = aosenSalesTemplate.value?.has_more ? ' · 仍有更多缺口，可生成完整模板' : ''
  salesImportMessage.value = `已载入 Aosen 销量营收模板预览 ${fmtNumber(aosenSalesTemplate.value?.count || 0)} 行${more}`
}

function loadAosenReviewHistoryTemplatePreview() {
  const csv = String(aosenReviewHistoryTemplate.value?.csv || '')
  if (!csv.trim()) {
    reviewHistoryImportMessage.value = '暂无 Aosen 评论历史模板'
    return
  }
  reviewHistoryImportText.value = csv
  reviewHistoryImportOpen.value = true
  reviewHistoryValidation.value = null
  const more = aosenReviewHistoryTemplate.value?.has_more ? ' · 仍有更多缺口，可生成完整模板' : ''
  reviewHistoryImportMessage.value = `已载入 Aosen 评论历史模板预览 ${fmtNumber(aosenReviewHistoryTemplate.value?.count || 0)} 行${more}`
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

async function rebuildPromotions(sites: string[]) {
  if (!sites.length) return
  const key = sites.length === 1 ? sites[0] : '__batch__'
  promoBusy.value = { ...promoBusy.value, [key]: true }
  promoMessage.value = { ...promoMessage.value, [key]: '' }
  try {
    const res = await promotionsRebuild({ sites })
    const created = Number(res?.created || 0)
    promoMessage.value = {
      ...promoMessage.value,
      [key]: `新增/更新 ${fmtNumber(created)} 条促销`,
    }
    for (const site of sites) {
      const item = res?.by_site?.[site]
      if (!item) continue
      const after = Number(item.after || 0)
      const siteCreated = Number(item.created || 0)
      promoMessage.value = {
        ...promoMessage.value,
        [site]: `促销 ${fmtNumber(after)} 条，新增/更新 ${fmtNumber(siteCreated)}`,
      }
    }
    await load()
  } catch (err) {
    promoMessage.value = { ...promoMessage.value, [key]: err instanceof Error ? err.message : String(err) }
  } finally {
    promoBusy.value = { ...promoBusy.value, [key]: false }
  }
}

async function importProductFieldFixes() {
  const csv = fieldFixImportText.value.trim()
  if (!csv) {
    fieldFixImportMessage.value = '请粘贴 CSV 数据'
    return
  }
  fieldFixImportBusy.value = true
  fieldFixImportMessage.value = ''
  try {
    const res = await productFieldFixesImport({ csv })
    fieldFixImportMessage.value = `已导入 ${fmtNumber(res?.rows || 0)} 行，更新 ${fmtNumber(res?.updated || 0)} 个商品`
    fieldFixValidation.value = null
    await load()
  } catch (err) {
    fieldFixImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    fieldFixImportBusy.value = false
  }
}

async function generateFieldFixTemplate() {
  fieldFixTemplateBusy.value = true
  fieldFixImportMessage.value = ''
  fieldFixValidation.value = null
  try {
    const res = await productFieldFixesTemplate({
      tenant: tenantId.value,
      include_hidden: includeHidden.value,
    })
    fieldFixImportText.value = String(res?.csv || '')
    const deferred = Array.isArray(res?.deferred_sites) && res.deferred_sites.length
      ? ` · 已排除 ${res.deferred_sites.join(' / ')}`
      : ''
    fieldFixImportMessage.value = `已生成 ${fmtNumber(res?.count || 0)} 行字段修正模板${deferred}`
  } catch (err) {
    fieldFixImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    fieldFixTemplateBusy.value = false
  }
}

async function validateProductFieldFixes() {
  const csv = fieldFixImportText.value.trim()
  if (!csv) {
    fieldFixImportMessage.value = '请粘贴 CSV 数据'
    return
  }
  fieldFixValidateBusy.value = true
  fieldFixImportMessage.value = ''
  fieldFixValidation.value = null
  try {
    const res = await productFieldFixesValidate({ csv })
    fieldFixValidation.value = res || {}
    fieldFixImportMessage.value = res?.valid
      ? `校验通过 ${fmtNumber(res?.valid_rows || 0)} 行`
      : `校验未通过：${fmtNumber(res?.errors?.length || 0)} 个错误`
  } catch (err) {
    fieldFixImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    fieldFixValidateBusy.value = false
  }
}

async function importSkuTargets() {
  const csv = skuTargetImportText.value.trim()
  if (!csv) {
    skuTargetImportMessage.value = '请粘贴 CSV 数据'
    return
  }
  skuTargetImportBusy.value = true
  skuTargetImportMessage.value = ''
  try {
    const res = await skuTargetsImport({ csv })
    skuTargetImportMessage.value = `已导入 ${fmtNumber(res?.rows || 0)} 条 SKU 目标，站点 ${fmtNumber(res?.sites?.length || 0)} 个`
    skuTargetValidation.value = null
    await load()
  } catch (err) {
    skuTargetImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    skuTargetImportBusy.value = false
  }
}

async function generateSkuTargetTemplate() {
  skuTargetTemplateBusy.value = true
  skuTargetImportMessage.value = ''
  skuTargetValidation.value = null
  try {
    const res = await skuTargetsTemplate({
      tenant: tenantId.value,
      include_hidden: includeHidden.value,
    })
    skuTargetImportText.value = String(res?.csv || '')
    const deferred = Array.isArray(res?.deferred_sites) && res.deferred_sites.length
      ? ` · 已排除 ${res.deferred_sites.join(' / ')}`
      : ''
    skuTargetImportMessage.value = `已生成 ${fmtNumber(res?.count || 0)} 行 SKU 目标模板${deferred}`
  } catch (err) {
    skuTargetImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    skuTargetTemplateBusy.value = false
  }
}

async function validateSkuTargets() {
  const csv = skuTargetImportText.value.trim()
  if (!csv) {
    skuTargetImportMessage.value = '请粘贴 CSV 数据'
    return
  }
  skuTargetValidateBusy.value = true
  skuTargetImportMessage.value = ''
  skuTargetValidation.value = null
  try {
    const res = await skuTargetsValidate({ csv })
    skuTargetValidation.value = res || {}
    skuTargetImportMessage.value = res?.valid
      ? `校验通过 ${fmtNumber(res?.valid_rows || 0)} 行`
      : `校验未通过：${fmtNumber(res?.errors?.length || 0)} 个错误`
  } catch (err) {
    skuTargetImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    skuTargetValidateBusy.value = false
  }
}

async function importPromotionSignals() {
  const csv = promoImportText.value.trim()
  if (!csv) {
    promoImportMessage.value = '请粘贴 CSV 数据'
    return
  }
  promoImportBusy.value = true
  promoImportMessage.value = ''
  try {
    const res = await promotionSignalsImport({ csv })
    promoImportMessage.value = `已导入 ${fmtNumber(res?.rows || 0)} 行，新增 ${fmtNumber(res?.created || 0)}，更新 ${fmtNumber(res?.updated || 0)}`
    promoValidation.value = null
    await load()
  } catch (err) {
    promoImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    promoImportBusy.value = false
  }
}

async function generatePromotionTemplate() {
  promoTemplateBusy.value = true
  promoImportMessage.value = ''
  promoValidation.value = null
  try {
    const res = await promotionSignalsTemplate({
      tenant: tenantId.value,
      include_hidden: includeHidden.value,
    })
    promoImportText.value = String(res?.csv || '')
    const deferred = Array.isArray(res?.deferred_sites) && res.deferred_sites.length
      ? ` · 已排除 ${res.deferred_sites.join(' / ')}`
      : ''
    promoImportMessage.value = `已生成 ${fmtNumber(res?.count || 0)} 行促销模板${deferred}`
  } catch (err) {
    promoImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    promoTemplateBusy.value = false
  }
}

async function validatePromotionSignals() {
  const csv = promoImportText.value.trim()
  if (!csv) {
    promoImportMessage.value = '请粘贴 CSV 数据'
    return
  }
  promoValidateBusy.value = true
  promoImportMessage.value = ''
  promoValidation.value = null
  try {
    const res = await promotionSignalsValidate({ csv })
    promoValidation.value = res || {}
    promoImportMessage.value = res?.valid
      ? `校验通过 ${fmtNumber(res?.valid_rows || 0)} 行`
      : `校验未通过：${fmtNumber(res?.errors?.length || 0)} 个错误`
  } catch (err) {
    promoImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    promoValidateBusy.value = false
  }
}

async function recomputeAnalytics(sites: string[]) {
  if (!sites.length) return
  const key = sites.length === 1 ? sites[0] : '__batch__'
  analyticsBusy.value = { ...analyticsBusy.value, [key]: true }
  analyticsMessage.value = { ...analyticsMessage.value, [key]: '' }
  try {
    const res = await analyticsRecompute({ sites })
    const totals = res?.totals || {}
    const msg = `估算SKU ${fmtNumber(totals.estimated_skus || 0)} · 历史不足 ${fmtNumber(totals.insufficient_history_skus || 0)}`
    analyticsMessage.value = { ...analyticsMessage.value, [key]: msg }
    for (const site of sites) {
      const item = res?.by_site?.[site]
      if (item) analyticsMessage.value = {
        ...analyticsMessage.value,
        [site]: `估算SKU ${fmtNumber(item.estimated_skus || 0)} · 历史不足 ${fmtNumber(item.insufficient_history_skus || 0)}`,
      }
    }
    await load()
  } catch (err) {
    analyticsMessage.value = { ...analyticsMessage.value, [key]: err instanceof Error ? err.message : String(err) }
  } finally {
    analyticsBusy.value = { ...analyticsBusy.value, [key]: false }
  }
}

async function importThirdPartyMetrics() {
  const csv = metricsImportText.value.trim()
  if (!csv) {
    metricsImportMessage.value = '请粘贴 CSV 数据'
    return
  }
  metricsImportBusy.value = true
  metricsImportMessage.value = ''
  try {
    const res = await thirdPartyMetricsImport({ csv })
    metricsImportMessage.value = `已导入 ${fmtNumber(res?.rows || 0)} 行，新增 ${fmtNumber(res?.created || 0)}，更新 ${fmtNumber(res?.updated || 0)}`
    metricsValidation.value = null
    await load()
  } catch (err) {
    metricsImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    metricsImportBusy.value = false
  }
}

async function generateMetricsTemplate() {
  metricsTemplateBusy.value = true
  metricsImportMessage.value = ''
  metricsValidation.value = null
  try {
    const res = await thirdPartyMetricsTemplate({
      tenant: tenantId.value,
      include_hidden: includeHidden.value,
    })
    metricsImportText.value = String(res?.csv || '')
    const summary = res?.summary || {}
    metricsImportMessage.value = `已生成 ${fmtNumber(res?.count || 0)} 行模板 · 缺流量 ${fmtNumber(summary.missing_traffic || 0)} · 缺转化 ${fmtNumber(summary.missing_conversion || 0)}`
  } catch (err) {
    metricsImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    metricsTemplateBusy.value = false
  }
}

async function validateThirdPartyMetrics() {
  const csv = metricsImportText.value.trim()
  if (!csv) {
    metricsImportMessage.value = '请粘贴 CSV 数据'
    return
  }
  metricsValidateBusy.value = true
  metricsImportMessage.value = ''
  metricsValidation.value = null
  try {
    const res = await thirdPartyMetricsValidate({ csv })
    metricsValidation.value = res || {}
    metricsImportMessage.value = res?.valid
      ? `校验通过 ${fmtNumber(res?.valid_rows || 0)} 行，新增 ${fmtNumber(res?.created || 0)}，更新 ${fmtNumber(res?.updated || 0)}`
      : `校验未通过：${fmtNumber(res?.errors?.length || 0)} 个错误`
  } catch (err) {
    metricsImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    metricsValidateBusy.value = false
  }
}

async function importSalesSignals() {
  const csv = salesImportText.value.trim()
  if (!csv) {
    salesImportMessage.value = '请粘贴 CSV 数据'
    return
  }
  salesImportBusy.value = true
  salesImportMessage.value = ''
  try {
    const res = await salesSignalsImport({ csv })
    salesImportMessage.value = `已导入 ${fmtNumber(res?.rows || 0)} 行，站点 ${fmtNumber(res?.sites?.length || 0)} 个`
    salesValidation.value = null
    await load()
  } catch (err) {
    salesImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    salesImportBusy.value = false
  }
}

async function generateSalesTemplate() {
  salesTemplateBusy.value = true
  salesImportMessage.value = ''
  salesValidation.value = null
  try {
    const res = await salesSignalsTemplate({
      tenant: tenantId.value,
      include_hidden: includeHidden.value,
    })
    salesImportText.value = String(res?.csv || '')
    const deferred = Array.isArray(res?.deferred_sites) && res.deferred_sites.length
      ? ` · 已排除 ${res.deferred_sites.join(' / ')}`
      : ''
    salesImportMessage.value = `已生成 ${fmtNumber(res?.count || 0)} 行销量模板${deferred}`
  } catch (err) {
    salesImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    salesTemplateBusy.value = false
  }
}

async function validateSalesSignals() {
  const csv = salesImportText.value.trim()
  if (!csv) {
    salesImportMessage.value = '请粘贴 CSV 数据'
    return
  }
  salesValidateBusy.value = true
  salesImportMessage.value = ''
  salesValidation.value = null
  try {
    const res = await salesSignalsValidate({ csv })
    salesValidation.value = res || {}
    salesImportMessage.value = res?.valid
      ? `校验通过 ${fmtNumber(res?.valid_rows || 0)} 行`
      : `校验未通过：${fmtNumber(res?.errors?.length || 0)} 个错误`
  } catch (err) {
    salesImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    salesValidateBusy.value = false
  }
}

async function importReviewHistory() {
  const csv = reviewHistoryImportText.value.trim()
  if (!csv) {
    reviewHistoryImportMessage.value = '请粘贴 CSV 数据'
    return
  }
  reviewHistoryImportBusy.value = true
  reviewHistoryImportMessage.value = ''
  try {
    const res = await reviewHistoryImport({ csv })
    const totals = Object.values(res?.by_site || {}).reduce((acc: Record<string, number>, item: any) => {
      acc.estimated_skus += Number(item?.estimated_skus || 0)
      acc.estimated_sales += Number(item?.estimated_sales || 0)
      return acc
    }, { estimated_skus: 0, estimated_sales: 0 })
    reviewHistoryImportMessage.value = `已导入 ${fmtNumber(res?.rows || 0)} 行，重算SKU ${fmtNumber(totals.estimated_skus)}，销量 ${fmtNumber(totals.estimated_sales)}`
    reviewHistoryValidation.value = null
    await load()
  } catch (err) {
    reviewHistoryImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    reviewHistoryImportBusy.value = false
  }
}

async function generateReviewHistoryTemplate() {
  reviewHistoryTemplateBusy.value = true
  reviewHistoryImportMessage.value = ''
  reviewHistoryValidation.value = null
  try {
    const res = await reviewHistoryTemplate({
      tenant: tenantId.value,
      include_hidden: includeHidden.value,
    })
    reviewHistoryImportText.value = String(res?.csv || '')
    const deferred = Array.isArray(res?.deferred_sites) && res.deferred_sites.length
      ? ` · 已排除 ${res.deferred_sites.join(' / ')}`
      : ''
    reviewHistoryImportMessage.value = `已生成 ${fmtNumber(res?.count || 0)} 行评论历史模板${deferred}`
  } catch (err) {
    reviewHistoryImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    reviewHistoryTemplateBusy.value = false
  }
}

async function validateReviewHistory() {
  const csv = reviewHistoryImportText.value.trim()
  if (!csv) {
    reviewHistoryImportMessage.value = '请粘贴 CSV 数据'
    return
  }
  reviewHistoryValidateBusy.value = true
  reviewHistoryImportMessage.value = ''
  reviewHistoryValidation.value = null
  try {
    const res = await reviewHistoryValidate({ csv })
    reviewHistoryValidation.value = res || {}
    reviewHistoryImportMessage.value = res?.valid
      ? `校验通过 ${fmtNumber(res?.valid_rows || 0)} 行`
      : `校验未通过：${fmtNumber(res?.errors?.length || 0)} 个错误`
  } catch (err) {
    reviewHistoryImportMessage.value = err instanceof Error ? err.message : String(err)
  } finally {
    reviewHistoryValidateBusy.value = false
  }
}

function openExternalDataImport(row: Record<string, any>) {
  const issues = Array.isArray(row.issues) ? row.issues.map(String) : []
  if (issues.includes('sales_history_insufficient')) {
    reviewHistoryImportOpen.value = true
  } else if (issues.some((issue) => ['sales_missing', 'revenue_missing'].includes(issue))) {
    salesImportOpen.value = true
  } else {
    metricsImportOpen.value = true
  }
}

function aosenStatusLabel(status: string) {
  return ({
    fail: '字段失败',
    needs_refresh: '需重抓/重算',
    needs_business_data: '需业务数据',
    pass: '通过',
  } as Record<string, string>)[status] || status || '-'
}

async function loadDetail(site: string, issue = detailIssue.value, page = detailPage.value) {
  detailSite.value = site
  detailIssue.value = issue
  detailPage.value = Math.max(1, page)
  detailLoading.value = true
  detailError.value = ''
  try {
    const params: Record<string, any> = { page: detailPage.value, limit: detailLimit.value }
    if (issue) params.issue = issue
    const res = await dataQualityProducts(site, params)
    detailRows.value = res?.items || []
    detailMeta.value = res || {}
    detailPage.value = Math.min(Math.max(1, Number(res?.page ?? detailPage.value)), detailTotalPages.value)
  } catch (err) {
    detailRows.value = []
    detailMeta.value = {}
    detailError.value = err instanceof Error ? err.message : String(err)
  } finally {
    detailLoading.value = false
  }
}

async function toggleDetail(rowOrSite: Record<string, any> | string) {
  const site = typeof rowOrSite === 'string' ? rowOrSite : String(rowOrSite.site || '')
  if (detailSite.value === site) {
    detailSite.value = ''
    detailRows.value = []
    detailMeta.value = {}
    detailError.value = ''
    detailPage.value = 1
    return
  }
  await loadDetail(site, typeof rowOrSite === 'string' ? '' : defaultDetailIssue(rowOrSite), 1)
}

async function switchDetailIssue(site: string, issue: string) {
  await loadDetail(site, issue, 1)
}

async function changeDetailPage(delta: number) {
  if (!detailSite.value) return
  const next = Math.min(detailTotalPages.value, Math.max(1, detailPage.value + delta))
  if (next === detailPage.value) return
  await loadDetail(detailSite.value, detailIssue.value, next)
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
        <USelect v-model="tenantSelect" class="select-ctl tenant-select" :items="tenantItems" value-key="value" @update:model-value="load" />
        <label class="inline-check">
          <input v-model="includeHidden" type="checkbox" @change="load" />
          <span>包含隐藏站点</span>
        </label>
        <button class="btn small primary" :disabled="loading || rerunBusy.__batch__ || !rerunSitesList.length" @click="rerunSites(rerunSitesList)">
          {{ rerunBusy.__batch__ ? '入队中...' : `重跑可修复项(${rerunSitesList.length})` }}
        </button>
        <button class="btn small promote" :disabled="loading || promoBusy.__batch__ || !promotionRebuildSites.length" @click="rebuildPromotions(promotionRebuildSites)">
          {{ promoBusy.__batch__ ? '重算中...' : `重算缺促销(${promotionRebuildSites.length})` }}
        </button>
        <button class="btn small" :class="{ active: promoImportOpen }" @click="promoImportOpen = !promoImportOpen">
          导入促销
        </button>
        <button class="btn small" :class="{ active: fieldFixImportOpen }" @click="fieldFixImportOpen = !fieldFixImportOpen">
          导入字段修正
        </button>
        <button class="btn small" :class="{ active: skuTargetImportOpen }" @click="skuTargetImportOpen = !skuTargetImportOpen">
          导入SKU目标
        </button>
        <button class="btn small" :disabled="loading || analyticsBusy.__batch__ || !analyticsRecomputeSites.length" @click="recomputeAnalytics(analyticsRecomputeSites)">
          {{ analyticsBusy.__batch__ ? '重算中...' : `重算销量趋势(${analyticsRecomputeSites.length})` }}
        </button>
        <button class="btn small" :class="{ active: metricsImportOpen }" @click="metricsImportOpen = !metricsImportOpen">
          导入流量转化
        </button>
        <button class="btn small" :class="{ active: salesImportOpen }" @click="salesImportOpen = !salesImportOpen">
          导入销量营收
        </button>
        <button class="btn small" :class="{ active: reviewHistoryImportOpen }" @click="reviewHistoryImportOpen = !reviewHistoryImportOpen">
          导入评论历史
        </button>
        <button class="btn small" :disabled="loading" @click="load">刷新</button>
      </div>
    </div>

    <div v-if="error" class="error">{{ error }}</div>
    <div v-if="promoImportOpen" class="import-panel">
      <div class="import-head">
        <b>促销信号导入</b>
        <span>CSV 字段：site,sku,promotion_type,promotion_name,discount_percent,promotion_price,threshold,start_time,end_time；默认模板不包含 vidaxl_us / vidaxl_ca。</span>
      </div>
      <textarea v-model="promoImportText" spellcheck="false" />
      <div class="import-actions">
        <button class="btn small" :disabled="promoTemplateBusy" @click="generatePromotionTemplate">
          {{ promoTemplateBusy ? '生成中...' : '生成促销模板' }}
        </button>
        <button class="btn small" :disabled="promoValidateBusy" @click="validatePromotionSignals">
          {{ promoValidateBusy ? '校验中...' : '预校验' }}
        </button>
        <button class="btn small primary" :disabled="promoImportBusy || promoValidateBusy" @click="importPromotionSignals">
          {{ promoImportBusy ? '导入中...' : '导入并刷新' }}
        </button>
        <span v-if="promoImportMessage" class="import-msg">{{ promoImportMessage }}</span>
      </div>
      <div v-if="promoValidation" class="validation-panel">
        <div class="validation-head">
          <b>{{ promoValidation.valid ? '校验通过' : '校验未通过' }}</b>
          <span>
            有效 {{ fmtNumber(promoValidation.valid_rows) }} / {{ fmtNumber(promoValidation.rows) }}
            · 跳过 {{ fmtNumber(promoValidation.skipped) }}
          </span>
        </div>
        <div v-if="promoValidation.errors?.length" class="validation-errors">
          <div v-for="item in promoValidation.errors.slice(0, 8)" :key="`${item.row}-${item.site || 'blank'}-${item.sku || 'sku'}`">
            第 {{ item.row }} 行 · {{ item.site || '-' }} · {{ item.sku || '-' }} · {{ (item.errors || []).join(' / ') }}
          </div>
        </div>
      </div>
    </div>
    <div v-if="fieldFixImportOpen" class="import-panel">
      <div class="import-head">
        <b>商品字段修正导入</b>
        <span>CSV 字段：site,sku,title,currency,category_path,image_urls,sale_price,original_price,spu,note；默认模板不包含 vidaxl_us / vidaxl_ca。</span>
      </div>
      <textarea v-model="fieldFixImportText" spellcheck="false" />
      <div class="import-actions">
        <button class="btn small" :disabled="fieldFixTemplateBusy" @click="generateFieldFixTemplate">
          {{ fieldFixTemplateBusy ? '生成中...' : '生成字段模板' }}
        </button>
        <button class="btn small" :disabled="fieldFixValidateBusy" @click="validateProductFieldFixes">
          {{ fieldFixValidateBusy ? '校验中...' : '预校验' }}
        </button>
        <button class="btn small primary" :disabled="fieldFixImportBusy || fieldFixValidateBusy" @click="importProductFieldFixes">
          {{ fieldFixImportBusy ? '导入中...' : '导入并刷新' }}
        </button>
        <span v-if="fieldFixImportMessage" class="import-msg">{{ fieldFixImportMessage }}</span>
      </div>
      <div v-if="fieldFixValidation" class="validation-panel">
        <div class="validation-head">
          <b>{{ fieldFixValidation.valid ? '校验通过' : '校验未通过' }}</b>
          <span>
            有效 {{ fmtNumber(fieldFixValidation.valid_rows) }} / {{ fmtNumber(fieldFixValidation.rows) }}
            · 跳过 {{ fmtNumber(fieldFixValidation.skipped) }}
          </span>
        </div>
        <div v-if="fieldFixValidation.errors?.length" class="validation-errors">
          <div v-for="item in fieldFixValidation.errors.slice(0, 8)" :key="`${item.row}-${item.site || 'blank'}-${item.sku || 'sku'}`">
            第 {{ item.row }} 行 · {{ item.site || '-' }} · {{ item.sku || '-' }} · {{ (item.errors || []).join(' / ') }}
          </div>
        </div>
      </div>
    </div>
    <div v-if="skuTargetImportOpen" class="import-panel">
      <div class="import-head">
        <b>SKU 目标口径导入</b>
        <span>CSV 字段：site,workspace_id,target_sku_count,note；更新 workspace 的目标 SKU 数，不修改商品 SKU；默认模板不包含 vidaxl_us / vidaxl_ca。</span>
      </div>
      <textarea v-model="skuTargetImportText" spellcheck="false" />
      <div class="import-actions">
        <button class="btn small" :disabled="skuTargetTemplateBusy" @click="generateSkuTargetTemplate">
          {{ skuTargetTemplateBusy ? '生成中...' : '生成SKU目标模板' }}
        </button>
        <button class="btn small" :disabled="skuTargetValidateBusy" @click="validateSkuTargets">
          {{ skuTargetValidateBusy ? '校验中...' : '预校验' }}
        </button>
        <button class="btn small primary" :disabled="skuTargetImportBusy || skuTargetValidateBusy" @click="importSkuTargets">
          {{ skuTargetImportBusy ? '导入中...' : '导入并刷新' }}
        </button>
        <span v-if="skuTargetImportMessage" class="import-msg">{{ skuTargetImportMessage }}</span>
      </div>
      <div v-if="skuTargetValidation" class="validation-panel">
        <div class="validation-head">
          <b>{{ skuTargetValidation.valid ? '校验通过' : '校验未通过' }}</b>
          <span>
            有效 {{ fmtNumber(skuTargetValidation.valid_rows) }} / {{ fmtNumber(skuTargetValidation.rows) }}
            · 跳过 {{ fmtNumber(skuTargetValidation.skipped) }}
          </span>
        </div>
        <div v-if="skuTargetValidation.errors?.length" class="validation-errors">
          <div v-for="item in skuTargetValidation.errors.slice(0, 8)" :key="`${item.row}-${item.site || 'blank'}-${item.workspace_id || 'ws'}`">
            第 {{ item.row }} 行 · {{ item.site || '-' }} · workspace {{ item.workspace_id || '-' }} · {{ (item.errors || []).join(' / ') }}
          </div>
        </div>
      </div>
    </div>
    <div v-if="metricsImportOpen" class="import-panel">
      <div class="import-head">
        <b>第三方指标导入</b>
        <span>CSV 字段：site,date,traffic,conversion_rate；conversion_rate 直接填百分比数值，如 2.5 表示 2.5%。</span>
      </div>
      <textarea v-model="metricsImportText" spellcheck="false" />
      <div class="import-actions">
        <button class="btn small" :disabled="metricsTemplateBusy" @click="generateMetricsTemplate">
          {{ metricsTemplateBusy ? '生成中...' : '生成缺口模板' }}
        </button>
        <button class="btn small" :disabled="metricsValidateBusy" @click="validateThirdPartyMetrics">
          {{ metricsValidateBusy ? '校验中...' : '预校验' }}
        </button>
        <button class="btn small primary" :disabled="metricsImportBusy || metricsValidateBusy" @click="importThirdPartyMetrics">
          {{ metricsImportBusy ? '导入中...' : '导入并刷新' }}
        </button>
        <span v-if="metricsImportMessage" class="import-msg">{{ metricsImportMessage }}</span>
      </div>
      <div v-if="metricsValidation" class="validation-panel">
        <div class="validation-head">
          <b>{{ metricsValidation.valid ? '校验通过' : '校验未通过' }}</b>
          <span>
            有效 {{ fmtNumber(metricsValidation.valid_rows) }} / {{ fmtNumber(metricsValidation.rows) }}
            · 新增 {{ fmtNumber(metricsValidation.created) }}
            · 更新 {{ fmtNumber(metricsValidation.updated) }}
          </span>
        </div>
        <div v-if="metricsValidation.errors?.length" class="validation-errors">
          <div v-for="item in metricsValidation.errors.slice(0, 8)" :key="`${item.row}-${item.site || 'blank'}`">
            第 {{ item.row }} 行 · {{ item.site || '-' }} · {{ (item.errors || []).join(' / ') }}
          </div>
        </div>
      </div>
    </div>
    <div v-if="salesImportOpen" class="import-panel">
      <div class="import-head">
        <b>30 日销量/营收导入</b>
        <span>CSV 字段：site,sku,date,thirty_day_sales,thirty_day_revenue；默认模板不包含 vidaxl_us / vidaxl_ca。</span>
      </div>
      <textarea v-model="salesImportText" spellcheck="false" />
      <div class="import-actions">
        <button class="btn small" :disabled="salesTemplateBusy" @click="generateSalesTemplate">
          {{ salesTemplateBusy ? '生成中...' : '生成销量模板' }}
        </button>
        <button class="btn small" :disabled="salesValidateBusy" @click="validateSalesSignals">
          {{ salesValidateBusy ? '校验中...' : '预校验' }}
        </button>
        <button class="btn small primary" :disabled="salesImportBusy || salesValidateBusy" @click="importSalesSignals">
          {{ salesImportBusy ? '导入中...' : '导入并刷新' }}
        </button>
        <span v-if="salesImportMessage" class="import-msg">{{ salesImportMessage }}</span>
      </div>
      <div v-if="salesValidation" class="validation-panel">
        <div class="validation-head">
          <b>{{ salesValidation.valid ? '校验通过' : '校验未通过' }}</b>
          <span>
            有效 {{ fmtNumber(salesValidation.valid_rows) }} / {{ fmtNumber(salesValidation.rows) }}
            · 跳过 {{ fmtNumber(salesValidation.skipped) }}
          </span>
        </div>
        <div v-if="salesValidation.errors?.length" class="validation-errors">
          <div v-for="item in salesValidation.errors.slice(0, 8)" :key="`${item.row}-${item.site || 'blank'}-${item.sku || 'sku'}`">
            第 {{ item.row }} 行 · {{ item.site || '-' }} · {{ item.sku || '-' }} · {{ (item.errors || []).join(' / ') }}
          </div>
        </div>
      </div>
    </div>
    <div v-if="reviewHistoryImportOpen" class="import-panel">
      <div class="import-head">
        <b>评论历史快照导入</b>
        <span>CSV 字段：site,sku,date,review_count,sale_price,original_price；导入后按同 SKU 评论增量重算 30 日销量/营收，默认模板不包含 vidaxl_us / vidaxl_ca。</span>
      </div>
      <textarea v-model="reviewHistoryImportText" spellcheck="false" />
      <div class="import-actions">
        <button class="btn small" :disabled="reviewHistoryTemplateBusy" @click="generateReviewHistoryTemplate">
          {{ reviewHistoryTemplateBusy ? '生成中...' : '生成评论历史模板' }}
        </button>
        <button class="btn small" :disabled="reviewHistoryValidateBusy" @click="validateReviewHistory">
          {{ reviewHistoryValidateBusy ? '校验中...' : '预校验' }}
        </button>
        <button class="btn small primary" :disabled="reviewHistoryImportBusy || reviewHistoryValidateBusy" @click="importReviewHistory">
          {{ reviewHistoryImportBusy ? '导入中...' : '导入并重算' }}
        </button>
        <span v-if="reviewHistoryImportMessage" class="import-msg">{{ reviewHistoryImportMessage }}</span>
      </div>
      <div v-if="reviewHistoryValidation" class="validation-panel">
        <div class="validation-head">
          <b>{{ reviewHistoryValidation.valid ? '校验通过' : '校验未通过' }}</b>
          <span>
            有效 {{ fmtNumber(reviewHistoryValidation.valid_rows) }} / {{ fmtNumber(reviewHistoryValidation.rows) }}
            · 跳过 {{ fmtNumber(reviewHistoryValidation.skipped) }}
          </span>
        </div>
        <div v-if="reviewHistoryValidation.errors?.length" class="validation-errors">
          <div v-for="item in reviewHistoryValidation.errors.slice(0, 8)" :key="`${item.row}-${item.site || 'blank'}-${item.sku || 'sku'}`">
            第 {{ item.row }} 行 · {{ item.site || '-' }} · {{ item.sku || '-' }} · {{ (item.errors || []).join(' / ') }}
          </div>
        </div>
      </div>
    </div>

    <div class="acceptance-panel">
      <div class="acceptance-head">
        <div>
          <b>Aosen 字段验收</b>
          <span>默认排除 vidaxl_us / vidaxl_ca，最终以线上运行环境的数据为准。</span>
        </div>
        <button class="btn small" :disabled="aosenAcceptanceBusy" @click="loadAosenAcceptance">
          {{ aosenAcceptanceBusy ? '刷新中...' : '刷新验收' }}
        </button>
      </div>
      <div v-if="aosenAcceptanceError" class="error">{{ aosenAcceptanceError }}</div>
      <div v-else class="acceptance-grid">
        <div class="acceptance-stat">
          <span>站点</span>
          <b>{{ fmtNumber(aosenAcceptanceSummary.sites) }}</b>
        </div>
        <div class="acceptance-stat pass">
          <span>通过</span>
          <b>{{ fmtNumber(aosenAcceptanceSummary.pass) }}</b>
        </div>
        <div class="acceptance-stat fail">
          <span>字段失败</span>
          <b>{{ fmtNumber(aosenAcceptanceSummary.fail) }}</b>
        </div>
        <div class="acceptance-stat warn">
          <span>需业务数据</span>
          <b>{{ fmtNumber(aosenAcceptanceSummary.needs_business_data) }}</b>
        </div>
        <div class="acceptance-stat warn">
          <span>需重抓/重算</span>
          <b>{{ fmtNumber(aosenAcceptanceSummary.needs_refresh) }}</b>
        </div>
        <div class="acceptance-stat">
          <span>促销缺口</span>
          <b>{{ fmtNumber(aosenAcceptanceSummary.promotions_missing) }}</b>
        </div>
        <div class="acceptance-stat">
          <span>类目/图片</span>
          <b>{{ fmtNumber((aosenAcceptanceSummary.category_missing || 0) + (aosenAcceptanceSummary.image_missing || 0)) }}</b>
        </div>
        <div class="acceptance-stat">
          <span>销量/收入</span>
          <b>{{ fmtNumber(aosenAcceptanceSummary.sales_or_revenue_missing) }}</b>
        </div>
      </div>
      <div v-if="aosenActionPlan.status" class="acceptance-actions">
        <button
          v-for="card in aosenActionCards"
          :key="card.key"
          class="acceptance-action"
          @click="applyQualityFilter(card.issue)"
        >
          <span>{{ card.label }}</span>
          <b>{{ fmtNumber(card.value) }}</b>
        </button>
      </div>
      <div v-if="aosenActionPlan.status" class="acceptance-template-actions">
        <button
          class="btn small promote"
          :disabled="promoBusy.__batch__ || !aosenPromotionRefreshSites.length"
          @click="rebuildPromotions(aosenPromotionRefreshSites)"
        >
          {{ promoBusy.__batch__ ? '重算中...' : `重算Aosen促销(${aosenPromotionRefreshSites.length})` }}
        </button>
        <button
          class="btn small"
          :disabled="analyticsBusy.__batch__ || !aosenBusinessDataSites.length"
          @click="recomputeAnalytics(aosenBusinessDataSites)"
        >
          {{ analyticsBusy.__batch__ ? '重算中...' : `重算Aosen销量(${aosenBusinessDataSites.length})` }}
        </button>
        <button class="btn small" @click="loadAosenPromotionTemplatePreview">
          载入促销模板预览
        </button>
        <button class="btn small" @click="loadAosenFieldFixTemplatePreview">
          载入字段模板预览
        </button>
        <button class="btn small" @click="loadAosenSkuTargetTemplatePreview">
          载入SKU目标模板
        </button>
        <button class="btn small" @click="loadAosenSalesTemplatePreview">
          载入销量模板预览
        </button>
        <button class="btn small" @click="loadAosenReviewHistoryTemplatePreview">
          载入评论历史模板
        </button>
      </div>
      <div v-if="aosenAttentionItems.length" class="acceptance-items">
        <button
          v-for="item in aosenAttentionItems"
          :key="item.site"
          class="acceptance-item"
          @click="applyQualityFilter((item.issues || [])[0] || '')"
        >
          <b>{{ item.site }}</b>
          <span>{{ aosenStatusLabel(item.status) }}</span>
          <small>{{ (item.issues || []).map(issueLabel).join(' / ') || '-' }}</small>
        </button>
      </div>
    </div>

    <div class="stat-row">
      <button
        v-for="card in summaryCards"
        :key="card.key || 'all'"
        class="stat-filter"
        :class="{ active: qualityFilter === card.key }"
        @click="applyQualityFilter(card.key)"
      >
        <StatCard :label="card.label" :value="fmtNumber(card.value)" />
      </button>
    </div>

    <div v-if="preconditionRows.length" class="precondition-panel">
      <div class="precondition-head">
        <div>
          <b>修复后重跑前置条件</b>
          <span>先处理这些原因，再批量重跑，避免无效任务堆积。</span>
        </div>
        <div class="precondition-actions">
          <span>{{ fmtNumber(summary.rerun_precondition_total || preconditionRows.length) }} 个站点受影响</span>
          <button
            v-if="proxyRuleSites.length"
            class="btn small promote"
            :disabled="proxyRuleBusy.__proxy_rules__"
            @click="applyRecommendedProxyRules(proxyRuleSites)"
          >
            {{ proxyRuleBusy.__proxy_rules__ ? '应用中...' : `应用代理规则(${proxyRuleSites.length})` }}
          </button>
          <small v-if="proxyRuleMessage.__proxy_rules__" class="precondition-message">{{ proxyRuleMessage.__proxy_rules__ }}</small>
        </div>
      </div>
      <div class="precondition-grid">
        <button
          v-for="item in preconditionRows"
          :key="item.issue"
          class="precondition-card"
          :class="{ active: qualityFilter === item.issue }"
          @click="applyPrecondition(item)"
        >
          <span class="precondition-title">{{ issueLabel(item.issue) }}</span>
          <b>{{ fmtNumber(item.count) }}</b>
          <small>{{ preconditionHint(item.issue) }}</small>
          <em>{{ preconditionAction(item.issue) }}</em>
          <span v-if="item.sites?.length" class="precondition-sites">{{ item.sites.slice(0, 4).join(' / ') }}{{ item.sites.length > 4 ? ' ...' : '' }}</span>
        </button>
      </div>
    </div>

    <div class="table-wrap">
      <UTable class="tbl ui-table" :data="visibleRows" :columns="qualityColumns" :loading="loading" sticky="header" empty="当前筛选没有站点">
        <template #site-cell="{ row }">
          <b>{{ row.original.site }}</b>
          <small>{{ row.original.brand || '-' }} · {{ row.original.country || '-' }}</small>
          <small v-if="row.original.workspaces?.length">
            {{ row.original.workspaces.map((w: any) => w.name).join(' / ') }}
          </small>
        </template>

        <template #status-cell="{ row }">
          <StatusBadge :status="row.original.status" />
        </template>

        <template #sku_count-cell="{ row }">
          {{ fmtNumber(row.original.sku_count) }} / {{ fmtNumber(row.original.spu_count) }}
        </template>

        <template #coverage_pct-cell="{ row }">
          {{ row.original.coverage_pct }}% · {{ fmtNumber(row.original.fetched_count) }}/{{ fmtNumber(row.original.estimated_full) }}
          <small v-if="row.original.target_sku_count">
            目标 SKU {{ fmtNumber(row.original.target_sku_count) }} · 偏差 {{ row.original.sku_deviation_pct }}%
            · {{ row.original.target_sku_source === 'acceptance' ? '验收口径' : '工作区配置' }}
          </small>
        </template>

        <template #promotion_count-cell="{ row }">
          {{ fmtNumber(row.original.promotion_count) }}
        </template>

        <template #signals-cell="{ row }">
          标题 {{ row.original.title_quality_pct }}% · 类目 {{ row.original.category_signal_pct }}% · 图片 {{ row.original.image_signal_pct }}% · 价格 {{ row.original.price_signal_pct }}% · 销量 {{ row.original.sales_signal_pct }}% · 收入 {{ row.original.revenue_signal_pct }}%
          <small v-if="row.original.weak_title_count">弱标题 {{ fmtNumber(row.original.weak_title_count) }}</small>
          <small v-if="row.original.category_missing_count || row.original.image_missing_count">
            类目缺 {{ fmtNumber(row.original.category_missing_count) }} · 图片缺 {{ fmtNumber(row.original.image_missing_count) }}
          </small>
          <small v-if="row.original.currency_missing_count || row.original.currency_mismatch_count">
            币种 {{ row.original.expected_currency || '-' }} · 缺 {{ fmtNumber(row.original.currency_missing_count) }} · 错 {{ fmtNumber(row.original.currency_mismatch_count) }}
          </small>
          <small v-if="row.original.price_source_configured" class="source-ok">
            价格源 {{ row.original.price_source_type || '-' }} 已配置
          </small>
          <small>流量 {{ fmtNumber(row.original.traffic_signal_count) }} · 转化 {{ fmtNumber(row.original.conversion_signal_count) }}</small>
        </template>

        <template #crawl_queue-cell="{ row }">
          <div v-if="queueBadges(row.original).length" class="queue-badges">
            <span v-for="item in queueBadges(row.original)" :key="item.key" :class="['queue-badge', item.key]">
              {{ item.label }} {{ fmtNumber(item.value) }}
            </span>
          </div>
          <span v-else class="muted">无活跃/失败</span>
          <small v-if="!row.original.crawl_queue?.total" class="queue-empty">该站点还没有任何采集任务记录</small>
          <small v-if="row.original.crawl_queue?.oldest_active_at">
            最早 {{ fmtDate(row.original.crawl_queue.oldest_active_at) }}
          </small>
          <RouterLink class="queue-link" :to="{ path: '/queue', query: { source: 'crawl', dataset: row.original.site } }">
            队列明细
          </RouterLink>
          <a class="queue-link" :href="reportHref(row.original.site, undefined, row.original)" target="_blank" rel="noreferrer">
            打开报表
          </a>
        </template>

        <template #latest_job-cell="{ row }">
          <span v-if="row.original.latest_job">#{{ row.original.latest_job.id }} {{ row.original.latest_job.status }}</span>
          <span v-else>-</span>
          <small v-if="row.original.last_error_code" class="error-code">{{ row.original.last_error_code }}</small>
          <small v-if="row.original.last_error" class="last-error">{{ row.original.last_error }}</small>
          <small>{{ fmtDate(row.original.latest_job?.finished_at || row.original.last_product_updated || row.original.last_crawled) }}</small>
        </template>

        <template #issues-cell="{ row }">
          <div class="issues">
            <span v-for="issue in row.original.issues" :key="issue">{{ issueLabel(issue) }}</span>
            <span v-if="!row.original.issues?.length" class="ok">质量正常</span>
          </div>
        </template>

        <template #suggested_action-cell="{ row }">
          <span class="suggest">{{ row.original.suggested_action || '-' }}</span>
        </template>

        <template #actions-cell="{ row }">
          <div class="row-actions">
            <button class="btn small" @click="toggleDetail(row.original)">
              {{ detailSite === row.original.site ? '收起' : '明细' }}
            </button>
            <button
              class="btn small"
              :disabled="rerunBusy[row.original.site] || !isRerunnableQualityRow(row.original)"
              :title="isRerunnableQualityRow(row.original) ? '将该站点加入抓取队列' : (row.original.suggested_action || '当前问题不能靠重跑解决')"
              @click="rerunSites([row.original.site])"
            >
              {{ rerunBusy[row.original.site] ? '入队中...' : (isRerunnableQualityRow(row.original) ? '重跑' : '不可重跑') }}
            </button>
            <button
              v-if="shouldRebuildPromotions(row.original)"
              class="btn small promote"
              :disabled="promoBusy[row.original.site]"
              @click="rebuildPromotions([row.original.site])"
            >
              {{ promoBusy[row.original.site] ? '重算中...' : '重算促销' }}
            </button>
            <button
              v-if="shouldRecomputeAnalytics(row.original)"
              class="btn small"
              :disabled="analyticsBusy[row.original.site]"
              @click="recomputeAnalytics([row.original.site])"
            >
              {{ analyticsBusy[row.original.site] ? '重算中...' : '重算销量' }}
            </button>
            <button
              v-if="needsProxyRule(row.original)"
              class="btn small promote"
              :disabled="proxyRuleBusy[row.original.site]"
              @click="applyRecommendedProxyRules([row.original.site])"
            >
              {{ proxyRuleBusy[row.original.site] ? '应用中...' : '应用代理规则' }}
            </button>
            <button
              v-if="row.original.issues?.includes('pdp_price_required')"
              class="btn small"
              :disabled="configBusy"
              @click="openSiteConfig(row.original.site, 'pdp_price_required')"
            >
              配置采集
            </button>
            <button
              v-if="row.original.external_data_required && !row.original.issues?.includes('pdp_price_required')"
              class="btn small"
              @click="openExternalDataImport(row.original)"
            >
              导入指标
            </button>
          </div>
          <small v-if="rerunMessage[row.original.site]" class="rerun-msg">{{ rerunMessage[row.original.site] }}</small>
          <small v-if="promoMessage[row.original.site]" class="rerun-msg promo">{{ promoMessage[row.original.site] }}</small>
          <small v-if="analyticsMessage[row.original.site]" class="rerun-msg">{{ analyticsMessage[row.original.site] }}</small>
          <small v-if="proxyRuleMessage[row.original.site]" class="rerun-msg promo">{{ proxyRuleMessage[row.original.site] }}</small>
        </template>
      </UTable>
    </div>

    <div v-if="detailSite" class="detail-panel standalone-detail">
      <div class="detail-head">
        <b>
          {{ detailSite }} {{ detailTitle }}
          <span v-if="detailMeta.total !== undefined" class="detail-count">
            当前 {{ fmtNumber(detailStart) }}-{{ fmtNumber(detailEnd) }} / 共 {{ fmtNumber(detailMeta.total) }}
          </span>
        </b>
        <div class="detail-tabs">
          <button
            v-for="item in detailIssues"
            :key="item.key || 'all'"
            :class="['btn small', { active: detailIssue === item.key }]"
            :disabled="detailLoading"
            @click="switchDetailIssue(detailSite, item.key)"
          >
            {{ item.label }}{{ detailIssueCount(item.key) }}
          </button>
        </div>
      </div>
      <div v-if="detailError" class="error">{{ detailError }}</div>
      <div v-else-if="detailLoading" class="empty inline">加载明细中...</div>
      <UTable v-else-if="detailRows.length" class="detail-table ui-table" :data="detailRows" :columns="activeDetailColumns" sticky="header">
        <template #sku-cell="{ row }">
          <a v-if="row.original.product_url" :href="row.original.product_url" target="_blank" rel="noreferrer">{{ row.original.sku || '-' }}</a>
          <span v-else>{{ row.original.sku || '-' }}</span>
          <small>{{ row.original.spu || '-' }}</small>
          <a v-if="detailKind === 'product'" class="inline-link" :href="reportHref(row.original.site, row.original.id, activeDetailRow || undefined)" target="_blank" rel="noreferrer">趋势</a>
        </template>

        <template #title-cell="{ row }">
          <b>{{ row.original.title || '-' }}</b>
          <small>{{ row.original.category_path || '-' }}</small>
        </template>

        <template #sale_price-cell="{ row }">
          {{ fmtNumber(row.original.sale_price || row.original.original_price) }}
        </template>

        <template #currency-cell="{ row }">
          {{ row.original.currency || '-' }} / {{ row.original.expected_currency || '-' }}
        </template>

        <template #thirty_day_sales-cell="{ row }">
          {{ fmtNumber(row.original.thirty_day_sales) }}
          <small>{{ fmtNumber(row.original.thirty_day_revenue) }}</small>
        </template>

        <template #status-cell="{ row }">
          <span v-if="detailKind === 'product'">{{ row.original.status || '-' }}</span>
          <StatusBadge v-else :status="row.original.normalized_status || row.original.status" />
        </template>

        <template #created_time-cell="{ row }">
          {{ fmtDate(row.original.created_time) }}
          <small>{{ fmtDate(row.original.published_at) }} / {{ fmtDate(row.original.updated_time) }}</small>
        </template>

        <template #latest_job-cell="{ row }">
          <RouterLink v-if="row.original.latest_job?.id && detailKind === 'product'" :to="{ path: '/queue', query: { source: 'crawl', dataset: row.original.site, status: row.original.latest_job.status } }">
            #{{ row.original.latest_job.id }} {{ row.original.latest_job.status }}
          </RouterLink>
          <RouterLink v-else-if="row.original.latest_job?.id" :to="{ path: '/queue', query: { source: 'crawl', dataset: row.original.site } }">
            #{{ row.original.latest_job.id }} {{ row.original.latest_job.status }}
          </RouterLink>
          <span v-else>-</span>
          <small v-if="detailKind === 'product'">{{ row.original.suggested_action || '-' }}</small>
        </template>

        <template #id-cell="{ row }">
          <RouterLink :to="{ path: '/queue', query: { source: row.original.source || 'crawl', dataset: detailSite, status: row.original.normalized_status || row.original.status } }">
            #{{ row.original.id }}
          </RouterLink>
        </template>

        <template #normalized_status-cell="{ row }">
          <StatusBadge :status="row.original.normalized_status || row.original.status" />
        </template>

        <template #trigger-cell="{ row }">
          {{ row.original.trigger || '-' }}
        </template>

        <template #failure_code-cell="{ row }">
          {{ row.original.failure_code || '-' }}
        </template>

        <template #failure_stage-cell="{ row }">
          {{ row.original.failure_stage || '-' }}
        </template>

        <template #retryable-cell="{ row }">
          {{ row.original.retryable === true ? '可重试' : row.original.retryable === false ? '不可重试' : '-' }}
        </template>

        <template #products_count-cell="{ row }">
          {{ fmtNumber(row.original.products_count) }} / {{ fmtNumber(row.original.new_count) }} / {{ fmtNumber(row.original.promotion_count) }}
        </template>

        <template #started_at-cell="{ row }">
          {{ fmtDate(row.original.started_at) }} / {{ fmtDate(row.original.finished_at) }}
        </template>

        <template #error-cell="{ row }">
          {{ row.original.failure_detail || row.original.error || '-' }}
        </template>

        <template #site-cell="{ row }">
          <a v-if="row.original.url" :href="row.original.url" target="_blank" rel="noreferrer">{{ row.original.site }}</a>
          <span v-else>{{ row.original.site }}</span>
        </template>

        <template #sku_count-cell="{ row }">
          <template v-if="detailKind === 'site'">
            {{ fmtNumber(row.original.sku_count) }} / {{ fmtNumber(row.original.spu_count) }}
          </template>
          <template v-else>
            {{ fmtNumber(row.original.sku_count) }}
          </template>
        </template>

        <template #coverage_pct-cell="{ row }">
          {{ row.original.coverage_pct ?? 0 }}% · {{ fmtNumber(row.original.fetched_count) }}/{{ fmtNumber(row.original.estimated_full) }}
        </template>

        <template #target_sku_count-cell="{ row }">
          {{ row.original.target_sku_count ? fmtNumber(row.original.target_sku_count) : '-' }}
          <small v-if="row.original.target_sku_source">({{ row.original.target_sku_source }})</small>
        </template>

        <template #sku_deviation_pct-cell="{ row }">
          {{ row.original.sku_deviation_pct === null || row.original.sku_deviation_pct === undefined ? '-' : `${row.original.sku_deviation_pct}%` }}
        </template>

        <template #promotion_count-cell="{ row }">
          {{ fmtNumber(row.original.promotion_count) }}
        </template>

        <template #last_crawled-cell="{ row }">
          {{ fmtDate(row.original.last_crawled) }}
        </template>

        <template #last_product_updated-cell="{ row }">
          {{ fmtDate(row.original.last_product_updated) }}
        </template>

        <template #date-cell="{ row }">
          {{ row.original.date || '-' }}
        </template>

        <template #new_product_count-cell="{ row }">
          {{ fmtNumber(row.original.new_product_count) }}
        </template>

        <template #estimated_sales-cell="{ row }">
          {{ fmtNumber(row.original.estimated_sales) }}
        </template>

        <template #estimated_revenue-cell="{ row }">
          {{ fmtNumber(row.original.estimated_revenue) }}
        </template>

        <template #traffic-cell="{ row }">
          {{ fmtNumber(row.original.traffic) }}
        </template>

        <template #conversion_rate-cell="{ row }">
          {{ row.original.conversion_rate === null || row.original.conversion_rate === undefined ? '-' : `${row.original.conversion_rate}%` }}
        </template>

        <template #issues-cell="{ row }">
          <div class="issues compact">
            <span v-for="issue in row.original.issues" :key="issue">{{ issueLabel(issue) }}</span>
          </div>
          <small v-if="detailKind === 'product' && row.original.price_source_configured" class="source-ok">
            价格源 {{ row.original.price_source_type || '-' }} 已配置
          </small>
        </template>

        <template #suggested_action-cell="{ row }">
          {{ row.original.suggested_action || '-' }}
          <div v-if="detailKind === 'job'" class="resolution-flags">
            <span v-if="row.original.rerun_recommended">可重跑</span>
            <span v-if="row.original.rerun_after_setup" class="after-setup">修复后重跑</span>
            <span v-if="row.original.rerun_blocked" class="blocked">暂不可重跑</span>
            <span v-if="row.original.external_data_required" class="external">需外部数据</span>
            <span v-if="row.original.last_error_code" class="error-flag">{{ row.original.last_error_code }}</span>
            <span v-for="pre in row.original.rerun_preconditions || []" :key="pre" class="precondition">{{ issueLabel(pre) }}</span>
          </div>
          <small v-if="detailKind === 'job' && row.original.last_error" class="last-error">{{ row.original.last_error }}</small>
        </template>

        <template #note-cell="{ row }">
          {{ row.original.note || '-' }}
        </template>
      </UTable>
      <div v-else class="empty inline">当前筛选没有问题明细</div>
      <div v-if="detailMeta.total > detailLimit" class="detail-pager">
        <button class="btn small" :disabled="detailLoading || detailPage <= 1" @click="changeDetailPage(-1)">上一页</button>
        <span>第 {{ detailPage }} / {{ detailTotalPages }} 页</span>
        <button class="btn small" :disabled="detailLoading || detailPage >= detailTotalPages" @click="changeDetailPage(1)">下一页</button>
      </div>
    </div>

    <div v-if="configOpen" class="modal-mask" @click.self="configOpen = false">
      <div class="config-modal">
        <div class="config-head">
          <div>
            <h2>{{ configSite }} 采集配置</h2>
            <span>用于补充 PDP 价格源、接口/feed 入口和站点代理等级。</span>
          </div>
          <button class="btn small" @click="configOpen = false">关闭</button>
        </div>
        <div v-if="configError" class="error">{{ configError }}</div>
        <div v-if="configMessage" class="import-msg">{{ configMessage }}</div>
        <div v-if="configBusy" class="empty inline">读取/保存配置中...</div>
        <div class="config-grid">
          <label>Proxy Tier
            <USelect v-model="configForm.proxy_tier" class="select-ctl" :items="proxyTierItems" value-key="value" />
          </label>
          <label>Price Source Type
            <USelect v-model="priceSourceTypeSelect" class="select-ctl" :items="priceSourceTypeItems" value-key="value" />
          </label>
          <label class="wide">Price Feed URL
            <input v-model.trim="configForm.price_feed_url" class="ctl" placeholder="https://.../feed.csv 或私有文件路径" />
          </label>
          <label class="wide">PDP Price API URL
            <input v-model.trim="configForm.pdp_price_api_url" class="ctl" placeholder="https://api.example.com/price?sku={sku}" />
          </label>
          <label>PDP Price Selector
            <input v-model.trim="configForm.pdp_price_selector" class="ctl" placeholder=".price, [data-price]" />
          </label>
          <label>PDP Title Selector
            <input v-model.trim="configForm.pdp_title_selector" class="ctl" placeholder="h1, [data-product-title]" />
          </label>
          <label>Max Items
            <input v-model.trim="configForm.price_source_max_items" class="ctl" placeholder="50" />
          </label>
          <label>Use Proxy
            <USelect v-model="priceSourceUseProxySelect" class="select-ctl" :items="booleanModeItems" value-key="value" />
          </label>
          <label>Allow Stealth
            <USelect v-model="priceSourceAllowStealthSelect" class="select-ctl" :items="stealthModeItems" value-key="value" />
          </label>
          <label>Timeout
            <input v-model.trim="configForm.price_source_timeout" class="ctl" placeholder="30" />
          </label>
          <label>Retries
            <input v-model.trim="configForm.price_source_retries" class="ctl" placeholder="1" />
          </label>
          <label>SKU Field
            <input v-model.trim="configForm.price_feed_sku_field" class="ctl" placeholder="sku / product_id" />
          </label>
          <label>Sale Price Field
            <input v-model.trim="configForm.price_feed_sale_price_field" class="ctl" placeholder="price / final_price" />
          </label>
          <label>Original Price Field
            <input v-model.trim="configForm.price_feed_original_price_field" class="ctl" placeholder="regular_price / msrp" />
          </label>
          <label>Currency Field
            <input v-model.trim="configForm.price_feed_currency_field" class="ctl" placeholder="currency" />
          </label>
          <label>Title Field
            <input v-model.trim="configForm.price_feed_title_field" class="ctl" placeholder="title / product_name" />
          </label>
          <label class="wide">Notes
            <textarea v-model.trim="configForm.notes" spellcheck="false" placeholder="数据源说明、账号归属、验证方式" />
          </label>
        </div>
        <div v-if="configKeys.length" class="config-keys">
          已配置字段：{{ configKeys.join(' / ') }}
        </div>
        <div v-if="configTestResult" class="config-test">
          <div class="test-head">
            <b>测试结果：{{ configTestResult.status }}</b>
            <span>
              样例 {{ fmtNumber(configTestResult.sample_count) }} ·
              源行 {{ fmtNumber(configTestResult.stats?.rows) }} ·
              匹配 {{ fmtNumber(configTestResult.stats?.matched) }} ·
              可更新 {{ fmtNumber(configTestResult.stats?.updated) }}
            </span>
          </div>
          <div v-if="configTestResult.stats?.error" class="test-error">
            {{ configTestResult.stats.error }}
          </div>
          <div v-if="configTestResult.samples?.length" class="test-samples">
            <div v-for="item in configTestResult.samples.slice(0, 5)" :key="item.sku" class="test-sample">
              <b>{{ item.sku }}</b>
              <span>
                {{ item.before?.sale_price ?? '-' }} → {{ item.after?.sale_price ?? '-' }}
                <small v-if="item.after?.currency">· {{ item.after.currency }}</small>
                <small v-if="item.changed" class="source-ok">可补齐</small>
              </span>
            </div>
          </div>
        </div>
        <div class="config-actions">
          <button class="btn small" @click="configOpen = false">取消</button>
          <button class="btn small" :disabled="configBusy || configTestBusy" @click="testPriceSourceConfig">
            {{ configTestBusy ? '测试中...' : '测试价格源' }}
          </button>
          <button class="btn small primary" :disabled="configBusy" @click="saveSiteConfig">
            {{ configBusy ? '保存中...' : '保存配置' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { padding:24px; display:flex; flex-direction:column; gap:16px; }
.page-head { display:flex; align-items:center; justify-content:space-between; gap:16px; }
.page-title { font-size:20px; font-weight:600; }
.page-subtitle { margin-top:4px; font-size:12px; opacity:.55; }
.head-actions { display:flex; align-items:center; justify-content:flex-end; gap:10px; flex-wrap:wrap; }
.ctl { min-height:32px; padding:5px 10px; border-radius:7px; border:1px solid var(--ui-border, rgba(148,163,184,.32)); background:var(--admin-control-bg, #fff); color:inherit; font-size:12px; }
.inline-check { display:inline-flex; align-items:center; gap:6px; font-size:12px; opacity:.78; white-space:nowrap; }
.import-panel { position:relative; z-index:1; display:flex; flex-direction:column; gap:10px; padding:12px; border:1px solid var(--ui-border, rgba(148,163,184,.32)); border-radius:12px; background:var(--admin-panel-soft, #f8fafc); }
.import-head { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.import-head b { font-size:13px; }
.import-head span { font-size:12px; color:var(--ui-muted, #9ca3af); }
.import-panel textarea { width:100%; min-height:96px; resize:vertical; padding:10px 12px; border-radius:8px; border:1px solid var(--ui-border, rgba(148,163,184,.32)); background:var(--admin-control-bg, #fff); color:inherit; font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size:12px; line-height:1.45; box-sizing:border-box; }
.import-actions { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.import-msg { color:var(--ui-muted, #9ca3af); font-size:12px; }
.validation-panel { display:flex; flex-direction:column; gap:8px; padding:10px 12px; border-radius:8px; border:1px solid rgba(148,163,184,.24); background:var(--admin-panel-muted, #f1f5f9); }
.validation-head { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.validation-head b { color:var(--admin-success-text, #047857); font-size:13px; }
.validation-head span,
.validation-errors { color:var(--ui-muted, #9ca3af); font-size:12px; line-height:1.45; }
.validation-errors { color:var(--admin-danger-text, #b91c1c); }
.acceptance-panel { display:flex; flex-direction:column; gap:10px; padding:12px; border:1px solid var(--ui-border, rgba(148,163,184,.32)); border-radius:10px; background:var(--admin-panel, #fff); }
.acceptance-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; color:var(--ui-muted, #9ca3af); font-size:12px; }
.acceptance-head b { display:block; color:var(--ui-text, #0f172a); font-size:13px; }
.acceptance-head span { line-height:1.45; }
.acceptance-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(118px,1fr)); gap:8px; }
.acceptance-stat { min-height:62px; display:flex; flex-direction:column; justify-content:center; gap:5px; padding:9px 10px; border:1px solid rgba(148,163,184,.24); border-radius:8px; background:var(--admin-panel-muted, #f1f5f9); }
.acceptance-stat span { color:var(--ui-muted, #9ca3af); font-size:12px; }
.acceptance-stat b { color:var(--ui-text, #0f172a); font-size:20px; line-height:1; }
.acceptance-stat.pass b { color:var(--admin-success-text, #047857); }
.acceptance-stat.fail b { color:var(--admin-danger-text, #b91c1c); }
.acceptance-stat.warn b { color:var(--admin-warn-text, #b45309); }
.acceptance-actions { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; }
.acceptance-action { min-height:50px; display:flex; align-items:center; justify-content:space-between; gap:8px; padding:8px 10px; border:1px solid rgba(148,163,184,.24); border-radius:8px; background:transparent; color:inherit; cursor:pointer; }
.acceptance-action:hover { border-color:rgba(14,165,233,.52); background:rgba(14,165,233,.08); }
.acceptance-action span { color:var(--ui-muted, #9ca3af); font-size:12px; }
.acceptance-action b { color:var(--ui-text, #0f172a); font-size:18px; }
.acceptance-template-actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.acceptance-items { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:8px; }
.acceptance-item { min-height:74px; display:flex; flex-direction:column; align-items:flex-start; gap:4px; padding:9px 10px; border:1px solid rgba(148,163,184,.24); border-radius:8px; background:transparent; color:inherit; text-align:left; cursor:pointer; }
.acceptance-item:hover { border-color:rgba(139,92,246,.52); background:rgba(139,92,246,.10); }
.acceptance-item b { font-size:12px; }
.acceptance-item span { color:var(--admin-warn-text, #b45309); font-size:11px; font-weight:700; }
.acceptance-item small { color:var(--ui-muted, #9ca3af); font-size:12px; line-height:1.35; }
.stat-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(128px,1fr)); gap:12px; }
.stat-filter { display:block; padding:0; border:0; background:transparent; color:inherit; text-align:left; cursor:pointer; }
.stat-filter :deep(.stat-card) { height:100%; transition:border-color .16s ease, background .16s ease; }
.stat-filter:hover :deep(.stat-card), .stat-filter.active :deep(.stat-card) { border-color:rgba(139,92,246,.62); background:rgba(139,92,246,.12); }
.precondition-panel { display:flex; flex-direction:column; gap:10px; padding:12px; border:1px solid var(--ui-border, rgba(148,163,184,.32)); border-radius:10px; background:var(--admin-panel-soft, #f8fafc); }
.precondition-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; color:var(--ui-muted, #9ca3af); font-size:12px; }
.precondition-head b { display:block; color:var(--ui-text, #0f172a); font-size:13px; }
.precondition-head span { line-height:1.45; }
.precondition-actions { display:flex; align-items:center; justify-content:flex-end; gap:8px; flex-wrap:wrap; max-width:520px; }
.precondition-message { flex:0 0 100%; color:var(--admin-success-text, #047857); text-align:right; font-size:12px; line-height:1.35; }
.precondition-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px; }
.precondition-card { min-height:138px; display:flex; flex-direction:column; align-items:flex-start; gap:6px; padding:10px; border:1px solid var(--ui-border, rgba(148,163,184,.32)); border-radius:8px; background:var(--admin-panel, #fff); color:inherit; text-align:left; cursor:pointer; }
.precondition-card:hover, .precondition-card.active { border-color:rgba(139,92,246,.52); background:rgba(139,92,246,.12); }
.precondition-title { color:var(--ui-text, #0f172a); font-size:12px; font-weight:700; }
.precondition-card b { color:var(--ui-text, #0f172a); font-size:22px; line-height:1; }
.precondition-card small { color:var(--ui-muted, #9ca3af); font-size:12px; line-height:1.35; }
.precondition-card em { margin-top:auto; color:#a78bfa; font-size:12px; font-style:normal; font-weight:700; }
.precondition-sites { width:100%; color:var(--ui-muted, #9ca3af); font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.table-wrap { position:relative; z-index:0; overflow:auto; border:1px solid var(--ui-border, rgba(148,163,184,.32)); border-radius:12px; background:var(--admin-panel, #fff); }
.tbl { width:100%; min-width:1440px; border-collapse:collapse; font-size:13px; }
.tbl th,.tbl td { padding:10px 12px; text-align:left; border-bottom:1px solid var(--ui-border, rgba(255,255,255,.06)); vertical-align:top; }
.tbl th { font-weight:600; opacity:.7; white-space:nowrap; }
.tbl b { display:block; }
.tbl small { display:block; margin-top:3px; opacity:.58; white-space:nowrap; }
.issues { display:flex; flex-wrap:wrap; gap:5px; min-width:190px; }
.issues span { display:inline-flex; padding:2px 7px; border-radius:999px; color:var(--admin-danger-text, #b91c1c); background:rgba(248,113,113,.13); border:1px solid rgba(248,113,113,.24); font-size:11px; font-weight:700; }
.issues span.ok { color:var(--admin-success-text, #047857); background:rgba(16,185,129,.13); border-color:rgba(16,185,129,.24); }
.issues.compact { min-width:0; }
.queue-badges { display:flex; flex-wrap:wrap; gap:5px; min-width:140px; }
.queue-badge { display:inline-flex; align-items:center; gap:3px; padding:2px 7px; border-radius:999px; font-size:11px; font-weight:700; border:1px solid rgba(148,163,184,.28); background:rgba(148,163,184,.12); color:var(--ui-text, #0f172a); }
.queue-badge.pending { color:var(--admin-warn-text, #b45309); background:rgba(245,158,11,.14); border-color:rgba(245,158,11,.28); }
.queue-badge.stale { color:var(--admin-warn-text, #b45309); background:rgba(217,119,6,.14); border-color:rgba(217,119,6,.28); }
.queue-badge.running { color:var(--admin-info-text, #2563eb); background:rgba(59,130,246,.14); border-color:rgba(59,130,246,.28); }
.queue-badge.stuck { color:var(--admin-danger-text, #b91c1c); background:rgba(248,113,113,.15); border-color:rgba(248,113,113,.3); }
.queue-badge.failed { color:#c2410c; background:rgba(249,115,22,.14); border-color:rgba(249,115,22,.3); }
.queue-badge.blocked { color:#be185d; background:rgba(236,72,153,.13); border-color:rgba(236,72,153,.28); }
.queue-badge.skipped { color:#6d28d9; background:rgba(124,58,237,.13); border-color:rgba(124,58,237,.28); }
.muted { opacity:.55; white-space:nowrap; }
.queue-empty { color:var(--admin-warn-text, #b45309); opacity:.86 !important; white-space:normal !important; }
.queue-link { display:inline-flex; margin-top:4px; font-size:12px; color:#a78bfa; text-decoration:none; }
.queue-link:hover { text-decoration:underline; }
.inline-link { display:inline-flex; margin-top:3px; color:#a78bfa; font-size:12px; text-decoration:none; }
.inline-link:hover { text-decoration:underline; }
.source-ok { color:var(--admin-success-text, #047857) !important; opacity:.95 !important; }
.suggest { max-width:260px; line-height:1.45; }
.error-code { color:var(--admin-danger-text, #b91c1c); opacity:1 !important; font-weight:700; }
.last-error { max-width:260px; white-space:normal !important; line-height:1.35; color:var(--ui-muted, #9ca3af); opacity:.78 !important; }
.btn.small { min-height:32px; padding:5px 10px; border-radius:6px; font-size:12px; border:1px solid var(--ui-border, rgba(148,163,184,.32)); background:transparent; color:inherit; cursor:pointer; }
.btn.small.primary { border-color:rgba(139,92,246,.45); color:#fff; background:rgba(139,92,246,.85); }
.btn.small.promote { border-color:rgba(20,184,166,.38); color:var(--admin-success-text, #047857); background:rgba(20,184,166,.12); }
.btn.small.promote:hover:not(:disabled) { background:rgba(20,184,166,.2); }
.btn.small.active { border-color:rgba(139,92,246,.55); color:#fff; background:rgba(139,92,246,.28); }
.btn:disabled { opacity:.55; cursor:not-allowed; }
.row-actions { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.detail-panel { border:1px solid var(--ui-border, rgba(148,163,184,.32)); border-radius:10px; padding:12px; background:var(--admin-panel, #fff); }
.standalone-detail { overflow:auto; }
.detail-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:10px; }
.detail-count { margin-left:8px; color:var(--ui-muted, #9ca3af); font-size:12px; font-weight:500; }
.detail-tabs { display:flex; align-items:center; justify-content:flex-end; gap:6px; flex-wrap:wrap; }
.detail-table { width:100%; border-collapse:collapse; font-size:12px; }
.detail-table th,.detail-table td { padding:8px 10px; border-bottom:1px solid var(--ui-border, rgba(255,255,255,.06)); vertical-align:top; }
.detail-table th { opacity:.62; font-weight:600; }
.detail-table a { color:#a78bfa; text-decoration:none; }
.detail-table a:hover { text-decoration:underline; }
.modal-mask { position:fixed; inset:0; z-index:120; display:flex; align-items:center; justify-content:center; padding:18px; background:var(--admin-overlay, rgba(15,23,42,.42)); backdrop-filter:blur(5px); }
.config-modal { width:min(760px, calc(100vw - 36px)); max-height:calc(100vh - 36px); overflow:auto; display:flex; flex-direction:column; gap:12px; padding:16px; border:1px solid var(--ui-border, rgba(148,163,184,.32)); border-radius:10px; background:var(--ui-panel, #fff); box-shadow:0 24px 80px rgba(15,23,42,.24); color:var(--ui-text, #0f172a); }
.config-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
.config-head h2 { margin:0; font-size:16px; font-weight:700; }
.config-head span { display:block; margin-top:4px; color:var(--ui-muted, #9ca3af); font-size:12px; }
.config-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
.config-grid label { display:flex; flex-direction:column; gap:5px; color:var(--ui-muted, #9ca3af); font-size:12px; }
.config-grid .wide { grid-column:1 / -1; }
.config-grid textarea { min-height:78px; resize:vertical; padding:8px 10px; border:1px solid var(--ui-border, rgba(148,163,184,.32)); border-radius:7px; background:var(--admin-control-bg, #fff); color:inherit; font:inherit; font-size:12px; line-height:1.45; }
.config-keys { color:var(--ui-muted, #9ca3af); font-size:12px; line-height:1.45; }
.config-test { display:flex; flex-direction:column; gap:8px; padding:10px 12px; border:1px solid rgba(34,197,94,.22); border-radius:8px; background:rgba(22,101,52,.08); }
.test-head { display:flex; justify-content:space-between; align-items:baseline; gap:10px; flex-wrap:wrap; }
.test-head b { font-size:13px; color:var(--admin-success-text, #047857); }
.test-head span,
.test-sample span { color:var(--ui-muted, #9ca3af); font-size:12px; }
.test-error { color:var(--admin-danger-text, #b91c1c); font-size:12px; line-height:1.45; }
.test-samples { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:6px; }
.test-sample { display:flex; justify-content:space-between; gap:8px; padding:6px 8px; border-radius:6px; background:var(--admin-panel-muted, #f1f5f9); font-size:12px; }
.test-sample b { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.config-actions { display:flex; justify-content:flex-end; gap:8px; }
.resolution-flags { display:flex; flex-wrap:wrap; gap:5px; margin-top:6px; }
.resolution-flags span { display:inline-flex; align-items:center; min-height:20px; padding:2px 7px; border-radius:999px; border:1px solid rgba(34,197,94,.28); color:#86efac; background:rgba(34,197,94,.12); font-size:11px; font-weight:700; white-space:nowrap; }
.resolution-flags span.blocked { border-color:rgba(248,113,113,.28); color:var(--admin-danger-text, #b91c1c); background:rgba(248,113,113,.12); }
.resolution-flags span.external { border-color:rgba(251,191,36,.32); color:var(--admin-warn-text, #b45309); background:rgba(251,191,36,.12); }
.resolution-flags span.after-setup { border-color:rgba(59,130,246,.30); color:var(--admin-info-text, #2563eb); background:rgba(59,130,246,.12); }
.resolution-flags span.precondition { border-color:rgba(148,163,184,.28); color:var(--ui-muted, #9ca3af); background:rgba(148,163,184,.10); }
.resolution-flags span.error-flag { border-color:rgba(167,139,250,.30); color:#6d28d9; background:rgba(167,139,250,.12); }
.detail-pager { display:flex; justify-content:flex-end; align-items:center; gap:8px; margin-top:10px; color:var(--ui-muted, #9ca3af); font-size:12px; }
.rerun-msg { display:block; margin-top:4px; color:var(--ui-muted, #9ca3af); max-width:140px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.rerun-msg.promo { color:#5eead4; }
.error { font-size:13px; color:#ef4444; }
.empty { text-align:center; opacity:.6; padding:24px; }
.empty.inline { padding:12px; }
@media (max-width:1100px) {
  .stat-row { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .config-grid { grid-template-columns:1fr; }
}
</style>
