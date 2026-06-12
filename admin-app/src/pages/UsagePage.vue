<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { usageByKey, usageByTenant, usageSummary } from '../api/usage'
import { fmtNumber } from '../api/client'
import StatCard from '../components/common/StatCard.vue'

const summary = ref<Record<string, any>>({})
const byKey = ref<any[]>([])
const byTenant = ref<any[]>([])
const loading = ref(false)
const error = ref('')

const filters = ref({ endpoint: '', start: '', end: '' })

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [s, k, t] = await Promise.all([
      usageSummary({
        endpoint: filters.value.endpoint,
        start: filters.value.start,
        end: filters.value.end
      }),
      usageByKey(),
      usageByTenant()
    ])
    summary.value = s || {}
    byKey.value = k?.items ?? []
    byTenant.value = t?.items ?? []
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
      <h1 class="page-title">计费用量</h1>
      <button class="btn small" :disabled="loading" @click="load">刷新</button>
    </div>

    <div class="toolbar">
      <input v-model="filters.endpoint" class="ctl grow" placeholder="endpoint 过滤 (如 /spine/worker/execute)" />
      <input v-model="filters.start" class="ctl" type="date" />
      <input v-model="filters.end" class="ctl" type="date" />
      <button class="ctl btn primary" :disabled="loading" @click="load">查询</button>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div class="stat-row">
      <StatCard label="总积分消耗" :value="fmtNumber(summary.total_credits)" />
      <StatCard label="总记录数" :value="fmtNumber(summary.total_records)" />
      <StatCard label="明细行数" :value="fmtNumber(summary.rows || 0)" />
    </div>

    <section class="block">
      <h2 class="block-title">按 API Key</h2>
      <div class="table-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th>API Key ID</th>
              <th>积分</th>
              <th>调用次数</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in byKey" :key="row.api_key_id">
              <td>{{ row.api_key_id }}</td>
              <td>{{ fmtNumber(row.credits) }}</td>
              <td>{{ fmtNumber(row.calls) }}</td>
            </tr>
            <tr v-if="!byKey.length">
              <td colspan="3" class="empty">{{ loading ? '加载中…' : '暂无数据' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="block">
      <h2 class="block-title">按租户</h2>
      <div class="table-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th>租户 (workspace_id)</th>
              <th>积分</th>
              <th>调用次数</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in byTenant" :key="row.workspace_id">
              <td>{{ row.workspace_id }}</td>
              <td>{{ fmtNumber(row.credits) }}</td>
              <td>{{ fmtNumber(row.calls) }}</td>
            </tr>
            <tr v-if="!byTenant.length">
              <td colspan="3" class="empty">{{ loading ? '加载中…' : '暂无数据' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page {
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
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

.grow {
  flex: 1;
  min-width: 240px;
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

.stat-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 240px));
  gap: 12px;
}

.block {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.block-title {
  font-size: 15px;
  font-weight: 600;
  opacity: 0.85;
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
</style>
