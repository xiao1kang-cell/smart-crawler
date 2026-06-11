<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { asList, fmtDate, fmtPrice, qs } from '../api/client'
import { getProduct, listProducts, listPromotions, listSites, productPriceHistory, siteOverview } from '../api/products'
import { useAuthStore } from '../stores/auth'

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
const dateRange = ref({ from: '2025-10-01', to: '2026-04-16' })
const lastUpdate = ref('2026.05.15')
const DEFAULT_CFG = {
  sections: { kpi: true, trend: true, products: true, promos: true },
  kpiCards: { sku: true, new: true, sales: true, revenue: true, traffic: true, conversion: true },
  productCols: { sku: true, title: true, attrs: true, price: false, rating: false, status: false },
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
const trends = computed<Record<string, any>[]>(() => asList(overview.value?.trends || [], ['trends', 'items']))
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / Number(pageSize.value || 10))))
// 趋势粒度:day / week / month。日快照按 ISO 周或月分桶,桶内取最后一天
// (sku_count/sales/revenue 都是时点/30天滚动值,不可累加,取桶末代表)。
const granularity = ref<'day' | 'week' | 'month'>('month')
function bucketKey(dateStr: string): string {
  const d = new Date(dateStr)
  if (Number.isNaN(d.getTime())) return dateStr
  if (granularity.value === 'month') return dateStr.slice(0, 7)
  if (granularity.value === 'week') {
    const onejan = new Date(d.getFullYear(), 0, 1)
    const week = Math.ceil(((d.getTime() - onejan.getTime()) / 86400000 + onejan.getDay() + 1) / 7)
    return `${d.getFullYear()}-W${String(week).padStart(2, '0')}`
  }
  return dateStr.slice(0, 10)
}
const aggregatedTrends = computed<Record<string, any>[]>(() => {
  if (granularity.value === 'day') return trends.value
  const buckets = new Map<string, Record<string, any>>()
  for (const t of trends.value) {
    const key = bucketKey(String(t.date || ''))
    buckets.set(key, { ...t, date: key }) // 后写覆盖 → 桶内保留最后一天
  }
  return Array.from(buckets.values())
})
const chartLines = computed(() => {
  const rows = aggregatedTrends.value
  if (!rows.length) return null
  const series = {
    sku: rows.map((x) => Number(x.sku_count || 0)),
    new: rows.map((x) => Number(x.new_product_count || 0)),
    sales: rows.map((x) => Number(x.estimated_sales || 0)),
    revenue: rows.map((x) => Math.round(Number(x.estimated_revenue || 0))),
    reviews: rows.map((x) => Number(x.review_total || 0)),
    rating: rows.map((x) => Number(x.avg_rating || 0))   // 0-5,单独按 max=5 缩放
  }
  const max = Math.max(1, ...series.sku, ...series.new, ...series.sales, ...series.revenue, ...series.reviews)
  return { dates: rows.map((x) => String(x.date || '')), series, max }
})

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
    const productParams: Record<string, unknown> = { site: site.value, page: page.value, page_size: pageSize.value }
    if (search.value) productParams.search = search.value
    // 子 tab → 后端唯一认的 tab 参数(all|bestseller|new)
    if (subTab.value === 'new' || subTab.value === 'bestseller') productParams.tab = subTab.value
    // 11 区筛选:仅透传非空值(qs() 会丢弃 undefined/''，这里同样跳过空串)
    for (const [k, v] of Object.entries(filters.value)) {
      if (v !== '' && v !== null && v !== undefined) productParams[k] = v
    }
    const [overviewData, productsData, promosData] = await Promise.all([
      siteOverview(site.value),
      listProducts(productParams),
      listPromotions({ site: site.value, page: promoPage.value, page_size: promoPageSize.value })
    ])
    overview.value = overviewData
    products.value = asList(productsData, ['items', 'products'])
    promotions.value = asList(promosData, ['items', 'promotions'])
    promoTotal.value = Number(promosData?.total || promotions.value.length || 0)
    const nextCards = overviewData?.cards && typeof overviewData.cards === 'object' ? overviewData.cards : overviewData
    total.value = Number(productsData?.total || nextCards?.sku_count || nextCards?.total_products || products.value.length || 0)
    const updateTime = overviewData?.last_run || overviewData?.updated_at
    lastUpdate.value = updateTime ? fmtDate(updateTime) : '2026.05.15'
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function exportProducts() {
  window.open(`/api/export/products${qs({ site: site.value, token: auth.token, workspace_id: auth.workspaceId })}`, '_blank')
}

