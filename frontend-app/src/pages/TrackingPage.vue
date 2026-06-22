<script setup lang="ts">
import {
  Download,
  ExternalLink,
  FileText,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Trash2,
} from 'lucide-vue-next'
import { computed, onMounted, ref } from 'vue'
import DataLoadingPanel from '../components/common/DataLoadingPanel.vue'
import { asList, currencyForMarket, fmtDate, fmtNumber, fmtPrice, shortUrl } from '../api/client'
import { addTracking, deleteTracking, editTracking, listTracking, pauseTracking, resumeTracking } from '../api/tracking'
import { useJobTrigger } from '../composables/useJobTrigger'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const rows = ref<Record<string, any>[]>([])
const facets = ref<{ markets: string[]; brands: string[]; statuses: string[] }>({ markets: [], brands: [], statuses: [] })
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const loading = ref(false)
const error = ref('')
const search = ref('')
const fMarket = ref('')
const fBrand = ref('')
const fStatus = ref('')
const showAdd = ref(false)
const addForm = ref({ url: '', brand: '', country: '' })
const addBusy = ref(false)
const editing = ref<Record<string, any> | null>(null)
const deleteTarget = ref<Record<string, any> | null>(null)
const deleteBusy = ref(false)
const jobTrigger = useJobTrigger({ onDone: () => load() })
const ALL_VALUE = '__all'

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / Number(pageSize.value || 10))))
const pageFrom = computed(() => total.value ? (page.value - 1) * Number(pageSize.value || 10) + 1 : 0)
const pageTo = computed(() => Math.min(total.value, page.value * Number(pageSize.value || 10)))
const hasFilters = computed(() => Boolean(search.value || fMarket.value || fBrand.value || fStatus.value))
const currentProducts = computed(() => rows.value.reduce((sum, row) => sum + Number(row.products || 0), 0))
const currentSkuRows = computed(() => rows.value.reduce((sum, row) => sum + Number(row.sku_count || 0), 0))
const salesReadyRows = computed(() => rows.value.filter((row) => row.sales_available).length)
const marketOptions = computed(() => facets.value.markets || [])
const brandOptions = computed(() => facets.value.brands || [])
const marketItems = computed(() => [
  { label: '全部', value: ALL_VALUE },
  ...marketOptions.value.map((market) => ({ label: market, value: market })),
])
const brandItems = computed(() => [
  { label: '全部', value: ALL_VALUE },
  ...brandOptions.value.map((brand) => ({ label: brand, value: brand })),
])
const statusItems = [
  { label: '全部', value: ALL_VALUE },
  { label: '追踪中', value: 'tracking' },
  { label: '已暂停', value: 'paused' },
  { label: '异常', value: 'error' },
]
const pageSizeItems = [10, 20, 50, 100, 200].map((value) => ({ label: `${value} / 页`, value }))
const marketSelect = computed({
  get: () => fMarket.value || ALL_VALUE,
  set: (value: string) => {
    fMarket.value = value === ALL_VALUE ? '' : value
    applySearch()
  },
})
const brandSelect = computed({
  get: () => fBrand.value || ALL_VALUE,
  set: (value: string) => {
    fBrand.value = value === ALL_VALUE ? '' : value
    applySearch()
  },
})
const statusSelect = computed({
  get: () => fStatus.value || ALL_VALUE,
  set: (value: string) => {
    fStatus.value = value === ALL_VALUE ? '' : value
    applySearch()
  },
})
const trackingColumns = [
  { accessorKey: 'country', header: 'Market' },
  { accessorKey: 'brand', header: 'Brand' },
  { accessorKey: 'site', header: 'URL' },
  { accessorKey: 'track_status', header: 'Status' },
  { accessorKey: 'products', header: 'Products', meta: { class: { th: 'num', td: 'num' } } },
  { accessorKey: 'thirty_day_sales', header: '30-Day Sales', meta: { class: { th: 'num', td: 'num' } } },
  { accessorKey: 'thirty_day_revenue', header: '30-Day Revenue', meta: { class: { th: 'num', td: 'num' } } },
  { accessorKey: 'display_updated_at', header: 'Updated Time' },
  { accessorKey: 'created_at', header: 'Created Time' },
  { accessorKey: 'creator', header: 'Creator' },
  { id: 'actions', header: 'Action', meta: { class: { th: 'actions-head', td: 'actions-cell' } } },
]

const canEdit = computed(() => {
  const u = auth.user
  if (!u) return false
  return u.global_role === 'super_admin' || ['admin', 'owner'].includes(u.workspace_role || '')
})

