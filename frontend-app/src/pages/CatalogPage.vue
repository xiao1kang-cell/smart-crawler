<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Download, Eye, ExternalLink, RefreshCw, RotateCw, X } from 'lucide-vue-next'
import { asList, fmtDate, fmtNumber, fmtPrice, qs } from '../api/client'
import { getProduct, listProducts, listSites, productPriceHistory } from '../api/products'
import { useAuthStore } from '../stores/auth'
import DataLoadingPanel from '../components/common/DataLoadingPanel.vue'
import PageLoading from '../components/common/PageLoading.vue'
import StatusBadge from '../components/common/StatusBadge.vue'
import { useJobTrigger } from '../composables/useJobTrigger'

const auth = useAuthStore()
const sites = ref<Record<string, any>[]>([])
const products = ref<Record<string, any>[]>([])
const selectedSite = ref(localStorage.getItem('sc_site') || '')
const search = ref('')
const tab = ref('all')
const statusFilter = ref('')
const minPrice = ref('')
const maxPrice = ref('')
const minSales = ref('')
const maxSales = ref('')
const total = ref(0)
const page = ref(1)
const pageSize = ref(30)
const loading = ref(false)
const error = ref('')
const exportMessage = ref('')
const jobTrigger = useJobTrigger({ onDone: () => load() })
const ALL_STATUS = '__all_status__'
const siteItems = computed(() => sites.value.map((site) => {
  const value = site.site || site.name
  return { label: value, value }
}))
const tabItems = [
  { label: '全部商品', value: 'all' },
  { label: '畅销商品', value: 'bestseller' },
  { label: '最新商品', value: 'new' },
]
const statusItems = [
  { label: '全部状态', value: ALL_STATUS },
  { label: 'Active', value: 'active' },
  { label: 'Sold out', value: 'sold_out' },
  { label: 'Offline', value: 'offline' },
]
const statusSelect = computed({
  get: () => statusFilter.value || ALL_STATUS,
  set: (value: string) => {
    statusFilter.value = value === ALL_STATUS ? '' : value
  },
})
const pageSizeItems = [
  { label: '30 / 页', value: 30 },
  { label: '60 / 页', value: 60 },
  { label: '100 / 页', value: 100 },
]
const productColumns = [
  { id: 'image', header: '' },
  { accessorKey: 'sku', header: '商品编码' },
  { accessorKey: 'title', header: '商品' },
  { accessorKey: 'price', header: '价格' },
  { accessorKey: 'ratings', header: '评分' },
  { accessorKey: 'thirty_day_sales', header: '30 天销量' },
  { accessorKey: 'thirty_day_revenue', header: '30 天收入' },
  { accessorKey: 'updated_time', header: '更新时间' },
  { accessorKey: 'status', header: '状态' },
  { id: 'actions', header: '操作' },
]
const priceHistoryColumns = [
  { accessorKey: 'date', header: '日期' },
  { accessorKey: 'sale_price', header: '售价' },
  { accessorKey: 'original_price', header: '原价' },
  { accessorKey: 'review_count', header: '评论数' },
]

// 商品详情 + 价格历史弹窗
const detail = ref<Record<string, any> | null>(null)
const priceHistory = ref<Record<string, any>[]>([])
const detailLoading = ref(false)

function totalPages() {
  return Math.max(1, Math.ceil((total.value || 0) / pageSize.value))
}

function productQueryParams() {
  return {
    site: selectedSite.value,
    tab: tab.value,
    search: search.value.trim(),
    status: statusFilter.value,
    min_price: minPrice.value,
    max_price: maxPrice.value,
    min_sales: minSales.value,
    max_sales: maxSales.value,
    page: page.value,
    page_size: pageSize.value,
  }
}

