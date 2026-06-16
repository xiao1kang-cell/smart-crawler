<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { asList, fmtDate, fmtPrice, qs } from '../api/client'
import { listProducts, listPromotions, listSites, productTrend, siteOverview } from '../api/products'
import { useAuthStore } from '../stores/auth'
import DataLoadingPanel from '../components/common/DataLoadingPanel.vue'
import PageLoading from '../components/common/PageLoading.vue'

const TrendLineChart = defineAsyncComponent(() => import('../components/charts/TrendLineChart.vue'))

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const routeWorkspaceId = String(route.query.workspace_id || '')
if (routeWorkspaceId && routeWorkspaceId !== auth.workspaceId) auth.setWorkspace(routeWorkspaceId)
const loading = ref(false)
const error = ref('')
const tab = ref<'shop' | 'product' | 'promo'>('shop')
const subTab = ref('all')
const search = ref('')
const cfgOpen = ref(false)
// 产品筛选(11 区,后端 /products 已支持)
const filtersOpen = ref(false)
function emptyFilters() {
  return {
    status: '', category: '',
    min_rating: '', max_rating: '', min_reviews: '', max_reviews: '',
    min_price: '', max_price: '', min_sales: '', max_sales: '',
    min_revenue: '', max_revenue: '',
    min_variants: '', max_variants: '',
    has_video: '', free_shipping: '',
    created_from: '', created_to: '',
  }
}
const filters = ref<Record<string, string>>(emptyFilters())
const activeFilterCount = computed(() => Object.values(filters.value).filter((v) => v !== '' && v !== null).length)
function applyFilters() {
  page.value = 1
  filtersOpen.value = false
  loadReport()
}
function resetFilters() {
  filters.value = emptyFilters()
  page.value = 1
  loadReport()
}
const sites = ref<Record<string, any>[]>([])
const site = ref(String(route.query.site || localStorage.getItem('sc_report_site') || ''))
const overview = ref<Record<string, any> | null>(null)
const products = ref<Record<string, any>[]>([])
const promotions = ref<Record<string, any>[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const promoTotal = ref(0)
const promoPage = ref(1)
const promoPageSize = ref(20)
const promoTotalPages = computed(() => Math.max(1, Math.ceil(promoTotal.value / Number(promoPageSize.value || 20))))
const promoSearch = ref('')
const promoType = ref('')
const promoDateFrom = ref('')
const promoDateTo = ref('')
const dateRange = ref({ from: '', to: '' })
const lastUpdate = ref('—')
const DEFAULT_CFG = {
  sections: { kpi: true, trend: true, products: true, promos: true },
  kpiCards: { sku: true, new: true, sales: true, revenue: true, traffic: true, conversion: true },
  productCols: {
    sku: true, title: true, label: true, variantId: true, variantCount: true, attrs: true,
    salePrice: true, price: true, sales: true, revenue: true,
    rating: true, reviews: true, status: true, category: true,
    inventory: true, video: true, freeShipping: true,
    createdTime: true, updatedTime: true, action: true,
  },
  timeRange: '30d'
}
function cloneCfg() {
  return JSON.parse(JSON.stringify(DEFAULT_CFG))
}
function loadCfg() {
  try {
    const raw = localStorage.getItem('sc_report_cfg')
    if (!raw) return cloneCfg()
    const saved = JSON.parse(raw)
    return {
      sections: { ...DEFAULT_CFG.sections, ...(saved.sections || {}) },
      kpiCards: { ...DEFAULT_CFG.kpiCards, ...(saved.kpiCards || {}) },
      productCols: { ...DEFAULT_CFG.productCols, ...(saved.productCols || {}) },
      timeRange: saved.timeRange || '30d'
    }
  } catch {
    return cloneCfg()
  }
}
const cfg = ref(loadCfg())
const activeSite = computed(() => sites.value.find((x) => (x.site || x.name) === site.value) || { site: site.value })
const visibleProductColumnCount = computed(() => Object.values(cfg.value.productCols).filter(Boolean).length)
const cards = computed<Record<string, any>>(() => {
  const data = overview.value || {}
  return data.cards && typeof data.cards === 'object' ? data.cards : data
})
const reportCurrency = computed(() => cards.value.currency || overview.value?.currency || activeSite.value.currency || null)
const trends = computed<Record<string, any>[]>(() => asList(overview.value?.trends || [], ['trends', 'items']))
const trendSummary = computed<Record<string, any>>(() => overview.value?.trend_summary || {})
const storeCurrentPeriod = computed(() => trendSummary.value?.current_period || null)
const storePreviousPeriod = computed(() => trendSummary.value?.previous_period || null)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / Number(pageSize.value || 10))))
const initialReportLoading = computed(() => loading.value && !overview.value && !products.value.length && !promotions.value.length)
const granularity = ref<'day' | 'week' | 'month'>('month')
const aggregatedTrends = computed<Record<string, any>[]>(() => trends.value)

async function loadSites() {
  sites.value = asList(await listSites(), ['sites', 'items'])
  if (!site.value && sites.value[0]) site.value = sites.value[0].site || sites.value[0].name
}

