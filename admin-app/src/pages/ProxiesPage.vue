<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Database, Plus, RefreshCw, RotateCw, ShieldCheck, ToggleLeft, ToggleRight, Unlock } from 'lucide-vue-next'
import {
  proxies,
  proxyAntiBotApplyRules,
  proxyAntiBotCheck,
  proxyAntiBotDiagnostics,
  proxyCheck,
  proxyClear,
  proxyEndpointCheck,
  proxyEndpointCreate,
  proxyEndpointUpdate,
  proxyImportFile,
  proxyPoolCreate,
  proxyPoolMemberUpsert,
  proxyPoolUpdate,
  proxyReload,
  proxyRuleCreate,
  proxyRuleUpdate
} from '../api/admin'
import { fmtDate, fmtNumber } from '../api/client'
import StatCard from '../components/common/StatCard.vue'

const info = ref<Record<string, any>>({})
const route = useRoute()
const loading = ref(false)
const busy = ref('')
const error = ref('')
const message = ref('')
const antiBot = ref<Record<string, any>>({})

const probeForm = ref({
  tier: 'residential',
  site: 'vidaxl_ca',
  url: 'https://www.vidaxl.ca/sitemap_index.xml',
  timeout: 8
})
const endpointForm = ref({
  proxy_url: '',
  endpoint_type: 'datacenter',
  name: '',
  provider: '',
  country: '',
  exclude_sites: '',
  notes: ''
})
const poolForm = ref({
  slug: '',
  name: '',
  pool_type: 'datacenter',
  fallback_pool_slug: '',
  description: ''
})
const memberForm = ref({
  pool_id: '',
  endpoint_id: '',
  priority: 100,
  weight: 1
})
const ruleForm = ref({
  site_pattern: '',
  match_type: 'exact',
  proxy_mode: 'pool',
  pool_slug: 'datacenter',
  fallback_pool_slug: '',
  priority: 50,
  notes: ''
})

const items = computed(() => info.value?.items || [])
const endpoints = computed(() => info.value?.endpoints || [])
const pools = computed(() => info.value?.pools || [])
const rules = computed(() => info.value?.rules || [])
const antiBotItems = computed(() => antiBot.value?.items || [])
const antiBotSummary = computed(() => antiBot.value?.summary || {})
const pool = computed(() => info.value?.pool || {})
const health = computed(() => info.value?.health || {})
const byStatus = computed(() => health.value?.by_status || {})
const available = computed(() => Object.values(pool.value?.by_tier || {}).reduce(
  (sum: number, row: any) => sum + Number(row?.available || 0),
  0
))
const problemCount = computed(() => Number(byStatus.value.degraded || 0) + Number(byStatus.value.down || 0) + Number(byStatus.value.blocked || 0))
const routeIssue = computed(() => String(route.query.issue || ''))
const routeSite = computed(() => String(route.query.site || ''))
const routeIssueLabel = computed(() => ({
  anti_bot_blocked: '反爬封禁',
  proxy_unavailable: '代理不可用',
  proxy_auth_failed: '代理鉴权失败'
} as Record<string, string>)[routeIssue.value] || routeIssue.value)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [proxyInfo, antiBotInfo] = await Promise.all([
      proxies(),
      proxyAntiBotDiagnostics()
    ])
    info.value = proxyInfo || {}
    antiBot.value = antiBotInfo || {}
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function runAction(label: string, fn: () => Promise<any>, okText: string) {
  busy.value = label
  error.value = ''
  message.value = ''
  try {
    const data = await fn()
    info.value = data || {}
    message.value = okText
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = ''
  }
}

function doReload() {
  return runAction('reload', proxyReload, '代理配置已热重载')
}

function doImport() {
  return runAction('import', proxyImportFile, '已从私有代理文件导入/同步')
}

function doCheck() {
  return runAction('check', () => proxyCheck(probeForm.value), '代理预检已完成')
}

