<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  datasetRecords,
  deleteRecord,
  listDatasets,
  promoteRecord,
  recordDetail
} from '../api/datasets'
import { fmtDate } from '../api/client'
import StatusBadge from '../components/common/StatusBadge.vue'

const route = useRoute()
const router = useRouter()

const datasetId = computed(() => Number(route.params.id))

const dataset = ref<any>(null)
const items = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

const qualityFilter = ref('')
const page = ref(1)
const size = ref(20)
const ALL_QUALITY = '__all_quality__'

const detail = ref<any>(null)
const detailLoading = ref(false)
const detailError = ref('')

const busyId = ref<number | null>(null)
const recordColumns = [
  { accessorKey: 'id', header: 'ID' },
  { accessorKey: 'source_url', header: '来源 URL' },
  { accessorKey: 'entity_type', header: '实体类型' },
  { accessorKey: 'quality_status', header: '质量' },
  { accessorKey: 'confidence', header: '置信度' },
  { accessorKey: 'fetched_at', header: '抓取时间' },
  { id: 'actions', header: '' }
]

const qualityFilterItems = [
  { label: '全部质量', value: ALL_QUALITY },
  { label: 'main', value: 'main' },
  { label: 'staging', value: 'staging' },
  { label: 'quarantine', value: 'quarantine' }
]
const qualitySelect = computed({
  get: () => qualityFilter.value || ALL_QUALITY,
  set: (value: string) => {
    qualityFilter.value = value === ALL_QUALITY ? '' : value
  }
})
const pageSizeItems = [
  { label: '20 / 页', value: 20 },
  { label: '50 / 页', value: 50 },
  { label: '100 / 页', value: 100 }
]

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size.value)))

const detailJson = computed(() =>
  detail.value ? JSON.stringify(detail.value.data ?? {}, null, 2) : ''
)

