<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { asList, currencyForMarket, fmtDate, fmtDateOnly, fmtNumber, fmtPrice, qs } from '../api/client'
import { listCategoriesCross, listProducts, listPromotions, listSites, productTrend, siteOverview } from '../api/products'
import { useAuthStore } from '../stores/auth'
import DataLoadingPanel from '../components/common/DataLoadingPanel.vue'
import PageLoading from '../components/common/PageLoading.vue'

const TrendLineChart = defineAsyncComponent(() => import('../components/charts/TrendLineChart.vue'))

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const routeWorkspaceId = String(route.query.workspace_id || '')
if (routeWorkspaceId && routeWorkspaceId !== auth.workspaceId) auth.setWorkspace(routeWorkspaceId)
const activeWorkspaceId = computed(() => String(route.query.workspace_id || auth.workspaceId || ''))
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
  filtersOpen.value = false
  loadReport()
}
const sites = ref<Record<string, any>[]>([])
const site = ref(String(route.query.site || localStorage.getItem('sc_report_site') || ''))
const categoryOptions = ref<string[]>([])
const overview = ref<Record<string, any> | null>(null)
const products = ref<Record<string, any>[]>([])
const promotions = ref<Record<string, any>[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const productExportScope = ref<'all' | 'page'>('all')
const promoTotal = ref(0)
const promoPage = ref(1)
const promoPageSize = ref(20)
const promoExportScope = ref<'all' | 'page'>('all')
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
const trendColumns = [
  { accessorKey: 'date', header: 'Period' },
  { accessorKey: 'sku_count', header: 'SKU' },
  { accessorKey: 'new_product_count', header: 'New Products' },
  { accessorKey: 'estimated_sales', header: 'Sales' },
  { accessorKey: 'estimated_revenue', header: 'Revenues' },
  { accessorKey: 'traffic', header: 'Traffic' },
  { accessorKey: 'conversion_rate', header: 'Conversion Rate' },
  { accessorKey: 'review_total', header: 'Reviews' }
]
const productColumnDefs = [
  { id: 'no', header: 'NO.', enabled: () => true },
  { accessorKey: 'sku', header: 'SKU', enabled: () => cfg.value.productCols.sku },
  { accessorKey: 'title', header: 'Products Details', enabled: () => cfg.value.productCols.title },
  { accessorKey: 'label', header: 'Label', enabled: () => cfg.value.productCols.label },
  { accessorKey: 'variantId', header: 'VariantId', enabled: () => cfg.value.productCols.variantId },
  { accessorKey: 'variantCount', header: 'Variants', enabled: () => cfg.value.productCols.variantCount },
  { accessorKey: 'attrs', header: 'Attributes', enabled: () => cfg.value.productCols.attrs },
  { accessorKey: 'salePrice', header: 'Sales Price', enabled: () => cfg.value.productCols.salePrice },
  { accessorKey: 'price', header: 'Price', enabled: () => cfg.value.productCols.price },
  { accessorKey: 'sales', header: 'Sales', enabled: () => cfg.value.productCols.sales },
  { accessorKey: 'revenue', header: 'Revenues', enabled: () => cfg.value.productCols.revenue },
  { accessorKey: 'rating', header: 'Ratings', enabled: () => cfg.value.productCols.rating },
  { accessorKey: 'reviews', header: 'Reviews', enabled: () => cfg.value.productCols.reviews },
  { accessorKey: 'status', header: 'Status', enabled: () => cfg.value.productCols.status },
  { accessorKey: 'category', header: 'Category', enabled: () => cfg.value.productCols.category },
  { accessorKey: 'inventory', header: 'Inventory', enabled: () => cfg.value.productCols.inventory },
  { accessorKey: 'video', header: 'Video', enabled: () => cfg.value.productCols.video },
  { accessorKey: 'freeShipping', header: 'Free shipping', enabled: () => cfg.value.productCols.freeShipping },
  { accessorKey: 'createdTime', header: 'Created Time', enabled: () => cfg.value.productCols.createdTime },
  { accessorKey: 'updatedTime', header: 'Updated Time', enabled: () => cfg.value.productCols.updatedTime },
  { id: 'action', header: 'Action', enabled: () => cfg.value.productCols.action }
]
const productTableColumns = computed(() => productColumnDefs.filter((col) => col.enabled()).map(({ enabled, ...col }) => col))
const promoColumns = [
  { id: 'no', header: 'NO.' },
  { accessorKey: 'sku', header: 'SKU' },
  { accessorKey: 'detected_time', header: 'Updated Time' },
  { accessorKey: 'product_title', header: 'Products Details' },
  { accessorKey: 'promotion_type', header: 'Type' },
  { accessorKey: 'promotion_name', header: 'Name' },
  { accessorKey: 'discount', header: 'Discount' },
  { accessorKey: 'original_price', header: 'Pre-price' },
  { accessorKey: 'promotion_price', header: 'Post-price' },
  { accessorKey: 'threshold', header: 'Threshold' },
  { accessorKey: 'start_time', header: 'Start Time' },
  { accessorKey: 'end_time', header: 'End Time' }
]
const trendDetailColumns = [
  { accessorKey: 'date', header: 'Date' },
  { accessorKey: 'sale_price', header: 'Sales Price' },
  { accessorKey: 'original_price', header: 'Price' },
  { accessorKey: 'avg_rating', header: 'Ratings' },
  { accessorKey: 'review_total', header: 'Reviews' },
  { accessorKey: 'estimated_sales', header: 'Sales' },
  { accessorKey: 'estimated_revenue', header: 'Revenues' }
]
const trendPromoColumns = [
  { accessorKey: 'detected_time', header: 'Updated Time' },
  { accessorKey: 'sku', header: 'SKU' },
  { accessorKey: 'product_title', header: 'Products Details' },
  { accessorKey: 'promotion_type', header: 'Type' },
  { accessorKey: 'promotion_name', header: 'Name' },
  { accessorKey: 'discount', header: 'Discount' },
  { accessorKey: 'original_price', header: 'Pre-price' },
  { accessorKey: 'promotion_price', header: 'Post-price' },
  { accessorKey: 'threshold', header: 'Threshold' },
  { accessorKey: 'start_time', header: 'Start Time' },
  { accessorKey: 'end_time', header: 'End Time' }
]
const cards = computed<Record<string, any>>(() => {
  const data = overview.value || {}
  return data.cards && typeof data.cards === 'object' ? data.cards : data
})
const reportCurrency = computed(() => cards.value.currency || overview.value?.currency || activeSite.value.currency || currencyForMarket(site.value) || null)
const trends = computed<Record<string, any>[]>(() => asList(overview.value?.trends || [], ['trends', 'items']))
const trendSummary = computed<Record<string, any>>(() => overview.value?.trend_summary || {})
const storeCurrentPeriod = computed(() => trendSummary.value?.current_period || null)
const storePreviousPeriod = computed(() => trendSummary.value?.previous_period || null)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / Number(pageSize.value || 10))))
const productPageButtons = computed(() => paginationPages(page.value, totalPages.value))
const promoPageButtons = computed(() => paginationPages(promoPage.value, promoTotalPages.value))
const allProductCount = computed(() => subTab.value === 'all' ? total.value : Number(cards.value.sku_count ?? cards.value.total_products ?? 0))
const bestsellerProductCount = computed(() => subTab.value === 'bestseller' ? total.value : Number(cards.value.bestseller_count ?? 0))
const latestProductCount = computed(() => subTab.value === 'new' ? total.value : Number(cards.value.new_product_count ?? 0))
const initialReportLoading = computed(() => loading.value && !overview.value && !products.value.length && !promotions.value.length)
const granularity = ref<'day' | 'week' | 'month'>('month')
const aggregatedTrends = computed<Record<string, any>[]>(() => trends.value)
const aggregatedTrendRows = computed(() => aggregatedTrends.value.slice().reverse())
const siteItems = computed(() => sites.value.map((s) => ({
  label: `${s.site || s.name} (${s.brand || 'brand'})`,
  value: s.site || s.name
})))
const granularityItems = [
  { label: 'By Month', value: 'month' },
  { label: 'By Week', value: 'week' },
  { label: 'By Days', value: 'day' }
]
const exportScopeItems = [
  { label: 'Export all', value: 'all' },
  { label: 'Export page', value: 'page' }
]
const productPageSizeItems = [
  { label: '10 / page', value: 10 },
  { label: '20 / page', value: 20 },
  { label: '50 / page', value: 50 },
  { label: '100 / page', value: 100 },
  { label: '200 / page', value: 200 }
]
const EMPTY_SELECT = '__empty__'
const promoPageSizeItems = [
  { label: '20 / page', value: 20 },
  { label: '50 / page', value: 50 },
  { label: '100 / page', value: 100 },
  { label: '200 / page', value: 200 }
]
const promoTypeItems = [
  { label: 'All types', value: EMPTY_SELECT },
  { label: 'Price Promotion', value: 'price' },
  { label: 'Coupons', value: 'coupon' },
  { label: 'Bundle', value: 'bundle' }
]
const timeRangeItems = [
  { label: '近 7 天', value: '7d' },
  { label: '近 30 天', value: '30d' },
  { label: '近 90 天', value: '90d' },
  { label: '全部', value: 'all' }
]
const categoryItems = computed(() => [
  { label: '全部类目', value: EMPTY_SELECT },
  ...categoryOptions.value.map((cat) => ({ label: cat, value: cat }))
])
const categorySelect = computed({
  get: () => filters.value.category || EMPTY_SELECT,
  set: (value: string) => {
    filters.value.category = value === EMPTY_SELECT ? '' : value
  }
})