function flag(cc?: string) {
  if (!cc || cc.length !== 2) return '🌐'
  return String.fromCodePoint(...[...cc.toUpperCase()].map((c) => 127397 + c.charCodeAt(0)))
}

function statusMeta(s?: string) {
  const key = (s || 'tracking').toLowerCase()
  return ({
    submitting: { label: '提交中', tone: 'busy' },
    queued: { label: '已入队', tone: 'busy' },
    pending: { label: '已入队', tone: 'busy' },
    running: { label: '抓取中', tone: 'busy' },
    success: { label: '已完成', tone: 'ok' },
    tracking: { label: '追踪中', tone: 'ok' },
    paused: { label: '已暂停', tone: 'idle' },
    error: { label: '异常', tone: 'bad' },
    failed: { label: '抓取失败', tone: 'bad' },
    blocked: { label: '被拦截', tone: 'bad' },
    skipped: { label: '已跳过', tone: 'idle' },
    unknown: { label: '同步中', tone: 'busy' },
  } as Record<string, { label: string; tone: string }>)[key] || { label: s || '未知', tone: 'idle' }
}

function displayStatus(row: Record<string, any>) {
  const triggerStatus = jobTrigger.stateFor(row.site)?.status
  if (triggerStatus && triggerStatus !== 'idle') return triggerStatus
  return row.display_status || row.track_status
}

function fmtTime(value?: string | null) {
  const out = fmtDate(value)
  return out === '-' ? '—' : out
}

function fmtMetric(value: unknown) {
  return fmtNumber(value)
}

function fmtSales(row: Record<string, any>) {
  return row.sales_available ? fmtNumber(row.thirty_day_sales) : '暂无'
}

function fmtRevenue(row: Record<string, any>) {
  if (!row.revenue_available) return '暂无'
  return fmtPrice(row.thirty_day_revenue || 0, row.currency || currencyForMarket(row.site || row.country))
}

