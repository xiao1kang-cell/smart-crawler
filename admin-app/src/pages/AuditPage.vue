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
      <table class="tbl">
        <thead>
          <tr>
            <th>ID</th>
            <th>时间</th>
            <th>操作者</th>
            <th>动作</th>
            <th>对象</th>
            <th>IP</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in items" :key="row.id">
            <td>{{ row.id }}</td>
            <td>{{ fmtDate(row.created_at) }}</td>
            <td>{{ row.actor_name || '-' }}</td>
            <td>{{ row.action || '-' }}</td>
            <td>{{ row.target_type || '-' }} #{{ row.target_id || '-' }}</td>
            <td>{{ row.ip || '-' }}</td>
            <td class="detail" :title="detailText(row.detail)">{{ detailText(row.detail) }}</td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="7" class="empty">{{ loading ? '加载中…' : '暂无审计记录' }}</td>
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