async function checkAntiBotSites() {
  busy.value = 'anti-bot-check'
  error.value = ''
  message.value = ''
  try {
    antiBot.value = await proxyAntiBotCheck({
      limit: 10,
      timeout: probeForm.value.timeout || 8,
    })
    message.value = `反爬站点预检完成：通过 ${fmtNumber(antiBot.value.ok || 0)}，失败 ${fmtNumber(antiBot.value.failed || 0)}`
    info.value = await proxies()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = ''
  }
}

async function applyAntiBotRules() {
  busy.value = 'anti-bot-apply'
  error.value = ''
  message.value = ''
  try {
    antiBot.value = await proxyAntiBotApplyRules({})
    message.value = `已应用 ${fmtNumber(antiBot.value.applied_count || 0)} 条反爬推荐规则`
    info.value = await proxies()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = ''
  }
}

function applyRouteContext() {
  const site = routeSite.value.trim()
  const issue = routeIssue.value.trim()
  if (!site && !issue) return
  if (site) {
    probeForm.value.site = site
    ruleForm.value.site_pattern = site
    ruleForm.value.notes = `来自数据质量：${routeIssueLabel.value || issue}`
  }
  if (issue === 'anti_bot_blocked') {
    probeForm.value.tier = 'residential'
    ruleForm.value.proxy_mode = 'pool'
    ruleForm.value.pool_slug = 'residential'
    ruleForm.value.fallback_pool_slug = 'datacenter'
    ruleForm.value.priority = 90
  } else if (issue === 'proxy_unavailable') {
    probeForm.value.tier = 'residential'
    ruleForm.value.proxy_mode = 'pool'
    ruleForm.value.pool_slug = 'residential'
    ruleForm.value.priority = 80
  } else if (issue === 'proxy_auth_failed') {
    probeForm.value.tier = 'residential'
    ruleForm.value.proxy_mode = 'pool'
    ruleForm.value.pool_slug = 'residential'
    ruleForm.value.priority = 85
  }
}

function doClear(row: Record<string, any>) {
  return runAction(`clear:${row.hash}`, () => proxyClear(row.hash), '代理冷却已解除')
}

function createEndpoint() {
  const payload = { ...endpointForm.value }
  return runAction('endpoint:create', () => proxyEndpointCreate(payload), '代理端点已保存').then(() => {
    endpointForm.value.proxy_url = ''
    endpointForm.value.name = ''
    endpointForm.value.provider = ''
    endpointForm.value.country = ''
    endpointForm.value.exclude_sites = ''
    endpointForm.value.notes = ''
  })
}

function toggleEndpoint(row: Record<string, any>) {
  return runAction(`endpoint:${row.id}`, () => proxyEndpointUpdate(row.id, { active: !row.active }), row.active ? '代理端点已停用' : '代理端点已启用')
}

function checkEndpoint(row: Record<string, any>) {
  return runAction(`endpoint-check:${row.id}`, () => proxyEndpointCheck(row.id, {
    url: probeForm.value.url,
    timeout: probeForm.value.timeout || 8
  }), '代理端点检测已完成')
}

function createPool() {
  const payload = { ...poolForm.value }
  return runAction('pool:create', () => proxyPoolCreate(payload), '代理池已保存').then(() => {
    poolForm.value.slug = ''
    poolForm.value.name = ''
    poolForm.value.fallback_pool_slug = ''
    poolForm.value.description = ''
  })
}

function togglePool(row: Record<string, any>) {
  return runAction(`pool:${row.id}`, () => proxyPoolUpdate(row.id, { active: !row.active }), row.active ? '代理池已停用' : '代理池已启用')
}

function addMember() {
  const poolId = Number(memberForm.value.pool_id)
  const endpointId = Number(memberForm.value.endpoint_id)
  return runAction('member:add', () => proxyPoolMemberUpsert(poolId, {
    endpoint_id: endpointId,
    priority: memberForm.value.priority,
    weight: memberForm.value.weight,
    active: true
  }), '代理已加入池')
}