function trackingUrlLabel(url?: string, fallback?: string) {
  if (!url) return fallback || ''
  try {
    const u = new URL(url)
    const fixed = `${u.protocol}//${u.host}/`
    return fixed.length > 150 ? `${fixed.slice(0, 149)}…` : fixed
  } catch {
    const label = shortUrl(url) || url || fallback || ''
    return label.length > 150 ? `${label.slice(0, 149)}…` : label
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const d = await listTracking({
      search: search.value.trim(),
      market: fMarket.value.trim(),
      brand: fBrand.value.trim(),
      status: fStatus.value,
      page: page.value,
      page_size: pageSize.value,
    })
    rows.value = asList(d, ['items'])
    facets.value = {
      markets: Array.isArray(d?.facets?.markets) ? d.facets.markets : [],
      brands: Array.isArray(d?.facets?.brands) ? d.facets.brands : [],
      statuses: Array.isArray(d?.facets?.statuses) ? d.facets.statuses : [],
    }
    total.value = Number(d?.total || rows.value.length || 0)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function applySearch() {
  page.value = 1
  load()
}

function setPage(next: number) {
  if (next === page.value) return
  page.value = next
  load()
}

function resetFilters() {
  search.value = ''
  fMarket.value = ''
  fBrand.value = ''
  fStatus.value = ''
  applySearch()
}

async function submitAdd() {
  if (!addForm.value.url.trim()) return
  addBusy.value = true
  error.value = ''
  try {
    await addTracking({
      url: addForm.value.url.trim(),
      brand: addForm.value.brand.trim() || undefined,
      country: addForm.value.country.trim().toUpperCase() || undefined,
    })
    showAdd.value = false
    addForm.value = { url: '', brand: '', country: '' }
    page.value = 1
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    addBusy.value = false
  }
}

async function saveEdit() {
  if (!editing.value) return
  try {
    await editTracking(editing.value.site, {
      brand: editing.value.brand,
      country: String(editing.value.country || '').toUpperCase(),
      review_rate: editing.value.review_rate === '' ? null : Number(editing.value.review_rate),
    })
    editing.value = null
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function togglePause(row: Record<string, any>) {
  try {
    if (row.track_status === 'paused') await resumeTracking(row.site)
    else await pauseTracking(row.site)
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function remove(row: Record<string, any>) {
  deleteTarget.value = row
}

async function confirmRemove() {
  if (!deleteTarget.value) return
  deleteBusy.value = true
  try {
    await deleteTracking(deleteTarget.value.site)
    deleteTarget.value = null
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    deleteBusy.value = false
  }
}

async function triggerCrawl(row: Record<string, any>) {
  error.value = ''
  await jobTrigger.trigger(row.site)
}

function reportHref(row: Record<string, any>) {
  const p = new URLSearchParams({ site: row.site })
  if (auth.workspaceId) p.set('workspace_id', auth.workspaceId)
  return `/report?${p.toString()}`
}

function exportUrl() {
  const p = new URLSearchParams()
  if (search.value.trim()) p.set('search', search.value.trim())
  if (fMarket.value.trim()) p.set('market', fMarket.value.trim())
  if (fBrand.value.trim()) p.set('brand', fBrand.value.trim())
  if (fStatus.value) p.set('status', fStatus.value)
  if (auth.token) p.set('token', auth.token)
  if (auth.workspaceId) p.set('workspace_id', auth.workspaceId)
  return `/api/tracking/export?${p.toString()}`
}

onMounted(async () => {
  if (auth.token && !auth.user) await auth.loadMe().catch(() => null)
  await load()
})
</script>

<template>
  <section class="tracking-page">
    <div class="tracking-head">
      <div>
        <div class="lead">标杆网站维护</div>
        <div class="sub">{{ loading ? '同步中' : `${fmtNumber(total)} 个追踪站点` }}</div>
      </div>
      <button v-if="canEdit" type="button" class="btn-prim tracking-add" @click="showAdd = true">
        <Plus class="size-4" />
        <span>Add Tracking</span>
      </button>
    </div>

    <UAlert v-if="error" color="error" variant="soft" :title="error" class="mb-4" />

    <div class="tracking-stats">
      <div class="tracking-stat">
        <span>筛选结果</span>
        <b>{{ fmtNumber(total) }}</b>
      </div>
      <div class="tracking-stat">
        <span>本页商品</span>
        <b>{{ fmtNumber(currentProducts) }}</b>
      </div>
      <div class="tracking-stat">
        <span>本页 SKU 行</span>
        <b>{{ fmtNumber(currentSkuRows) }}</b>
      </div>
      <div class="tracking-stat" :class="{ warn: salesReadyRows < rows.length }">
        <span>销量估算</span>
        <b>{{ salesReadyRows }}/{{ rows.length }}</b>
      </div>
    </div>

    <div class="tracking-toolbar">
      <label class="tracking-field search-field">
        <Search class="size-4 field-icon" />
        <UInput v-model="search" variant="none" placeholder="URL / Brand name / Site code" @keyup.enter="applySearch" />
      </label>
      <label class="tracking-field small">
        <span>Market</span>
        <USelect v-model="marketSelect" :items="marketItems" value-key="value" />
      </label>
      <label class="tracking-field brand-filter">
        <span>Brand</span>
        <USelect v-model="brandSelect" :items="brandItems" value-key="value" />
      </label>
      <label class="tracking-field select-field">
        <span>Status</span>
        <USelect v-model="statusSelect" :items="statusItems" value-key="value" />
      </label>
      <button type="button" class="tool-btn primary" :disabled="loading" @click="applySearch">
        <SlidersHorizontal v-if="!loading" class="size-4" />
        <RefreshCw v-else class="size-4 spin" />
        <span>{{ loading ? '筛选中' : '筛选' }}</span>
      </button>
      <button v-if="hasFilters" type="button" class="tool-btn ghost" :disabled="loading" @click="resetFilters">
        <span>清空</span>
      </button>
      <a class="tool-btn ghost" :href="exportUrl()" target="_blank" rel="noopener">
        <Download class="size-4" />
        <span>导出</span>
      </a>
    </div>

    <DataLoadingPanel class="tracking-table-wrap" :loading="loading" :has-data="rows.length > 0" label="正在更新站点列表">
      <UTable
        class="tracking-table"
        :data="rows"
        :columns="trackingColumns"
        :loading="loading"
        sticky="header"
        empty="暂无追踪站点"
        loading-color="primary"
        loading-animation="carousel"
      >
        <template #country-cell="{ row }">
          <div class="market-cell"><span class="flag">{{ flag(row.original.country) }}</span><span>{{ row.original.country || '—' }}</span></div>
        </template>
        <template #brand-cell="{ row }">
          <span class="brand-cell">{{ row.original.brand || '—' }}</span>
        </template>
        <template #site-cell="{ row }">
          <div>
            <a class="site-link" :href="row.original.url" target="_blank" rel="noopener" :title="row.original.url">
              <span>{{ trackingUrlLabel(row.original.url, row.original.site) }}</span>
              <ExternalLink class="size-3" />
            </a>
            <div class="site-code">{{ row.original.site }}</div>
          </div>
        </template>
        <template #track_status-cell="{ row }">
          <div class="status-cell">
            <span class="status-pill" :class="statusMeta(displayStatus(row.original)).tone">{{ statusMeta(displayStatus(row.original)).label }}</span>
            <small v-if="row.original.last_error_code" class="status-detail">{{ row.original.last_error_code }}</small>
            <small v-else-if="row.original.latest_job?.status === 'pending' || row.original.latest_job?.status === 'running'" class="status-detail">{{ row.original.latest_job.status }}</small>
          </div>
        </template>
        <template #products-cell="{ row }">
          {{ fmtMetric(row.original.products) }}
        </template>
        <template #thirty_day_sales-cell="{ row }">
          <span :class="{ muted: !row.original.sales_available }">{{ fmtSales(row.original) }}</span>
        </template>
        <template #thirty_day_revenue-cell="{ row }">
          <span :class="{ muted: !row.original.revenue_available }">{{ fmtRevenue(row.original) }}</span>
        </template>
        <template #display_updated_at-cell="{ row }">
          <span class="time-cell">{{ fmtTime(row.original.display_updated_at || row.original.last_crawled || row.original.updated_at) }}</span>
        </template>
        <template #created_at-cell="{ row }">
          <span class="time-cell">{{ fmtTime(row.original.created_at) }}</span>
        </template>
        <template #creator-cell="{ row }">
          <span class="creator-cell">{{ row.original.creator || '系统' }}</span>
        </template>
        <template #actions-cell="{ row }">
          <div class="actions-cell">
            <a :href="reportHref(row.original)" target="_blank" rel="noopener" class="action-btn" title="打开报告" aria-label="打开报告">
              <FileText class="size-4" />
              <span class="sr-only">报告</span>
            </a>
            <template v-if="canEdit">
              <button type="button" class="action-btn" title="编辑" aria-label="编辑" @click="editing = { ...row.original }">
                <Pencil class="size-4" />
                <span class="sr-only">编辑</span>
              </button>
              <button type="button" class="action-btn" :title="row.original.track_status === 'paused' ? '恢复追踪' : '暂停追踪'" :aria-label="row.original.track_status === 'paused' ? '恢复追踪' : '暂停追踪'" @click="togglePause(row.original)">
                <Play v-if="row.original.track_status === 'paused'" class="size-4" />
                <Pause v-else class="size-4" />
                <span class="sr-only">{{ row.original.track_status === 'paused' ? '恢复' : '暂停' }}</span>
              </button>
              <button type="button" class="action-btn" :class="jobTrigger.classFor(row.original.site)" :disabled="jobTrigger.isBusy(row.original.site)" :title="jobTrigger.labelFor(row.original.site, '重跑抓取')" :aria-label="jobTrigger.labelFor(row.original.site, '重跑抓取')" @click="triggerCrawl(row.original)">
                <RefreshCw v-if="!jobTrigger.isBusy(row.original.site)" class="size-4" />
                <RefreshCw v-else class="size-4 spin" />
                <span class="sr-only">{{ jobTrigger.labelFor(row.original.site, '重跑') }}</span>
              </button>
              <button type="button" class="action-btn danger" title="移出追踪" aria-label="移出追踪" @click="remove(row.original)">
                <Trash2 class="size-4" />
                <span class="sr-only">删除</span>
              </button>
              <div v-if="jobTrigger.detailFor(row.original.site)" class="row-trigger-status" :class="jobTrigger.classFor(row.original.site)">
                {{ jobTrigger.labelFor(row.original.site, '重跑') }} · {{ jobTrigger.detailFor(row.original.site) }}
              </div>
            </template>
          </div>
        </template>
        <template #loading>
          <span class="tracking-empty">加载中...</span>
        </template>
        <template #empty>
          <span class="tracking-empty">暂无追踪站点</span>
        </template>
      </UTable>
    </DataLoadingPanel>

    <div class="tracking-pager">
      <span>{{ pageFrom }}-{{ pageTo }} / {{ fmtNumber(total) }}</span>
      <UPagination
        :page="page"
        :total="total"
        :items-per-page="pageSize"
        :disabled="loading || totalPages <= 1"
        size="sm"
        show-edges
        @update:page="setPage"
      />
      <span class="pager-info">{{ page }} / {{ totalPages }}</span>
      <USelect v-model="pageSize" class="pager-select" variant="outline" :items="pageSizeItems" value-key="value" @update:model-value="page = 1; load()" />
    </div>

    <UModal v-model:open="showAdd" title="Add Tracking" :ui="{ content: 'tracking-dialog', header: 'dialog-head', body: 'dialog-body', footer: 'dialog-foot', title: 'dialog-title' }">
      <template #body>
        <UFormField label="URL" class="dialog-field">
          <UInput v-model="addForm.url" placeholder="https://brand.example.com" />
        </UFormField>
        <UFormField label="Brand" class="dialog-field">
          <UInput v-model="addForm.brand" maxlength="50" />
        </UFormField>
        <UFormField label="Market" class="dialog-field">
          <UInput v-model="addForm.country" maxlength="8" placeholder="US" />
        </UFormField>
      </template>
      <template #footer>
        <button type="button" class="dialog-btn ghost" @click="showAdd = false">取消</button>
        <button type="button" class="dialog-btn primary" :disabled="addBusy" @click="submitAdd">
          <Plus v-if="!addBusy" class="size-4" />
          <RefreshCw v-else class="size-4 spin" />
          <span>{{ addBusy ? '探测中' : '添加并抓取' }}</span>
        </button>
      </template>
    </UModal>

    <UModal :open="Boolean(editing)" title="Edit Tracking" :ui="{ content: 'tracking-dialog', header: 'dialog-head', body: 'dialog-body', footer: 'dialog-foot', title: 'dialog-title' }" @update:open="(open: boolean) => { if (!open) editing = null }">
      <template #body>
        <UFormField v-if="editing" label="Brand" class="dialog-field">
          <UInput v-model="editing.brand" maxlength="50" />
        </UFormField>
        <UFormField v-if="editing" label="Market" class="dialog-field">
          <UInput v-model="editing.country" maxlength="8" />
        </UFormField>
        <UFormField v-if="editing" label="Review Rate" class="dialog-field">
          <UInput v-model="editing.review_rate" type="number" step="0.001" />
        </UFormField>
      </template>
      <template #footer>
        <button type="button" class="dialog-btn ghost" @click="editing = null">取消</button>
        <button type="button" class="dialog-btn primary" @click="saveEdit">
          <Pencil class="size-4" />
          <span>保存</span>
        </button>
      </template>
    </UModal>

    <UModal :open="Boolean(deleteTarget)" title="移出追踪站点" :ui="{ content: 'tracking-dialog', header: 'dialog-head', body: 'dialog-body', footer: 'dialog-foot', title: 'dialog-title' }" @update:open="(open: boolean) => { if (!open && !deleteBusy) deleteTarget = null }">
      <template #body>
        <div v-if="deleteTarget" class="delete-confirm">
          <Trash2 class="size-5" />
          <div>
            <b>{{ deleteTarget.brand || deleteTarget.site }}</b>
            <p>确认从当前工作区移出该标杆站点？历史商品数据不会在其他仍启用该站点的工作区被删除。</p>
            <small>{{ deleteTarget.url || deleteTarget.site }}</small>
          </div>
        </div>
      </template>
      <template #footer>
        <button type="button" class="dialog-btn ghost" :disabled="deleteBusy" @click="deleteTarget = null">取消</button>
        <button type="button" class="dialog-btn danger-solid" :disabled="deleteBusy" @click="confirmRemove">
          <Trash2 v-if="!deleteBusy" class="size-4" />
          <RefreshCw v-else class="size-4 spin" />
          <span>{{ deleteBusy ? '移出中' : '确认移出' }}</span>
        </button>
      </template>
    </UModal>
  </section>
</template>

<style scoped>
.tracking-page { display:flex; flex-direction:column; gap:14px; }
.tracking-head { display:flex; align-items:flex-start; justify-content:space-between; gap:14px; }
.tracking-add { min-width:120px; }

.tracking-stats { display:grid; grid-template-columns:repeat(4,minmax(140px,1fr)); gap:10px; }
.tracking-stat { border:1px solid var(--ui-border); background:linear-gradient(180deg,var(--ui-card),var(--ui-card-soft)); border-radius:8px; padding:10px 12px; min-height:70px; }
.tracking-stat span { display:block; color:var(--ui-muted); font-size:.72rem; margin-bottom:4px; }
.tracking-stat b { display:block; color:var(--ui-heading); font-size:1.28rem; line-height:1.25; }
.tracking-stat.warn b { color:var(--ui-amber,#b45309); }

.tracking-toolbar { display:flex; align-items:center; gap:8px; flex-wrap:wrap; border:1px solid var(--ui-border); background:var(--ui-card); border-radius:8px; padding:9px; box-shadow:0 10px 24px rgba(37,29,61,.06); }
.tracking-field { height:38px; display:inline-flex; align-items:center; gap:8px; background:var(--ui-card-soft); border:1px solid var(--ui-border); border-radius:7px; padding:0 10px; color:var(--ui-muted); transition:border-color .15s, background .15s; }
.tracking-field:focus-within { border-color:var(--ui-purple-line); background:var(--ui-card); }
.tracking-field :deep(input) { width:150px; height:100%; border:0; outline:0; background:transparent; color:var(--ui-heading); font-size:.82rem; }
.tracking-field :deep(button[role="combobox"]) {
  width:86px;
  min-height:36px;
  border:0!important;
  border-radius:0!important;
  background:transparent!important;
  color:var(--ui-heading)!important;
  box-shadow:none!important;
  padding:0 4px 0 0!important;
  font-size:.82rem;
  cursor:pointer;
  justify-content:space-between;
}
.tracking-field :deep(button[role="combobox"]:hover) { background:transparent!important; }
.tracking-field :deep(button[role="combobox"]:focus-visible) {
  outline:0!important;
}
.tracking-field.search-field :deep(input) { width:230px; }
.tracking-field.small :deep(button) { width:64px; text-transform:uppercase; }
.tracking-field.brand-filter :deep(button) { width:136px; }
.tracking-field span { color:var(--ui-muted); font-size:.72rem; font-weight:800; white-space:nowrap; }
.field-icon { color:var(--ui-dim); flex-shrink:0; }

.tool-btn, .dialog-btn { min-height:38px!important; height:38px!important; padding:0 12px!important; border:1px solid var(--ui-border)!important; border-radius:7px!important; display:inline-flex!important; align-items:center!important; justify-content:center!important; gap:6px!important; cursor:pointer; font-size:.8rem!important; line-height:1!important; font-weight:800!important; text-decoration:none; white-space:nowrap; transition:background .15s,border-color .15s,color .15s,transform .15s; box-shadow:none; }
.tool-btn.primary, .dialog-btn.primary { border-color:transparent!important; color:#fff!important; background:linear-gradient(135deg,#a78bfa,#7c3aed)!important; box-shadow:0 8px 18px rgba(124,58,237,.18)!important; }
.tool-btn.ghost, .dialog-btn.ghost { color:var(--ui-purple-strong)!important; background:var(--ui-purple-soft)!important; border-color:rgba(167,139,250,.28)!important; }
.dialog-btn.danger-solid { border-color:transparent!important; color:#fff!important; background:linear-gradient(135deg,#fb7185,#be123c)!important; box-shadow:0 8px 18px rgba(190,18,60,.16)!important; }
.tool-btn:hover, .dialog-btn:hover { transform:translateY(-1px); }
.tool-btn.ghost:hover, .dialog-btn.ghost:hover { background:rgba(167,139,250,.20); }
.tool-btn:disabled, .dialog-btn:disabled { opacity:.58; cursor:not-allowed; transform:none; }

.action-btn { width:32px!important; min-width:32px!important; height:32px!important; min-height:32px!important; padding:0!important; border:1px solid transparent!important; border-radius:7px!important; display:inline-flex!important; align-items:center!important; justify-content:center!important; cursor:pointer; color:var(--ui-muted)!important; background:transparent!important; box-shadow:none!important; text-decoration:none; line-height:1!important; transition:background .15s,border-color .15s,color .15s,transform .15s; }
.action-btn:hover { color:var(--ui-purple-strong)!important; background:var(--ui-purple-soft)!important; border-color:rgba(167,139,250,.28)!important; transform:translateY(-1px); }
.action-btn.danger:hover { color:var(--ui-red,#be123c)!important; background:rgba(248,113,113,.12)!important; border-color:rgba(248,113,113,.28)!important; }
.action-btn:disabled { cursor:not-allowed; opacity:.75; }
.action-btn svg { width:16px!important; height:16px!important; flex-shrink:0; }
.action-btn.trigger-queued, .action-btn.trigger-running, .action-btn.trigger-submitting, .action-btn.trigger-unknown { color:var(--ui-purple-strong)!important; background:var(--ui-purple-soft)!important; border-color:rgba(167,139,250,.34)!important; }
.action-btn.trigger-success { color:#047857!important; background:rgba(16,185,129,.14)!important; border-color:rgba(16,185,129,.30)!important; }
.action-btn.trigger-failed, .action-btn.trigger-blocked { color:var(--ui-red,#be123c)!important; background:rgba(248,113,113,.12)!important; border-color:rgba(248,113,113,.30)!important; }
.sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }

.tracking-table-wrap { width:100%; overflow:auto; border:1px solid var(--ui-border); border-radius:8px; background:var(--ui-card); box-shadow:0 14px 32px rgba(37,29,61,.08); }
.tracking-table :deep(table) { width:100%; min-width:1280px; border-collapse:separate; border-spacing:0; font-size:.84rem; }
.tracking-table :deep(thead) { position:sticky; top:0; z-index:2; }
.tracking-table :deep(th) { text-align:left; padding:10px 12px; background:var(--ui-card-soft); color:var(--ui-muted); border-bottom:1px solid var(--ui-border); font-size:.72rem; font-weight:800; white-space:nowrap; }
.tracking-table :deep(td) { padding:10px 12px; border-bottom:1px solid var(--ui-border); color:var(--ui-text); vertical-align:middle; }
.tracking-table :deep(tbody tr:hover td) { background:rgba(20,184,166,.06); }
.tracking-table :deep(tbody tr:last-child td) { border-bottom:0; }
.tracking-table :deep(.num) { text-align:right; font-variant-numeric:tabular-nums; }
.tracking-table :deep(.muted) { color:var(--ui-muted); }

.market-cell { display:flex; align-items:center; gap:7px; white-space:nowrap; }
.flag { width:20px; display:inline-flex; justify-content:center; font-family:"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif; }
.brand-cell { color:var(--ui-heading); font-weight:750; }
.site-link { max-width:270px; display:inline-flex; align-items:center; gap:5px; color:var(--ui-purple-strong); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; vertical-align:middle; }
.site-link span { overflow:hidden; text-overflow:ellipsis; }
.site-code { margin-top:2px; color:var(--ui-muted); font-size:.7rem; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.status-cell { display:flex; flex-direction:column; align-items:flex-start; gap:3px; min-width:82px; }
.status-pill { display:inline-flex; align-items:center; justify-content:center; min-width:58px; padding:3px 9px; border-radius:999px; font-size:.72rem; font-weight:800; }
.status-pill.ok { color:#047857; background:rgba(16,185,129,.16); border:1px solid rgba(16,185,129,.3); }
.status-pill.busy { color:var(--ui-purple-strong); background:var(--ui-purple-soft); border:1px solid rgba(167,139,250,.32); }
.status-pill.idle { color:var(--ui-muted); background:rgba(148,163,184,.12); border:1px solid rgba(148,163,184,.24); }
.status-pill.bad { color:var(--ui-red,#be123c); background:rgba(248,113,113,.14); border:1px solid rgba(248,113,113,.34); }
.status-detail { max-width:110px; color:var(--ui-red,#be123c); font-size:.68rem; line-height:1.25; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.time-cell { min-width:142px; color:var(--ui-muted); font-size:.78rem; white-space:nowrap; }
.creator-cell { color:var(--ui-text); font-size:.78rem; white-space:nowrap; }
.source-cell { min-width:74px; }
.source-cell span { display:block; color:var(--ui-text); }
.source-cell small { display:block; color:var(--ui-muted); font-size:.7rem; margin-top:1px; }
.actions-head { text-align:left; }
.actions-cell { display:flex; gap:4px; flex-wrap:wrap; min-width:164px; align-items:center; }
.row-trigger-status { flex:0 0 100%; max-width:180px; margin-top:2px; padding:3px 7px; border-radius:7px; background:var(--ui-card-soft); border:1px solid var(--ui-border); color:var(--ui-muted); font-size:.68rem; line-height:1.35; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.row-trigger-status.trigger-queued, .row-trigger-status.trigger-running, .row-trigger-status.trigger-submitting, .row-trigger-status.trigger-unknown { color:var(--ui-purple-strong); background:var(--ui-purple-soft); border-color:rgba(167,139,250,.28); }
.row-trigger-status.trigger-success { color:#047857; background:rgba(16,185,129,.14); border-color:rgba(16,185,129,.30); }
.row-trigger-status.trigger-failed, .row-trigger-status.trigger-blocked { color:var(--ui-red,#be123c); background:rgba(248,113,113,.12); border-color:rgba(248,113,113,.30); }
.tracking-empty { text-align:center; color:var(--ui-muted); padding:34px 12px!important; }

.tracking-pager { display:flex; align-items:center; justify-content:flex-end; gap:8px; color:var(--ui-muted); font-size:.82rem; }
.tracking-pager :deep(button) { height:34px; min-width:38px; border:1px solid var(--ui-border); border-radius:7px; background:var(--ui-card-soft); color:var(--ui-heading); padding:0 10px; box-shadow:none; }
.tracking-pager :deep(button:disabled) { opacity:.45; cursor:not-allowed; }
.tracking-pager .pager-info { color:var(--ui-heading); min-width:62px; text-align:center; }
.tracking-pager .pager-select { width:108px; flex:0 0 auto; }

:global(.tracking-dialog) { width:430px; max-width:calc(100vw - 32px); max-height:calc(100vh - 28px); display:flex; flex-direction:column; background:var(--ui-card); color:var(--ui-text); border:1px solid var(--ui-border); border-radius:8px; box-shadow:0 26px 70px rgba(0,0,0,.36); overflow:hidden; }
:global(.dialog-head) { position:relative; min-height:54px; display:flex; align-items:center; justify-content:space-between; gap:10px; padding:14px 56px 14px 16px; border-bottom:1px solid var(--ui-border); background:linear-gradient(180deg,var(--ui-card-soft),var(--ui-card)); }
:global(.dialog-head button[aria-label="Close"]) { width:36px!important; min-width:36px!important; height:36px!important; padding:0!important; top:9px!important; right:10px!important; display:inline-flex!important; align-items:center!important; justify-content:center!important; border-radius:9px!important; color:var(--ui-muted)!important; }
:global(.dialog-head button[aria-label="Close"]:hover) { background:var(--ui-card-soft)!important; color:var(--ui-heading)!important; }
:global(.dialog-head button[aria-label="Close"] svg) { width:18px!important; height:18px!important; }
:global(.dialog-title) { color:var(--ui-heading); font-size:1rem; font-weight:900; }
:global(.dialog-body) { position:relative; z-index:1; display:flex; flex-direction:column; gap:12px; padding:16px 16px 20px; overflow:auto; max-height:calc(100vh - 170px); background:var(--ui-card); }
:global(.dialog-foot) { position:relative; z-index:2; display:flex; align-items:center; justify-content:flex-end; gap:8px; padding:12px 16px; border-top:1px solid var(--ui-border); background:var(--ui-card-soft); flex-shrink:0; }
:global(.dialog-field) { display:flex; flex-direction:column; gap:6px; color:var(--ui-muted); font-size:.76rem; font-weight:800; }
:global(.dialog-field input) { width:100%; height:38px; border:1px solid var(--ui-border); border-radius:7px; background:var(--ui-card-soft); color:var(--ui-heading); padding:0 11px; outline:0; font-size:.84rem; box-shadow:none; }
:global(.dialog-field input:focus) { border-color:var(--ui-purple-line); background:var(--ui-card); }
.delete-confirm { display:flex; gap:12px; align-items:flex-start; color:var(--ui-text); min-width:0; }
.delete-confirm > svg { margin-top:2px; color:var(--ui-red,#be123c); flex-shrink:0; }
.delete-confirm b { display:block; color:var(--ui-heading); margin-bottom:5px; }
.delete-confirm p { margin:0; color:var(--ui-text); line-height:1.55; font-size:.84rem; }
.delete-confirm small { display:block; max-width:100%; margin-top:8px; margin-bottom:4px; color:var(--ui-muted); overflow-wrap:anywhere; word-break:break-word; line-height:1.45; }

.spin { animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }

:global(html[data-theme="dark"]) .tracking-table-wrap { box-shadow:0 16px 38px rgba(0,0,0,.46); }
:global(html[data-theme="dark"]) .tracking-table :deep(tbody tr:hover td) { background:rgba(20,184,166,.08); }
:global(html[data-theme="dark"]) .status-pill.ok { color:#6ee7b7; }
:global(html[data-theme="dark"]) .tracking-toolbar { box-shadow:0 16px 38px rgba(0,0,0,.34); }
:global(html[data-theme="dark"]) .tool-btn.primary,
:global(html[data-theme="dark"]) .dialog-btn.primary { background:linear-gradient(135deg,#a78bfa,#7c3aed); color:#fff; }

@media (max-width: 760px) {
  .tracking-head { flex-direction:column; }
  .tracking-add { width:100%; }
  .tracking-stats { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .tracking-field, .tracking-field.search-field, .tool-btn { width:100%; }
  .tracking-field :deep(input), .tracking-field.search-field :deep(input) { width:100%; }
  .tracking-field :deep(button) { width:100%; }
  .tracking-pager { justify-content:center; flex-wrap:wrap; }
  :global(.dialog-foot) { flex-wrap:wrap; }
  :global(.dialog-foot .dialog-btn) { flex:1 1 132px; }
}
</style>
