<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { asList, fmtDate } from '../../api/client'
import { crawlDiagnostics, listJobs } from '../../api/jobs'
import DataLoadingPanel from '../common/DataLoadingPanel.vue'
import PageLoading from '../common/PageLoading.vue'
import StatusBadge from '../common/StatusBadge.vue'

defineProps<{
  embedded?: boolean
}>()

const jobs = ref<Record<string, any>[]>([])
const diagnostics = ref<Record<string, any> | null>(null)
const error = ref('')
const loading = ref(false)
const pageSize = ref(80)
const runningCount = computed(() => jobs.value.filter((j) => j.status === 'running').length)
const successCount = computed(() => jobs.value.filter((j) => ['success', 'completed'].includes(j.status)).length)

async function load() {
  loading.value = true
  error.value = ''
  try {
    jobs.value = asList(await listJobs({ limit: pageSize.value }), ['jobs', 'items'])
    diagnostics.value = await crawlDiagnostics({ limit: 8 }).catch(() => null)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function fmtJobTime(value?: string | null) {
  const out = fmtDate(value)
  return out === '-' ? '—' : out
}

function failureText(job: Record<string, any>) {
  return job.failure_code || (job.error ? 'unknown' : '—')
}

function failureTitle(job: Record<string, any>) {
  const bits = [job.failure_code, job.failure_stage].filter(Boolean)
  return bits.length ? bits.join(' · ') : (job.error || '—')
}

onMounted(load)
</script>

<template>
  <section :class="{ 'jobs-panel-embedded': embedded }">
    <div v-if="!embedded" class="lead">采集任务 · 进程状态</div>
    <div class="sub">{{ runningCount }} 跑中 · {{ successCount }} 成功 · 共 {{ jobs.length }} 显示</div>
    <UAlert v-if="error" color="error" variant="soft" :title="error" class="mb-4" />
    <div class="jobs-toolbar">
      <button class="btn-go" :disabled="loading" @click="load">{{ loading ? '刷新中...' : '刷新' }}</button>
      <select v-model="pageSize" @change="load">
        <option :value="40">最近 40 条</option>
        <option :value="80">最近 80 条</option>
        <option :value="120">最近 120 条</option>
      </select>
    </div>
    <div v-if="diagnostics?.failure_counts && Object.keys(diagnostics.failure_counts).length" class="diag-strip">
      <span class="diag-title">失败分布</span>
      <span v-for="[code, count] in Object.entries(diagnostics.failure_counts).slice(0, 6)" :key="code" class="diag-pill">
        {{ code }} · {{ count }}
      </span>
    </div>
    <DataLoadingPanel class="jobs-list" :loading="loading" :has-data="jobs.length > 0" label="正在更新任务列表">
      <div class="job-row head"><div>#</div><div>站点</div><div>状态</div><div>商品</div><div>耗时</div><div>失败码</div><div>建议动作</div><div>完成</div></div>
      <PageLoading v-if="loading && !jobs.length" compact title="加载采集任务..." note="正在读取最近任务队列" />
      <template v-else>
        <div v-for="job in jobs" :key="job.id" class="job-row">
          <div>{{ job.id }}</div>
          <div>{{ job.site || job.brand }}</div>
          <div><StatusBadge :status="job.status" /></div>
          <div>{{ job.products_count || job.product_count || 0 }}</div>
          <div>{{ job.duration_sec ? Math.round(job.duration_sec) + ' 秒' : '—' }}</div>
          <div :title="failureTitle(job)" class="failure-code">{{ failureText(job) }}</div>
          <div :title="job.failure_detail || job.error || ''" class="job-action">{{ job.suggested_action || '—' }}</div>
          <div>{{ fmtJobTime(job.finished_at) }}</div>
        </div>
      </template>
      <div v-if="!loading && !jobs.length" class="empty-state">
        <b>暂无采集任务</b>
        可以从覆盖率页面触发一个站点抓取。
      </div>
    </DataLoadingPanel>
  </section>
</template>

<style scoped>
.jobs-panel-embedded {
  display: grid;
  gap: 10px;
}
.jobs-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.jobs-toolbar select {
  height: 34px;
  border: 1px solid var(--ui-border);
  border-radius: 7px;
  background: var(--ui-card-soft);
  color: var(--ui-heading);
  padding: 0 10px;
}
.job-row {
  grid-template-columns: 70px minmax(120px, 1fr) 110px 80px 85px minmax(120px, .9fr) minmax(260px, 1.5fr) minmax(150px, 1fr);
}
.diag-strip {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 0 0 10px;
}
.diag-title {
  color: var(--ui-muted);
  font-size: 12px;
}
.diag-pill {
  border: 1px solid var(--ui-border);
  border-radius: 999px;
  padding: 4px 8px;
  background: var(--ui-card-soft);
  color: var(--ui-heading);
  font-size: 12px;
}
.failure-code,
.job-action {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
