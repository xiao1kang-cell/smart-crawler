<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { asList, fmtNumber, fmtPrice } from '../api/client'
import { addTracking, deleteTracking, editTracking, listTracking, pauseTracking, resumeTracking } from '../api/tracking'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const rows = ref<Record<string, any>[]>([])
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

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / Number(pageSize.value || 10))))
const canEdit = computed(() => {
  const u = auth.user
  if (!u) return false
  return u.global_role === 'super_admin' || ['admin', 'owner'].includes(u.role || '')
})
function flag(cc?: string) {
  if (!cc || cc.length !== 2) return '🌐'
  return String.fromCodePoint(...[...cc.toUpperCase()].map((c) => 127397 + c.charCodeAt(0)))
}
function statusLabel(s?: string) {
  return ({ tracking: 'Tracking', paused: 'Paused', error: '⚠️ 异常' } as Record<string, string>)[s || ''] || s || '—'
}

async function load() {
  loading.value = true; error.value = ''
  try {
    const d = await listTracking({
      search: search.value, market: fMarket.value, brand: fBrand.value,
      status: fStatus.value, page: page.value, page_size: pageSize.value,
    })
    rows.value = asList(d, ['items'])
    total.value = Number(d?.total || rows.value.length || 0)
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { loading.value = false }
}
function applySearch() { page.value = 1; load() }

async function submitAdd() {
  if (!addForm.value.url.trim()) return
  addBusy.value = true; error.value = ''
  try {
    await addTracking({ url: addForm.value.url.trim(), brand: addForm.value.brand.trim() || undefined, country: addForm.value.country.trim() || undefined })
    showAdd.value = false
    addForm.value = { url: '', brand: '', country: '' }
    page.value = 1; await load()
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { addBusy.value = false }
}
async function saveEdit() {
  if (!editing.value) return
  try {
    await editTracking(editing.value.site, { brand: editing.value.brand, country: editing.value.country, review_rate: editing.value.review_rate === '' ? null : Number(editing.value.review_rate) })
    editing.value = null; await load()
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
}
async function togglePause(row: Record<string, any>) {
  try {
    if (row.track_status === 'paused') await resumeTracking(row.site)
    else await pauseTracking(row.site)
    await load()
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
}
async function remove(row: Record<string, any>) {
  if (!window.confirm(`确认删除追踪「${row.brand || row.site}」？此操作不可撤销。`)) return
  try { await deleteTracking(row.site); await load() }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
}
function reportHref(row: Record<string, any>) {
  const p = new URLSearchParams({ site: row.site })
  if (auth.workspaceId) p.set('workspace_id', auth.workspaceId)
  return `/report?${p.toString()}`
}
function exportUrl() {
  const p = new URLSearchParams({ search: search.value, market: fMarket.value, brand: fBrand.value, status: fStatus.value, token: auth.token })
  if (auth.workspaceId) p.set('workspace_id', auth.workspaceId)
  return `/api/tracking/export?${p.toString()}`
}

onMounted(async () => {
  if (auth.token && !auth.user) await auth.loadMe().catch(() => null)
  await load()
})
</script>

<template>
  <section>
    <div class="lead">标杆网站维护</div>
    <div class="sub">{{ loading ? '加载中' : total + ' 个追踪站点' }}</div>
    <UAlert v-if="error" color="error" variant="soft" :title="error" class="mb-4" />

    <div class="tk-toolbar">
      <button v-if="canEdit" class="btn-prim" @click="showAdd = true">+ Add Tracking</button>
      <input class="tk-in" v-model="search" placeholder="🔍 URL / Brand" @keyup.enter="applySearch" />
      <input class="tk-in" v-model="fMarket" placeholder="Market (US/DE…)" @keyup.enter="applySearch" />
      <input class="tk-in" v-model="fBrand" placeholder="Brand" @keyup.enter="applySearch" />
      <select class="tk-in" v-model="fStatus" @change="applySearch">
        <option value="">全部状态</option><option value="tracking">Tracking</option>
        <option value="paused">Paused</option><option value="error">异常</option>
      </select>
      <button class="btn-muted" @click="applySearch">筛选</button>
      <a class="btn-muted" :href="exportUrl()" target="_blank">📥 导出</a>
    </div>

    <table class="tk-table">
      <thead><tr>
        <th>Market</th><th>Brand</th><th>URL</th><th>Status</th><th>Products</th>
        <th>30-Day Sales</th><th>30-Day Revenue</th><th>Updated</th><th>Created</th><th>Creator</th><th>操作</th>
      </tr></thead>
      <tbody>
        <tr v-for="r in rows" :key="r.site">
          <td>{{ flag(r.country) }} {{ r.country || '—' }}</td>
          <td>{{ r.brand || '—' }}</td>
          <td><a class="title-text" :href="r.url" target="_blank" rel="noopener" :title="r.url">{{ r.url }}</a></td>
          <td><span class="tk-badge" :class="r.track_status">{{ statusLabel(r.track_status) }}</span></td>
          <td>{{ fmtNumber(r.products) }}</td>
          <td>{{ fmtNumber(r.thirty_day_sales) }}</td>
          <td>{{ fmtPrice(r.thirty_day_revenue, undefined) }}</td>
          <td>{{ (r.updated_at || '').replace('T', ' ').slice(0, 16) || '—' }}</td>
          <td>{{ (r.created_at || '').replace('T', ' ').slice(0, 16) || '—' }}</td>
          <td>{{ r.creator || '—' }}</td>
          <td class="tk-actions">
            <a :href="reportHref(r)" target="_blank" rel="noopener" class="btn-mini">报告</a>
            <template v-if="canEdit">
              <button class="btn-mini" @click="editing = { ...r }">编辑</button>
              <button class="btn-mini" @click="togglePause(r)">{{ r.track_status === 'paused' ? '恢复' : '暂停' }}</button>
              <button v-if="r.source === 'user'" class="btn-mini btn-danger" @click="remove(r)">删除</button>
            </template>
          </td>
        </tr>
        <tr v-if="!rows.length"><td colspan="11" class="tk-empty">暂无追踪站点</td></tr>
      </tbody>
    </table>

    <div class="pagination">
      <button @click="page = Math.max(1, page - 1); load()" :disabled="page <= 1">‹</button>
      <span>{{ page }} / {{ totalPages }}</span>
      <button @click="page = Math.min(totalPages, page + 1); load()" :disabled="page >= totalPages">›</button>
      <select v-model="pageSize" @change="page = 1; load()">
        <option :value="10">10</option><option :value="20">20</option><option :value="50">50</option>
        <option :value="100">100</option><option :value="200">200</option>
      </select>
    </div>

    <div v-if="showAdd" class="tk-modal" @click.self="showAdd = false">
      <div class="tk-card">
        <h3>+ Add Tracking</h3>
        <label>URL<input v-model="addForm.url" placeholder="https://brand.example.com" /></label>
        <label>Brand（选填）<input v-model="addForm.brand" maxlength="50" /></label>
        <label>Market（选填，如 US）<input v-model="addForm.country" maxlength="8" /></label>
        <div class="tk-card-foot">
          <button class="btn-muted" @click="showAdd = false">取消</button>
          <button class="btn-prim" :disabled="addBusy" @click="submitAdd">{{ addBusy ? '探测中…' : '添加并抓取' }}</button>
        </div>
      </div>
    </div>

    <div v-if="editing" class="tk-modal" @click.self="editing = null">
      <div class="tk-card">
        <h3>编辑追踪</h3>
        <label>Brand<input v-model="editing.brand" maxlength="50" /></label>
        <label>Market<input v-model="editing.country" maxlength="8" /></label>
        <label>留评率 review_rate<input v-model="editing.review_rate" type="number" step="0.001" /></label>
        <div class="tk-card-foot">
          <button class="btn-muted" @click="editing = null">取消</button>
          <button class="btn-prim" @click="saveEdit">保存</button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.tk-toolbar { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; align-items:center; }
.tk-in { padding:6px 10px; border:1px solid #d1d5db; border-radius:7px; font-size:13px; font-family:inherit; }
.tk-table { width:100%; border-collapse:collapse; font-size:13px; }
.tk-table th, .tk-table td { text-align:left; padding:10px 12px; border-bottom:1px solid #f0f1f3; }
.title-text { display:inline-block; max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; vertical-align:bottom; }
.tk-badge { padding:2px 8px; border-radius:9px; font-size:11px; font-weight:700; }
.tk-badge.tracking { background:#dcfce7; color:#166534; }
.tk-badge.paused { background:#f3f4f6; color:#6b7280; }
.tk-badge.error { background:#fee2e2; color:#991b1b; }
.tk-actions { display:flex; gap:6px; flex-wrap:wrap; }
.btn-mini { padding:3px 8px; border:1px solid #d1d5db; border-radius:6px; background:#fff; cursor:pointer; font-size:12px; }
.btn-mini.btn-danger { color:#b91c1c; border-color:#fecaca; }
.tk-empty { text-align:center; color:#9ca3af; padding:28px; }
.tk-modal { position:fixed; inset:0; background:rgba(0,0,0,.4); display:flex; align-items:center; justify-content:center; z-index:100; }
.tk-card { background:#fff; border-radius:12px; padding:22px; width:420px; max-width:92vw; display:flex; flex-direction:column; gap:12px; }
.tk-card label { display:flex; flex-direction:column; gap:4px; font-size:12.5px; color:#6b7280; }
.tk-card input, .tk-card select { padding:7px 10px; border:1px solid #d1d5db; border-radius:7px; font-size:13px; font-family:inherit; }
.tk-card-foot { display:flex; justify-content:flex-end; gap:8px; margin-top:6px; }
.pagination { display:flex; align-items:center; gap:8px; justify-content:center; margin-top:14px; }
</style>