function createRule() {
  const payload = { ...ruleForm.value }
  return runAction('rule:create', () => proxyRuleCreate(payload), '站点代理规则已保存').then(() => {
    ruleForm.value.site_pattern = ''
    ruleForm.value.fallback_pool_slug = ''
    ruleForm.value.notes = ''
  })
}

function toggleRule(row: Record<string, any>) {
  return runAction(`rule:${row.id}`, () => proxyRuleUpdate(row.id, { enabled: !row.enabled }), row.enabled ? '规则已停用' : '规则已启用')
}

function statusLabel(status?: string) {
  return ({ healthy: '健康', degraded: '降级', down: '不可用', blocked: '认证阻断', unknown: '未知', pool_blocked: '池内冷却' } as Record<string, string>)[status || ''] || status || '-'
}

function statusClass(row: Record<string, any>) {
  const status = String(row.status || '')
  if (status === 'healthy') return 'ok'
  if (status === 'degraded' || status === 'unknown') return 'warn'
  return 'bad'
}

function ruleStatusLabel(status?: string) {
  return ({
    primary_available: '主池可用',
    fallback_available: '备用池生效',
    unavailable: '无可用代理',
    misconfigured: '配置不完整',
    direct: '直连',
    disabled: '已停用'
  } as Record<string, string>)[status || ''] || status || '-'
}

function ruleStatusClass(status?: string) {
  if (status === 'primary_available' || status === 'direct') return 'ok'
  if (status === 'fallback_available' || status === 'disabled') return 'warn'
  return 'bad'
}

function rulePoolSummary(row: Record<string, any>) {
  if (row.effective_status === 'direct') return '不使用代理'
  const primary = row.primary_pool_slug
    ? `主池 ${row.primary_pool_slug}: ${fmtNumber(row.primary_available_count)}/${fmtNumber(row.primary_member_count)} 可用`
    : '主池未配置'
  const fallback = row.fallback_pool_slug
    ? `备用 ${row.fallback_pool_slug}: ${fmtNumber(row.fallback_available_count)}/${fmtNumber(row.fallback_member_count)} 可用`
    : '无备用池'
  return `${primary} · ${fallback}`
}

function poolPoolSummary(row: Record<string, any>) {
  const primary = `主池 ${row.slug}: ${fmtNumber(row.primary_available_count ?? row.available_count)}/${fmtNumber(row.primary_member_count ?? row.member_count)} 可用`
  const fallback = row.fallback_pool_slug
    ? `备用 ${row.fallback_pool_slug}: ${fmtNumber(row.fallback_available_count)}/${fmtNumber(row.fallback_member_count)} 可用`
    : '无备用池'
  return `${primary} · ${fallback} · 实际 ${fmtNumber(row.effective_available_count ?? row.available_count)}`
}

function shortHash(hash?: string) {
  return hash ? hash.slice(0, 12) : '-'
}

onMounted(() => {
  applyRouteContext()
  load()
})