function isoDate(value: Date) {
  const y = value.getFullYear()
  const m = String(value.getMonth() + 1).padStart(2, '0')
  const d = String(value.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

function defaultDateRange(range: string) {
  if (range === 'all') return { from: '', to: '' }
  const days = Number(String(range || '30d').replace('d', '')) || 30
  const to = new Date()
  const from = new Date(to)
  from.setDate(to.getDate() - Math.max(0, days - 1))
  return { from: isoDate(from), to: isoDate(to) }
}

function applyConfiguredTimeRange(force = false) {
  if (!force && (dateRange.value.from || dateRange.value.to)) return
  dateRange.value = defaultDateRange(cfg.value.timeRange)
}

async function loadSites() {
  sites.value = asList(await listSites(), ['sites', 'items'])
  if (!site.value && sites.value[0]) site.value = sites.value[0].site || sites.value[0].name
}

async function loadCategoryOptions() {
  if (!site.value) {
    categoryOptions.value = []
    return
  }
  try {
    const data = await listCategoriesCross({ sites: site.value })
    const rows = asList(data?.[site.value] || [], ['items', 'categories'])
    categoryOptions.value = rows
      .map((row: Record<string, any>) => String(row.name || '').trim())
      .filter(Boolean)
      .slice(0, 300)
  } catch {
    categoryOptions.value = []
  }
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
    const productId = String(route.query.pid || route.query.product_id || '')
    router.replace({
      path: '/report',
      query: {
        site: site.value,
        ...(workspaceId ? { workspace_id: workspaceId } : {}),
        ...(productId ? { pid: productId, panel: 'trend' } : {}),
      },
    })
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
    promoTotal.value = Number(promosData?.total ?? promotions.value.length ?? 0)
    const nextCards = overviewData?.cards && typeof overviewData.cards === 'object' ? overviewData.cards : overviewData
    total.value = Number(productsData?.total ?? nextCards?.sku_count ?? nextCards?.total_products ?? products.value.length ?? 0)
    const updateTime = overviewData?.last_run || overviewData?.updated_at
    lastUpdate.value = updateTime ? fmtDate(updateTime) : '—'
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function productQueryParams() {
  const params: Record<string, unknown> = {
    site: site.value,
    token: auth.token,
    workspace_id: activeWorkspaceId.value,
    scope: 'products',
    export_scope: productExportScope.value,
  }
  if (productExportScope.value === 'page') {
    params.page = page.value
    params.page_size = pageSize.value
  }
  if (search.value.trim()) params.search = search.value.trim()
  if (subTab.value === 'new' || subTab.value === 'bestseller') params.tab = subTab.value
  for (const [k, v] of Object.entries(filters.value)) {
    if (v !== '' && v !== null && v !== undefined) params[k] = v
  }
  return params
}

function paginationPages(current: number, totalCount: number) {
  const totalValue = Math.max(1, Number(totalCount || 1))
  const currentValue = Math.min(Math.max(1, Number(current || 1)), totalValue)
  const start = Math.max(1, Math.min(currentValue - 2, totalValue - 4))
  const end = Math.min(totalValue, start + 4)
  return Array.from({ length: end - start + 1 }, (_, idx) => start + idx)
}

function exportProducts() {
  window.open(`/api/export/products${qs(productQueryParams())}`, '_blank')
}

function exportPromotions() {
  const params: Record<string, unknown> = {
    site: site.value,
    token: auth.token,
    workspace_id: activeWorkspaceId.value,
    search: promoSearch.value.trim(),
    type: promoType.value,
    date_from: promoDateFrom.value,
    date_to: promoDateTo.value,
    export_scope: promoExportScope.value,
  }
  if (promoExportScope.value === 'page') {
    params.page = promoPage.value
    params.page_size = promoPageSize.value
  }
  window.open(`/api/export/promotions${qs(params)}`, '_blank')
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
  const out = fmtDate(String(value))
  return out === '-' ? '--' : out
}

function dateOnly(value: unknown) {
  if (!value) return '--'
  const out = fmtDateOnly(String(value))
  return out === '-' ? '--' : out
}

function dateTime(value: unknown) {
  if (!value) return '--'
  const out = fmtDate(String(value))
  return out === '-' ? '--' : out
}

function productCreatedTime(p: Record<string, any>) {
  return p.published_at || p.created_time
}

function attrEntries(p: Record<string, any>) {
  const attrs = p.attributes && typeof p.attributes === 'object' ? p.attributes : {}
  const noisyKeys = new Set(['offers', 'offer', 'promotions', 'promotion', 'coupons', 'coupon', 'deals', 'deal'])
  return Object.entries(attrs).filter(([key, v]) => {
    if (noisyKeys.has(String(key).toLowerCase())) return false
    if (v === null || v === undefined || v === '') return false
    if (Array.isArray(v) || typeof v === 'object') return false
    return true
  })
}

function productLabels(p: Record<string, any>) {
  const labels: string[] = []
  const push = (value: unknown) => {
    const label = String(value || '').trim()
    if (label && !labels.includes(label)) labels.push(label)
  }
  push(p.label)
  if (p.is_bestseller) push('TOP')
  if (p.is_new) push('NEW')
  if (Array.isArray(p.promotion_labels)) {
    for (const label of p.promotion_labels.slice(0, 3)) push(label)
  }
  return labels
}

function ratingLabel(p: Record<string, any>) {
  const rating = p.ratings ?? p.rating
  if (rating === null || rating === undefined || rating === '') return '--'
  const reviews = p.review_count != null ? ` (${Number(p.review_count).toLocaleString()})` : ''
  return `${rating}${reviews}`
}

function ratingStars(p: Record<string, any>) {
  const rating = Number(p.ratings ?? p.rating)
  if (!Number.isFinite(rating) || rating <= 0) return '—'
  const rounded = Math.max(0, Math.min(5, Math.round(rating)))
  return `${'★'.repeat(rounded)}${'☆'.repeat(5 - rounded)}`
}

function productStatusLabel(value: unknown) {
  const key = String(value || '').trim().toLowerCase().replace(/[\s-]+/g, '_')
  if (!key) return '--'
  if (['on_sale', 'active', 'available', 'sale'].includes(key)) return 'on sale'
  if (['out_of_stock', 'sold_out', 'unavailable', 'oos'].includes(key)) return 'Out of stock'
  if (['discontinued', 'offline', 'removed', 'inactive'].includes(key)) return 'discontinued'
  return String(value)
}

function productStatusTone(value: unknown) {
  const label = productStatusLabel(value).toLowerCase()
  if (label === 'on sale') return 'ok'
  if (label === 'out of stock') return 'warn'
  if (label === 'discontinued') return 'idle'
  return 'idle'
}

function promoTitle(p: Record<string, any>) {
  return p.product_title || p.title || p.promotion_name || p.sku || '--'
}

function promoName(p: Record<string, any>) {
  return p.promotion_name || p.name || p.promotion_type || p.type || '--'
}

function promoTypeLabel(value: unknown) {
  const key = String(value || '').trim().toLowerCase().replace(/[\s-]+/g, '_')
  if (!key) return '--'
  if (['coupon', 'coupons'].includes(key)) return 'Coupons'
  if (['price', 'price_promotion', 'sale', 'discount'].includes(key)) return 'Price Promotion'
  if (['bundle', 'bundle_promotion'].includes(key)) return 'Bundle'
  return String(value)
}

function promoLabels(p: Record<string, any>) {
  const labels: string[] = []
  const push = (value: unknown) => {
    const label = String(value || '').trim()
    if (label && !labels.includes(label)) labels.push(label)
  }
  push(p.product_label)
  if (p.is_bestseller) push('TOP')
  if (p.is_new) push('NEW')
  push(p.promotion_name)
  return labels.slice(0, 4)
}

function rowCurrency(row?: Record<string, any> | null) {
  return row?.currency || detail.value?.currency || reportCurrency.value || currencyForMarket(row?.site || site.value) || null
}

function promoDiscount(p: Record<string, any>) {
  if (p.discount_percent !== null && p.discount_percent !== undefined) return `${p.discount_percent}%`
  const original = Number(p.original_price || 0)
  const promo = Number(p.promotion_price || 0)
  const currency = rowCurrency(p)
  if (original > 0 && promo > 0 && original > promo) return fmtPrice(original - promo, currency)
  if (p.promotion_price !== null && p.promotion_price !== undefined) return fmtPrice(p.promotion_price, currency)
  return '--'
}

// 商品详情 + 价格历史弹窗
const detail = ref<Record<string, any> | null>(null)
const priceHistory = ref<Record<string, any>[]>([])
const productTrendDetail = ref<Record<string, any> | null>(null)
const selectedTrendProductId = ref<string | number>('')
const detailLoading = ref(false)
const trendGranularity = ref<'day' | 'week' | 'month'>('month')
const trendDateFrom = ref('')
const trendDateTo = ref('')
const trendPromoSearch = ref('')
const trendPromoType = ref('')
const trendPromoSku = ref('')
const trendPromoPage = ref(1)
const trendPromoPageSize = ref(20)
const trendPromoExportScope = ref<'all' | 'page'>('all')
const productTrendSeries = [
  { key: 'estimated_sales', name: 'Sales', color: '#f59e0b' },
  { key: 'estimated_revenue', name: 'Revenues', color: '#ef4444' },
  { key: 'sale_price', name: 'Price', color: '#3b82f6' },
  { key: 'review_total', name: 'Reviews', color: '#8b5cf6' },
  { key: 'avg_rating', name: 'Ratings', color: '#ec4899', yAxisIndex: 1 },
]
const productTrendRows = computed(() => productTrendDetail.value?.trend || priceHistory.value || [])
const productPromotions = computed(() => productTrendDetail.value?.promotions || [])
const productSummary = computed(() => productTrendDetail.value?.summary || {})
const productTrendVariants = computed<Record<string, any>[]>(() => productTrendDetail.value?.variants || [])
const trendVariantItems = computed(() => {
  const rows = productTrendVariants.value.map((item) => ({ label: variantLabel(item), value: item.id }))
  if (!rows.length && detail.value) return [{ label: detail.value.sku || String(detail.value.id), value: detail.value.id }]
  return rows
})
const trendPromoSkuItems = computed(() => [
  { label: 'All SKU promotions', value: EMPTY_SELECT },
  ...productTrendVariants.value.map((item) => ({ label: item.sku || String(item.id), value: item.sku }))
])
const productCurrentPeriod = computed(() => productSummary.value?.current_period || null)
const productPreviousPeriod = computed(() => productSummary.value?.previous_period || null)
const trendGranularityLabel = computed(() => granularityLabel(trendGranularity.value))
const trendPromoTotal = computed(() => Number(productSummary.value?.promotion_total ?? productSummary.value?.promotion_count ?? productPromotions.value.length ?? 0))
const trendPromoTotalPages = computed(() => Math.max(1, Math.ceil(trendPromoTotal.value / Number(trendPromoPageSize.value || 20))))
const trendPromoPageButtons = computed(() => paginationPages(trendPromoPage.value, trendPromoTotalPages.value))
function productTrendParams(includeToken = false) {
  return {
    ...(includeToken ? { token: auth.token, workspace_id: activeWorkspaceId.value } : {}),
    granularity: trendGranularity.value,
    date_from: trendDateFrom.value,
    date_to: trendDateTo.value,
    promo_search: trendPromoSearch.value.trim(),
    promo_type: trendPromoType.value,
    promo_sku: trendPromoSku.value,
    promo_page: trendPromoPage.value,
    promo_page_size: trendPromoPageSize.value,
  }
}
async function openDetail(id: number | string | undefined) {
  if (id === undefined || id === null) return
  selectedTrendProductId.value = id
  trendPromoPage.value = 1
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
function applyProductTrendFilters() {
  trendPromoPage.value = 1
  reloadProductTrend()
}
function resetProductTrendFilters() {
  trendDateFrom.value = ''
  trendDateTo.value = ''
  trendPromoSearch.value = ''
  trendPromoType.value = ''
  trendPromoSku.value = ''
  trendPromoPage.value = 1
  reloadProductTrend()
}
async function switchProductTrendSku() {
  if (!selectedTrendProductId.value) return
  trendPromoSku.value = ''
  trendPromoPage.value = 1
  await openDetail(selectedTrendProductId.value)
}
function granularityLabel(value: string) {
  if (value === 'month') return 'By Month'
  if (value === 'week') return 'By Week'
  return 'By Days'
}
function changeTrendPromoPage(next: number) {
  const value = Math.min(trendPromoTotalPages.value, Math.max(1, next))
  if (value === trendPromoPage.value) return
  trendPromoPage.value = value
  reloadProductTrend()
}

function setProductPage(next: number) {
  page.value = next
}

function setPromoPage(next: number) {
  promoPage.value = next
}

function setPromoType(value: string) {
  promoType.value = value === EMPTY_SELECT ? '' : String(value)
  applyPromoFilters()
}

async function setSelectedTrendProduct(value: string | number) {
  selectedTrendProductId.value = value
  await switchProductTrendSku()
}

function setTrendGranularity(value: 'day' | 'week' | 'month') {
  trendGranularity.value = value
  applyProductTrendFilters()
}

function setTrendPromoSku(value: string) {
  trendPromoSku.value = value === EMPTY_SELECT ? '' : String(value)
  applyProductTrendFilters()
}

function setTrendPromoType(value: string) {
  trendPromoType.value = value === EMPTY_SELECT ? '' : String(value)
  applyProductTrendFilters()
}

function setTrendPromoPageSize(value: number) {
  trendPromoPageSize.value = Number(value)
  trendPromoPage.value = 1
  reloadProductTrend()
}

function exportProductTrend() {
  if (!detail.value?.id) return
  window.open(`/api/export/product-trend${qs({ pid: detail.value.id, ...productTrendParams(true), export_scope: trendPromoExportScope.value })}`, '_blank')
}
function productTrendHref(p: Record<string, any>) {
  const params: Record<string, unknown> = {
    site: site.value,
    pid: p.id,
    panel: 'trend',
  }
  if (activeWorkspaceId.value) params.workspace_id = activeWorkspaceId.value
  return `/report${qs(params)}`
}
async function openProductTrend(p: Record<string, any>) {
  const params: Record<string, string> = {
    site: site.value,
    pid: String(p.id),
    panel: 'trend',
  }
  if (activeWorkspaceId.value) params.workspace_id = String(activeWorkspaceId.value)
  router.replace({ path: '/report', query: params })
  openedRouteProductId.value = String(p.id || '')
  tab.value = 'product'
  await openDetail(p.id)
}
function promoSkuLabel(p: Record<string, any>) {
  return p.listing_sku || p.sku || p.item_id || '--'
}
function promoVariantText(p: Record<string, any>) {
  const skus = Array.isArray(p.variant_skus) ? p.variant_skus.filter(Boolean) : []
  const label = promoSkuLabel(p)
  const count = Number(p.variant_count || skus.length || 0)
  if (count <= 1 && (!p.sku || p.sku === label)) return ''
  const variants = skus.filter((sku: string) => sku !== label).slice(0, 3)
  if (variants.length) {
    return `${count} SKU: ${variants.join(' / ')}${count > variants.length + 1 ? ' ...' : ''}`
  }
  return `${count || 1} SKU variants`
}
function variantLabel(item: Record<string, any>) {
  const parts = [item.sku || `#${item.id}`]
  if (item.variant_id && item.variant_id !== item.sku) parts.push(item.variant_id)
  const attrs = item.attributes && typeof item.attributes === 'object'
    ? Object.entries(item.attributes).slice(0, 2).map(([k, v]) => `${k}:${v}`).join(' / ')
    : ''
  if (attrs) parts.push(attrs)
  return parts.join(' · ')
}
function metricValue(...values: unknown[]) {
  return values.find((value) => value !== null && value !== undefined && value !== '')
}
function metricNumber(...values: unknown[]) {
  const value = metricValue(...values)
  if (value === undefined) return '--'
  const n = Number(value)
  return Number.isFinite(n) ? n.toLocaleString() : '--'
}
function metricPrice(currency: string | null | undefined, ...values: unknown[]) {
  const value = metricValue(...values)
  return value === undefined ? '--' : fmtPrice(value, currency)
}
function metricPercent(...values: unknown[]) {
  const value = metricValue(...values)
  if (value === undefined) return '--'
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(2)}%` : '--'
}
function storeDelta(key: string, format: 'number' | 'price' | 'percent' = 'number') {
  const cur = Number(storeCurrentPeriod.value?.[key] || 0)
  const prev = Number(storePreviousPeriod.value?.[key] || 0)
  if (!storePreviousPeriod.value) return 'No previous period'
  const delta = cur - prev
  if (format === 'price') return `${delta >= 0 ? '+' : ''}${fmtPrice(delta, reportCurrency.value)} vs previous`
  if (format === 'percent') return `${delta >= 0 ? '+' : ''}${delta.toFixed(2)} vs previous`
  return `${delta >= 0 ? '+' : ''}${delta.toLocaleString()} vs previous`
}
function thirdPartySub(key: string, format: 'number' | 'percent' = 'number') {
  const value = storeCurrentPeriod.value?.[key] ?? cards.value?.[key]
  if (value === null || value === undefined) return '暂无第三方数据'
  return storeDelta(key, format)
}
function periodDelta(key: string, format: 'number' | 'price' = 'number') {
  const cur = Number(productCurrentPeriod.value?.[key] || 0)
  const prev = Number(productPreviousPeriod.value?.[key] || 0)
  if (!productPreviousPeriod.value) return 'No previous period'
  const delta = cur - prev
  if (format === 'price') return `${delta >= 0 ? '+' : ''}${fmtPrice(delta, productSummary.value?.currency || detail.value?.currency)} vs previous`
  return `${delta >= 0 ? '+' : ''}${delta.toLocaleString()} vs previous`
}
function closeDetail() {
  detail.value = null
  priceHistory.value = []
  productTrendDetail.value = null
  selectedTrendProductId.value = ''
}

const openedRouteProductId = ref('')
async function openRouteProductTrend() {
  const productId = String(route.query.pid || route.query.product_id || '')
  if (!productId || openedRouteProductId.value === productId) return
  tab.value = 'product'
  openedRouteProductId.value = productId
  await openDetail(productId)
}

function saveCfg() {
  localStorage.setItem('sc_report_cfg', JSON.stringify(cfg.value))
  applyConfiguredTimeRange(true)
  cfgOpen.value = false
  loadReport()
}

function resetCfg() {
  cfg.value = cloneCfg()
  localStorage.removeItem('sc_report_cfg')
  applyConfiguredTimeRange(true)
}

watch(site, async () => {
  page.value = 1
  promoPage.value = 1
  filters.value.category = ''
  await loadCategoryOptions()
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
  applyConfiguredTimeRange()
  await loadSites()
  await loadCategoryOptions()
  await loadReport()
  await openRouteProductTrend()
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
            <USelect v-model="site" class="report-select site-select" :items="siteItems" value-key="value" />
            <button v-if="canEdit" class="icon-btn" @click="cfgOpen = true">⚙ 自定义</button>
            <button class="icon-btn" @click="loadReport">↻ 刷新</button>
          </div>
        </div>
        <div class="meta">
            <span>总 SKU 行: <b>{{ cards.sku_count ?? total ?? '--' }}</b></span>
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
          <div v-if="cfg.kpiCards.sku" class="stat"><div class="lbl">SKU</div><div class="val">{{ metricNumber(storeCurrentPeriod?.sku_count, cards.sku_count) }}</div><div class="sub">{{ storeDelta('sku_count') }}</div></div>
          <div v-if="cfg.kpiCards.new" class="stat"><div class="lbl">New Products</div><div class="val">{{ metricNumber(storeCurrentPeriod?.new_product_count, cards.new_product_count) }}</div><div class="sub">{{ storeDelta('new_product_count') }}</div></div>
          <div v-if="cfg.kpiCards.sales" class="stat"><div class="lbl">30-Day Sales</div><div class="val">{{ metricNumber(storeCurrentPeriod?.estimated_sales, cards.thirty_day_sales) }}</div><div class="sub">{{ storeDelta('estimated_sales') }}</div></div>
          <div v-if="cfg.kpiCards.revenue" class="stat"><div class="lbl">30-Day Revenues</div><div class="val">{{ metricPrice(reportCurrency, storeCurrentPeriod?.estimated_revenue, cards.thirty_day_revenue) }}</div><div class="sub">{{ storeDelta('estimated_revenue', 'price') }}</div></div>
          <div v-if="cfg.kpiCards.traffic" class="stat"><div class="lbl">30-Day Traffic</div><div class="val">{{ metricNumber(storeCurrentPeriod?.traffic, cards.traffic) }}</div><div class="sub">{{ thirdPartySub('traffic') }}</div></div>
          <div v-if="cfg.kpiCards.conversion" class="stat"><div class="lbl">30-Day Conversion Rate</div><div class="val">{{ metricPercent(storeCurrentPeriod?.conversion_rate, cards.conversion_rate) }}</div><div class="sub">{{ thirdPartySub('conversion_rate', 'percent') }}</div></div>
        </div>

        <div v-if="cfg.sections.trend" class="section">
          <div class="section-head">
            <h3>📈 Sales Trends <span class="desc">分析整体销售情况和品牌市场份额</span></h3>
            <div class="actions">
              <USelect v-model="granularity" class="report-select gran-select" :items="granularityItems" value-key="value" />
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
          <div v-if="aggregatedTrends.length" class="store-trend-table">
            <div class="store-trend-title">Trend Details</div>
            <UTable class="ui-table" :data="aggregatedTrendRows" :columns="trendColumns" sticky="header">
              <template #date-cell="{ row }">{{ row.original.date || row.original.source_date || '-' }}</template>
              <template #sku_count-cell="{ row }">{{ fmtNumber(row.original.sku_count) }}</template>
              <template #new_product_count-cell="{ row }">{{ fmtNumber(row.original.new_product_count) }}</template>
              <template #estimated_sales-cell="{ row }">{{ fmtNumber(row.original.estimated_sales) }}</template>
              <template #estimated_revenue-cell="{ row }">{{ fmtPrice(row.original.estimated_revenue, reportCurrency) }}</template>
              <template #traffic-cell="{ row }">{{ row.original.traffic != null ? fmtNumber(row.original.traffic) : '暂无第三方数据' }}</template>
              <template #conversion_rate-cell="{ row }">{{ row.original.conversion_rate != null ? Number(row.original.conversion_rate).toFixed(2) + '%' : '暂无第三方数据' }}</template>
              <template #review_total-cell="{ row }">{{ row.original.review_total != null ? fmtNumber(row.original.review_total) : '-' }}</template>
            </UTable>
          </div>
        </div>
      </template>

      <template v-if="(tab === 'product' || tab === 'shop') && cfg.sections.products">
        <div class="section">
          <div class="section-head">
            <h3>📦 产品分析 <span class="desc">查看产品的基本信息和详细属性</span></h3>
            <div class="actions">
              <button class="icon-btn" @click="loadReport">↻ Refresh</button>
              <USelect v-if="canEdit" v-model="productExportScope" class="report-select export-scope" :items="exportScopeItems" value-key="value" aria-label="Product export scope" />
              <button v-if="canEdit" class="icon-btn" @click="exportProducts">↓ Export</button>
            </div>
          </div>
          <div class="sub-tabs">
            <button :class="{ active: subTab === 'all' }" @click="subTab = 'all'; page = 1">All Products({{ allProductCount || 0 }})</button>
            <button :class="{ active: subTab === 'bestseller' }" @click="subTab = 'bestseller'; page = 1">BestSelling Products({{ bestsellerProductCount || 0 }})</button>
            <button :class="{ active: subTab === 'new' }" @click="subTab = 'new'; page = 1">Newest Products({{ latestProductCount || 0 }})</button>
            <div class="right">
              <button class="icon-btn" :class="{ 'filter-on': activeFilterCount > 0 }" @click="filtersOpen = !filtersOpen">☷ Filter<span v-if="activeFilterCount">({{ activeFilterCount }})</span></button>
              <input class="search-box" v-model="search" placeholder="🔍 Title / SKU / URL / Attributes" @keyup.enter="loadReport" />
            </div>
          </div>
          <DataLoadingPanel class="report-table-wrap" :loading="loading" :has-data="products.length > 0" label="正在更新产品列表">
            <UTable class="ui-table report-data-table" :data="products" :columns="productTableColumns" :loading="loading" sticky="header" empty="暂无数据 · 切换 site 或先抓取">
              <template #no-cell="{ row }">{{ (page - 1) * pageSize + row.index + 1 }}</template>
              <template #sku-cell="{ row }"><a class="sku-link" :href="row.original.product_url || undefined" :target="row.original.product_url ? '_blank' : undefined" rel="noopener" @click.stop>{{ row.original.sku || row.original.item_id }}</a></template>
              <template #title-cell="{ row }">
                <div class="title-cell" @click="openDetail(row.original.id)">
                  <img v-if="row.original.image" :src="row.original.image" class="thumb thumb-img" alt="" />
                  <div v-else class="thumb">📦</div>
                  <div class="info">
                    <span class="title-text" :title="productTitle(row.original)">{{ productTitle(row.original) }}</span>
                    <div v-if="productLabels(row.original).length" class="mini-tags">
                      <span v-for="(tag, idx) in productLabels(row.original).slice(0, 4)" :key="`${tag}-${idx}`">{{ tag }}</span>
                    </div>
                  </div>
                </div>
              </template>
              <template #label-cell="{ row }">
                <div v-if="productLabels(row.original).length" class="mini-tags">
                  <span v-for="tag in productLabels(row.original)" :key="tag">{{ tag }}</span>
                </div>
                <span v-else>--</span>
              </template>
              <template #variantId-cell="{ row }">{{ row.original.variant_id || row.original.variantId || '--' }}</template>
              <template #variantCount-cell="{ row }">{{ row.original.variant_count ?? 1 }}</template>
              <template #attrs-cell="{ row }">
                <div class="attr-cell">
                  <div v-for="[key, val] in attrEntries(row.original).slice(0, 4)" :key="key">{{ key }}: {{ val }}</div>
                  <div v-if="attrEntries(row.original).length > 4" style="color:#9ca3af">+{{ attrEntries(row.original).length - 4 }}</div>
                  <div v-if="attrEntries(row.original).length === 0" style="color:#9ca3af">--</div>
                </div>
              </template>
              <template #salePrice-cell="{ row }">{{ fmtPrice(row.original.sale_price ?? row.original.price, rowCurrency(row.original)) }}</template>
              <template #price-cell="{ row }">{{ fmtPrice(row.original.original_price, rowCurrency(row.original)) }}</template>
              <template #sales-cell="{ row }">{{ row.original.thirty_day_sales != null ? Number(row.original.thirty_day_sales).toLocaleString() : '0' }}</template>
              <template #revenue-cell="{ row }">{{ fmtPrice(row.original.thirty_day_revenue ?? 0, rowCurrency(row.original)) }}</template>
              <template #rating-cell="{ row }">
                <div class="rating-cell">
                  <span class="stars">{{ ratingStars(row.original) }}</span>
                  <small>{{ ratingLabel(row.original) }}</small>
                </div>
              </template>
              <template #reviews-cell="{ row }">{{ row.original.review_count != null ? Number(row.original.review_count).toLocaleString() : '0' }}</template>
              <template #status-cell="{ row }"><span class="product-status" :class="productStatusTone(row.original.status)">{{ productStatusLabel(row.original.status) }}</span></template>
              <template #category-cell="{ row }"><span class="title-text" :title="row.original.category_path">{{ row.original.category_path || '--' }}</span></template>
              <template #inventory-cell="{ row }">{{ row.original.inventory ?? '--' }}</template>
              <template #video-cell="{ row }">{{ yesNo(row.original.has_video) }}</template>
              <template #freeShipping-cell="{ row }">{{ yesNo(row.original.has_free_shipping) }}</template>
              <template #createdTime-cell="{ row }">{{ dateOnly(productCreatedTime(row.original)) }}</template>
              <template #updatedTime-cell="{ row }">{{ shortDate(row.original.updated_time) }}</template>
              <template #action-cell="{ row }"><a class="row-action" :href="productTrendHref(row.original)" @click.stop.prevent="openProductTrend(row.original)">Trend</a></template>
            </UTable>
          </DataLoadingPanel>
          <div class="pagination">
            <UPagination
              :page="page"
              :total="total"
              :items-per-page="Number(pageSize)"
              size="sm"
              show-edges
              @update:page="setProductPage"
            />
            <USelect v-model="pageSize" class="report-select page-size-select" :items="productPageSizeItems" value-key="value" @update:model-value="page = 1" />
          </div>
        </div>
      </template>

      <template v-if="(tab === 'promo' || tab === 'shop') && cfg.sections.promos">
        <div class="section">
          <div class="section-head">
            <h3>🎁 销售促销 <span class="desc">查看产品的促销信息</span></h3>
            <div class="actions">
              <button class="icon-btn" @click="loadReport">↻ Refresh</button>
              <USelect v-if="canEdit" v-model="promoExportScope" class="report-select export-scope" :items="exportScopeItems" value-key="value" aria-label="Promotion export scope" />
              <button v-if="canEdit" class="icon-btn" @click="exportPromotions">↓ Export</button>
            </div>
          </div>
          <div class="promo-filters">
            <input v-model="promoSearch" placeholder="Search SKU / Product title / URL / Campaign" @keyup.enter="applyPromoFilters" />
            <USelect :model-value="promoType || EMPTY_SELECT" class="report-select promo-type-select" :items="promoTypeItems" value-key="value" @update:model-value="setPromoType" />
            <input v-model="promoDateFrom" type="date" @change="applyPromoFilters" />
            <input v-model="promoDateTo" type="date" @change="applyPromoFilters" />
            <button class="icon-btn" @click="applyPromoFilters">Filter</button>
            <button class="icon-btn" @click="resetPromoFilters">Reset</button>
          </div>
          <DataLoadingPanel class="report-table-wrap" :loading="loading" :has-data="promotions.length > 0" label="正在更新促销列表">
            <UTable class="ui-table report-data-table" :data="promotions" :columns="promoColumns" :loading="loading" sticky="header" empty="暂无促销数据">
              <template #no-cell="{ row }">{{ (promoPage - 1) * promoPageSize + row.index + 1 }}</template>
              <template #sku-cell="{ row }">
                <div class="sku-stack">
                  <a class="sku-link" :href="row.original.product_url || undefined" :target="row.original.product_url ? '_blank' : undefined" rel="noopener">{{ promoSkuLabel(row.original) }}</a>
                  <span v-if="promoVariantText(row.original)" class="sku-variants">{{ promoVariantText(row.original) }}</span>
                </div>
              </template>
              <template #detected_time-cell="{ row }">{{ shortDate(row.original.detected_time || row.original.updated_at) }}</template>
              <template #product_title-cell="{ row }">
                <div class="title-cell">
                  <img v-if="row.original.product_image" :src="row.original.product_image" class="thumb thumb-img" alt="" />
                  <div v-else class="thumb">📦</div>
                  <div class="info">
                    <span class="title-text" :title="promoTitle(row.original)">{{ promoTitle(row.original) }}</span>
                    <div v-if="promoLabels(row.original).length" class="mini-tags">
                      <span v-for="(label, idx) in promoLabels(row.original)" :key="`${label}-${idx}`">{{ label }}</span>
                    </div>
                  </div>
                </div>
              </template>
              <template #promotion_type-cell="{ row }">{{ promoTypeLabel(row.original.promotion_type || row.original.type) }}</template>
              <template #promotion_name-cell="{ row }"><span class="title-text" :title="promoName(row.original)">{{ promoName(row.original) }}</span></template>
              <template #discount-cell="{ row }">{{ promoDiscount(row.original) }}</template>
              <template #original_price-cell="{ row }">{{ fmtPrice(row.original.original_price, rowCurrency(row.original)) }}</template>
              <template #promotion_price-cell="{ row }">{{ fmtPrice(row.original.promotion_price, rowCurrency(row.original)) }}</template>
              <template #threshold-cell="{ row }">{{ row.original.threshold || '--' }}</template>
              <template #start_time-cell="{ row }">{{ dateTime(row.original.start_time) }}</template>
              <template #end_time-cell="{ row }">{{ dateTime(row.original.end_time) }}</template>
            </UTable>
          </DataLoadingPanel>
          <div v-if="promoTotal" class="pagination">
            <UPagination
              :page="promoPage"
              :total="promoTotal"
              :items-per-page="Number(promoPageSize)"
              size="sm"
              show-edges
              @update:page="setPromoPage"
            />
            <USelect v-model="promoPageSize" class="report-select page-size-select" :items="promoPageSizeItems" value-key="value" @update:model-value="promoPage = 1" />
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
            <div class="cfg-row"><input id="cs-trend" type="checkbox" v-model="cfg.sections.trend"><label for="cs-trend">Sales Trends chart</label></div>
            <div class="cfg-row"><input id="cs-prod" type="checkbox" v-model="cfg.sections.products"><label for="cs-prod">Product Analysis table</label></div>
            <div class="cfg-row"><input id="cs-promo" type="checkbox" v-model="cfg.sections.promos"><label for="cs-promo">Sales Promotion table</label></div>
          </div>
          <div v-if="cfg.sections.kpi" class="cfg-group">
            <h4>KPI 卡片（6 选 N）</h4>
            <div class="cfg-row"><input id="ck-sku" type="checkbox" v-model="cfg.kpiCards.sku"><label for="ck-sku">SKU</label></div>
            <div class="cfg-row"><input id="ck-new" type="checkbox" v-model="cfg.kpiCards.new"><label for="ck-new">New Products</label></div>
            <div class="cfg-row"><input id="ck-sales" type="checkbox" v-model="cfg.kpiCards.sales"><label for="ck-sales">30-Day Sales</label></div>
            <div class="cfg-row"><input id="ck-rev" type="checkbox" v-model="cfg.kpiCards.revenue"><label for="ck-rev">30-Day Revenues</label></div>
            <div class="cfg-row"><input id="ck-tra" type="checkbox" v-model="cfg.kpiCards.traffic"><label for="ck-tra">30-Day Traffic</label></div>
            <div class="cfg-row"><input id="ck-cv" type="checkbox" v-model="cfg.kpiCards.conversion"><label for="ck-cv">30-Day Conversion Rate</label></div>
          </div>
          <div v-if="cfg.sections.products" class="cfg-group">
            <h4>Product table columns</h4>
            <div class="cfg-row"><input id="cc-sku" type="checkbox" v-model="cfg.productCols.sku"><label for="cc-sku">SKU</label></div>
            <div class="cfg-row"><input id="cc-title" type="checkbox" v-model="cfg.productCols.title"><label for="cc-title">Products Details</label></div>
            <div class="cfg-row"><input id="cc-label" type="checkbox" v-model="cfg.productCols.label"><label for="cc-label">Label</label></div>
            <div class="cfg-row"><input id="cc-variant" type="checkbox" v-model="cfg.productCols.variantId"><label for="cc-variant">VariantId</label></div>
            <div class="cfg-row"><input id="cc-variant-count" type="checkbox" v-model="cfg.productCols.variantCount"><label for="cc-variant-count">Variants</label></div>
            <div class="cfg-row"><input id="cc-attr" type="checkbox" v-model="cfg.productCols.attrs"><label for="cc-attr">Attributes</label></div>
            <div class="cfg-row"><input id="cc-sale-price" type="checkbox" v-model="cfg.productCols.salePrice"><label for="cc-sale-price">Sales Price</label></div>
            <div class="cfg-row"><input id="cc-price" type="checkbox" v-model="cfg.productCols.price"><label for="cc-price">Price</label></div>
            <div class="cfg-row"><input id="cc-sales" type="checkbox" v-model="cfg.productCols.sales"><label for="cc-sales">Sales</label></div>
            <div class="cfg-row"><input id="cc-revenue" type="checkbox" v-model="cfg.productCols.revenue"><label for="cc-revenue">Revenues</label></div>
            <div class="cfg-row"><input id="cc-rating" type="checkbox" v-model="cfg.productCols.rating"><label for="cc-rating">Ratings</label></div>
            <div class="cfg-row"><input id="cc-reviews" type="checkbox" v-model="cfg.productCols.reviews"><label for="cc-reviews">Reviews</label></div>
            <div class="cfg-row"><input id="cc-status" type="checkbox" v-model="cfg.productCols.status"><label for="cc-status">Status</label></div>
            <div class="cfg-row"><input id="cc-category" type="checkbox" v-model="cfg.productCols.category"><label for="cc-category">Category</label></div>
            <div class="cfg-row"><input id="cc-inventory" type="checkbox" v-model="cfg.productCols.inventory"><label for="cc-inventory">Inventory</label></div>
            <div class="cfg-row"><input id="cc-video" type="checkbox" v-model="cfg.productCols.video"><label for="cc-video">Video</label></div>
            <div class="cfg-row"><input id="cc-shipping" type="checkbox" v-model="cfg.productCols.freeShipping"><label for="cc-shipping">Free shipping</label></div>
            <div class="cfg-row"><input id="cc-created" type="checkbox" v-model="cfg.productCols.createdTime"><label for="cc-created">Created Time</label></div>
            <div class="cfg-row"><input id="cc-updated" type="checkbox" v-model="cfg.productCols.updatedTime"><label for="cc-updated">Updated Time</label></div>
            <div class="cfg-row"><input id="cc-action" type="checkbox" v-model="cfg.productCols.action"><label for="cc-action">Action</label></div>
          </div>
          <div class="cfg-group">
            <h4>时间范围</h4>
            <div class="cfg-row">
              <label style="flex:0">默认</label>
              <USelect v-model="cfg.timeRange" class="report-select cfg-select" :items="timeRangeItems" value-key="value" />
            </div>
          </div>
        </div>
        <div class="cfg-foot">
          <button @click="resetCfg">恢复默认</button>
          <button class="primary" @click="saveCfg">保存配置</button>
        </div>
      </div>

      <!-- 产品筛选弹窗 -->
      <div v-if="filtersOpen" class="od-modal" @click.self="filtersOpen = false">
        <div class="od-modal-card filter-modal">
          <div class="od-modal-head">
            <h3>产品筛选</h3>
            <button class="od-x" @click="filtersOpen = false">✕</button>
          </div>
          <div class="filter-grid">
            <label>Category
              <USelect v-model="categorySelect" class="report-select filter-select" :items="categoryItems" value-key="value" />
            </label>
            <label>Status
              <span class="choice-row">
                <button type="button" :class="{ active: filters.status === '' }" @click="filters.status = ''">All</button>
                <button type="button" :class="{ active: filters.status === 'on_sale' }" @click="filters.status = 'on_sale'">on sale</button>
                <button type="button" :class="{ active: filters.status === 'out_of_stock' }" @click="filters.status = 'out_of_stock'">out of stock</button>
              </span>
            </label>
            <label>Ratings<span class="rng"><input v-model="filters.min_rating" type="number" step="0.1" placeholder="min" /><input v-model="filters.max_rating" type="number" step="0.1" placeholder="max" /></span></label>
            <label>Reviews<span class="rng"><input v-model="filters.min_reviews" type="number" placeholder="min" /><input v-model="filters.max_reviews" type="number" placeholder="max" /></span></label>
            <label>Price<span class="rng"><input v-model="filters.min_price" type="number" placeholder="min" /><input v-model="filters.max_price" type="number" placeholder="max" /></span></label>
            <label>Sales<span class="rng"><input v-model="filters.min_sales" type="number" placeholder="min" /><input v-model="filters.max_sales" type="number" placeholder="max" /></span></label>
            <label>Revenues<span class="rng"><input v-model="filters.min_revenue" type="number" placeholder="min" /><input v-model="filters.max_revenue" type="number" placeholder="max" /></span></label>
            <label>Variants<span class="rng"><input v-model="filters.min_variants" type="number" placeholder="min" /><input v-model="filters.max_variants" type="number" placeholder="max" /></span></label>
            <label>Video
              <span class="choice-row">
                <button type="button" :class="{ active: filters.has_video === '' }" @click="filters.has_video = ''">All</button>
                <button type="button" :class="{ active: filters.has_video === 'true' }" @click="filters.has_video = 'true'">YES</button>
                <button type="button" :class="{ active: filters.has_video === 'false' }" @click="filters.has_video = 'false'">NO</button>
              </span>
            </label>
            <label>Free Shipping
              <span class="choice-row">
                <button type="button" :class="{ active: filters.free_shipping === '' }" @click="filters.free_shipping = ''">All</button>
                <button type="button" :class="{ active: filters.free_shipping === 'true' }" @click="filters.free_shipping = 'true'">YES</button>
                <button type="button" :class="{ active: filters.free_shipping === 'false' }" @click="filters.free_shipping = 'false'">NO</button>
              </span>
            </label>
            <label>Created Time<span class="rng"><input v-model="filters.created_from" type="date" /><input v-model="filters.created_to" type="date" /></span></label>
          </div>
          <div class="filter-actions">
            <button @click="resetFilters">Reset</button>
            <button class="primary" @click="applyFilters">Apply filters</button>
          </div>
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
                  <span>Sales Price <b>{{ fmtPrice(productSummary.price ?? detail.sale_price, productSummary.currency || detail.currency) }}</b></span>
                  <span v-if="productSummary.original_price || detail.original_price">Price <s>{{ fmtPrice(productSummary.original_price ?? detail.original_price, productSummary.currency || detail.currency) }}</s></span>
                  <span>Ratings <b>{{ ratingStars({ ratings: productSummary.ratings ?? detail.ratings }) }} {{ productSummary.ratings || detail.ratings || '—' }}</b> ({{ productSummary.review_count ?? detail.review_count ?? 0 }})</span>
                  <span>30-Day Sales <b>{{ productSummary.thirty_day_sales ?? detail.thirty_day_sales ?? 0 }}</b></span>
                </div>
                <div class="prod-detail-badges">
                  <span>{{ detail.status || '—' }}</span>
                  <a v-if="detail.product_url" :href="detail.product_url" target="_blank" class="prod-detail-link">原页 ↗</a>
                </div>
              </div>
            </div>

            <div class="trend-controls">
              <USelect :model-value="selectedTrendProductId" class="report-select" :items="trendVariantItems" value-key="value" @update:model-value="setSelectedTrendProduct" />
              <USelect :model-value="trendGranularity" class="report-select" :items="granularityItems" value-key="value" @update:model-value="setTrendGranularity" />
              <input v-model="trendDateFrom" type="date" @change="applyProductTrendFilters" />
              <input v-model="trendDateTo" type="date" @change="applyProductTrendFilters" />
              <input v-model="trendPromoSearch" placeholder="Search product title / campaign" @keyup.enter="applyProductTrendFilters" />
              <USelect :model-value="trendPromoSku || EMPTY_SELECT" class="report-select" :items="trendPromoSkuItems" value-key="value" @update:model-value="setTrendPromoSku" />
              <USelect :model-value="trendPromoType || EMPTY_SELECT" class="report-select" :items="promoTypeItems" value-key="value" @update:model-value="setTrendPromoType" />
              <button class="row-action" @click="applyProductTrendFilters">Filter</button>
              <button class="row-action" @click="resetProductTrendFilters">Reset</button>
              <USelect v-if="canEdit" v-model="trendPromoExportScope" class="report-select export-scope" :items="exportScopeItems" value-key="value" aria-label="Product trend promotion export scope" />
              <button v-if="canEdit" class="row-action" @click="exportProductTrend">Export</button>
            </div>
            <div v-if="productTrendVariants.length > 1" class="variant-count-note">
              当前 listing 共 {{ productTrendVariants.length }} 个 SKU，可在上方切换查看各 SKU 趋势。
            </div>

            <div class="product-trend-kpis">
              <div><span>30-Day Sales</span><b>{{ productCurrentPeriod?.estimated_sales ?? productSummary.thirty_day_sales ?? 0 }}</b><small>{{ periodDelta('estimated_sales') }}</small></div>
              <div><span>30-Day Revenues</span><b>{{ fmtPrice(productCurrentPeriod?.estimated_revenue ?? productSummary.thirty_day_revenue ?? 0, productSummary.currency || detail.currency) }}</b><small>{{ periodDelta('estimated_revenue', 'price') }}</small></div>
              <div><span>Price</span><b>{{ fmtPrice(productCurrentPeriod?.sale_price ?? productSummary.price ?? detail.sale_price, productSummary.currency || detail.currency) }}</b><small>{{ trendGranularityLabel }}</small></div>
              <div><span>Ratings</span><b>{{ productCurrentPeriod?.avg_rating ?? productSummary.ratings ?? detail.ratings ?? '—' }}</b><small>Current SKU</small></div>
              <div><span>Reviews</span><b>{{ productCurrentPeriod?.review_total ?? productSummary.review_count ?? detail.review_count ?? 0 }}</b><small>{{ periodDelta('review_total') }}</small></div>
              <div><span>Promotions</span><b>{{ productSummary.promotion_count ?? productPromotions.length }}</b><small>{{ trendPromoSku ? 'Current SKU' : 'All SKU' }}</small></div>
            </div>

            <div v-if="productSummary.data_notes?.length" class="data-notes">
              <span v-for="note in productSummary.data_notes" :key="note">{{ note }}</span>
            </div>

            <div class="prod-detail-history">
              <h4>Sales Trends <span class="sub">({{ trendGranularityLabel }})</span></h4>
              <TrendLineChart v-if="productTrendRows.length" :rows="productTrendRows" :series="productTrendSeries" :height="300" />
              <div v-else class="sub">暂无趋势数据</div>
            </div>

            <div class="prod-detail-history">
              <h4>Trend Details</h4>
              <div v-if="!productTrendRows.length" class="sub">暂无历史快照</div>
              <UTable v-else class="ui-table" :data="productTrendRows" :columns="trendDetailColumns" sticky="header">
                <template #date-cell="{ row }">{{ (row.original.date || '').slice(0, 10) }}</template>
                <template #sale_price-cell="{ row }">{{ fmtPrice(row.original.sale_price, rowCurrency(row.original)) }}</template>
                <template #original_price-cell="{ row }">{{ fmtPrice(row.original.original_price, rowCurrency(row.original)) }}</template>
                <template #avg_rating-cell="{ row }">{{ row.original.avg_rating ?? row.original.ratings ?? '—' }}</template>
                <template #review_total-cell="{ row }">{{ row.original.review_total ?? row.original.review_count ?? '—' }}</template>
                <template #estimated_sales-cell="{ row }">{{ row.original.estimated_sales ?? 0 }}</template>
                <template #estimated_revenue-cell="{ row }">{{ fmtPrice(row.original.estimated_revenue ?? 0, rowCurrency(row.original)) }}</template>
              </UTable>
            </div>

            <div class="prod-detail-history">
              <h4>Sales Promotion <span class="sub">({{ trendPromoSku ? `SKU ${trendPromoSku}` : 'All listing SKU' }})</span></h4>
              <div v-if="!productPromotions.length" class="sub">暂无促销记录</div>
              <UTable v-else class="ui-table" :data="productPromotions" :columns="trendPromoColumns" sticky="header">
                <template #detected_time-cell="{ row }">{{ dateTime(row.original.detected_time) }}</template>
                <template #sku-cell="{ row }">
                  <div class="sku-stack">
                    <span>{{ promoSkuLabel(row.original) }}</span>
                    <span v-if="promoVariantText(row.original)" class="sku-variants">{{ promoVariantText(row.original) }}</span>
                  </div>
                </template>
                <template #product_title-cell="{ row }">
                  <div class="title-cell">
                    <img v-if="row.original.product_image || detail.image" :src="row.original.product_image || detail.image" class="thumb thumb-img" alt="" />
                    <div v-else class="thumb">📦</div>
                    <div class="info">
                      <span class="title-text" :title="row.original.product_title || productTitle(detail)">{{ row.original.product_title || productTitle(detail) }}</span>
                      <div v-if="promoLabels(row.original).length" class="mini-tags">
                        <span v-for="(label, idx) in promoLabels(row.original)" :key="`${label}-${idx}`">{{ label }}</span>
                      </div>
                    </div>
                  </div>
                </template>
                <template #promotion_type-cell="{ row }">{{ promoTypeLabel(row.original.promotion_type) }}</template>
                <template #promotion_name-cell="{ row }">{{ row.original.promotion_name || row.original.product_title || '--' }}</template>
                <template #discount-cell="{ row }">{{ promoDiscount(row.original) }}</template>
                <template #original_price-cell="{ row }">{{ fmtPrice(row.original.original_price, rowCurrency(row.original)) }}</template>
                <template #promotion_price-cell="{ row }">{{ fmtPrice(row.original.promotion_price, rowCurrency(row.original)) }}</template>
                <template #threshold-cell="{ row }">{{ row.original.threshold || '/' }}</template>
                <template #start_time-cell="{ row }">{{ dateTime(row.original.start_time) }}</template>
                <template #end_time-cell="{ row }">{{ dateTime(row.original.end_time) }}</template>
              </UTable>
              <div v-if="trendPromoTotal > trendPromoPageSize" class="pagination trend-promo-pager">
                <UPagination
                  :page="trendPromoPage"
                  :total="trendPromoTotal"
                  :items-per-page="Number(trendPromoPageSize)"
                  size="sm"
                  show-edges
                  @update:page="changeTrendPromoPage"
                />
                <USelect
                  :model-value="trendPromoPageSize"
                  class="report-select page-size-select"
                  :items="promoPageSizeItems"
                  value-key="value"
                  @update:model-value="setTrendPromoPageSize"
                />
                <span class="pager-total">共 {{ trendPromoTotal }} 条</span>
              </div>
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
  --report-control-bg: var(--ui-card-soft);
  --report-control-bg-strong: var(--ui-card);
  --report-control-border: var(--ui-border);
  --report-control-text: var(--ui-heading);
  --report-control-muted: var(--ui-muted);
  --report-panel-soft: var(--ui-card-soft);
  --report-accent: var(--ui-purple);
  --report-accent-strong: var(--ui-purple-strong);
  --report-accent-soft: var(--ui-purple-soft);
  --report-warning-bg: rgba(251, 191, 36, .14);
  --report-warning-text: #b45309;
}
.icon-btn.filter-on { border-color:var(--report-accent); color:var(--report-accent); background:var(--report-accent-soft); }
.report-select {
  min-width:0;
  min-height:34px!important;
  border:1px solid var(--report-control-border)!important;
  border-radius:8px!important;
  background:var(--report-control-bg)!important;
  color:var(--report-control-text)!important;
  flex:0 0 auto;
  box-shadow:0 8px 18px rgba(37,29,61,.06)!important;
}
.report-select:hover {
  border-color:var(--report-accent)!important;
  background:var(--report-control-bg-strong)!important;
}
.report-select:focus-visible {
  outline:2px solid rgba(124,58,237,.18)!important;
  outline-offset:2px!important;
}
:global(.report-page .report-select) {
  min-width:0;
  min-height:34px!important;
  border:1px solid var(--report-control-border)!important;
  border-radius:8px!important;
  background:var(--report-control-bg)!important;
  color:var(--report-control-text)!important;
  flex:0 0 auto;
  box-shadow:0 8px 18px rgba(37,29,61,.06)!important;
}
:global(.report-page .report-select:hover) {
  border-color:var(--report-accent)!important;
  background:var(--report-control-bg-strong)!important;
}
:global(.report-page .report-select:focus-visible) {
  outline:2px solid rgba(124,58,237,.18)!important;
  outline-offset:2px!important;
}
.site-select { width:220px!important; max-width:100%; }
.gran-select { width:132px!important; min-width:132px!important; }
.export-scope { width:132px!important; min-width:132px!important; }
.page-size-select { width:116px!important; min-width:116px!important; }
.promo-type-select { width:142px!important; min-width:142px!important; }
.cfg-select,.filter-select { width:100%!important; }
:global(.report-page .site-select) { width:220px!important; max-width:100%; }
:global(.report-page .gran-select) { width:132px!important; min-width:132px!important; }
:global(.report-page .export-scope) { width:132px!important; min-width:132px!important; }
:global(.report-page .page-size-select) { width:116px!important; min-width:116px!important; }
:global(.report-page .promo-type-select) { width:142px!important; min-width:142px!important; }
:global(.report-page .cfg-select),:global(.report-page .filter-select) { width:100%!important; }
.date-input { height:32px; padding:0 9px; border:1px solid var(--report-control-border); border-radius:7px; background:var(--report-control-bg); color:var(--report-control-text); font-size:12px; font-family:inherit; }
.range-sep,.range-note { color:var(--report-control-muted); font-size:12px; }
.store-trend-table { margin-top:14px; overflow:auto; border:1px solid var(--report-control-border); border-radius:8px; background:var(--report-control-bg-strong); }
.store-trend-title { padding:10px 12px; border-bottom:1px solid var(--report-control-border); color:var(--report-control-text); font-size:13px; font-weight:700; }
.store-trend-table table { width:100%; min-width:860px; border-collapse:collapse; font-size:12.5px; }
.store-trend-table th,.store-trend-table td { padding:8px 10px; border-bottom:1px solid var(--report-control-border); text-align:left; white-space:nowrap; color:var(--report-control-text); }
.store-trend-table th { background:var(--report-panel-soft); color:var(--report-control-muted); font-weight:700; }
.store-trend-table tr:last-child td { border-bottom:0; }
.filter-modal { width:min(760px, calc(100vw - 36px)); max-height:calc(100vh - 36px); overflow:auto; padding:0; background:var(--ui-card); color:var(--ui-text); border-color:var(--ui-border); border-radius:8px; box-shadow:0 26px 70px rgba(0,0,0,.28); }
.filter-modal .od-modal-head { min-height:54px; margin:0; padding:14px 16px; border-bottom:1px solid var(--ui-border); background:linear-gradient(180deg,var(--ui-card-soft),var(--ui-card)); }
.filter-modal .od-modal-head h3 { margin:0; color:var(--ui-heading); font-size:1rem; font-weight:900; }
.filter-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:12px; padding:16px; }
.filter-grid label { display:flex; flex-direction:column; gap:6px; font-size:12px; font-weight:800; color:var(--report-control-muted); }
.filter-grid input { min-height:36px; padding:7px 10px; border:1px solid var(--report-control-border); border-radius:7px; font-size:12.5px; font-family:inherit; background:var(--report-control-bg); color:var(--report-control-text); outline:none; box-shadow:none; }
.filter-grid input:focus { border-color:var(--report-accent); background:var(--report-control-bg-strong); }
.filter-grid input::placeholder, .promo-filters input::placeholder { color:var(--report-control-muted); }
.filter-grid .rng { display:flex; gap:6px; }
.filter-grid .rng input { width:100%; min-width:0; }
.choice-row { display:flex; flex-wrap:wrap; gap:6px; }
.choice-row button { min-height:32px; padding:5px 10px; border:1px solid var(--report-control-border); border-radius:7px; background:var(--report-control-bg); color:var(--report-control-muted); cursor:pointer; font-size:12px; font-weight:700; font-family:inherit; white-space:nowrap; transition:background .15s,border-color .15s,color .15s,transform .15s; }
.choice-row button:hover { color:var(--report-accent-strong); border-color:rgba(167,139,250,.36); background:var(--report-accent-soft); }
.choice-row button.active { color:#fff; border-color:var(--report-accent); background:var(--report-accent); }
.mini-tags { display:flex; flex-wrap:wrap; gap:4px; }
.mini-tags span { display:inline-flex; align-items:center; min-height:20px; padding:2px 7px; border-radius:999px; background:var(--report-accent-soft); color:var(--report-accent-strong); font-size:11px; font-weight:600; white-space:nowrap; }
.rating-cell { display:flex; flex-direction:column; align-items:flex-start; gap:2px; min-width:82px; }
.rating-cell .stars { color:#f59e0b; font-size:12px; line-height:1; letter-spacing:0; white-space:nowrap; }
.rating-cell small { color:var(--report-control-muted); font-size:11px; line-height:1.2; white-space:nowrap; }
.product-status { display:inline-flex; align-items:center; justify-content:center; min-height:22px; padding:2px 8px; border-radius:999px; border:1px solid var(--report-control-border); font-size:11px; font-weight:700; white-space:nowrap; }
.product-status.ok { color:#047857; background:rgba(16,185,129,.14); border-color:rgba(16,185,129,.30); }
.product-status.warn { color:#b45309; background:rgba(245,158,11,.14); border-color:rgba(245,158,11,.30); }
.product-status.idle { color:var(--report-control-muted); background:var(--report-control-bg); }
.sku-link { color:var(--report-accent-strong); text-decoration:none; font-weight:700; }
.sku-link:hover { text-decoration:underline; }
.sku-stack { display:flex; flex-direction:column; align-items:flex-start; gap:3px; min-width:0; }
.sku-variants { max-width:160px; color:var(--report-control-muted); font-size:11px; line-height:1.25; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.row-action { min-height:32px; display:inline-flex; align-items:center; justify-content:center; padding:5px 10px; border:1px solid rgba(167,139,250,.36); border-radius:7px; background:var(--report-accent-soft); color:var(--report-accent-strong); cursor:pointer; font-size:12px; font-weight:700; font-family:inherit; white-space:nowrap; text-decoration:none; }
.row-action:hover { background:rgba(167,139,250,.20); }
.filter-actions { position:sticky; bottom:0; z-index:3; display:flex; justify-content:flex-end; gap:8px; margin:0; padding:12px 16px; border-top:1px solid var(--ui-border); background:var(--ui-card-soft); }
.filter-actions button { min-height:38px; padding:0 16px; border-radius:7px; border:1px solid var(--report-control-border); background:var(--report-control-bg); color:var(--report-control-text); cursor:pointer; font-size:12.5px; font-weight:800; font-family:inherit; transition:background .15s,border-color .15s,color .15s,transform .15s; }
.filter-actions button:hover { transform:translateY(-1px); color:var(--report-accent-strong); border-color:rgba(167,139,250,.36); background:var(--report-accent-soft); }
.filter-actions button.primary { background:linear-gradient(135deg,#a78bfa,#7c3aed); color:#fff; border-color:transparent; box-shadow:0 8px 18px rgba(124,58,237,.18); }
.promo-filters { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
.promo-filters input { padding:7px 10px; border:1px solid var(--report-control-border); border-radius:7px; font-size:12.5px; font-family:inherit; background:var(--report-control-bg); color:var(--report-control-text); }
.promo-filters input:first-child { min-width:220px; }
.report-table-wrap { overflow:auto; border-radius:8px; }
.thumb-img { object-fit:cover; }
.prod-detail-top { display:flex; gap:14px; align-items:flex-start; flex-wrap:wrap; }
.prod-detail-img { width:120px; height:120px; object-fit:cover; border-radius:8px; }
.prod-detail-img-empty { display:flex; align-items:center; justify-content:center; font-size:2rem; background:var(--report-control-bg); color:var(--report-control-muted); }
.prod-detail-meta { flex:1; min-width:220px; }
.prod-detail-title { font-weight:600; line-height:1.5; }
.prod-detail-stats { margin-top:8px; display:flex; gap:18px; flex-wrap:wrap; font-size:0.86rem; }
.prod-detail-badges { margin-top:8px; display:flex; gap:10px; align-items:center; }
.prod-detail-link { color:var(--report-accent-strong); font-size:0.82rem; }
.prod-detail-history { margin-top:16px; }
.prod-detail-history h4 { margin:0 0 8px; }
.product-trend-modal { width:min(1120px, calc(100vw - 36px)); max-width:min(1120px, calc(100vw - 36px)); max-height:calc(100vh - 36px); overflow:auto; }
.trend-controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:14px; padding:10px; border:1px solid var(--report-control-border); border-radius:9px; background:var(--report-panel-soft); }
.trend-controls input { min-height:32px; padding:6px 9px; border:1px solid var(--report-control-border); border-radius:7px; background:var(--report-control-bg); color:var(--report-control-text); font-size:12.5px; font-family:inherit; }
.trend-controls input[type="text"],.trend-controls input:not([type]) { min-width:190px; }
.variant-count-note { margin-top:8px; color:var(--report-control-muted); font-size:12px; }
.product-trend-kpis { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; margin-top:16px; }
.product-trend-kpis div { min-height:70px; padding:10px 12px; border:1px solid var(--report-control-border); border-radius:9px; background:var(--report-panel-soft); }
.product-trend-kpis span { display:block; font-size:12px; color:var(--report-control-muted); margin-bottom:4px; }
.product-trend-kpis b { display:block; color:var(--report-control-text); font-size:18px; line-height:1.3; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.product-trend-kpis small { display:block; margin-top:3px; color:var(--report-control-muted); font-size:11px; line-height:1.25; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.data-notes { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
.data-notes span { border:1px solid rgba(251,191,36,.35); border-radius:999px; padding:4px 9px; background:var(--report-warning-bg); color:var(--report-warning-text); font-size:12px; }
.prod-detail-history table { width:100%; border-collapse:collapse; font-size:12.5px; }
.prod-detail-history th,.prod-detail-history td { padding:8px 9px; border-bottom:1px solid var(--report-control-border); text-align:left; white-space:nowrap; color:var(--report-control-text); }
.prod-detail-history th { color:var(--report-control-muted); background:var(--report-panel-soft); font-weight:700; }
.od-modal-head { position:sticky; top:0; z-index:3; display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; background:var(--ui-card); padding-bottom:10px; }
.od-x { width:30px; height:30px; display:inline-flex; align-items:center; justify-content:center; background:var(--report-control-bg); color:var(--report-control-muted); border:1px solid var(--report-control-border); border-radius:7px; cursor:pointer; font-size:1rem; }
.od-x:hover { color:var(--report-accent-strong); background:var(--report-accent-soft); border-color:rgba(167,139,250,.36); }
:global(html[data-theme="dark"]) .report-page {
  --report-warning-bg: rgba(251, 191, 36, .16);
  --report-warning-text: #fcd34d;
}
:global(html[data-theme="dark"]) .report-select {
  box-shadow:0 8px 18px rgba(0,0,0,.28)!important;
}
@media (max-width: 980px) {
  .product-trend-kpis { grid-template-columns:repeat(2,minmax(0,1fr)); }
}
@media (max-width: 720px) {
  .report-page { padding:12px; }
  .crumb { flex-direction:column; align-items:flex-start; gap:5px; }
  .site-card .top { flex-direction:column; align-items:stretch; gap:10px; }
  .site-card h2 { line-height:1.25; overflow-wrap:anywhere; }
  .site-card .url { display:block; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .date-picker { width:100%; display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .date-picker .site-select { grid-column:1 / -1; min-width:0; width:100%; }
  .date-picker .icon-btn { width:100%; justify-content:center; }
  .report-page .tab-row { display:grid; grid-template-columns:1fr; gap:6px; border-bottom:0; margin-bottom:14px; }
  .report-page .tab-row button { justify-content:center; border:1px solid var(--report-control-border); border-radius:8px; background:var(--report-control-bg-strong); }
  .report-page .tab-row button.active { border-color:var(--report-accent); background:var(--report-accent-soft); }
  .section-head { flex-direction:column; align-items:stretch; gap:10px; }
  .section-head .actions { width:100%; display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .section-head .actions > * { min-width:0; width:100%; justify-content:center; }
  .sub-tabs { margin:12px 0; display:grid; grid-template-columns:1fr; gap:8px; }
  .sub-tabs .right { margin-left:0; display:grid; grid-template-columns:1fr; gap:8px; }
  .sub-tabs button,.search-box { width:100%; min-width:0; }
  .promo-filters { display:grid; grid-template-columns:1fr; }
  .promo-filters input:first-child,.promo-filters input,.promo-filters .report-select,.promo-filters button { width:100%!important; min-width:0!important; }
  :global(.promo-filters .report-select) { width:100%!important; min-width:0!important; }
  .filter-modal { width:calc(100vw - 28px); max-height:calc(100vh - 28px); }
  .filter-grid { grid-template-columns:1fr; padding:14px; }
  .filter-actions { flex-wrap:wrap; }
  .filter-actions button { flex:1 1 130px; }
  .cfg-drawer { width:min(100vw, 420px); }
  .cfg-foot { flex-wrap:wrap; }
  .cfg-foot button { flex:1 1 130px; }
  .product-trend-modal { width:calc(100vw - 28px); max-width:calc(100vw - 28px); }
  .prod-detail-top { flex-direction:column; }
  .prod-detail-img { width:96px; height:96px; }
  .trend-controls { display:grid; grid-template-columns:1fr; }
  .trend-controls input,.trend-controls .report-select,.trend-controls input[type="text"],.trend-controls input:not([type]) { width:100%!important; min-width:0!important; }
  :global(.trend-controls .report-select) { width:100%!important; min-width:0!important; }
}
@media (max-width: 560px) {
  .product-trend-kpis { grid-template-columns:1fr; }
}
</style>
