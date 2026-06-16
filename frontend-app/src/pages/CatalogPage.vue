<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { asList, fmtNumber, fmtPrice, qs } from '../api/client'
import { getProduct, listProducts, listSites, productPriceHistory } from '../api/products'
import { useAuthStore } from '../stores/auth'
import DataLoadingPanel from '../components/common/DataLoadingPanel.vue'
import PageLoading from '../components/common/PageLoading.vue'
import StatusBadge from '../components/common/StatusBadge.vue'

const auth = useAuthStore()
const sites = ref<Record<string, any>[]>([])
const products = ref<Record<string, any>[]>([])
const selectedSite = ref(localStorage.getItem('sc_site') || '')
const search = ref('')
const total = ref(0)
const page = ref(1)
const PAGE_SIZE = 30
const loading = ref(false)
const error = ref('')

// 商品详情 + 价格历史弹窗
const detail = ref<Record<string, any> | null>(null)
const priceHistory = ref<Record<string, any>[]>([])
const detailLoading = ref(false)

function totalPages() {
  return Math.max(1, Math.ceil((total.value || 0) / PAGE_SIZE))
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
    const productData = await listProducts({
      site: selectedSite.value,
      search: search.value,
      page: page.value,
      page_size: PAGE_SIZE,
    })
    products.value = asList(productData, ['items', 'products'])
    total.value = Number(productData?.total || products.value.length || 0)
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
  window.open(`/api/export/products${qs({ site: selectedSite.value, token: auth.token, workspace_id: auth.workspaceId })}`, '_blank')
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

onMounted(load)
</script>

<template>
  <section>
    <div class="lead">商品库</div>
    <div class="sub">{{ loading ? '加载中' : (fmtNumber(total) + ' 条') }} · {{ selectedSite || '未选择站点' }}</div>
    <UAlert v-if="error" color="error" variant="soft" :title="error" />
    <div class="cat-filters">
        <select v-model="selectedSite" @change="onSiteChange">
          <option v-for="site in sites" :key="site.site || site.name" :value="site.site || site.name">{{ site.site || site.name }}</option>
        </select>
        <input v-model="search" placeholder="搜索 SKU / 标题 / 类目" @keydown.enter="runSearch" />
        <button class="btn-go" :disabled="loading" @click="runSearch">{{ loading ? '刷新中…' : '🔄 刷新' }}</button>
        <button class="btn-go" @click="exportProducts">📥 导出表格</button>
    </div>

    <DataLoadingPanel class="cat-table-wrap" :loading="loading" :has-data="products.length > 0" label="正在更新商品列表">
      <PageLoading v-if="loading && !products.length" compact title="加载商品数据..." note="正在读取站点商品库" />
      <table v-else class="cat-table">
        <thead><tr><th></th><th>商品编码</th><th>商品</th><th>价格</th><th>评分</th><th>30 天销量</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="p in products" :key="p.id || `${p.site}-${p.sku}`" style="cursor:pointer" @click="openDetail(p.id)">
            <td><img v-if="p.image" :src="p.image" class="thumb-img" alt="" /><div v-else class="thumb">📦</div></td>
            <td><code v-if="!p.product_url">{{ p.sku || p.item_id || p.id }}</code><a v-else :href="p.product_url" target="_blank" rel="noopener" class="sku-link" @click.stop><code>{{ p.sku || p.item_id || p.id }}</code></a></td>
            <td><span class="title-text" :title="productTitle(p)">{{ productTitle(p) }}</span></td>
            <td><span>{{ productPrice(p) }}</span><div v-if="p.original_price && p.original_price !== p.sale_price" class="price-sub">原价 {{ fmtPrice(p.original_price, p.currency) }}</div></td>
            <td>{{ p.ratings || p.rating || '—' }}</td>
            <td>{{ p.thirty_day_sales || 0 }}</td>
            <td><StatusBadge :status="p.status" /></td>
          </tr>
        </tbody>
      </table>
      <div v-if="!loading && !products.length" class="empty-state cat-table-empty">
        <b>当前站点暂无商品数据</b>
        可先在覆盖率页面触发抓取，或切换到已有数据的站点。
      </div>
      <div v-if="totalPages() > 1" class="cat-pager">
        <button class="btn-go" :disabled="page <= 1 || loading" @click="gotoPage(page - 1)">‹ 上一页</button>
        <span class="cat-pager-info">第 {{ page }} / {{ totalPages() }} 页 · 共 {{ fmtNumber(total) }} 条</span>
        <button class="btn-go" :disabled="page >= totalPages() || loading" @click="gotoPage(page + 1)">下一页 ›</button>
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
              <div class="prod-detail-stats">
                <span>价格 <b>{{ productPrice(detail) }}</b></span>
                <span v-if="detail.original_price">原价 <s>{{ fmtPrice(detail.original_price, detail.currency) }}</s></span>
                <span>评分 <b>{{ detail.ratings || '—' }}</b> ({{ detail.review_count || 0 }})</span>
                <span>30天销量 <b>{{ detail.thirty_day_sales || 0 }}</b></span>
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
            <table v-else class="cat-table cat-table-sm">
              <thead><tr><th>日期</th><th>售价</th><th>原价</th><th>评论数</th></tr></thead>
              <tbody>
                <tr v-for="(h, i) in priceHistory" :key="i">
                  <td>{{ (h.date || '').slice(0, 10) }}</td>
                  <td>{{ h.sale_price != null ? h.sale_price : '—' }}</td>
                  <td>{{ h.original_price != null ? h.original_price : '—' }}</td>
                  <td>{{ h.review_count != null ? h.review_count : '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.title-text { display:-webkit-box; max-width:420px; overflow:hidden; -webkit-line-clamp:2; -webkit-box-orient:vertical; line-height:1.35; vertical-align:bottom; }
.sku-link { text-decoration:none; }
.sku-link code { color:var(--ui-primary, #7c6ce0); }
.thumb-img { width:32px; height:32px; border-radius:6px; object-fit:cover; display:block; border:1px solid var(--ui-border, #2a2a3a); }
.price-sub { color:var(--ui-muted, #9ca3af); font-size:.72rem; margin-top:2px; }
.cat-pager { display:flex; justify-content:center; align-items:center; gap:12px; margin-top:14px; }
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