watch(() => route.fullPath, applyRouteContext)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1 class="page-title">代理管理</h1>
        <p class="page-subtitle">后台统一维护普通 IP、住宅 IP、代理池和站点策略。</p>
      </div>
      <div class="head-actions">
        <button class="btn small" :disabled="loading || !!busy" @click="load">
          <RefreshCw class="size-4" />
          <span>刷新</span>
        </button>
        <button class="btn small" :disabled="!!busy" @click="doImport">
          <Database class="size-4" />
          <span>{{ busy === 'import' ? '导入中' : '导入文件' }}</span>
        </button>
        <button class="btn small primary" :disabled="!!busy" @click="doReload">
          <RotateCw class="size-4" />
          <span>{{ busy === 'reload' ? '重载中' : '热重载' }}</span>
        </button>
      </div>
    </div>

    <div v-if="error" class="error">{{ error }}</div>
    <div v-if="message" class="message">{{ message }}</div>
    <div v-if="routeIssue || routeSite" class="context-panel">
      <div>
        <b>来自数据质量</b>
        <span>{{ routeSite || '站点未指定' }} · {{ routeIssueLabel || '代理前置条件' }}</span>
      </div>
      <span>已预填代理预检和站点规则，优先验证住宅池可用性。</span>
    </div>

    <div class="stat-row">
      <StatCard label="池内代理" :value="fmtNumber(pool.total)" />
      <StatCard label="当前可用" :value="fmtNumber(available)" />
      <StatCard label="配置端点" :value="fmtNumber(endpoints.length)" />
      <StatCard label="异常代理" :value="fmtNumber(problemCount)" />
    </div>

    <section class="block">
      <div class="block-head">
        <h2 class="block-title">反爬站点诊断</h2>
        <span class="meta">
          {{ fmtNumber(antiBot.count || 0) }} 个站点 ·
          需规则 {{ fmtNumber(antiBotSummary.needs_rule || 0) }} ·
          可用规则 {{ fmtNumber(antiBotSummary.with_available_rule || 0) }}
        </span>
      </div>
      <div class="anti-actions">
        <button class="btn small" :disabled="!!busy || !antiBotItems.length" @click="applyAntiBotRules">
          <RotateCw class="size-4" />
          <span>{{ busy === 'anti-bot-apply' ? '应用中' : `应用推荐规则(${antiBotSummary.needs_rule || antiBotItems.length})` }}</span>
        </button>
        <button class="btn small primary" :disabled="!!busy || !antiBotItems.length" @click="checkAntiBotSites">
          <ShieldCheck class="size-4" />
          <span>{{ busy === 'anti-bot-check' ? '批量预检中' : `批量预检(${antiBotItems.length})` }}</span>
        </button>
      </div>
      <div class="mini-table anti-table">
        <div v-for="row in antiBotItems" :key="row.site" class="mini-row">
          <div class="rule-info">
            <div class="rule-title">
              <b>{{ row.site }}</b>
              <span v-for="issue in row.issues" :key="issue" class="badge bad">{{ issue }}</span>
              <span v-if="row.rule_status" class="badge" :class="ruleStatusClass(row.rule_status)">{{ ruleStatusLabel(row.rule_status) }}</span>
              <span v-else class="badge warn">建议住宅池</span>
            </div>
            <span>{{ row.last_error_code || '-' }} · {{ row.last_error || row.suggested_action || '-' }}</span>
            <span>
              推荐 {{ row.recommended_rule?.pool_slug || 'residential' }}
              <template v-if="row.current_rule">· 当前 {{ row.current_rule.proxy_mode }}{{ row.current_rule.pool_slug ? `:${row.current_rule.pool_slug}` : '' }}</template>
            </span>
            <span v-if="row.probe" :class="['probe-inline', row.probe.ok ? 'ok' : 'bad']">
              预检 {{ row.probe.ok ? '通过' : '失败' }} · {{ row.probe.status_code || row.probe.failure_code || '-' }}
            </span>
          </div>
          <button class="btn small" :disabled="!!busy" @click="probeForm.site = row.site; probeForm.url = row.url || probeForm.url; probeForm.tier = row.current_rule?.pool_slug ? `pool:${row.current_rule.pool_slug}` : 'residential'; doCheck()">
            单站预检
          </button>
        </div>
        <div v-if="!antiBotItems.length" class="empty">暂无反爬/代理阻断站点</div>
      </div>
    </section>

    <section class="block">
      <div class="block-head">
        <h2 class="block-title">代理预检</h2>
        <span class="meta">更新于 {{ fmtDate(info.updated_at) }}</span>
      </div>
      <div class="form-grid probe-grid">
        <select v-model="probeForm.tier" class="ctl">
          <option value="residential">住宅 IP</option>
          <option value="datacenter">普通 IP</option>
          <option v-for="p in pools" :key="p.id" :value="`pool:${p.slug}`">池: {{ p.slug }}</option>
        </select>
        <input v-model="probeForm.site" class="ctl" placeholder="site" />
        <input v-model="probeForm.url" class="ctl span-2" placeholder="URL" />
        <input v-model.number="probeForm.timeout" type="number" min="3" max="30" class="ctl" />
        <button class="btn small primary" :disabled="!!busy" @click="doCheck">
          <ShieldCheck class="size-4" />
          <span>{{ busy === 'check' ? '检测中' : '预检' }}</span>
        </button>
      </div>
      <div v-if="info.probe" class="probe-result" :class="info.probe.ok ? 'ok' : 'bad'">
        <b>{{ info.probe.ok ? '通过' : '失败' }}</b>
        <span>{{ info.probe.status_code || info.probe.failure_code || '-' }}</span>
        <span>{{ info.probe.failure_detail || info.probe.url }}</span>
      </div>
    </section>

    <section class="block">
      <h2 class="block-title">新增代理端点</h2>
      <div class="form-grid endpoint-grid">
        <input v-model="endpointForm.proxy_url" class="ctl span-2" placeholder="http://user:pass@host:port" />
        <select v-model="endpointForm.endpoint_type" class="ctl">
          <option value="datacenter">普通 IP</option>
          <option value="residential">住宅 IP</option>
        </select>
        <input v-model="endpointForm.name" class="ctl" placeholder="名称" />
        <input v-model="endpointForm.provider" class="ctl" placeholder="供应商" />
        <input v-model="endpointForm.country" class="ctl" placeholder="国家/地区" />
        <input v-model="endpointForm.exclude_sites" class="ctl" placeholder="排除站点, 逗号分隔" />
        <input v-model="endpointForm.notes" class="ctl" placeholder="备注" />
        <button class="btn small primary" :disabled="!!busy || !endpointForm.proxy_url" @click="createEndpoint">
          <Plus class="size-4" />
          <span>保存端点</span>
        </button>
      </div>
    </section>

    <section class="split">
      <div class="block">
        <h2 class="block-title">代理池</h2>
        <div class="form-grid pool-grid">
          <input v-model="poolForm.slug" class="ctl" placeholder="pool slug" />
          <input v-model="poolForm.name" class="ctl" placeholder="池名称" />
          <select v-model="poolForm.pool_type" class="ctl">
            <option value="datacenter">普通 IP</option>
            <option value="residential">住宅 IP</option>
            <option value="mixed">混合池</option>
          </select>
          <input v-model="poolForm.fallback_pool_slug" class="ctl" placeholder="fallback slug" />
          <input v-model="poolForm.description" class="ctl span-2" placeholder="说明" />
          <button class="btn small primary" :disabled="!!busy || !poolForm.slug" @click="createPool">保存池</button>
        </div>
        <div class="mini-table">
          <div v-for="p in pools" :key="p.id" class="mini-row">
            <div class="rule-info">
              <div class="rule-title">
                <b>{{ p.name || p.slug }}</b>
                <span class="badge" :class="ruleStatusClass(p.effective_status)">{{ ruleStatusLabel(p.effective_status) }}</span>
              </div>
              <span>{{ p.slug }} · {{ p.pool_type }}</span>
              <span>{{ poolPoolSummary(p) }}</span>
            </div>
            <button class="icon-btn" :disabled="!!busy" @click="togglePool(p)">
              <ToggleRight v-if="p.active" class="size-4" />
              <ToggleLeft v-else class="size-4" />
            </button>
          </div>
        </div>
      </div>

      <div class="block">
        <h2 class="block-title">池成员</h2>
        <div class="form-grid pool-grid">
          <select v-model="memberForm.pool_id" class="ctl">
            <option value="">选择代理池</option>
            <option v-for="p in pools" :key="p.id" :value="p.id">{{ p.slug }}</option>
          </select>
          <select v-model="memberForm.endpoint_id" class="ctl">
            <option value="">选择代理</option>
            <option v-for="e in endpoints" :key="e.id" :value="e.id">{{ e.name || e.host }} · {{ e.endpoint_type }}</option>
          </select>
          <input v-model.number="memberForm.priority" type="number" class="ctl" placeholder="优先级" />
          <input v-model.number="memberForm.weight" type="number" min="1" class="ctl" placeholder="权重" />
          <button class="btn small primary" :disabled="!!busy || !memberForm.pool_id || !memberForm.endpoint_id" @click="addMember">加入池</button>
        </div>
      </div>
    </section>

    <section class="block">
      <h2 class="block-title">站点代理规则</h2>
      <div class="form-grid rule-grid">
        <input v-model="ruleForm.site_pattern" class="ctl" placeholder="site, 如 vidaxl_ca" />
        <select v-model="ruleForm.match_type" class="ctl">
          <option value="exact">精确</option>
          <option value="contains">包含</option>
          <option value="prefix">前缀</option>
        </select>
        <select v-model="ruleForm.proxy_mode" class="ctl">
          <option value="pool">指定池</option>
          <option value="datacenter">普通 IP</option>
          <option value="residential">住宅 IP</option>
          <option value="none">不使用代理</option>
        </select>
        <select v-model="ruleForm.pool_slug" class="ctl">
          <option value="">选择池</option>
          <option v-for="p in pools" :key="p.id" :value="p.slug">{{ p.slug }}</option>
        </select>
        <select v-model="ruleForm.fallback_pool_slug" class="ctl">
          <option value="">无备用池</option>
          <option v-for="p in pools" :key="p.id" :value="p.slug">备用: {{ p.slug }}</option>
        </select>
        <input v-model.number="ruleForm.priority" type="number" class="ctl" placeholder="优先级" />
        <input v-model="ruleForm.notes" class="ctl" placeholder="备注" />
        <button class="btn small primary" :disabled="!!busy || !ruleForm.site_pattern" @click="createRule">保存规则</button>
      </div>
      <div class="mini-table">
        <div v-for="r in rules" :key="r.id" class="mini-row">
          <div class="rule-info">
            <div class="rule-title">
              <b>{{ r.site_pattern }}</b>
              <span class="badge" :class="ruleStatusClass(r.effective_status)">{{ ruleStatusLabel(r.effective_status) }}</span>
            </div>
            <span>{{ r.match_type }} · {{ r.proxy_mode }}{{ r.pool_slug ? `:${r.pool_slug}` : '' }} · P{{ r.priority }}</span>
            <span>{{ rulePoolSummary(r) }}</span>
            <span v-if="r.notes">{{ r.notes }}</span>
          </div>
          <button class="icon-btn" :disabled="!!busy" @click="toggleRule(r)">
            <ToggleRight v-if="r.enabled" class="size-4" />
            <ToggleLeft v-else class="size-4" />
          </button>
        </div>
      </div>
    </section>

    <section class="block">
      <div class="block-head">
        <h2 class="block-title">配置端点</h2>
        <span class="meta">明文凭据不会在页面回显</span>
      </div>
      <div class="table-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th>代理</th>
              <th>类型</th>
              <th>供应商</th>
              <th>池</th>
              <th>排除站点</th>
              <th>来源</th>
              <th>状态</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in endpoints" :key="row.id">
              <td>
                <div class="proxy-cell">
                  <b>{{ row.proxy || row.host || '-' }}</b>
                  <span>{{ shortHash(row.hash) }}</span>
                </div>
              </td>
              <td>{{ row.endpoint_type }}</td>
              <td>{{ row.provider || row.country || '-' }}</td>
              <td>{{ (row.pools || []).join(', ') || '-' }}</td>
              <td>{{ (row.exclude || []).join(', ') || '-' }}</td>
              <td>{{ row.source || '-' }}</td>
              <td>
                <div class="state-stack">
                  <span class="badge" :class="row.active ? 'ok' : 'warn'">{{ row.active ? '启用' : '停用' }}</span>
                  <span class="badge" :class="statusClass(row.health || {})">{{ statusLabel(row.health_status || row.health?.status) }}</span>
                </div>
              </td>
              <td>
                <button class="icon-btn" :disabled="!!busy" @click="toggleEndpoint(row)">
                  <ToggleRight v-if="row.active" class="size-4" />
                  <ToggleLeft v-else class="size-4" />
                </button>
                <button class="btn small" :disabled="!!busy" @click="checkEndpoint(row)">
                  {{ busy === `endpoint-check:${row.id}` ? '检测中' : '检测' }}
                </button>
              </td>
            </tr>
            <tr v-if="!endpoints.length">
              <td colspan="8" class="empty">{{ loading ? '加载中...' : '暂无代理端点' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="block">
      <h2 class="block-title">健康状态</h2>
      <div class="table-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th>代理</th>
              <th>层级</th>
              <th>状态</th>
              <th>池状态</th>
              <th>成功 / 失败</th>
              <th>连续失败</th>
              <th>最近失败</th>
              <th>检测时间</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in items" :key="row.hash">
              <td>
                <div class="proxy-cell">
                  <b>{{ row.proxy || '-' }}</b>
                  <span>{{ shortHash(row.hash) }}</span>
                </div>
              </td>
              <td>{{ row.tier || '-' }}</td>
              <td><span class="badge" :class="statusClass(row)">{{ statusLabel(row.status) }}</span></td>
              <td>
                <span class="badge" :class="row.pool_available ? 'ok' : 'warn'">
                  {{ row.pool_available ? '可用' : row.pool_blocked_for_sec ? `冷却 ${row.pool_blocked_for_sec}s` : '未配置' }}
                </span>
              </td>
              <td>{{ fmtNumber(row.success_count) }} / {{ fmtNumber(row.failure_count) }}</td>
              <td>{{ fmtNumber(row.consecutive_failures) }}</td>
              <td class="err-cell" :title="row.last_failure_detail || ''">{{ row.last_failure_code || '-' }}</td>
              <td>{{ fmtDate(row.last_checked_at || row.updated_at) }}</td>
              <td>
                <button class="btn small" :disabled="!!busy || !row.hash" @click="doClear(row)">
                  <Unlock class="size-4" />
                  <span>{{ busy === `clear:${row.hash}` ? '处理中' : '解除冷却' }}</span>
                </button>
              </td>
            </tr>
            <tr v-if="!items.length">
              <td colspan="9" class="empty">{{ loading ? '加载中...' : '暂无代理状态' }}</td>
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
  gap: 16px;
}

.page-head,
.block-head,
.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.page-head,
.block-head {
  justify-content: space-between;
}

.page-title {
  font-size: 20px;
  font-weight: 600;
}

.page-subtitle {
  margin-top: 4px;
  font-size: 12px;
  opacity: 0.6;
}

.stat-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.context-panel {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 12px;
  border: 1px solid rgba(139, 92, 246, 0.28);
  border-radius: 8px;
  background: rgba(139, 92, 246, 0.10);
  color: var(--ui-muted, #9ca3af);
  font-size: 12px;
}

.context-panel b {
  display: block;
  color: var(--ui-text, #e5e7eb);
  font-size: 13px;
}

.context-panel span {
  line-height: 1.45;
}

.block {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.split {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
}

.block-title {
  font-size: 15px;
  font-weight: 600;
  opacity: 0.9;
}

.form-grid {
  display: grid;
  gap: 10px;
  align-items: center;
}

.probe-grid {
  grid-template-columns: 180px 180px minmax(280px, 1fr) 90px auto;
}

.endpoint-grid,
.rule-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr)) auto;
}

.pool-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr)) auto;
}

