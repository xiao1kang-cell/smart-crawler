<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { audit } from '../api/admin'
import { fmtDate } from '../api/client'

const items = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

const filters = ref({ actor: '', action: '', start: '', end: '' })
const page = ref(1)
const size = ref(20)

const pageSizeItems = [
  { label: '20 / 页', value: 20 },
  { label: '50 / 页', value: 50 },
  { label: '100 / 页', value: 100 }
]
const auditColumns = [
  { accessorKey: 'id', header: 'ID' },
  { accessorKey: 'created_at', header: '时间' },
  { accessorKey: 'actor_name', header: '操作者' },
  { accessorKey: 'action', header: '动作' },
  { accessorKey: 'target_type', header: '对象' },
  { accessorKey: 'ip', header: 'IP' },
  { accessorKey: 'detail', header: '详情' }
]

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size.value)))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await audit({
      ...filters.value,
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

function detailText(value: unknown) {
  if (!value) return '-'
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

watch(size, () => {
  page.value = 1
  load()
})

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h1 class="page-title">审计</h1>
      <button class="btn small" :disabled="loading" @click="load">刷新</button>
    </div>

    <div class="toolbar">
      <input v-model="filters.actor" class="ctl" placeholder="actor" />
      <input v-model="filters.action" class="ctl" placeholder="action" />
      <input v-model="filters.start" class="ctl" type="date" />
      <input v-model="filters.end" class="ctl" type="date" />
      <button class="ctl btn primary" :disabled="loading" @click="page = 1; load()">查询</button>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div class="table-wrap">
      <UTable class="tbl ui-table" :data="items" :columns="auditColumns" :loading="loading" sticky="header" empty="暂无审计记录">
        <template #created_at-cell="{ row }">{{ fmtDate(row.original.created_at) }}</template>
        <template #actor_name-cell="{ row }">{{ row.original.actor_name || '-' }}</template>
        <template #action-cell="{ row }">{{ row.original.action || '-' }}</template>
        <template #target_type-cell="{ row }">{{ row.original.target_type || '-' }} #{{ row.original.target_id || '-' }}</template>
        <template #ip-cell="{ row }">{{ row.original.ip || '-' }}</template>
        <template #detail-cell="{ row }">
          <span class="detail" :title="detailText(row.original.detail)">{{ detailText(row.original.detail) }}</span>
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
  </div>
</template>

<style scoped>
.page {
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-head,
.toolbar,
.pager {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.page-head {
  justify-content: space-between;
}

.page-title {
  font-size: 20px;
  font-weight: 600;
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

.btn.primary {
  border: none;
  color: #fff;
  background: var(--ui-color-primary-500, #6366f1);
}

.btn.small {
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 12px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
  background: transparent;
  color: inherit;
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

.detail {
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.empty {
  text-align: center;
  opacity: 0.6;
  padding: 24px;
}

.pager {
  font-size: 13px;
  opacity: 0.85;
}
</style>
