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
      <h1 class="page-title">数据集</h1>
      <button class="btn small" :disabled="loading" @click="load">刷新</button>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div class="table-wrap">
      <table class="tbl">
        <thead>
          <tr>
            <th>ID</th>
            <th>名称</th>
            <th>slug</th>
            <th>实体类型</th>
            <th>记录数</th>
            <th>租户</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in items" :key="row.id">
            <td>{{ row.id }}</td>
            <td>{{ row.name || '-' }}</td>
            <td>{{ row.slug || '-' }}</td>
            <td>{{ row.entity_type || '-' }}</td>
            <td>{{ fmtNumber(row.record_count) }}</td>
            <td>{{ row.workspace_id ?? '-' }}</td>
            <td>
              <button class="btn small" @click="view(row.id)">查看</button>
            </td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="7" class="empty">{{ loading ? '加载中…' : '暂无数据集' }}</td>
          </tr>
        </tbody>
      </table>
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