.span-2 {
  grid-column: span 2;
}

.ctl {
  min-width: 0;
  height: 34px;
  padding: 7px 10px;
  border-radius: 7px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
  background: var(--ui-bg, rgba(0, 0, 0, 0.15));
  color: inherit;
  font-size: 13px;
  outline: none;
}

.ctl:focus {
  border-color: var(--ui-color-primary-500, #8b5cf6);
  box-shadow: 0 0 0 2px rgba(139, 92, 246, 0.16);
}

.btn,
.icon-btn {
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  white-space: nowrap;
}

.btn.primary {
  border: none;
  color: #fff;
  background: var(--ui-color-primary-500, #6366f1);
}

.btn.small {
  min-height: 32px;
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 12px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
  background-color: transparent;
  color: inherit;
}

.btn.small.primary {
  border-color: transparent;
  background: var(--ui-color-primary-500, #6366f1);
  color: #fff;
}

.icon-btn {
  width: 32px;
  height: 32px;
  border-radius: 6px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
  background: transparent;
  color: inherit;
}

.btn:disabled,
.icon-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.message,
.error {
  font-size: 13px;
}

.message {
  color: #10b981;
}

.error,
.err-cell {
  color: #ef4444;
}

.meta {
  font-size: 12px;
  opacity: 0.6;
}

.anti-actions {
  display: flex;
  justify-content: flex-end;
}

.probe-result {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 13px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
}

.probe-result.ok {
  color: #10b981;
}

.probe-result.bad {
  color: #ef4444;
}

.probe-inline {
  opacity: 1 !important;
  font-weight: 700;
}

.probe-inline.ok {
  color: #10b981;
}

.probe-inline.bad {
  color: #ef4444;
}

.mini-table,
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.08));
  border-radius: 8px;
}

