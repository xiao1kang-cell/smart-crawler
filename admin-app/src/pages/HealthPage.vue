<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { config, inventory } from '../api/admin'
import { health } from '../api/health'
import { fmtDate, fmtNumber } from '../api/client'
import StatCard from '../components/common/StatCard.vue'
import StatusBadge from '../components/common/StatusBadge.vue'

const healthInfo = ref<Record<string, any>>({})
const configInfo = ref<Record<string, any>>({})
const inventoryInfo = ref<Record<string, any>>({})
const loading = ref(false)
const error = ref('')

const stuckRunning = computed(() => healthInfo.value?.reclaim_hint?.stuck_running ?? 0)
const legacy = computed(() => inventoryInfo.value?.legacy || {})
const spine = computed(() => inventoryInfo.value?.spine || {})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [h, c, inv] = await Promise.all([health(), config(), inventory()])
    healthInfo.value = h || {}
    configInfo.value = c || {}
    inventoryInfo.value = inv || {}
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
      <h1 class="page-title">健康</h1>
      <button class="btn small" :disabled="loading" @click="load">刷新</button>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <section class="block">
      <div class="block-head">
        <h2 class="block-title">Worker</h2>
        <StatusBadge :status="healthInfo.worker_status" />
      </div>
      <div class="stat-row">
        <StatCard label="待处理" :value="fmtNumber(healthInfo.pending)" />
        <StatCard label="卡住运行" :value="fmtNumber(stuckRunning)" />
        <StatCard label="心跳间隔" :value="`${fmtNumber(configInfo.heartbeat_interval)}s`" />
        <StatCard label="卡住阈值" :value="`${fmtNumber(configInfo.stuck_timeout_sec)}s`" />
      </div>
      <p class="meta">最近活动：{{ fmtDate(healthInfo.last_activity_at) }}</p>
    </section>

    <section class="block">
      <h2 class="block-title">库存断面</h2>
      <div class="stat-row">
        <StatCard label="商品库" :value="fmtNumber(legacy.products)" />
        <StatCard label="评论库" :value="fmtNumber(legacy.reviews)" />
        <StatCard label="按需任务" :value="fmtNumber(legacy.ondemand_jobs)" />
        <StatCard label="spine 任务" :value="fmtNumber(spine.spine_jobs)" />
        <StatCard label="结构化记录" :value="fmtNumber(spine.extracted_records)" />
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

.block {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.block-head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.block-title {
  font-size: 15px;
  font-weight: 600;
  opacity: 0.85;
}

.stat-row {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
}

.meta {
  font-size: 13px;
  opacity: 0.65;
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

@media (max-width: 1000px) {
  .stat-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 700px) {
  .stat-row {
    grid-template-columns: 1fr;
  }
}
</style>
