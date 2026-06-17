<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { listCoverage } from '../api/coverage'
import { asList, fmtNumber, proxyAvailable } from '../api/client'
import { latestDailyDelta, listJobs } from '../api/jobs'
import { listSites } from '../api/products'
import { proxyStatus } from '../api/settings'
import PageLoading from '../components/common/PageLoading.vue'
import { useJobTrigger } from '../composables/useJobTrigger'

const askInput = ref('')
const askResult = ref<Record<string, any> | null>(null)
const askRunning = ref(false)
const loading = ref(false)
const coverage = ref<Record<string, any>>({ summary: {}, sites: [] })
const jobs = ref<Record<string, any>[]>([])
const proxy = ref<Record<string, any>>({})
const delta = ref<Record<string, any> | null>(null)
const siteRows = ref<Record<string, any>[]>([])
const jobTrigger = useJobTrigger({ onDone: () => load() })

const starters = [
  { icon: '📊', title: '总览快照', desc: '当前工作区覆盖率和商品总数', key: 'overview' },
  { icon: '🆕', title: '今日变化', desc: '昨日新增和变化', key: 'new' },
  { icon: '⚠️', title: '失败任务', desc: '采集异常列表', key: 'failed' },
  { icon: '🔔', title: '代理池', desc: '10 代理调用统计', key: 'proxy' },
  { icon: '🚀', title: '触发抓取', desc: '触发当前选中站点', key: 'trigger' }
]

const sites = computed(() => asList(coverage.value, ['sites', 'items', 'coverage']))
const totalSku = computed(() => Number(coverage.value.summary?.total_current_sku ?? 0))
const coveragePct = computed(() => Number(coverage.value.summary?.overall_coverage_pct ?? 0))
const runningJobs = computed(() => jobs.value.filter((j) => j.status === 'running').length)
const successJobs = computed(() => jobs.value.filter((j) => ['success', 'completed'].includes(j.status)).length)
const proxyOk = computed(() => proxyAvailable(proxy.value))
const proxyTotal = computed(() => Number(proxy.value.total || 0))
const triggerSite = computed(() => String((askResult.value?.data as Record<string, any> | null)?.site || ''))

function askTypeLabel(type?: string) {
  return ({ overview: '总览快照', new: '今日变化', failed: '失败任务', proxy: '代理池', trigger: '触发抓取' } as Record<string, string>)[type || ''] || '查询结果'
}

async function load() {
  loading.value = true
  try {
    const [coverageData, jobsData, proxyData, deltaData, siteData] = await Promise.all([
      listCoverage().catch(() => ({ summary: {}, sites: [] })),
      listJobs({ limit: 30 }).catch(() => ({ jobs: [] })),
      proxyStatus().catch(() => ({})),
      latestDailyDelta().catch(() => null),
      listSites().catch(() => [])
    ])
    coverage.value = coverageData
    jobs.value = asList(jobsData, ['jobs', 'items'])
    proxy.value = proxyData || {}
    delta.value = deltaData
    siteRows.value = asList(siteData, ['sites', 'items'])
  } finally {
    loading.value = false
  }
}

async function runStarter(key: string) {
  askRunning.value = true
  try {
    await load()
    let data: unknown = coverage.value
    if (key === 'new') data = delta.value
    else if (key === 'failed') data = jobs.value.filter((job) => String(job.status) === 'failed').slice(0, 15)
    else if (key === 'proxy') data = proxy.value
    else if (key === 'trigger') {
      const site = siteRows.value[0]?.site || siteRows.value[0]?.name
      if (!site) data = { msg: '当前工作区还没有可触发的站点' }
      else {
        const state = await jobTrigger.trigger(site)
        data = { msg: `已触发 ${site}`, site, job_id: state?.jobId }
      }
    }
    askResult.value = { type: key, data }
  } finally {
    askRunning.value = false
  }
}

async function runAskInput() {
  const q = askInput.value.trim().toLowerCase()
  if (!q) return
  if (q.includes('代理')) return runStarter('proxy')
  if (q.includes('失败') || q.includes('异常') || q.includes('job')) return runStarter('failed')
  if (q.includes('新增') || q.includes('变化') || q.includes('delta') || q.includes('今日')) return runStarter('new')
  if (q.includes('触发') || q.includes('抓取') || q.includes('crawl')) return runStarter('trigger')
  return runStarter('overview')
}

onMounted(load)
</script>

<template>
  <section>
    <div class="lead">智能问答</div>
    <div class="sub">输入关键词或点击快捷入口查询实时数据</div>

    <div class="ask-shell">
      <div class="ask-main">
        <div class="ask-input">
          <textarea v-model="askInput" placeholder="例如：代理状态 / 失败任务 / 今日新增 / 触发抓取" @keydown.enter.prevent="runAskInput" />
          <div class="ask-input-row">
            <span>按回车查询，或点击下方快捷入口</span>
            <button class="btn-go" :disabled="askRunning" @click="runAskInput">{{ askRunning ? '查询中…' : '▶ 查询' }}</button>
          </div>
        </div>

        <div v-if="!askResult" class="starters">
          <div v-for="s in starters" :key="s.key" class="starter" @click="runStarter(s.key)">
            <div class="icon">{{ s.icon }}</div>
            <div class="title">{{ s.title }}</div>
            <div class="desc">{{ s.desc }}</div>
          </div>
        </div>

        <div v-if="askResult" class="answer-card">
          <div class="answer-head">
            <span>{{ askTypeLabel(askResult.type) }}</span>
            <button @click="askResult = null">清除</button>
          </div>
          <div v-if="askResult.type === 'trigger' && triggerSite" class="trigger-note answer-trigger" :class="jobTrigger.classFor(triggerSite)">
            {{ jobTrigger.labelFor(triggerSite, '触发抓取') }} · {{ jobTrigger.detailFor(triggerSite) }}
          </div>
          <pre>{{ JSON.stringify(askResult.data, null, 2) }}</pre>
        </div>
      </div>

      <div class="trace">
        <h4>实时数据</h4>
        <PageLoading v-if="loading && !sites.length && !jobs.length" compact title="同步实时数据..." />
        <div v-else class="trace-lines">
          <div>📦 商品: <b>{{ fmtNumber(totalSku) }}</b></div>
          <div>🌐 覆盖: <b>{{ coveragePct }}%</b></div>
          <div>⚙️ 跑中: <b>{{ runningJobs }}</b></div>
          <div>🔔 代理: <b>{{ proxyOk }}/{{ proxyTotal }}</b></div>
          <div>🏪 站点: <b>{{ sites.length }}</b></div>
          <div>✅ 完成: <b>{{ successJobs }}</b></div>
        </div>
      </div>
    </div>
  </section>
</template>