.mini-row {
  min-height: 48px;
  padding: 8px 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid var(--ui-border, rgba(255, 255, 255, 0.06));
}

.mini-row:last-child {
  border-bottom: 0;
}

.mini-row > div {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.mini-row b {
  overflow: hidden;
  text-overflow: ellipsis;
}

.mini-row span {
  font-size: 12px;
  opacity: 0.58;
}

.rule-info {
  flex: 1;
}

.rule-title {
  display: flex;
  align-items: center;
  flex-direction: row !important;
  gap: 8px !important;
}

.rule-title .badge {
  flex: 0 0 auto;
  opacity: 1;
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

.proxy-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-width: 360px;
}

.proxy-cell b {
  overflow: hidden;
  text-overflow: ellipsis;
}

.proxy-cell span {
  font-size: 11px;
  opacity: 0.55;
}

.state-stack {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.badge {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 3px 8px;
  font-size: 12px;
  border: 1px solid transparent;
}

.badge.ok {
  color: #10b981;
  border-color: rgba(16, 185, 129, 0.3);
  background: rgba(16, 185, 129, 0.1);
}

.badge.warn {
  color: #f59e0b;
  border-color: rgba(245, 158, 11, 0.3);
  background: rgba(245, 158, 11, 0.1);
}

.badge.bad {
  color: #ef4444;
  border-color: rgba(239, 68, 68, 0.3);
  background: rgba(239, 68, 68, 0.1);
}

.empty {
  text-align: center;
  opacity: 0.6;
  padding: 24px;
}

@media (max-width: 1180px) {
  .split,
  .stat-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .probe-grid,
  .endpoint-grid,
  .rule-grid,
  .pool-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .page-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .head-actions {
    flex-wrap: wrap;
  }

  .split,
  .stat-row,
  .probe-grid,
  .endpoint-grid,
  .rule-grid,
  .pool-grid {
    grid-template-columns: 1fr;
  }

  .span-2 {
    grid-column: span 1;
  }
}
</style>
