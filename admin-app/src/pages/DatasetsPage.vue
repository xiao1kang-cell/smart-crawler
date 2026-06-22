<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { listDatasets } from '../api/datasets'
import { fmtNumber } from '../api/client'

const router = useRouter()

const items = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const datasetColumns = [
  { accessorKey: 'id', header: 'ID' },
  { accessorKey: 'name', header: '名称' },
  { accessorKey: 'slug', header: 'slug' },
  { accessorKey: 'entity_type', header: '实体类型' },
  { accessorKey: 'record_count', header: '记录数' },
  { accessorKey: 'workspace_id', header: '租户' },
  { id: 'actions', header: '' }
]

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await listDatasets()
    items.value = res?.items ?? []
    total.value = res?.total ?? items.value.length
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function view(id: number) {
  router.push(`/datasets/${id}`)
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1 class="page-title">通用数据集</h1>
        <p class="page-subtitle">仅统计 spine normalized 层；商品库和 VOC 在概览的数据库存中单独统计。</p>
      </div>
      <button class="btn small" :disabled="loading" @click="load">刷新</button>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div class="table-wrap">
      <UTable class="tbl ui-table" :data="items" :columns="datasetColumns" :loading="loading" sticky="header" empty="暂无数据集">
        <template #name-cell="{ row }">{{ row.original.name || '-' }}</template>
        <template #slug-cell="{ row }">{{ row.original.slug || '-' }}</template>
        <template #entity_type-cell="{ row }">{{ row.original.entity_type || '-' }}</template>
        <template #record_count-cell="{ row }">{{ fmtNumber(row.original.record_count) }}</template>
        <template #workspace_id-cell="{ row }">{{ row.original.workspace_id ?? '-' }}</template>
        <template #actions-cell="{ row }">
          <button class="btn small" @click="view(row.original.id)">查看</button>
        </template>
      </UTable>
    </div>

    <p class="count">共 {{ total }} 个数据集</p>
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

.page-title {
  font-size: 20px;
  font-weight: 600;
}

.page-subtitle {
  margin-top: 4px;
  font-size: 12px;
  opacity: 0.55;
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

.empty {
  text-align: center;
  opacity: 0.6;
  padding: 24px;
}

.count {
  font-size: 13px;
  opacity: 0.6;
}
</style>
