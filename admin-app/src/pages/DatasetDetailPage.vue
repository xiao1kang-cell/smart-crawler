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

const detail = ref<any>(null)
const detailLoading = ref(false)
const detailError = ref('')

const busyId = ref<number | null>(null)

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
    await load()
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
    await load()
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
  loadDataset()
  load()
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div class="head-left">
        <button class="btn small" @click="router.push('/datasets')">← 返回</button>
        <h1 class="page-title">{{ dataset?.name || `数据集 #${datasetId}` }}</h1>
      </div>
      <button class="btn small" :disabled="loading" @click="load">刷新</button>
    </div>

    <div v-if="dataset" class="meta">
      <span>ID：{{ dataset.id }}</span>
      <span>slug：{{ dataset.slug || '-' }}</span>
      <span>实体类型：{{ dataset.entity_type || '-' }}</span>
      <span>记录数：{{ dataset.record_count ?? '-' }}</span>
      <span>租户：{{ dataset.workspace_id ?? '-' }}</span>
    </div>

    <div class="toolbar">
      <select v-model="qualityFilter" class="ctl">
        <option value="">全部质量</option>
        <option value="main">main</option>
        <option value="staging">staging</option>
        <option value="quarantine">quarantine</option>
      </select>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div class="table-wrap">
      <table class="tbl">
        <thead>
          <tr>
            <th>ID</th>
            <th>来源 URL</th>
            <th>实体类型</th>
            <th>质量</th>
            <th>置信度</th>
            <th>抓取时间</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in items" :key="row.id">
            <td>{{ row.id }}</td>
            <td :title="row.source_url">{{ truncate(row.source_url) }}</td>
            <td>{{ row.entity_type || '-' }}</td>
            <td><StatusBadge :status="row.quality_status" /></td>
            <td>{{ row.confidence ?? '-' }}</td>
            <td>{{ fmtDate(row.fetched_at) }}</td>
            <td class="actions">
              <button class="btn small" @click="openDetail(row.id)">详情</button>
              <button
                class="btn small"
                :disabled="busyId === row.id || row.quality_status === 'main'"
                @click="doPromote(row.id)"
              >
                提升
              </button>
              <button class="btn small danger" :disabled="busyId === row.id" @click="doDelete(row.id)">
                删除
              </button>
            </td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="7" class="empty">{{ loading ? '加载中…' : '暂无记录' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="pager">
      <button class="btn small" :disabled="page <= 1" @click="changePage(-1)">上一页</button>
      <span>第 {{ page }} / {{ totalPages }} 页 · 共 {{ total }} 条</span>
      <button class="btn small" :disabled="page >= totalPages" @click="changePage(1)">下一页</button>
      <select v-model.number="size" class="ctl">
        <option :value="20">20 / 页</option>
        <option :value="50">50 / 页</option>
        <option :value="100">100 / 页</option>
      </select>
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
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  justify-content: flex-end;
  z-index: 50;
}

.drawer {
  width: min(560px, 92vw);
  height: 100%;
  overflow-y: auto;
  background: var(--ui-bg-elevated, #1a1a1f);
  border-left: 1px solid var(--ui-border, rgba(255, 255, 255, 0.1));
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
  background: var(--ui-bg, rgba(0, 0, 0, 0.25));
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.08));
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