async function loadReport() {
  if (!auth.token) {
    window.location.href = '/app'
    return
  }
  if (!site.value) return
  loading.value = true
  error.value = ''
  try {
    localStorage.setItem('sc_report_site', site.value)
    const workspaceId = String(route.query.workspace_id || auth.workspaceId || '')
    router.replace({ path: '/report', query: { site: site.value, ...(workspaceId ? { workspace_id: workspaceId } : {}) } })
    const overviewParams = {
      granularity: granularity.value,
      date_from: dateRange.value.from,
      date_to: dateRange.value.to,
    }
    const productParams: Record<string, unknown> = { site: site.value, page: page.value, page_size: pageSize.value }
    if (search.value) productParams.search = search.value
    // 子 tab → 后端唯一认的 tab 参数(all|bestseller|new)
    if (subTab.value === 'new' || subTab.value === 'bestseller') productParams.tab = subTab.value
    // 11 区筛选:仅透传非空值(qs() 会丢弃 undefined/''，这里同样跳过空串)
    for (const [k, v] of Object.entries(filters.value)) {
      if (v !== '' && v !== null && v !== undefined) productParams[k] = v
    }
    const [overviewData, productsData, promosData] = await Promise.all([
      siteOverview(site.value, overviewParams),
      listProducts(productParams),
      listPromotions({
        site: site.value,
        page: promoPage.value,
        page_size: promoPageSize.value,
        search: promoSearch.value.trim(),
        type: promoType.value,
        date_from: promoDateFrom.value,
        date_to: promoDateTo.value,
      })
    ])
    overview.value = overviewData
    products.value = asList(productsData, ['items', 'products'])
    promotions.value = asList(promosData, ['items', 'promotions'])
    promoTotal.value = Number(promosData?.total || promotions.value.length || 0)
    const nextCards = overviewData?.cards && typeof overviewData.cards === 'object' ? overviewData.cards : overviewData
    total.value = Number(productsData?.total || nextCards?.sku_count || nextCards?.total_products || products.value.length || 0)
    const updateTime = overviewData?.last_run || overviewData?.updated_at
    lastUpdate.value = updateTime ? fmtDate(updateTime) : '—'
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function productQueryParams() {
  const params: Record<string, unknown> = { site: site.value, token: auth.token, workspace_id: auth.workspaceId }
  if (search.value.trim()) params.search = search.value.trim()
  if (subTab.value === 'new' || subTab.value === 'bestseller') params.tab = subTab.value
  for (const [k, v] of Object.entries(filters.value)) {
    if (v !== '' && v !== null && v !== undefined) params[k] = v
  }
  return params
}

function exportProducts() {
  window.open(`/api/export/products${qs(productQueryParams())}`, '_blank')
}

function exportPromotions() {
  window.open(`/api/export/promotions${qs({
    site: site.value,
    token: auth.token,
    workspace_id: auth.workspaceId,
    search: promoSearch.value.trim(),
    type: promoType.value,
    date_from: promoDateFrom.value,
    date_to: promoDateTo.value,
  })}`, '_blank')
}

function applyPromoFilters() {
  promoPage.value = 1
  loadReport()
}

function resetPromoFilters() {
  promoSearch.value = ''
  promoType.value = ''
  promoDateFrom.value = ''
  promoDateTo.value = ''
  applyPromoFilters()
}

function productTitle(p: Record<string, any>) {
  return p.title || p.name || p.product_title || p.spu || p.sku || p.item_id || `商品 #${p.id}`
}

function yesNo(value: unknown) {
  if (value === true) return 'YES'
  if (value === false) return 'NO'
  return '--'
}

function shortDate(value: unknown) {
  if (!value) return '--'
  return String(value).slice(0, 16).replace('T', ' ')
}

function attrEntries(p: Record<string, any>) {
  const attrs = p.attributes && typeof p.attributes === 'object' ? p.attributes : {}
  return Object.entries(attrs).filter(([, v]) => v !== null && v !== undefined && String(v) !== '')
}

function productLabels(p: Record<string, any>) {
  const labels = []
  if (p.label) labels.push(String(p.label))
  if (p.is_new) labels.push('NEW')
  if (p.is_bestseller) labels.push('TOP')
  return labels
}

function promoTitle(p: Record<string, any>) {
  return p.product_title || p.title || p.promotion_name || p.sku || '--'
}

function promoName(p: Record<string, any>) {
  return p.promotion_name || p.name || p.promotion_type || p.type || '--'
}

// 商品详情 + 价格历史弹窗
const detail = ref<Record<string, any> | null>(null)
const priceHistory = ref<Record<string, any>[]>([])
const productTrendDetail = ref<Record<string, any> | null>(null)
const detailLoading = ref(false)
const trendGranularity = ref<'day' | 'week' | 'month'>('month')
const trendDateFrom = ref('')
const trendDateTo = ref('')
const trendPromoSearch = ref('')
const trendPromoType = ref('')
const productTrendSeries = [
  { key: 'estimated_sales', name: '销量', color: '#f59e0b' },
  { key: 'estimated_revenue', name: '收入', color: '#ef4444' },
  { key: 'sale_price', name: '售价', color: '#3b82f6' },
  { key: 'review_total', name: '评论数', color: '#8b5cf6' },
  { key: 'avg_rating', name: '评分', color: '#ec4899', yAxisIndex: 1 },
]
const productTrendRows = computed(() => productTrendDetail.value?.trend || priceHistory.value || [])
const productPromotions = computed(() => productTrendDetail.value?.promotions || [])
const productSummary = computed(() => productTrendDetail.value?.summary || {})
const productCurrentPeriod = computed(() => productSummary.value?.current_period || null)
const productPreviousPeriod = computed(() => productSummary.value?.previous_period || null)
function productTrendParams(includeToken = false) {
  return {
    ...(includeToken ? { token: auth.token, workspace_id: auth.workspaceId } : {}),
    granularity: trendGranularity.value,
    date_from: trendDateFrom.value,
    date_to: trendDateTo.value,
    promo_search: trendPromoSearch.value.trim(),
    promo_type: trendPromoType.value,
  }
}
async function openDetail(id: number | string | undefined) {
  if (id === undefined || id === null) return
  detail.value = null
  priceHistory.value = []
  productTrendDetail.value = null
  detailLoading.value = true
  try {
    const trend = await productTrend(id, productTrendParams())
    productTrendDetail.value = trend
    detail.value = trend?.product || null
    priceHistory.value = asList(trend?.trend || [], ['items', 'history'])
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    detailLoading.value = false
  }
}
async function reloadProductTrend() {
  if (!detail.value?.id) return
  detailLoading.value = true
  try {
    productTrendDetail.value = await productTrend(detail.value.id, productTrendParams())
    priceHistory.value = asList(productTrendDetail.value?.trend || [], ['items', 'history'])
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    detailLoading.value = false
  }
}
function resetProductTrendFilters() {
  trendDateFrom.value = ''
  trendDateTo.value = ''
  trendPromoSearch.value = ''
  trendPromoType.value = ''
  reloadProductTrend()
}
function exportProductTrend() {
  if (!detail.value?.id) return
  window.open(`/api/export/product-trend${qs({ pid: detail.value.id, ...productTrendParams(true) })}`, '_blank')
}
function storeDelta(key: string, format: 'number' | 'price' | 'percent' = 'number') {
  const cur = Number(storeCurrentPeriod.value?.[key] || 0)
  const prev = Number(storePreviousPeriod.value?.[key] || 0)
  if (!storePreviousPeriod.value) return '无上周期'
  const delta = cur - prev
  if (format === 'price') return `${delta >= 0 ? '+' : ''}${fmtPrice(delta, reportCurrency.value)} vs 上周期`
  if (format === 'percent') return `${delta >= 0 ? '+' : ''}${delta.toFixed(2)} vs 上周期`
  return `${delta >= 0 ? '+' : ''}${delta.toLocaleString()} vs 上周期`
}
function periodDelta(key: string) {
  const cur = Number(productCurrentPeriod.value?.[key] || 0)
  const prev = Number(productPreviousPeriod.value?.[key] || 0)
  if (!productPreviousPeriod.value) return '无上周期'
  const delta = cur - prev
  return `${delta >= 0 ? '+' : ''}${delta.toLocaleString()} vs 上周期`
}
function closeDetail() {
  detail.value = null
  priceHistory.value = []
  productTrendDetail.value = null
}

function saveCfg() {
  localStorage.setItem('sc_report_cfg', JSON.stringify(cfg.value))
  cfgOpen.value = false
}

function resetCfg() {
  cfg.value = cloneCfg()
  localStorage.removeItem('sc_report_cfg')
}

watch(site, () => {
  page.value = 1
  promoPage.value = 1
  loadReport()
})
watch([page, pageSize, subTab], loadReport)
watch([promoPage, promoPageSize], loadReport)
watch([granularity, () => dateRange.value.from, () => dateRange.value.to], loadReport)
// 权限:viewer 只读,可查看面板但编辑动作(导出/自定义报表)受限。
// 规格"仅有权限人员可查看/编辑"——查看放开,编辑按角色门控。
const canEdit = computed(() => {
  const u = auth.user
  if (!u) return false
  if (u.global_role === 'super_admin' || u.role === 'admin' || u.role === 'owner') return true
  return ['admin', 'owner', 'operator'].includes(u.workspace_role || u.role || '')
})
onMounted(async () => {
  if (auth.token && !auth.user) await auth.loadMe().catch(() => null)
  await loadSites()
  await loadReport()
})
</script>

<template>
  <main class="report-page">
    <section class="report-container">
      <div class="crumb">
        <div><a href="/app">首页</a> &gt; <b>{{ site }} | {{ (site || '').split('_')[1]?.toUpperCase() || 'US' }}</b></div>
        <div class="last-update">最后更新时间: {{ lastUpdate }}</div>
      </div>

      <div class="site-card">
        <div class="top">
          <div>
            <h2>{{ site }} | {{ (site || '').split('_')[1]?.toUpperCase() || 'US' }}</h2>
            <a class="url" :href="activeSite.url || '#'" target="_blank">{{ activeSite.url || site }}</a>
          </div>
          <div class="date-picker">
            <select v-model="site">
              <option v-for="s in sites" :key="s.site || s.name" :value="s.site || s.name">{{ s.site || s.name }} ({{ s.brand || 'brand' }})</option>
            </select>
            <button v-if="canEdit" class="icon-btn" @click="cfgOpen = true">⚙ 自定义</button>
            <button class="icon-btn" @click="loadReport">↻ 刷新</button>
          </div>
        </div>
        <div class="meta">
          <span>总 SKU 行: <b>{{ cards.sku_count || total || '--' }}</b></span>
          <span>总类别数: <b>{{ cards.category_count != null ? cards.category_count : '--' }}</b></span>
        </div>
      </div>

      <div class="tab-row">
        <button :class="{ active: tab === 'shop' }" @click="tab = 'shop'">🏬 店铺分析</button>
        <button :class="{ active: tab === 'product' }" @click="tab = 'product'">📦 产品分析</button>
        <button :class="{ active: tab === 'promo' }" @click="tab = 'promo'">🎁 销售促销</button>
      </div>

      <UAlert v-if="error" color="error" variant="soft" :title="error" class="mb-4" />

      <PageLoading v-if="initialReportLoading" title="加载站点报表..." note="正在汇总概览、商品和促销数据" />

      <template v-else>
      <template v-if="tab === 'shop'">
        <div v-if="cfg.sections.kpi" class="stats">
          <div v-if="cfg.kpiCards.sku" class="stat"><div class="lbl">SKU</div><div class="val">{{ (storeCurrentPeriod?.sku_count ?? cards.sku_count) ? Number(storeCurrentPeriod?.sku_count ?? cards.sku_count).toLocaleString() : '--' }}</div><div class="sub">{{ storeDelta('sku_count') }}</div></div>
          <div v-if="cfg.kpiCards.new" class="stat"><div class="lbl">新增产品</div><div class="val">{{ (storeCurrentPeriod?.new_product_count ?? cards.new_product_count) ? Number(storeCurrentPeriod?.new_product_count ?? cards.new_product_count).toLocaleString() : '--' }}</div><div class="sub">{{ storeDelta('new_product_count') }}</div></div>
          <div v-if="cfg.kpiCards.sales" class="stat"><div class="lbl">30天销量</div><div class="val">{{ (storeCurrentPeriod?.estimated_sales ?? cards.thirty_day_sales) ? Number(storeCurrentPeriod?.estimated_sales ?? cards.thirty_day_sales).toLocaleString() : '--' }}</div><div class="sub">{{ storeDelta('estimated_sales') }}</div></div>
          <div v-if="cfg.kpiCards.revenue" class="stat"><div class="lbl">30天收入</div><div class="val">{{ (storeCurrentPeriod?.estimated_revenue ?? cards.thirty_day_revenue) ? fmtPrice(storeCurrentPeriod?.estimated_revenue ?? cards.thirty_day_revenue, reportCurrency) : '--' }}</div><div class="sub">{{ storeDelta('estimated_revenue', 'price') }}</div></div>
          <div v-if="cfg.kpiCards.traffic" class="stat"><div class="lbl">评论总数</div><div class="val">{{ storeCurrentPeriod?.review_total != null ? Number(storeCurrentPeriod.review_total).toLocaleString() : '--' }}</div><div class="sub">{{ storeDelta('review_total') }}</div></div>
          <div v-if="cfg.kpiCards.conversion" class="stat"><div class="lbl">平均评分</div><div class="val">{{ storeCurrentPeriod?.avg_rating != null ? Number(storeCurrentPeriod.avg_rating).toFixed(2) : '--' }}</div><div class="sub">{{ storeDelta('avg_rating', 'percent') }}</div></div>
        </div>

        <div v-if="cfg.sections.trend" class="section">
          <div class="section-head">
            <h3>📈 销售趋势 <span class="desc">分析整体销售情况和品牌市场份额</span></h3>
            <div class="actions">
              <select v-model="granularity" class="gran-select">
                <option value="month">按月</option>
                <option value="week">按周</option>
                <option value="day">按天</option>
              </select>
              <input v-model="dateRange.from" class="date-input" type="date" />
              <span class="range-sep">→</span>
              <input v-model="dateRange.to" class="date-input" type="date" />
              <span class="range-note">{{ trendSummary.visible_points || 0 }} 点</span>
            </div>
          </div>
          <div class="chart-wrap">
            <TrendLineChart v-if="aggregatedTrends.length" :rows="aggregatedTrends" />
            <div v-else-if="loading" class="loading">趋势数据加载中…</div>
            <div v-else class="loading">暂无趋势数据</div>
          </div>
        </div>
      </template>

      <template v-if="(tab === 'product' || tab === 'shop') && cfg.sections.products">
        <div class="section">
          <div class="section-head">
            <h3>📦 产品分析 <span class="desc">查看产品的基本信息和详细属性</span></h3>
            <div class="actions">
              <button class="icon-btn" @click="loadReport">↻ 刷新</button>
              <button v-if="canEdit" class="icon-btn" @click="exportProducts">↓ 导出</button>
            </div>
          </div>
          <div class="sub-tabs">
            <button :class="{ active: subTab === 'all' }" @click="subTab = 'all'; page = 1">所有产品({{ total || 0 }})</button>
            <button :class="{ active: subTab === 'bestseller' }" @click="subTab = 'bestseller'; page = 1">畅销产品({{ cards.bestseller_count || 0 }})</button>
            <button :class="{ active: subTab === 'new' }" @click="subTab = 'new'; page = 1">最新产品({{ cards.new_product_count || 0 }})</button>
            <div class="right">
              <button class="icon-btn" :class="{ 'filter-on': activeFilterCount > 0 }" @click="filtersOpen = !filtersOpen">☷ 筛选<span v-if="activeFilterCount">({{ activeFilterCount }})</span></button>
              <input class="search-box" v-model="search" placeholder="🔍 搜索" @keyup.enter="loadReport" />
            </div>
          </div>
          <div v-if="filtersOpen" class="filter-panel">
            <div class="filter-grid">
              <label>类目<input v-model="filters.category" placeholder="如 Outdoor" /></label>
              <label>状态
                <select v-model="filters.status">
                  <option value="">全部</option>
                  <option value="on_sale">在售</option>
                  <option value="out_of_stock">缺货</option>
                  <option value="discontinued">下架</option>
                </select>
              </label>
              <label>评分<span class="rng"><input v-model="filters.min_rating" type="number" step="0.1" placeholder="min" /><input v-model="filters.max_rating" type="number" step="0.1" placeholder="max" /></span></label>
              <label>评论数<span class="rng"><input v-model="filters.min_reviews" type="number" placeholder="min" /><input v-model="filters.max_reviews" type="number" placeholder="max" /></span></label>
              <label>价格<span class="rng"><input v-model="filters.min_price" type="number" placeholder="min" /><input v-model="filters.max_price" type="number" placeholder="max" /></span></label>
              <label>30天销量<span class="rng"><input v-model="filters.min_sales" type="number" placeholder="min" /><input v-model="filters.max_sales" type="number" placeholder="max" /></span></label>
              <label>30天收入<span class="rng"><input v-model="filters.min_revenue" type="number" placeholder="min" /><input v-model="filters.max_revenue" type="number" placeholder="max" /></span></label>
              <label>变体数<span class="rng"><input v-model="filters.min_variants" type="number" placeholder="min" /><input v-model="filters.max_variants" type="number" placeholder="max" /></span></label>
              <label>视频
                <select v-model="filters.has_video"><option value="">不限</option><option value="true">有</option><option value="false">无</option></select>
              </label>
              <label>免运费
                <select v-model="filters.free_shipping"><option value="">不限</option><option value="true">是</option><option value="false">否</option></select>
              </label>
              <label>创建时间<span class="rng"><input v-model="filters.created_from" type="date" /><input v-model="filters.created_to" type="date" /></span></label>
            </div>
            <div class="filter-actions">
              <button @click="resetFilters">重置</button>
              <button class="primary" @click="applyFilters">应用筛选</button>
            </div>
          </div>
          <DataLoadingPanel class="report-table-wrap" :loading="loading" :has-data="products.length > 0" label="正在更新产品列表">
            <table>
              <thead>
                <tr>
                  <th style="width:50px">NO.</th>
                  <th v-if="cfg.productCols.sku">Sku</th>
                  <th v-if="cfg.productCols.title" style="min-width:280px">Product Details</th>
                  <th v-if="cfg.productCols.label">Label</th>
                  <th v-if="cfg.productCols.variantId">VariantId</th>
                  <th v-if="cfg.productCols.variantCount" style="width:90px">Variants</th>
                  <th v-if="cfg.productCols.attrs" style="min-width:180px">Attributes</th>
                  <th v-if="cfg.productCols.salePrice" style="width:100px">Sale Price</th>
                  <th v-if="cfg.productCols.price" style="width:100px">Price</th>
                  <th v-if="cfg.productCols.sales" style="width:110px">30-Day Sales</th>
                  <th v-if="cfg.productCols.revenue" style="width:120px">30-Day Revenue</th>
                  <th v-if="cfg.productCols.rating" style="width:90px">Ratings</th>
                  <th v-if="cfg.productCols.reviews" style="width:90px">Reviews</th>
                  <th v-if="cfg.productCols.status" style="width:100px">Status</th>
                  <th v-if="cfg.productCols.category" style="min-width:180px">Category</th>
                  <th v-if="cfg.productCols.inventory" style="width:100px">Inventory</th>
                  <th v-if="cfg.productCols.video" style="width:80px">Video</th>
                  <th v-if="cfg.productCols.freeShipping" style="width:120px">Free shipping</th>
                  <th v-if="cfg.productCols.createdTime" style="width:150px">Created Time</th>
                  <th v-if="cfg.productCols.updatedTime" style="width:150px">Update Time</th>
                  <th v-if="cfg.productCols.action" style="width:88px">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(p, i) in products" :key="p.id || p.sku" style="cursor:pointer" @click="openDetail(p.id)">
                  <td>{{ (page - 1) * pageSize + i + 1 }}</td>
                  <td v-if="cfg.productCols.sku"><a class="sku-link" :href="p.product_url || undefined" :target="p.product_url ? '_blank' : undefined" rel="noopener" @click.stop>{{ p.sku || p.item_id }}</a></td>
                  <td v-if="cfg.productCols.title"><div class="title-cell"><img v-if="p.image" :src="p.image" class="thumb thumb-img" alt="" /><div v-else class="thumb">📦</div><div class="info"><span class="title-text" :title="productTitle(p)">{{ productTitle(p) }}</span><span v-if="p.is_new" class="new-badge">NEW</span></div></div></td>
                  <td v-if="cfg.productCols.label">
                    <div v-if="productLabels(p).length" class="mini-tags">
                      <span v-for="tag in productLabels(p)" :key="tag">{{ tag }}</span>
                    </div>
                    <span v-else>--</span>
                  </td>
                  <td v-if="cfg.productCols.variantId">{{ p.variant_id || p.variantId || '--' }}</td>
                  <td v-if="cfg.productCols.variantCount">{{ p.variant_count ?? 1 }}</td>
                  <td v-if="cfg.productCols.attrs">
                    <div class="attr-cell">
                      <div v-for="[key, val] in attrEntries(p).slice(0, 4)" :key="key">{{ key }}: {{ val }}</div>
                      <div v-if="attrEntries(p).length > 4" style="color:#9ca3af">+{{ attrEntries(p).length - 4 }}</div>
                      <div v-if="attrEntries(p).length === 0" style="color:#9ca3af">--</div>
                    </div>
                  </td>
                  <td v-if="cfg.productCols.salePrice">{{ fmtPrice(p.sale_price ?? p.price, p.currency) }}</td>
                  <td v-if="cfg.productCols.price">{{ fmtPrice(p.original_price, p.currency) }}</td>
                  <td v-if="cfg.productCols.sales">{{ p.thirty_day_sales != null ? Number(p.thirty_day_sales).toLocaleString() : '0' }}</td>
                  <td v-if="cfg.productCols.revenue">{{ fmtPrice(p.thirty_day_revenue ?? 0, p.currency) }}</td>
                  <td v-if="cfg.productCols.rating">{{ p.ratings || p.rating || '--' }}</td>
                  <td v-if="cfg.productCols.reviews">{{ p.review_count != null ? Number(p.review_count).toLocaleString() : '0' }}</td>
                  <td v-if="cfg.productCols.status">{{ p.status || '--' }}</td>
                  <td v-if="cfg.productCols.category"><span class="title-text" :title="p.category_path">{{ p.category_path || '--' }}</span></td>
                  <td v-if="cfg.productCols.inventory">{{ p.inventory ?? '--' }}</td>
                  <td v-if="cfg.productCols.video">{{ yesNo(p.has_video) }}</td>
                  <td v-if="cfg.productCols.freeShipping">{{ yesNo(p.has_free_shipping) }}</td>
                  <td v-if="cfg.productCols.createdTime">{{ shortDate(p.created_time) }}</td>
                  <td v-if="cfg.productCols.updatedTime">{{ shortDate(p.updated_time) }}</td>
                  <td v-if="cfg.productCols.action"><button class="row-action" @click.stop="openDetail(p.id)">趋势</button></td>
                </tr>
                <tr v-if="loading && !products.length">
                  <td :colspan="1 + visibleProductColumnCount" class="empty">产品数据加载中...</td>
                </tr>
                <tr v-else-if="!products.length">
                  <td :colspan="1 + visibleProductColumnCount" class="empty">暂无数据 · 切换 site 或先抓取</td>
                </tr>
              </tbody>
            </table>
          </DataLoadingPanel>
          <div class="pagination">
            <button @click="page = Math.max(1, page - 1)" :disabled="page <= 1">‹</button>
            <button v-for="p in Math.min(totalPages, 5)" :key="p" @click="page = p" :class="{ active: page === p }">{{ p }}</button>
            <span v-if="totalPages > 5">…</span>
            <button v-if="totalPages > 5" @click="page = totalPages">{{ totalPages }}</button>
            <button @click="page = Math.min(totalPages, page + 1)" :disabled="page >= totalPages">›</button>
            <select v-model="pageSize" @change="page = 1">
              <option :value="10">10 条/页</option>
              <option :value="20">20 条/页</option>
              <option :value="50">50 条/页</option>
            </select>
          </div>
        </div>
      </template>

      <template v-if="(tab === 'promo' || tab === 'shop') && cfg.sections.promos">
        <div class="section">
          <div class="section-head">
            <h3>🎁 销售促销 <span class="desc">查看产品的促销信息</span></h3>
            <div class="actions">
              <button class="icon-btn" @click="loadReport">↻ 刷新</button>
              <button v-if="canEdit" class="icon-btn" @click="exportPromotions">↓ 导出</button>
            </div>
          </div>
          <div class="promo-filters">
            <input v-model="promoSearch" placeholder="搜索 SKU / 商品 / 活动" @keyup.enter="applyPromoFilters" />
            <select v-model="promoType" @change="applyPromoFilters">
              <option value="">全部类型</option>
              <option value="price">价格促销</option>
              <option value="coupon">Coupons</option>
              <option value="bundle">Bundle</option>
            </select>
            <input v-model="promoDateFrom" type="date" @change="applyPromoFilters" />
            <input v-model="promoDateTo" type="date" @change="applyPromoFilters" />
            <button class="icon-btn" @click="applyPromoFilters">筛选</button>
            <button class="icon-btn" @click="resetPromoFilters">清空</button>
          </div>
          <DataLoadingPanel class="report-table-wrap" :loading="loading" :has-data="promotions.length > 0" label="正在更新促销列表">
            <table>
              <thead><tr>
                <th style="width:50px">NO.</th>
                <th style="width:110px">SKU</th>
                <th style="width:150px">Updated Time</th>
                <th style="min-width:300px">Products Details</th>
                <th style="width:120px">Type</th>
                <th style="min-width:180px">Name</th>
                <th style="width:90px">Discount</th>
                <th style="width:100px">Pre-price</th>
                <th style="width:100px">Post-price</th>
                <th style="min-width:130px">Threshold</th>
                <th style="width:150px">Start Time</th>
                <th style="width:150px">End Time</th>
              </tr></thead>
              <tbody>
                <tr v-for="(p, i) in promotions" :key="p.id || p.sku">
                  <td>{{ (promoPage - 1) * promoPageSize + i + 1 }}</td>
                  <td><a class="sku-link" :href="p.product_url || undefined" :target="p.product_url ? '_blank' : undefined" rel="noopener">{{ p.sku || p.item_id }}</a></td>
                  <td>{{ shortDate(p.detected_time || p.updated_at) }}</td>
                  <td>
                    <div class="title-cell">
                      <img v-if="p.product_image" :src="p.product_image" class="thumb thumb-img" alt="" />
                      <div v-else class="thumb">📦</div>
                      <div class="info">
                        <span class="title-text" :title="promoTitle(p)">{{ promoTitle(p) }}</span>
                        <span v-if="p.promotion_name" class="new-badge">{{ p.promotion_name }}</span>
                      </div>
                    </div>
                  </td>
                  <td>{{ p.promotion_type || p.type || '价格促销' }}</td>
                  <td><span class="title-text" :title="promoName(p)">{{ promoName(p) }}</span></td>
                  <td>{{ p.discount_percent != null ? p.discount_percent + '%' : '--' }}</td>
                  <td>{{ fmtPrice(p.original_price, p.currency) }}</td>
                  <td>{{ fmtPrice(p.promotion_price, p.currency) }}</td>
                  <td>{{ p.threshold || '--' }}</td>
                  <td>{{ p.start_time ? p.start_time.slice(0, 16).replace('T', ' ') : '--' }}</td>
                  <td>{{ p.end_time ? p.end_time.slice(0, 16).replace('T', ' ') : '--' }}</td>
                </tr>
                <tr v-if="loading && !promotions.length">
                  <td colspan="12" class="empty">促销数据加载中...</td>
                </tr>
                <tr v-else-if="!promotions.length">
                  <td colspan="12" class="empty">暂无促销数据</td>
                </tr>
              </tbody>
            </table>
          </DataLoadingPanel>
          <div v-if="promoTotal" class="pagination">
            <button @click="promoPage = Math.max(1, promoPage - 1)" :disabled="promoPage <= 1">‹</button>
            <button v-for="p in Math.min(promoTotalPages, 5)" :key="p" @click="promoPage = p" :class="{ active: promoPage === p }">{{ p }}</button>
            <span v-if="promoTotalPages > 5">…</span>
            <button v-if="promoTotalPages > 5" @click="promoPage = promoTotalPages">{{ promoTotalPages }}</button>
            <button @click="promoPage = Math.min(promoTotalPages, promoPage + 1)" :disabled="promoPage >= promoTotalPages">›</button>
            <select v-model="promoPageSize" @change="promoPage = 1">
              <option :value="20">20 条/页</option>
              <option :value="50">50 条/页</option>
              <option :value="100">100 条/页</option>
            </select>
            <span style="margin-left:8px;color:#9ca3af;font-size:12px">共 {{ promoTotal }} 条</span>
          </div>
        </div>
      </template>
      </template>

      <div class="cfg-mask" :class="{ open: cfgOpen }" @click="cfgOpen = false"></div>
      <div class="cfg-drawer" :class="{ open: cfgOpen }">
        <div class="cfg-head">
          <h3>⚙ 自定义报表</h3>
          <button class="close" @click="cfgOpen = false">✕</button>
        </div>
        <div class="cfg-body">
          <div class="cfg-group">
            <h4>显示板块</h4>
            <div class="cfg-row"><input id="cs-kpi" type="checkbox" v-model="cfg.sections.kpi"><label for="cs-kpi">KPI 指标卡</label></div>
            <div class="cfg-row"><input id="cs-trend" type="checkbox" v-model="cfg.sections.trend"><label for="cs-trend">销售趋势 chart</label></div>
            <div class="cfg-row"><input id="cs-prod" type="checkbox" v-model="cfg.sections.products"><label for="cs-prod">产品分析 table</label></div>
            <div class="cfg-row"><input id="cs-promo" type="checkbox" v-model="cfg.sections.promos"><label for="cs-promo">销售促销 table</label></div>
          </div>
          <div v-if="cfg.sections.kpi" class="cfg-group">
            <h4>KPI 卡片（6 选 N）</h4>
            <div class="cfg-row"><input id="ck-sku" type="checkbox" v-model="cfg.kpiCards.sku"><label for="ck-sku">SKU</label></div>
            <div class="cfg-row"><input id="ck-new" type="checkbox" v-model="cfg.kpiCards.new"><label for="ck-new">新增产品</label></div>
            <div class="cfg-row"><input id="ck-sales" type="checkbox" v-model="cfg.kpiCards.sales"><label for="ck-sales">30 天销量</label></div>
            <div class="cfg-row"><input id="ck-rev" type="checkbox" v-model="cfg.kpiCards.revenue"><label for="ck-rev">30 天收入</label></div>
            <div class="cfg-row"><input id="ck-tra" type="checkbox" v-model="cfg.kpiCards.traffic"><label for="ck-tra">30 天流量</label></div>
            <div class="cfg-row"><input id="ck-cv" type="checkbox" v-model="cfg.kpiCards.conversion"><label for="ck-cv">30 天转化率</label></div>
          </div>
          <div v-if="cfg.sections.products" class="cfg-group">
            <h4>产品 table 列</h4>
            <div class="cfg-row"><input id="cc-sku" type="checkbox" v-model="cfg.productCols.sku"><label for="cc-sku">库存单位 SKU</label></div>
            <div class="cfg-row"><input id="cc-title" type="checkbox" v-model="cfg.productCols.title"><label for="cc-title">产品详情</label></div>
            <div class="cfg-row"><input id="cc-label" type="checkbox" v-model="cfg.productCols.label"><label for="cc-label">Label</label></div>
            <div class="cfg-row"><input id="cc-variant" type="checkbox" v-model="cfg.productCols.variantId"><label for="cc-variant">VariantId</label></div>
            <div class="cfg-row"><input id="cc-variant-count" type="checkbox" v-model="cfg.productCols.variantCount"><label for="cc-variant-count">Variants</label></div>
            <div class="cfg-row"><input id="cc-attr" type="checkbox" v-model="cfg.productCols.attrs"><label for="cc-attr">属性</label></div>
            <div class="cfg-row"><input id="cc-sale-price" type="checkbox" v-model="cfg.productCols.salePrice"><label for="cc-sale-price">Sale Price</label></div>
            <div class="cfg-row"><input id="cc-price" type="checkbox" v-model="cfg.productCols.price"><label for="cc-price">Price</label></div>
            <div class="cfg-row"><input id="cc-sales" type="checkbox" v-model="cfg.productCols.sales"><label for="cc-sales">30 天销量</label></div>
            <div class="cfg-row"><input id="cc-revenue" type="checkbox" v-model="cfg.productCols.revenue"><label for="cc-revenue">30 天收入</label></div>
            <div class="cfg-row"><input id="cc-rating" type="checkbox" v-model="cfg.productCols.rating"><label for="cc-rating">评分</label></div>
            <div class="cfg-row"><input id="cc-reviews" type="checkbox" v-model="cfg.productCols.reviews"><label for="cc-reviews">评论数</label></div>
            <div class="cfg-row"><input id="cc-status" type="checkbox" v-model="cfg.productCols.status"><label for="cc-status">状态</label></div>
            <div class="cfg-row"><input id="cc-category" type="checkbox" v-model="cfg.productCols.category"><label for="cc-category">Category</label></div>
            <div class="cfg-row"><input id="cc-inventory" type="checkbox" v-model="cfg.productCols.inventory"><label for="cc-inventory">Inventory</label></div>
            <div class="cfg-row"><input id="cc-video" type="checkbox" v-model="cfg.productCols.video"><label for="cc-video">Video</label></div>
            <div class="cfg-row"><input id="cc-shipping" type="checkbox" v-model="cfg.productCols.freeShipping"><label for="cc-shipping">Free shipping</label></div>
            <div class="cfg-row"><input id="cc-created" type="checkbox" v-model="cfg.productCols.createdTime"><label for="cc-created">Created Time</label></div>
            <div class="cfg-row"><input id="cc-updated" type="checkbox" v-model="cfg.productCols.updatedTime"><label for="cc-updated">Update Time</label></div>
            <div class="cfg-row"><input id="cc-action" type="checkbox" v-model="cfg.productCols.action"><label for="cc-action">趋势操作</label></div>
          </div>
          <div class="cfg-group">
            <h4>时间范围</h4>
            <div class="cfg-row">
              <label style="flex:0">默认</label>
              <select v-model="cfg.timeRange" style="margin-left:14px;flex:1">
                <option value="7d">近 7 天</option>
                <option value="30d">近 30 天</option>
                <option value="90d">近 90 天</option>
                <option value="all">全部</option>
              </select>
            </div>
          </div>
        </div>
        <div class="cfg-foot">
          <button @click="resetCfg">恢复默认</button>
          <button class="primary" @click="saveCfg">保存配置</button>
        </div>
      </div>

      <!-- 产品趋势分析弹窗 -->
      <div v-if="detail || detailLoading" class="od-modal" @click.self="closeDetail">
        <div class="od-modal-card product-trend-modal">
          <div class="od-modal-head">
            <h3>产品趋势分析</h3>
            <button class="od-x" @click="closeDetail">✕</button>
          </div>
          <div v-if="detailLoading" class="sub">加载中…</div>
          <div v-else-if="detail">
            <div class="prod-detail-top">
              <img v-if="detail.image" :src="detail.image" class="prod-detail-img" />
              <div v-else class="prod-detail-img prod-detail-img-empty">📦</div>
              <div class="prod-detail-meta">
                <div class="prod-detail-title">{{ productTitle(detail) }}</div>
                <div class="sub">SKU: {{ detail.sku }} · {{ detail.site }}</div>
                <div class="prod-detail-stats">
                  <span>价格 <b>{{ fmtPrice(productSummary.price ?? detail.sale_price, productSummary.currency || detail.currency) }}</b></span>
                  <span v-if="productSummary.original_price || detail.original_price">原价 <s>{{ fmtPrice(productSummary.original_price ?? detail.original_price, productSummary.currency || detail.currency) }}</s></span>
                  <span>评分 <b>{{ productSummary.ratings || detail.ratings || '—' }}</b> ({{ productSummary.review_count ?? detail.review_count ?? 0 }})</span>
                  <span>30天销量 <b>{{ productSummary.thirty_day_sales ?? detail.thirty_day_sales ?? 0 }}</b></span>
                </div>
                <div class="prod-detail-badges">
                  <span>{{ detail.status || '—' }}</span>
                  <a v-if="detail.product_url" :href="detail.product_url" target="_blank" class="prod-detail-link">原页 ↗</a>
                </div>
              </div>
            </div>

            <div class="trend-controls">
              <select v-model="trendGranularity" @change="reloadProductTrend">
                <option value="month">By Month</option>
                <option value="week">By Week</option>
                <option value="day">By Day</option>
              </select>
              <input v-model="trendDateFrom" type="date" @change="reloadProductTrend" />
              <input v-model="trendDateTo" type="date" @change="reloadProductTrend" />
              <input v-model="trendPromoSearch" placeholder="搜索促销 / 商品标题" @keyup.enter="reloadProductTrend" />
              <select v-model="trendPromoType" @change="reloadProductTrend">
                <option value="">全部活动</option>
                <option value="coupon">Coupons</option>
                <option value="price">Price Promotion</option>
                <option value="bundle">Bundle</option>
              </select>
              <button class="row-action" @click="reloadProductTrend">筛选</button>
              <button class="row-action" @click="resetProductTrendFilters">清空</button>
              <button v-if="canEdit" class="row-action" @click="exportProductTrend">导出</button>
            </div>

            <div class="product-trend-kpis">
              <div><span>Sales</span><b>{{ productCurrentPeriod?.estimated_sales ?? productSummary.thirty_day_sales ?? 0 }}</b><small>{{ periodDelta('estimated_sales') }}</small></div>
              <div><span>Revenues</span><b>{{ fmtPrice(productCurrentPeriod?.estimated_revenue ?? productSummary.thirty_day_revenue ?? 0, productSummary.currency || detail.currency) }}</b><small>{{ periodDelta('estimated_revenue') }}</small></div>
              <div><span>Price</span><b>{{ fmtPrice(productCurrentPeriod?.sale_price ?? productSummary.price ?? detail.sale_price, productSummary.currency || detail.currency) }}</b><small>{{ trendGranularity }}</small></div>
              <div><span>Ratings</span><b>{{ productCurrentPeriod?.avg_rating ?? productSummary.ratings ?? detail.ratings ?? '—' }}</b><small>当前 SKU</small></div>
              <div><span>Reviews</span><b>{{ productCurrentPeriod?.review_total ?? productSummary.review_count ?? detail.review_count ?? 0 }}</b><small>{{ periodDelta('review_total') }}</small></div>
              <div><span>促销</span><b>{{ productSummary.promotion_count ?? productPromotions.length }}</b></div>
            </div>

            <div v-if="productSummary.data_notes?.length" class="data-notes">
              <span v-for="note in productSummary.data_notes" :key="note">{{ note }}</span>
            </div>

            <div class="prod-detail-history">
              <h4>销售趋势 <span class="sub">({{ trendGranularity }})</span></h4>
              <TrendLineChart v-if="productTrendRows.length" :rows="productTrendRows" :series="productTrendSeries" :height="300" />
              <div v-else class="sub">暂无趋势数据</div>
            </div>

            <div class="prod-detail-history">
              <h4>趋势明细</h4>
              <div v-if="!productTrendRows.length" class="sub">暂无历史快照</div>
              <table v-else>
                <thead><tr><th>日期</th><th>售价</th><th>原价</th><th>评论数</th><th>估算销量</th><th>估算收入</th></tr></thead>
                <tbody>
                  <tr v-for="(h, i) in productTrendRows" :key="i">
                    <td>{{ (h.date || '').slice(0, 10) }}</td>
                    <td>{{ fmtPrice(h.sale_price, detail?.currency) }}</td>
                    <td>{{ fmtPrice(h.original_price, detail?.currency) }}</td>
                    <td>{{ h.review_total ?? h.review_count ?? '—' }}</td>
                    <td>{{ h.estimated_sales ?? 0 }}</td>
                    <td>{{ fmtPrice(h.estimated_revenue ?? 0, detail?.currency) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div class="prod-detail-history">
              <h4>销售促销</h4>
              <div v-if="!productPromotions.length" class="sub">暂无促销记录</div>
              <table v-else>
                <thead><tr><th>类型</th><th>名称</th><th>折扣</th><th>原价</th><th>促销价</th><th>开始</th><th>结束</th><th>更新时间</th></tr></thead>
                <tbody>
                  <tr v-for="promo in productPromotions" :key="promo.id">
                    <td>{{ promo.promotion_type || '--' }}</td>
                    <td>{{ promo.promotion_name || promo.product_title || '--' }}</td>
                    <td>{{ promo.discount_percent != null ? promo.discount_percent + '%' : '--' }}</td>
                    <td>{{ fmtPrice(promo.original_price, promo.currency || detail?.currency) }}</td>
                    <td>{{ fmtPrice(promo.promotion_price, promo.currency || detail?.currency) }}</td>
                    <td>{{ promo.start_time ? promo.start_time.slice(0, 10) : '--' }}</td>
                    <td>{{ promo.end_time ? promo.end_time.slice(0, 10) : '--' }}</td>
                    <td>{{ promo.detected_time ? promo.detected_time.slice(0, 16).replace('T', ' ') : '--' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </section>
  </main>
</template>

<style scoped>
.title-text { display:inline-block; max-width:380px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; vertical-align:bottom; }
.report-page {
  --report-control-bg: #fff;
  --report-control-border: #d1d5db;
  --report-control-text: #1f2329;
  --report-control-muted: #6b7280;
  --report-panel-soft: #fafbfc;
}
.icon-btn.filter-on { border-color:#7c6ce0; color:#7c6ce0; }
.gran-select { padding:7px 30px 7px 10px; border:1px solid var(--report-control-border); border-radius:7px; font-size:12.5px; font-family:inherit; background:var(--report-control-bg); color:var(--report-control-text); cursor:pointer; }
.date-input { height:32px; padding:0 9px; border:1px solid var(--report-control-border); border-radius:7px; background:var(--report-control-bg); color:var(--report-control-text); font-size:12px; font-family:inherit; }
.range-sep,.range-note { color:var(--report-control-muted); font-size:12px; }
.filter-panel { border:1px solid var(--report-control-border); border-radius:10px; padding:14px; margin-bottom:14px; background:var(--report-panel-soft); }
.filter-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:12px; }
.filter-grid label { display:flex; flex-direction:column; gap:4px; font-size:12px; color:var(--report-control-muted); }
.filter-grid input, .filter-grid select { padding:7px 9px; border:1px solid var(--report-control-border); border-radius:7px; font-size:12.5px; font-family:inherit; background:var(--report-control-bg); color:var(--report-control-text); }
.filter-grid input::placeholder, .promo-filters input::placeholder { color:var(--report-control-muted); }
.filter-grid .rng { display:flex; gap:6px; }
.filter-grid .rng input { width:100%; min-width:0; }
.mini-tags { display:flex; flex-wrap:wrap; gap:4px; }
.mini-tags span { display:inline-flex; align-items:center; min-height:20px; padding:2px 7px; border-radius:999px; background:#eef2ff; color:#4338ca; font-size:11px; font-weight:600; white-space:nowrap; }
.row-action { padding:5px 10px; border:1px solid #c4b5fd; border-radius:7px; background:#fff; color:#6d28d9; cursor:pointer; font-size:12px; font-family:inherit; white-space:nowrap; }
.row-action:hover { background:#f5f3ff; }
.filter-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:12px; }
.filter-actions button { padding:7px 16px; border-radius:7px; border:1px solid var(--report-control-border); background:var(--report-control-bg); color:var(--report-control-text); cursor:pointer; font-size:12.5px; font-family:inherit; }
.filter-actions button.primary { background:#7c6ce0; color:#fff; border-color:#7c6ce0; }
.promo-filters { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
.promo-filters input,.promo-filters select { padding:7px 10px; border:1px solid var(--report-control-border); border-radius:7px; font-size:12.5px; font-family:inherit; background:var(--report-control-bg); color:var(--report-control-text); }
.promo-filters input:first-child { min-width:220px; }
.report-table-wrap { overflow:auto; border-radius:8px; }
.thumb-img { object-fit:cover; }
.prod-detail-top { display:flex; gap:14px; align-items:flex-start; flex-wrap:wrap; }
.prod-detail-img { width:120px; height:120px; object-fit:cover; border-radius:8px; }
.prod-detail-img-empty { display:flex; align-items:center; justify-content:center; font-size:2rem; background:#f3f4f6; }
.prod-detail-meta { flex:1; min-width:220px; }
.prod-detail-title { font-weight:600; line-height:1.5; }
.prod-detail-stats { margin-top:8px; display:flex; gap:18px; flex-wrap:wrap; font-size:0.86rem; }
.prod-detail-badges { margin-top:8px; display:flex; gap:10px; align-items:center; }
.prod-detail-link { color:#6b7280; font-size:0.82rem; }
.prod-detail-history { margin-top:16px; }
.prod-detail-history h4 { margin:0 0 8px; }
.product-trend-modal { width:min(1120px, calc(100vw - 36px)); max-width:min(1120px, calc(100vw - 36px)); max-height:calc(100vh - 36px); overflow:auto; }
.trend-controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:14px; padding:10px; border:1px solid var(--report-control-border); border-radius:9px; background:var(--report-panel-soft); }
.trend-controls input,.trend-controls select { min-height:32px; padding:6px 9px; border:1px solid var(--report-control-border); border-radius:7px; background:var(--report-control-bg); color:var(--report-control-text); font-size:12.5px; font-family:inherit; }
.trend-controls input[type="text"],.trend-controls input:not([type]) { min-width:190px; }
.product-trend-kpis { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; margin-top:16px; }
.product-trend-kpis div { min-height:70px; padding:10px 12px; border:1px solid var(--report-control-border); border-radius:9px; background:var(--report-panel-soft); }
.product-trend-kpis span { display:block; font-size:12px; color:var(--report-control-muted); margin-bottom:4px; }
.product-trend-kpis b { display:block; color:#111827; font-size:18px; line-height:1.3; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.product-trend-kpis small { display:block; margin-top:3px; color:var(--report-control-muted); font-size:11px; line-height:1.25; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.data-notes { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
.data-notes span { border:1px solid #e5e7eb; border-radius:999px; padding:4px 9px; background:#fff7ed; color:#9a3412; font-size:12px; }
.prod-detail-history table { width:100%; border-collapse:collapse; font-size:12.5px; }
.prod-detail-history th,.prod-detail-history td { padding:8px 9px; border-bottom:1px solid #e5e7eb; text-align:left; white-space:nowrap; }
.prod-detail-history th { color:#6b7280; background:#f9fafb; font-weight:700; }
.od-modal-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; }
.od-x { background:transparent; border:0; cursor:pointer; font-size:1rem; }
@media (max-width: 980px) {
  .product-trend-kpis { grid-template-columns:repeat(2,minmax(0,1fr)); }
}
@media (max-width: 560px) {
  .product-trend-kpis { grid-template-columns:1fr; }
}
</style>