async function loadDataset() {
  try {
    const res = await listDatasets()
    dataset.value = (res?.items ?? []).find((d: any) => Number(d.id) === datasetId.value) ?? null
  } catch {
    dataset.value = null
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await datasetRecords(datasetId.value, {
      quality_status: qualityFilter.value,
      page: page.value,
      size: size.value
    })
    items.value = res?.items ?? []
    total.value = res?.total ?? 0
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function loadAll() {
  await Promise.all([loadDataset(), load()])
}

watch([qualityFilter, size], () => {
  page.value = 1
  load()
})

function changePage(delta: number) {
  const next = page.value + delta
  if (next < 1 || next > totalPages.value) return
  page.value = next
  load()
}

function setPage(next: number) {
  page.value = next
  load()
}

async function openDetail(id: number) {
  detail.value = null
  detailError.value = ''
  detailLoading.value = true
  try {
    detail.value = await recordDetail(id)
  } catch (err) {
    detailError.value = err instanceof Error ? err.message : String(err)
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  detail.value = null
  detailError.value = ''
}

async function doPromote(id: number) {
  if (!window.confirm(`确认将记录 #${id} 提升为 main？`)) return
  busyId.value = id
  try {
    await promoteRecord(id)
    await loadAll()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busyId.value = null
  }
}

async function doDelete(id: number) {
  if (!window.confirm(`确认删除记录 #${id}？此操作不可恢复。`)) return
  if (!window.confirm(`再次确认：永久删除记录 #${id}？`)) return
  busyId.value = id
  try {
    await deleteRecord(id)
    if (detail.value?.id === id) closeDetail()
    await loadAll()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busyId.value = null
  }
}

function truncate(s?: string | null, n = 48) {
  const v = String(s || '')
  return v.length > n ? `${v.slice(0, n)}…` : v
}

onMounted(() => {
  loadAll()
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div class="head-left">
        <button class="btn small" @click="router.push('/datasets')">← 返回</button>
        <h1 class="page-title">{{ dataset?.name || `数据集 #${datasetId}` }}</h1>
      </div>
      <button class="btn small" :disabled="loading" @click="loadAll">刷新</button>
    </div>

    <div v-if="dataset" class="meta">
      <span>ID：{{ dataset.id }}</span>
      <span>slug：{{ dataset.slug || '-' }}</span>
      <span>实体类型：{{ dataset.entity_type || '-' }}</span>
      <span>记录数：{{ dataset.record_count ?? '-' }}</span>
      <span>租户：{{ dataset.workspace_id ?? '-' }}</span>
    </div>

    <div class="toolbar">
      <USelect v-model="qualitySelect" class="select-ctl" :items="qualityFilterItems" value-key="value" />
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div class="table-wrap">
      <UTable class="tbl ui-table" :data="items" :columns="recordColumns" :loading="loading" sticky="header" empty="暂无记录">
        <template #source_url-cell="{ row }">
          <span :title="row.original.source_url">{{ truncate(row.original.source_url) }}</span>
        </template>
        <template #entity_type-cell="{ row }">{{ row.original.entity_type || '-' }}</template>
        <template #quality_status-cell="{ row }"><StatusBadge :status="row.original.quality_status" /></template>
        <template #confidence-cell="{ row }">{{ row.original.confidence ?? '-' }}</template>
        <template #fetched_at-cell="{ row }">{{ fmtDate(row.original.fetched_at) }}</template>
        <template #actions-cell="{ row }">
          <div class="actions">
            <button class="btn small" @click="openDetail(row.original.id)">详情</button>
            <button
              class="btn small"
              :disabled="busyId === row.original.id || row.original.quality_status === 'main'"
              @click="doPromote(row.original.id)"
            >
              提升
            </button>
            <button class="btn small danger" :disabled="busyId === row.original.id" @click="doDelete(row.original.id)">
              删除
            </button>
          </div>
        </template>
      </UTable>
    </div>

    <div class="pager">
      <UPagination
        :page="page"
        :total="total"
        :items-per-page="size"
        :disabled="loading || totalPages <= 1"
        size="sm"
        show-edges
        @update:page="setPage"
      />
      <span>第 {{ page }} / {{ totalPages }} 页 · 共 {{ total }} 条</span>
      <USelect v-model="size" class="size-select" :items="pageSizeItems" value-key="value" />
    </div>

    <div v-if="detail || detailLoading || detailError" class="drawer-mask" @click.self="closeDetail">
      <aside class="drawer">
        <div class="drawer-head">
          <h2 class="drawer-title">记录详情 {{ detail ? `#${detail.id}` : '' }}</h2>
          <button class="btn small" @click="closeDetail">关闭</button>
        </div>

        <div v-if="detailLoading" class="drawer-body">加载中…</div>
        <div v-else-if="detailError" class="drawer-body error">{{ detailError }}</div>
        <div v-else-if="detail" class="drawer-body">
          <div class="dsec">
            <div class="dsec-head">基本</div>
            <div class="meta">
              <span>实体类型：{{ detail.entity_type || '-' }}</span>
              <span>质量：<StatusBadge :status="detail.quality_status" /></span>
              <span>置信度：{{ detail.confidence ?? '-' }}</span>
            </div>
          </div>

          <div class="dsec">
            <div class="dsec-head">data</div>
            <pre class="json">{{ detailJson }}</pre>
          </div>

          <div v-if="detail.provenance" class="dsec">
            <div class="dsec-head">provenance</div>
            <div class="kv">
              <div><span class="k">source_url</span><span class="v">{{ detail.provenance.source_url || '-' }}</span></div>
              <div><span class="k">canonical_url</span><span class="v">{{ detail.provenance.canonical_url || '-' }}</span></div>
              <div><span class="k">content_hash</span><span class="v">{{ detail.provenance.content_hash || '-' }}</span></div>
              <div><span class="k">extraction_method</span><span class="v">{{ detail.provenance.extraction_method || '-' }}</span></div>
              <div><span class="k">fetched_at</span><span class="v">{{ fmtDate(detail.provenance.fetched_at) }}</span></div>
            </div>
          </div>

          <div class="dsec">
            <div class="dsec-head">snapshot</div>
            <div v-if="detail.snapshot" class="kv">
              <div><span class="k">id</span><span class="v">{{ detail.snapshot.id }}</span></div>
              <div><span class="k">url</span><span class="v">{{ detail.snapshot.url || '-' }}</span></div>
              <div><span class="k">fetched_at</span><span class="v">{{ fmtDate(detail.snapshot.fetched_at) }}</span></div>
            </div>
            <div v-else class="meta">无快照</div>
          </div>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.head-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.page-title {
  font-size: 20px;
  font-weight: 600;
}

.meta {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
  align-items: center;
  font-size: 13px;
  opacity: 0.8;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.ctl {
  padding: 8px 12px;
  border-radius: 8px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
  background: var(--ui-bg, rgba(0, 0, 0, 0.15));
  color: inherit;
  font-size: 14px;
}

.btn {
  cursor: pointer;
}

.btn.small {
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 12px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
  background: transparent;
  color: inherit;
}

.btn.danger {
  color: #ef4444;
  border-color: rgba(239, 68, 68, 0.4);
}

.btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.error {
  font-size: 13px;
  color: #ef4444;
}

.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.08));
  border-radius: 12px;
}

.tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.tbl th,
.tbl td {
  padding: 10px 12px;
  text-align: left;
  border-bottom: 1px solid var(--ui-border, rgba(255, 255, 255, 0.06));
  white-space: nowrap;
}

.tbl th {
  font-weight: 600;
  opacity: 0.7;
}

.actions {
  display: flex;
  gap: 6px;
}

.empty {
  text-align: center;
  opacity: 0.6;
  padding: 24px;
}

.pager {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
}

.drawer-mask {
  position: fixed;
  inset: 0;
  background: var(--admin-overlay, rgba(15, 23, 42, 0.42));
  backdrop-filter: blur(4px);
  display: flex;
  justify-content: flex-end;
  z-index: 120;
}

.drawer {
  width: min(560px, 92vw);
  height: 100%;
  overflow-y: auto;
  background: var(--ui-panel, #fff);
  color: var(--ui-text, #0f172a);
  border-left: 1px solid var(--ui-border, rgba(148, 163, 184, 0.32));
  box-shadow: -20px 0 60px rgba(15, 23, 42, 0.22);
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.drawer-title {
  font-size: 16px;
  font-weight: 600;
}

.drawer-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.dsec {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.dsec-head {
  font-size: 13px;
  font-weight: 600;
  opacity: 0.7;
}

.json {
  margin: 0;
  padding: 12px;
  border-radius: 8px;
  background: var(--admin-panel-soft, #f8fafc);
  border: 1px solid var(--ui-border, rgba(148, 163, 184, 0.32));
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 320px;
  overflow: auto;
}

.kv {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
}

.kv > div {
  display: flex;
  gap: 10px;
}

.kv .k {
  flex: 0 0 130px;
  opacity: 0.6;
}

.kv .v {
  flex: 1;
  word-break: break-all;
}
</style>