// 商品详情 + 价格历史弹窗
const detail = ref<Record<string, any> | null>(null)
const priceHistory = ref<Record<string, any>[]>([])
const detailLoading = ref(false)
async function openDetail(id: number | string | undefined) {
  if (id === undefined || id === null) return
  detail.value = null
  priceHistory.value = []
  detailLoading.value = true
  try {
    const [d, h] = await Promise.all([getProduct(id), productPriceHistory(id)])
    detail.value = d
    priceHistory.value = Array.isArray(h) ? h : asList(h, ['items', 'history'])
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    detailLoading.value = false
  }
}
function closeDetail() {
  detail.value = null
  priceHistory.value = []
}

function saveCfg() {
  localStorage.setItem('sc_report_cfg', JSON.stringify(cfg.value))
  cfgOpen.value = false
}

function resetCfg() {
  cfg.value = cloneCfg()
  localStorage.removeItem('sc_report_cfg')
}

function makePath(values: number[], max: number, w: number, h: number, pad = 20) {
  if (!values.length) return ''
  const stepX = (w - 2 * pad) / Math.max(values.length - 1, 1)
  return values.map((value, index) => {
    const x = pad + index * stepX
    const y = h - pad - (value / max) * (h - 2 * pad)
    return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}

watch(site, () => {
  page.value = 1
  promoPage.value = 1
  loadReport()
})
watch([page, pageSize, subTab], loadReport)
watch([promoPage, promoPageSize], loadReport)
// 权限:viewer 只读,可查看面板但编辑动作(导出/自定义报表)受限。
// 规格"仅有权限人员可查看/编辑"——查看放开,编辑按角色门控。
const canEdit = computed(() => {
  const u = auth.user
  if (!u) return false
  if (u.global_role === 'super_admin' || u.role === 'admin' || u.role === 'owner') return true
  return ['admin', 'owner', 'operator', 'member', 'user'].includes(u.workspace_role || u.role || '')
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
          <span>总产品数: <b>{{ cards.sku_count || total || '--' }}</b></span>
          <span>总类别数: <b>{{ cards.category_count != null ? cards.category_count : '--' }}</b></span>
        </div>
      </div>

      <div class="tab-row">
        <button :class="{ active: tab === 'shop' }" @click="tab = 'shop'">🏬 店铺分析</button>
        <button :class="{ active: tab === 'product' }" @click="tab = 'product'">📦 产品分析</button>
        <button :class="{ active: tab === 'promo' }" @click="tab = 'promo'">🎁 销售促销</button>
      </div>

      <UAlert v-if="error" color="error" variant="soft" :title="error" class="mb-4" />

      <template v-if="tab === 'shop'">
        <div v-if="cfg.sections.kpi" class="stats">
          <div v-if="cfg.kpiCards.sku" class="stat"><div class="lbl">SKU</div><div class="val">{{ cards.sku_count ? Number(cards.sku_count).toLocaleString() : '--' }}</div><div class="sub">在售 SKU</div></div>
          <div v-if="cfg.kpiCards.new" class="stat"><div class="lbl">新增产品</div><div class="val">{{ cards.new_product_count ? Number(cards.new_product_count).toLocaleString() : '--' }}</div><div class="sub">New Arrivals</div></div>
          <div v-if="cfg.kpiCards.sales" class="stat"><div class="lbl">30天销量</div><div class="val">{{ cards.thirty_day_sales ? Number(cards.thirty_day_sales).toLocaleString() : '--' }}</div><div class="sub">上个周期: --</div></div>
          <div v-if="cfg.kpiCards.revenue" class="stat"><div class="lbl">30天收入</div><div class="val">{{ cards.thirty_day_revenue ? '$' + (Number(cards.thirty_day_revenue) / 1000).toFixed(1) + 'k' : '--' }}</div><div class="sub">上个周期: --</div></div>
          <div v-if="cfg.kpiCards.traffic" class="stat"><div class="lbl">畅销产品</div><div class="val">{{ cards.bestseller_count != null ? Number(cards.bestseller_count).toLocaleString() : '--' }}</div><div class="sub">Bestsellers</div></div>
          <div v-if="cfg.kpiCards.conversion" class="stat"><div class="lbl">类别数</div><div class="val">{{ cards.category_count != null ? Number(cards.category_count).toLocaleString() : '--' }}</div><div class="sub">Categories</div></div>
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
              <span style="font-size:12px;color:#6b7280">{{ dateRange.from }} → {{ dateRange.to }}</span>
            </div>
          </div>
          <div class="chart-wrap">
            <div class="legend">
              <span><span class="legend-dot" style="background:#3b82f6"></span>库存单位</span>
              <span><span class="legend-dot" style="background:#10b981"></span>新SKU</span>
              <span><span class="legend-dot" style="background:#f59e0b"></span>销售</span>
              <span><span class="legend-dot" style="background:#ef4444"></span>收入</span>
              <span><span class="legend-dot" style="background:#8b5cf6"></span>评论数</span>
              <span><span class="legend-dot" style="background:#ec4899"></span>评分(0-5)</span>
            </div>
            <svg v-if="chartLines" class="chart-svg" viewBox="0 0 900 260" preserveAspectRatio="none">
              <g class="grid">
                <line x1="40" y1="40" x2="880" y2="40" />
                <line x1="40" y1="100" x2="880" y2="100" />
                <line x1="40" y1="160" x2="880" y2="160" />
                <line x1="40" y1="220" x2="880" y2="220" />
              </g>
              <g class="axis-label">
                <text x="32" y="44" text-anchor="end">{{ chartLines.max }}</text>
                <text x="32" y="104" text-anchor="end">{{ Math.round(chartLines.max * 0.66) }}</text>
                <text x="32" y="164" text-anchor="end">{{ Math.round(chartLines.max * 0.33) }}</text>
                <text x="32" y="224" text-anchor="end">0</text>
              </g>
              <g>
                <path :d="makePath(chartLines.series.sku, chartLines.max, 900, 240)" stroke="#3b82f6" fill="none" stroke-width="2" />
                <path :d="makePath(chartLines.series.new, chartLines.max, 900, 240)" stroke="#10b981" fill="none" stroke-width="2" />
                <path :d="makePath(chartLines.series.sales, chartLines.max, 900, 240)" stroke="#f59e0b" fill="none" stroke-width="2" />
                <path :d="makePath(chartLines.series.revenue, chartLines.max, 900, 240)" stroke="#ef4444" fill="none" stroke-width="2" />
                <path :d="makePath(chartLines.series.reviews, chartLines.max, 900, 240)" stroke="#8b5cf6" fill="none" stroke-width="2" />
                <path :d="makePath(chartLines.series.rating, 5, 900, 240)" stroke="#ec4899" fill="none" stroke-width="2" stroke-dasharray="4,3" />
              </g>
              <g class="axis-label">
                <text
                  v-for="(d, i) in chartLines.dates.filter((_, idx) => idx % Math.ceil(chartLines!.dates.length / 10) === 0)"
                  :key="`${d}-${i}`"
                  :x="40 + i * (860 / Math.max(chartLines.dates.length - 1, 1)) * Math.ceil(chartLines.dates.length / 10)"
                  y="248"
                  text-anchor="end"
                >{{ d.slice(5) }}</text>
              </g>
            </svg>
            <div v-else class="loading">趋势数据加载中…</div>
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
          <table>
            <thead>
              <tr>
                <th style="width:50px">NO.</th>
                <th v-if="cfg.productCols.sku">库存单位</th>
                <th v-if="cfg.productCols.title">产品详情</th>
                <th v-if="cfg.productCols.attrs">属性</th>
                <th v-if="cfg.productCols.price" style="width:90px">价格</th>
                <th v-if="cfg.productCols.rating" style="width:80px">评分</th>
                <th v-if="cfg.productCols.status" style="width:90px">状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(p, i) in products" :key="p.id || p.sku" style="cursor:pointer" @click="openDetail(p.id)">
                <td>{{ (page - 1) * pageSize + i + 1 }}</td>
                <td v-if="cfg.productCols.sku"><a class="sku-link" :href="p.product_url || undefined" :target="p.product_url ? '_blank' : undefined" rel="noopener" @click.stop>{{ p.sku || p.item_id }}</a></td>
                <td v-if="cfg.productCols.title"><div class="title-cell"><div class="thumb">📦</div><div class="info"><span class="title-text" :title="p.title || p.name">{{ p.title || p.name || '' }}</span><span v-if="p.is_new" class="new-badge">NEW</span></div></div></td>
                <td v-if="cfg.productCols.attrs">
                  <div class="attr-cell">
                    <div v-if="p.attributes?.color">Color: {{ p.attributes.color }}</div>
                    <div v-if="p.attributes?.size">Size: {{ p.attributes.size }}</div>
                    <div v-if="p.attributes?.material">Material: {{ p.attributes.material }}</div>
                    <div v-if="!p.attributes || Object.keys(p.attributes).length === 0" style="color:#9ca3af">--</div>
                  </div>
                </td>
                <td v-if="cfg.productCols.price">{{ fmtPrice(p.sale_price ?? p.price, p.currency) }}</td>
                <td v-if="cfg.productCols.rating">{{ p.ratings || p.rating ? (p.ratings || p.rating) + ' (' + (p.review_count || 0) + ')' : '--' }}</td>
                <td v-if="cfg.productCols.status">{{ p.status || '--' }}</td>
              </tr>
              <tr v-if="!products.length">
                <td :colspan="1 + visibleProductColumnCount" class="empty">暂无数据 · 切换 site 或先抓取</td>
              </tr>
            </tbody>
          </table>
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
            </div>
          </div>
          <table>
            <thead><tr>
              <th style="width:50px">NO.</th><th>库存单位</th><th>促销名称</th><th>类型</th>
              <th style="width:80px">折扣</th><th style="width:90px">原价</th><th style="width:90px">促销价</th>
              <th>门槛</th><th style="width:130px">开始时间</th><th style="width:130px">结束时间</th><th style="width:130px">更新时间</th>
            </tr></thead>
            <tbody>
              <tr v-for="(p, i) in promotions" :key="p.id || p.sku">
                <td>{{ (promoPage - 1) * promoPageSize + i + 1 }}</td>
                <td><a class="sku-link" :href="p.product_url || undefined" :target="p.product_url ? '_blank' : undefined" rel="noopener">{{ p.sku || p.item_id }}</a></td>
                <td><span class="title-text" :title="p.promotion_name || p.product_title || p.title">{{ p.promotion_name || p.product_title || p.title || '--' }}</span></td>
                <td>{{ p.promotion_type || p.type || '价格促销' }}</td>
                <td>{{ p.discount_percent != null ? p.discount_percent + '%' : '--' }}</td>
                <td>{{ fmtPrice(p.original_price, p.currency) }}</td>
                <td>{{ fmtPrice(p.promotion_price, p.currency) }}</td>
                <td>{{ p.threshold || '--' }}</td>
                <td>{{ p.start_time ? p.start_time.slice(0, 16).replace('T', ' ') : '--' }}</td>
                <td>{{ p.end_time ? p.end_time.slice(0, 16).replace('T', ' ') : '--' }}</td>
                <td>{{ (p.detected_time || p.updated_at || '').slice(0, 16).replace('T', ' ') }}</td>
              </tr>
              <tr v-if="!promotions.length">
                <td colspan="11" class="empty">暂无促销数据</td>
              </tr>
            </tbody>
          </table>
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
            <div class="cfg-row"><input id="cc-attr" type="checkbox" v-model="cfg.productCols.attrs"><label for="cc-attr">属性</label></div>
            <div class="cfg-row"><input id="cc-price" type="checkbox" v-model="cfg.productCols.price"><label for="cc-price">价格</label></div>
            <div class="cfg-row"><input id="cc-rating" type="checkbox" v-model="cfg.productCols.rating"><label for="cc-rating">评分</label></div>
            <div class="cfg-row"><input id="cc-status" type="checkbox" v-model="cfg.productCols.status"><label for="cc-status">状态</label></div>
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

      <!-- 商品详情弹窗 -->
      <div v-if="detail || detailLoading" class="od-modal" @click.self="closeDetail">
        <div class="od-modal-card" style="max-width:680px">
          <div class="od-modal-head">
            <h3>商品详情</h3>
            <button class="od-x" @click="closeDetail">✕</button>
          </div>
          <div v-if="detailLoading" class="sub">加载中…</div>
          <div v-else-if="detail">
            <div class="prod-detail-top">
              <img v-if="detail.image" :src="detail.image" class="prod-detail-img" />
              <div v-else class="prod-detail-img prod-detail-img-empty">📦</div>
              <div class="prod-detail-meta">
                <div class="prod-detail-title">{{ detail.title }}</div>
                <div class="sub">SKU: {{ detail.sku }} · {{ detail.site }}</div>
                <div class="prod-detail-stats">
                  <span>价格 <b>{{ fmtPrice(detail.sale_price, detail.currency) }}</b></span>
                  <span v-if="detail.original_price">原价 <s>{{ fmtPrice(detail.original_price, detail.currency) }}</s></span>
                  <span>评分 <b>{{ detail.ratings || '—' }}</b> ({{ detail.review_count || 0 }})</span>
                  <span>30天销量 <b>{{ detail.thirty_day_sales || 0 }}</b></span>
                </div>
                <div class="prod-detail-badges">
                  <span>{{ detail.status || '—' }}</span>
                  <a v-if="detail.product_url" :href="detail.product_url" target="_blank" class="prod-detail-link">原页 ↗</a>
                </div>
              </div>
            </div>
            <div class="prod-detail-history">
              <h4>价格历史</h4>
              <div v-if="!priceHistory.length" class="sub">暂无价格历史</div>
              <table v-else>
                <thead><tr><th>日期</th><th>售价</th><th>原价</th><th>评论数</th></tr></thead>
                <tbody>
                  <tr v-for="(h, i) in priceHistory" :key="i">
                    <td>{{ (h.date || '').slice(0, 10) }}</td>
                    <td>{{ fmtPrice(h.sale_price, detail?.currency) }}</td>
                    <td>{{ fmtPrice(h.original_price, detail?.currency) }}</td>
                    <td>{{ h.review_count != null ? h.review_count : '—' }}</td>
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
.icon-btn.filter-on { border-color:#7c6ce0; color:#7c6ce0; }
.gran-select { padding:5px 10px; border:1px solid #d1d5db; border-radius:6px; font-size:12.5px; font-family:inherit; background:#fff; cursor:pointer; }
.filter-panel { border:1px solid #e5e7eb; border-radius:10px; padding:14px; margin-bottom:14px; background:#fafbfc; }
.filter-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:12px; }
.filter-grid label { display:flex; flex-direction:column; gap:4px; font-size:12px; color:#6b7280; }
.filter-grid input, .filter-grid select { padding:5px 8px; border:1px solid #d1d5db; border-radius:6px; font-size:12.5px; font-family:inherit; }
.filter-grid .rng { display:flex; gap:6px; }
.filter-grid .rng input { width:100%; min-width:0; }
.filter-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:12px; }
.filter-actions button { padding:6px 16px; border-radius:7px; border:1px solid #d1d5db; background:#fff; cursor:pointer; font-size:12.5px; font-family:inherit; }
.filter-actions button.primary { background:#7c6ce0; color:#fff; border-color:#7c6ce0; }
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
.od-modal-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; }
.od-x { background:transparent; border:0; cursor:pointer; font-size:1rem; }
</style>
