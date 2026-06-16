<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { tenants } from '../api/admin'
import { fmtDate, fmtNumber } from '../api/client'
import StatusBadge from '../components/common/StatusBadge.vue'

const items = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

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
      <table class="tbl">
        <thead>
          <tr>
            <th>ID</th>
            <th>租户</th>
            <th>slug</th>
            <th>状态</th>
            <th>成员</th>
            <th>站点</th>
            <th>商品</th>
            <th>评论</th>
            <th>API Key</th>
            <th>用量积分</th>
            <th>spine 任务</th>
            <th>按需任务</th>
            <th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in items" :key="row.id">
            <td>{{ row.id }}</td>
            <td>{{ row.name || '-' }}</td>
            <td>{{ row.slug || '-' }}</td>
            <td><StatusBadge :status="row.status" /></td>
            <td>{{ fmtNumber(row.member_count) }}</td>
            <td>{{ fmtNumber(row.site_count) }}</td>
            <td>{{ fmtNumber(row.product_count) }}</td>
            <td>{{ fmtNumber(row.review_count) }}</td>
            <td>{{ fmtNumber(row.api_key_count) }}</td>
            <td>{{ fmtNumber(row.usage_credits) }}</td>
            <td>{{ fmtNumber(row.spine_job_count) }}</td>
            <td>{{ fmtNumber(row.ondemand_job_count) }}</td>
            <td>{{ fmtDate(row.created_at) }}</td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="13" class="empty">{{ loading ? '加载中…' : '暂无租户' }}</td>
          </tr>
        </tbody>
      </table>
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
