<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { tenants } from '../api/admin'
import { fmtDate, fmtNumber } from '../api/client'
import StatusBadge from '../components/common/StatusBadge.vue'

const items = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const tenantColumns = [
  { accessorKey: 'id', header: 'ID' },
  { accessorKey: 'name', header: '租户' },
  { accessorKey: 'slug', header: 'slug' },
  { accessorKey: 'status', header: '状态' },
  { accessorKey: 'member_count', header: '成员' },
  { accessorKey: 'site_count', header: '站点' },
  { accessorKey: 'product_count', header: '商品' },
  { accessorKey: 'review_count', header: '评论' },
  { accessorKey: 'api_key_count', header: 'API Key' },
  { accessorKey: 'usage_credits', header: '用量积分' },
  { accessorKey: 'spine_job_count', header: 'spine 任务' },
  { accessorKey: 'ondemand_job_count', header: '按需任务' },
  { accessorKey: 'created_at', header: '创建时间' }
]

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await tenants()
    items.value = res?.items ?? []
    total.value = res?.total ?? items.value.length
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h1 class="page-title">租户用户</h1>
      <button class="btn small" :disabled="loading" @click="load">刷新</button>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div class="table-wrap">
      <UTable class="tbl ui-table" :data="items" :columns="tenantColumns" :loading="loading" sticky="header" empty="暂无租户">
        <template #name-cell="{ row }">{{ row.original.name || '-' }}</template>
        <template #slug-cell="{ row }">{{ row.original.slug || '-' }}</template>
        <template #status-cell="{ row }"><StatusBadge :status="row.original.status" /></template>
        <template #member_count-cell="{ row }">{{ fmtNumber(row.original.member_count) }}</template>
        <template #site_count-cell="{ row }">{{ fmtNumber(row.original.site_count) }}</template>
        <template #product_count-cell="{ row }">{{ fmtNumber(row.original.product_count) }}</template>
        <template #review_count-cell="{ row }">{{ fmtNumber(row.original.review_count) }}</template>
        <template #api_key_count-cell="{ row }">{{ fmtNumber(row.original.api_key_count) }}</template>
        <template #usage_credits-cell="{ row }">{{ fmtNumber(row.original.usage_credits) }}</template>
        <template #spine_job_count-cell="{ row }">{{ fmtNumber(row.original.spine_job_count) }}</template>
        <template #ondemand_job_count-cell="{ row }">{{ fmtNumber(row.original.ondemand_job_count) }}</template>
        <template #created_at-cell="{ row }">{{ fmtDate(row.original.created_at) }}</template>
      </UTable>
    </div>

    <p class="count">共 {{ total }} 个租户</p>
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