async function loadSites() {
  const siteData = await listSites()
  sites.value = asList(siteData, ['sites'])
  if (sites.value.length && !sites.value.some((site) => (site.site || site.name) === selectedSite.value)) {
    selectedSite.value = sites.value[0].site || sites.value[0].name || ''
    rememberSite()
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    if (!sites.value.length) await loadSites()
    if (!selectedSite.value) {
      products.value = []
      total.value = 0
      return
    }
    const productData = await listProducts(productQueryParams())
    products.value = asList(productData, ['items', 'products'])
    total.value = Number(productData?.total ?? products.value.length ?? 0)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function onSiteChange() {
  rememberSite()
  page.value = 1
  load()
}

function runSearch() {
  page.value = 1
  load()
}

function resetFilters() {
  search.value = ''
  tab.value = 'all'
  statusFilter.value = ''
  minPrice.value = ''
  maxPrice.value = ''
  minSales.value = ''
  maxSales.value = ''
  page.value = 1
  load()
}

function gotoPage(p: number) {
  const n = Math.min(Math.max(1, p), totalPages())
  if (n === page.value) return
  page.value = n
  load()
}

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

function exportProducts() {
  if (!selectedSite.value) return
  exportMessage.value = '已按当前筛选打开导出'
  window.open(`/api/export/products${qs({
    ...productQueryParams(),
    page: undefined,
    page_size: undefined,
    token: auth.token,
    workspace_id: auth.workspaceId,
    scope: 'products',
  })}`, '_blank')
}

async function triggerCrawl() {
  if (!selectedSite.value) return
  await jobTrigger.trigger(selectedSite.value)
}

function rememberSite() {
  localStorage.setItem('sc_site', selectedSite.value)
}

function productTitle(p: Record<string, any>) {
  return p.title || p.name || p.product_title || p.spu || p.sku || p.item_id || `商品 #${p.id}`
}

function productPrice(p: Record<string, any>) {
  return fmtPrice(p.sale_price ?? p.price ?? p.original_price, p.currency)
}

function productRevenue(p: Record<string, any>) {
  return fmtPrice(p.thirty_day_revenue, p.currency)
}

onMounted(load)
</script>

<template>
  <section>
    <div class="lead">商品库</div>
    <div class="sub">{{ loading ? '加载中' : (fmtNumber(total) + ' 条') }} · {{ selectedSite || '未选择站点' }}</div>
    <UAlert v-if="error" color="error" variant="soft" :title="error" />
    <div class="cat-filters">
        <USelect v-model="selectedSite" class="cat-select site-select" :items="siteItems" value-key="value" @update:model-value="onSiteChange" />
        <USelect v-model="tab" class="cat-select" :items="tabItems" value-key="value" @update:model-value="runSearch" />
        <USelect v-model="statusSelect" class="cat-select" :items="statusItems" value-key="value" @update:model-value="runSearch" />
        <input v-model="search" placeholder="搜索 SKU / 标题 / 类目" @keydown.enter="runSearch" />
        <input v-model="minPrice" class="num-filter" inputmode="decimal" placeholder="最低价" @keydown.enter="runSearch" />
        <input v-model="maxPrice" class="num-filter" inputmode="decimal" placeholder="最高价" @keydown.enter="runSearch" />
        <input v-model="minSales" class="num-filter" inputmode="numeric" placeholder="最低销量" @keydown.enter="runSearch" />
        <input v-model="maxSales" class="num-filter" inputmode="numeric" placeholder="最高销量" @keydown.enter="runSearch" />
        <USelect v-model="pageSize" class="cat-select page-size" :items="pageSizeItems" value-key="value" @update:model-value="runSearch" />
        <button class="btn-go" :disabled="loading" title="按当前筛选刷新列表" @click="runSearch">
          <RefreshCw :size="15" :class="{ spin: loading }" />
          <span>{{ loading ? '刷新中' : '刷新列表' }}</span>
        </button>
        <button class="btn-go" title="清空筛选" @click="resetFilters">
          <X :size="15" />
          <span>清空</span>
        </button>
        <button class="btn-go primary" :disabled="!selectedSite || jobTrigger.isBusy(selectedSite)" title="提交当前站点抓取任务" @click="triggerCrawl">
          <RotateCw :size="15" />
          <span>{{ jobTrigger.labelFor(selectedSite, '触发抓取') }}</span>
        </button>
        <button class="btn-go" :disabled="!selectedSite" title="按当前筛选导出商品" @click="exportProducts">
          <Download :size="15" />
          <span>导出表格</span>
        </button>
    </div>
    <div v-if="jobTrigger.detailFor(selectedSite) || exportMessage" class="catalog-action-note" :class="jobTrigger.classFor(selectedSite)">
      {{ jobTrigger.detailFor(selectedSite) || exportMessage }}
    </div>

    <DataLoadingPanel class="cat-table-wrap" :loading="loading" :has-data="products.length > 0" label="正在更新商品列表">
      <PageLoading v-if="loading && !products.length" compact title="加载商品数据..." note="正在读取站点商品库" />
      <UTable v-else class="cat-table ui-table" :data="products" :columns="productColumns" :loading="loading" sticky="header" empty="暂无商品数据">
        <template #image-cell="{ row }">
          <img v-if="row.original.image" :src="row.original.image" class="thumb-img" alt="" />
          <div v-else class="thumb">📦</div>
        </template>
        <template #sku-cell="{ row }">
          <code v-if="!row.original.product_url">{{ row.original.sku || row.original.item_id || row.original.id }}</code>
          <a v-else :href="row.original.product_url" target="_blank" rel="noopener" class="sku-link" @click.stop>
            <code>{{ row.original.sku || row.original.item_id || row.original.id }}</code>
          </a>
        </template>
        <template #title-cell="{ row }">
          <span class="title-text" :title="productTitle(row.original)">{{ productTitle(row.original) }}</span>
        </template>
        <template #price-cell="{ row }">
          <span>{{ productPrice(row.original) }}</span>
          <div v-if="row.original.original_price && row.original.original_price !== row.original.sale_price" class="price-sub">原价 {{ fmtPrice(row.original.original_price, row.original.currency) }}</div>
        </template>
        <template #ratings-cell="{ row }">{{ row.original.ratings || row.original.rating || '—' }}</template>
        <template #thirty_day_sales-cell="{ row }">{{ row.original.thirty_day_sales || 0 }}</template>
        <template #thirty_day_revenue-cell="{ row }">{{ productRevenue(row.original) }}</template>
        <template #updated_time-cell="{ row }">{{ fmtDate(row.original.updated_time || row.original.created_time) }}</template>
        <template #status-cell="{ row }"><StatusBadge :status="row.original.status" /></template>
        <template #actions-cell="{ row }">
          <button class="row-icon" title="查看商品详情" @click="openDetail(row.original.id)">
            <Eye :size="15" />
          </button>
          <a v-if="row.original.product_url" class="row-icon link" title="打开商品原页" :href="row.original.product_url" target="_blank" rel="noopener">
            <ExternalLink :size="15" />
          </a>
        </template>
      </UTable>
      <div v-if="!loading && !products.length" class="empty-state cat-table-empty">
        <b>当前站点暂无商品数据</b>
        可先在覆盖率页面触发抓取，或切换到已有数据的站点。
      </div>
      <div v-if="totalPages() > 1" class="cat-pager">
        <UPagination
          :page="page"
          :total="total"
          :items-per-page="pageSize"
          :disabled="loading"
          size="sm"
          show-edges
          @update:page="gotoPage"
        />
        <span class="cat-pager-info">第 {{ page }} / {{ totalPages() }} 页 · 共 {{ fmtNumber(total) }} 条</span>
      </div>
    </DataLoadingPanel>

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
              <div class="prod-detail-title">{{ productTitle(detail) }}</div>
              <div class="sub">SKU: {{ detail.sku }} · {{ detail.site }}</div>
              <div class="sub">更新时间: {{ fmtDate(detail.updated_time || detail.created_time) }}</div>
              <div class="prod-detail-stats">
                <span>价格 <b>{{ productPrice(detail) }}</b></span>
                <span v-if="detail.original_price">原价 <s>{{ fmtPrice(detail.original_price, detail.currency) }}</s></span>
                <span>评分 <b>{{ detail.ratings || '—' }}</b> ({{ detail.review_count || 0 }})</span>
                <span>30天销量 <b>{{ detail.thirty_day_sales || 0 }}</b></span>
                <span>30天收入 <b>{{ productRevenue(detail) }}</b></span>
                <span>库存 <b>{{ detail.inventory ?? '—' }}</b></span>
              </div>
              <div class="prod-detail-badges">
                <StatusBadge :status="detail.status" />
                <a v-if="detail.product_url" :href="detail.product_url" target="_blank" class="prod-detail-link">原页 ↗</a>
              </div>
            </div>
          </div>
          <div class="prod-detail-history">
            <h4>价格历史</h4>
            <div v-if="!priceHistory.length" class="sub">暂无价格历史</div>
            <UTable v-else class="cat-table cat-table-sm ui-table" :data="priceHistory" :columns="priceHistoryColumns" empty="暂无价格历史">
              <template #date-cell="{ row }">{{ (row.original.date || '').slice(0, 10) }}</template>
              <template #sale_price-cell="{ row }">{{ fmtPrice(row.original.sale_price, detail.currency) }}</template>
              <template #original_price-cell="{ row }">{{ fmtPrice(row.original.original_price, detail.currency) }}</template>
              <template #review_count-cell="{ row }">{{ row.original.review_count != null ? row.original.review_count : '—' }}</template>
            </UTable>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.cat-filters {
  align-items: center;
}
.cat-filters .cat-select,
.cat-filters input {
  min-height: 34px;
}
.cat-filters .num-filter {
  width: 96px;
}
.cat-filters .page-size {
  width: 92px;
}
.cat-filters .btn-go,
.cat-pager .btn-go {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 34px;
  padding: 0 12px;
  border: 1px solid rgba(167, 139, 250, .28);
  border-radius: 7px;
  background: var(--ui-card-soft);
  color: var(--ui-purple-strong, #7c3aed);
  white-space: nowrap;
}
.cat-filters .btn-go.primary {
  border-color: rgba(124, 58, 237, .34);
  background: var(--ui-purple-soft);
}
.cat-filters .btn-go:disabled,
.cat-pager .btn-go:disabled {
  cursor: not-allowed;
}
.spin {
  animation: catalog-spin .8s linear infinite;
}
@keyframes catalog-spin {
  to { transform: rotate(360deg); }
}
.catalog-action-note {
  margin: -4px 0 10px;
  padding: 7px 9px;
  border-radius: 8px;
  border: 1px solid var(--ui-border);
  background: var(--ui-card-soft);
  color: var(--ui-muted);
  font-size: .75rem;
}
.catalog-action-note.trigger-queued,
.catalog-action-note.trigger-running,
.catalog-action-note.trigger-submitting,
.catalog-action-note.trigger-unknown {
  border-color: rgba(167, 139, 250, .28);
  background: var(--ui-purple-soft);
  color: var(--ui-purple-strong);
}
.catalog-action-note.trigger-success {
  border-color: rgba(16, 185, 129, .3);
  background: rgba(16, 185, 129, .14);
  color: #047857;
}
.catalog-action-note.trigger-failed,
.catalog-action-note.trigger-blocked {
  border-color: rgba(248, 113, 113, .3);
  background: rgba(248, 113, 113, .12);
  color: #be123c;
}
.title-text { display:-webkit-box; max-width:420px; overflow:hidden; -webkit-line-clamp:2; -webkit-box-orient:vertical; line-height:1.35; vertical-align:bottom; }
.sku-link { text-decoration:none; }
.sku-link code { color:var(--ui-primary, #7c6ce0); }
.thumb-img { width:32px; height:32px; border-radius:6px; object-fit:cover; display:block; border:1px solid var(--ui-border, #2a2a3a); }
.row-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  margin-right: 5px;
  border: 1px solid var(--ui-border);
  border-radius: 7px;
  background: var(--ui-card-soft);
  color: var(--ui-muted);
  cursor: pointer;
  vertical-align: middle;
}
.row-icon:hover {
  color: var(--ui-purple-strong, #7c3aed);
  border-color: rgba(167, 139, 250, .34);
}
.row-icon.link {
  text-decoration: none;
}
.price-sub { color:var(--ui-muted, #9ca3af); font-size:.72rem; margin-top:2px; }
.cat-pager { display:flex; justify-content:center; align-items:center; gap:12px; margin-top:14px; flex-wrap:wrap; }
.cat-pager-info { color:var(--ui-muted, #9ca3af); font-size:0.82rem; }
.prod-detail-top { display:flex; gap:14px; align-items:flex-start; flex-wrap:wrap; }
.prod-detail-img { width:120px; height:120px; object-fit:cover; border-radius:8px; border:1px solid var(--ui-border, #2a2a3a); }
.prod-detail-img-empty { display:flex; align-items:center; justify-content:center; font-size:2rem; background:var(--ui-card-soft, #1a1a26); }
.prod-detail-meta { flex:1; min-width:220px; }
.prod-detail-title { font-weight:600; line-height:1.5; }
.prod-detail-stats { margin-top:8px; display:flex; gap:18px; flex-wrap:wrap; font-size:0.86rem; }
.prod-detail-badges { margin-top:8px; display:flex; gap:10px; align-items:center; }
.prod-detail-link { color:var(--ui-muted, #9ca3af); font-size:0.82rem; }
.prod-detail-history { margin-top:16px; }
.prod-detail-history h4 { margin:0 0 8px; }
.cat-table-sm { font-size:0.8rem; }
.od-modal-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; }
.od-x { background:transparent; border:0; color:var(--ui-muted, #9ca3af); cursor:pointer; font-size:1rem; }
</style>
