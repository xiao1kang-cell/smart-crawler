<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { asList, proxyAvailable } from '../api/client'
import { listSites } from '../api/products'
import {
  addWorkspaceSite,
  createInvite,
  createUser,
  createWorkspace,
  listInvites,
  listUsers,
  listWorkspaceSites,
  proxyStatus,
  resetUserPassword,
  updateInvite,
  updateUser,
  updateWorkspaceSite
} from '../api/settings'
import { useAuthStore } from '../stores/auth'
import { useWorkspaceStore } from '../stores/workspace'
import PageLoading from '../components/common/PageLoading.vue'
import JobsPanel from '../components/settings/JobsPanel.vue'

const auth = useAuthStore()
const workspace = useWorkspaceStore()

const section = ref('jobs')
const currentWorkspaceId = ref('')
const workspaceForm = ref({ name: '', slug: '' })
const siteForm = ref({ site: '' })
const userForm = ref({ username: '', email: '', display_name: '', role: 'user', password: '' })
const inviteForm = ref({ target_type: 'new_workspace', workspace_id: '', max_uses: 1, expires_in_days: 7, default_role: 'user' })
const users = ref<Record<string, any>[]>([])
const invites = ref<Record<string, any>[]>([])
const workspaceSites = ref<Record<string, any>[]>([])
const sites = ref<Record<string, any>[]>([])
const proxy = ref<Record<string, any> | null>(null)
const busy = ref('')
const error = ref('')
const message = ref('')
const showCreateUser = ref(false)
const createdUserPassword = ref('')
const newInviteCode = ref('')

const menu = [
  { key: 'users', label: '用户管理', desc: '内部账号与角色' },
  { key: 'jobs', label: '采集任务', desc: '队列与进程状态' },
  { key: 'workspace', label: '工作区', desc: '当前租户与切换' },
  { key: 'workspace_sites', label: '工作区站点', desc: '可见站点清单' },
  { key: 'invites', label: '邀请注册', desc: '新租户或成员邀请' },
  { key: 'proxy', label: '代理池', desc: '本地代理健康' },
  { key: 'docs', label: '文档工具', desc: '报告与接口文档' }
]
const workspaceItems = computed(() => workspace.workspaces.map((w) => ({
  label: formatWorkspaceName(w),
  value: String(w.id),
})))
const roleItems = [
  { label: '普通用户', value: 'user' },
  { label: '只读用户', value: 'viewer' },
  { label: '管理员', value: 'admin' },
]
const inviteTargetItems = [
  { label: '创建注册用户自己的新租户', value: 'new_workspace' },
  { label: '加入已有租户 / 工作区', value: 'workspace' },
]
const inviteRoleItems = [
  { label: '普通用户 · 可使用功能', value: 'user' },
  { label: '只读用户 · 只读查看', value: 'viewer' },
]

const canAdmin = computed(() => auth.user?.role === 'admin' || auth.user?.global_role === 'super_admin')
const canManageWorkspaces = computed(() => auth.user?.global_role === 'super_admin' || (auth.user?.username === 'admin' && auth.user?.role === 'admin') || ['admin', 'owner'].includes(auth.user?.workspace_role || ''))
const canManageInvites = computed(() => auth.user?.global_role === 'super_admin')
const showWorkspaceMenu = computed(() => canManageWorkspaces.value || workspace.workspaces.length > 1)
const showWorkspaceSettingsFeature = false
const showWorkspaceSitesFeature = false
const visibleMenu = computed(() => menu.filter((item) => {
  if (item.key === 'workspace') return showWorkspaceSettingsFeature && showWorkspaceMenu.value
  if (item.key === 'workspace_sites') return showWorkspaceSitesFeature && canManageWorkspaces.value
  if (item.key === 'users') return canAdmin.value
  if (item.key === 'invites') return canManageInvites.value
  return true
}))
const menuSummary = computed(() => visibleMenu.value.map((item) => item.label).join(' · ') || '设置')
const proxyTotal = computed(() => Number(proxy.value?.total || proxy.value?.proxies?.length || proxy.value?.details?.length || 0))
const proxyOk = computed(() => proxyAvailable(proxy.value))
const proxyHealth = computed(() => proxy.value?.health || {})
const proxyStatusCounts = computed(() => proxyHealth.value?.by_status || {})
const proxyDetails = computed(() => asList(proxyHealth.value, ['details']).slice(0, 6))
const proxyProblemCount = computed(() => {
  const counts = proxyStatusCounts.value
  return Number(counts.down || 0) + Number(counts.blocked || 0) + Number(counts.degraded || 0)
})
const initialLoading = computed(() => busy.value === 'load' && !workspace.workspaces.length && !sites.value.length && !users.value.length && !invites.value.length)

watch(visibleMenu, (items) => {
  if (!items.some((item) => item.key === section.value)) {
    section.value = items[0]?.key || 'jobs'
  }
}, { immediate: true })

function formatWorkspaceName(row?: Record<string, any> | null) {
  if (!row) return '—'
  return row.name || row.slug || `工作区 #${row.id}`
}

function formatRole(role?: string) {
  return ({ admin: '管理员', user: '普通用户', viewer: '只读用户', operator: '操作员', member: '成员' } as Record<string, string>)[role || ''] || role || '—'
}

function formatAccountStatus(status?: string) {
  return ({ active: '正常', disabled: '禁用', locked: '锁定' } as Record<string, string>)[status || ''] || status || '正常'
}

function formatProxyStatus(status?: string) {
  return ({ healthy: '健康', degraded: '降级', down: '不可用', blocked: '认证阻断', unknown: '未知' } as Record<string, string>)[status || ''] || status || '未知'
}

function proxyTone(status?: string) {
  return ['healthy'].includes(status || '') ? 'ok' : ['degraded', 'unknown'].includes(status || '') ? 'warn' : 'bad'
}

function openCreateUser() {
  userForm.value = { username: '', email: '', display_name: '', role: 'user', password: '' }
  createdUserPassword.value = ''
  showCreateUser.value = true
}

async function guarded(label: string, fn: () => Promise<void>) {
  busy.value = label
  error.value = ''
  message.value = ''
  try {
    await fn()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = ''
  }
}

async function load() {
  await guarded('load', async () => {
    await Promise.all([auth.loadMe().catch(() => null), workspace.load()])
    currentWorkspaceId.value = String(auth.workspaceId || workspace.currentWorkspace?.id || '')
    if (!inviteForm.value.workspace_id && currentWorkspaceId.value) inviteForm.value.workspace_id = currentWorkspaceId.value
    const workspaceId = currentWorkspaceId.value
    const [usersData, invitesData, siteData, workspaceSiteData, proxyData] = await Promise.all([
      listUsers().catch(() => ({ users: [] })),
      canManageInvites.value ? listInvites().catch(() => ({ invites: [] })) : Promise.resolve({ invites: [] }),
      listSites().catch(() => ({ sites: [] })),
      workspaceId ? listWorkspaceSites(workspaceId).catch(() => ({ sites: [] })) : Promise.resolve({ sites: [] }),
      proxyStatus().catch(() => null)
    ])
    users.value = asList(usersData, ['users', 'items'])
    invites.value = asList(invitesData, ['invites', 'items'])
    sites.value = asList(siteData, ['sites', 'items'])
    workspaceSites.value = asList(workspaceSiteData, ['sites', 'items'])
    proxy.value = proxyData
  })
}

async function switchWorkspace() {
  auth.setWorkspace(currentWorkspaceId.value)
  await load()
}

async function saveWorkspace() {
  await guarded('workspace', async () => {
    const data = await createWorkspace(workspaceForm.value)
    workspaceForm.value = { name: '', slug: '' }
    if (data?.id) auth.setWorkspace(String(data.id))
    await load()
    message.value = '工作区已创建'
  })
}

async function addSite(siteCode = siteForm.value.site) {
  const workspaceId = currentWorkspaceId.value || auth.workspaceId
  if (!workspaceId || !siteCode) return
  await guarded('site', async () => {
    await addWorkspaceSite(workspaceId, { site: siteCode })
    siteForm.value.site = ''
    await load()
    message.value = '站点已加入工作区'
  })
}

async function toggleWorkspaceSite(site: Record<string, any>) {
  const workspaceId = currentWorkspaceId.value || auth.workspaceId
  if (!workspaceId) return
  await guarded('site', async () => {
    await updateWorkspaceSite(workspaceId, site.id, { enabled: site.enabled === false })
    await load()
  })
}

async function saveWorkspaceSiteTarget(site: Record<string, any>) {
  const workspaceId = currentWorkspaceId.value || auth.workspaceId
  if (!workspaceId || !site.id) return
  await guarded('site-target', async () => {
    await updateWorkspaceSite(workspaceId, site.id, {
      target_sku_count: site.target_sku_count || null,
    })
    await load()
    message.value = '目标 SKU 已保存'
  })
}

async function saveUser() {
  await guarded('user', async () => {
    const data = await createUser({ ...userForm.value, workspace_id: currentWorkspaceId.value ? Number(currentWorkspaceId.value) : undefined })
    createdUserPassword.value = data?.temporary_password || ''
    userForm.value.password = ''
    showCreateUser.value = false
    await load()
    message.value = '用户已创建'
  })
}

async function patchUser(user: Record<string, any>, patch: Record<string, any>) {
  await guarded('user', async () => {
    await updateUser(user.id, patch)
    await load()
  })
}

async function patchUserRole(user: Record<string, any>, value: string | number) {
  await patchUser(user, { role: String(value) })
}

async function resetPassword(user: Record<string, any>) {
  await guarded('reset', async () => {
    const data = await resetUserPassword(user.id, {})
    createdUserPassword.value = data?.temporary_password || ''
    message.value = '临时密码已生成'
  })
}

async function saveInvite() {
  await guarded('invite', async () => {
    const payload: Record<string, unknown> = { ...inviteForm.value }
    if (payload.target_type === 'new_workspace') payload.workspace_id = null
    else payload.workspace_id = payload.workspace_id || currentWorkspaceId.value
    const data = await createInvite(payload)
    newInviteCode.value = data?.code || ''
    await load()
    message.value = '邀请码已生成'
  })
}

async function patchInvite(invite: Record<string, any>, active = false) {
  await guarded('invite', async () => {
    await updateInvite(invite.id, { active })
    await load()
  })
}

async function refreshProxy() {
  await guarded('proxy', async () => {
    proxy.value = await proxyStatus()
    message.value = '代理状态已刷新'
  })
}

async function copyText(text: string) {
  await navigator.clipboard.writeText(text)
  message.value = '已复制'
}

onMounted(load)
</script>

<template>
  <section class="settings-page">
    <div class="lead">设置</div>
    <div class="sub">{{ menuSummary }}</div>
    <UAlert v-if="error" color="error" variant="soft" :title="error" class="mb-4" />
    <UAlert v-if="message" color="success" variant="soft" :title="message" class="mb-4" />

    <PageLoading v-if="initialLoading" title="加载设置..." note="正在读取工作区、用户、邀请和代理状态" />

    <div v-else class="subnav-layout">
      <nav class="subnav" aria-label="设置二级菜单">
        <button v-for="item in visibleMenu" :key="item.key" class="subnav-item" :class="{ active: section === item.key }" @click="section = item.key">
          <span>{{ item.label }}</span>
          <small>{{ item.desc }}</small>
        </button>
      </nav>

      <div class="subnav-content">
        <div v-if="showWorkspaceSettingsFeature && section === 'workspace' && showWorkspaceMenu" class="set-block">
          <h3>🏢 工作区</h3>
          <div class="form-grid one">
            <USelect v-model="currentWorkspaceId" class="set-select" :items="workspaceItems" value-key="value" @update:model-value="switchWorkspace" />
          </div>
          <div class="key-row">
            <div class="info">
              <b>{{ formatWorkspaceName(workspace.workspaces.find((w) => String(w.id) === String(currentWorkspaceId))) }}</b>
              <span class="meta">当前看板、报告和导出按这个工作区隔离</span>
            </div>
          </div>
          <template v-if="canManageWorkspaces">
            <div class="form-grid">
              <input class="set-inp" v-model="workspaceForm.name" placeholder="新工作区名称" />
              <input class="set-inp" v-model="workspaceForm.slug" placeholder="工作区标识" />
              <button class="mini-btn wide" :disabled="busy === 'workspace'" @click="saveWorkspace">创建工作区</button>
            </div>
          </template>
        </div>

        <div v-if="showWorkspaceSitesFeature && section === 'workspace_sites' && canManageWorkspaces" class="set-block">
          <h3>🌐 工作区站点</h3>
          <div class="form-grid">
            <input class="set-inp" v-model="siteForm.site" placeholder="全局站点编码，例如 songmics_us" />
            <button class="mini-btn wide" :disabled="busy === 'site'" @click="addSite()">加入站点</button>
          </div>
          <div class="settings-scroll-list">
            <div v-for="s in workspaceSites" :key="s.id || s.site" class="key-row">
              <div class="info">
                <b>{{ s.site || s.name }}</b>
                <span class="key-prefix">{{ s.brand || '—' }} · {{ s.country || '—' }}</span>
                <span class="meta">状态={{ s.enabled === false ? '停用' : '启用' }} · 隐藏={{ s.hidden ? '是' : '否' }}</span>
                <label class="inline-target">
                  <span>目标 SKU</span>
                  <input v-model.number="s.target_sku_count" class="set-inp mini" type="number" min="0" placeholder="验收目标" @keyup.enter="saveWorkspaceSiteTarget(s)" />
                  <button class="mini-btn" :disabled="busy === 'site-target'" @click="saveWorkspaceSiteTarget(s)">保存</button>
                </label>
              </div>
              <button class="mini-btn" :class="s.enabled === false ? '' : 'bad'" @click="toggleWorkspaceSite(s)">{{ s.enabled === false ? '启用' : '停用' }}</button>
            </div>
            <div v-if="!workspaceSites.length" class="empty-state">
              <b>暂无工作区站点</b>
              先把全局站点编码加入当前工作区
            </div>
          </div>
          <div v-if="sites.length" class="quick-site-row">
            <button v-for="site in sites.slice(0, 12)" :key="site.site || site.name" class="mini-btn" @click="addSite(site.site || site.name)">+ {{ site.site || site.name }}</button>
          </div>
        </div>

        <div v-if="section === 'jobs'" class="set-block">
          <h3>⚙ 采集任务</h3>
          <JobsPanel embedded />
        </div>

        <div v-if="section === 'users' && canAdmin" class="set-block">
          <h3>
            <span>👥 用户管理</span>
            <button type="button" class="mini-btn" @click="openCreateUser">创建用户</button>
          </h3>
          <div v-if="createdUserPassword" class="secret-box">{{ createdUserPassword }}</div>
          <div class="settings-scroll-list">
            <div v-for="u in users" :key="u.id" class="key-row">
              <div class="info">
                <b>{{ u.display_name || u.username }}</b>
                <span class="key-prefix">{{ u.email }} · @{{ u.username }}</span>
                <span class="meta">角色={{ formatRole(u.role) }} · 状态={{ formatAccountStatus(u.status) }}</span>
              </div>
              <div style="display:flex;gap:6px">
                <USelect :model-value="u.role" class="set-select role-select" :items="roleItems" value-key="value" @update:model-value="patchUserRole(u, $event)" />
                <button class="mini-btn" @click="resetPassword(u)">重置密码</button>
                <button class="mini-btn bad" @click="patchUser(u, { status: u.status === 'active' ? 'disabled' : 'active' })">{{ u.status === 'active' ? '禁用' : '启用' }}</button>
              </div>
            </div>
            <div v-if="!users.length" class="empty-state">
              <b>暂无用户</b>
              创建用户后会显示在这里
            </div>
          </div>
        </div>

        <div v-if="section === 'invites' && canManageInvites" class="set-block">
          <h3>🎟 邀请注册</h3>
          <div class="hint-box">
            <b>用途：</b>生成一个注册通行证，发给外部用户。
            <template v-if="inviteForm.target_type === 'new_workspace'">用户注册后会自动创建自己的新租户，并成为该租户负责人。</template>
            <template v-else>用户注册后会加入 <b>{{ formatWorkspaceName(workspace.workspaces.find((w) => String(w.id) === String(inviteForm.workspace_id || currentWorkspaceId))) }}</b>。</template>
            明文只显示一次，历史列表只保留前缀。
          </div>
          <div class="form-grid">
            <div class="form-field wide">
              <label>邀请类型</label>
              <USelect v-model="inviteForm.target_type" class="set-select" :items="inviteTargetItems" value-key="value" />
              <small>外部新客户用“自己的新租户”；邀请同事进现有租户时才选“已有租户”。</small>
            </div>
            <div v-if="canManageWorkspaces && inviteForm.target_type === 'workspace'" class="form-field wide">
              <label>加入哪个租户 / 工作区</label>
              <USelect v-model="inviteForm.workspace_id" class="set-select" :items="workspaceItems" value-key="value" />
              <small>只有内部协作或给同一客户加成员时使用这个模式。</small>
            </div>
            <div class="form-field">
              <label>可注册人数</label>
              <input class="set-inp" type="number" min="1" v-model.number="inviteForm.max_uses" placeholder="例如 1" />
              <small>填 1 表示只能被一个人注册使用。</small>
            </div>
            <div class="form-field">
              <label>有效期</label>
              <input class="set-inp" type="number" min="1" v-model.number="inviteForm.expires_in_days" placeholder="例如 7" />
              <small>超过天数后自动失效。</small>
            </div>
            <div class="form-field">
              <label>注册后角色</label>
              <USelect v-model="inviteForm.default_role" class="set-select" :items="inviteRoleItems" value-key="value" />
              <small>邀请码不能创建管理员，管理员需内部创建。</small>
            </div>
            <button class="mini-btn wide" :disabled="busy === 'invite'" @click="saveInvite">生成注册邀请码</button>
          </div>
          <div v-if="newInviteCode" class="secret-box invite-code-box">
            <div>
              <div class="meta">完整邀请码，只显示这一次</div>
              <code>{{ newInviteCode }}</code>
            </div>
            <button class="mini-btn" @click="copyText(newInviteCode)">复制</button>
          </div>
          <div class="settings-scroll-list">
            <div v-for="i in invites" :key="i.id" class="key-row">
              <div class="info">
                <b>{{ i.code_prefix || i.code }}</b>
                <span class="key-prefix">
                  {{ (i.target_type || 'workspace') === 'new_workspace' ? '注册后创建自己的租户' : (formatWorkspaceName(workspace.workspaces.find((w) => String(w.id) === String(i.workspace_id))) || ('工作区 #' + (i.workspace_id || '—'))) }}
                  · 注册角色 {{ formatRole(i.default_role) }}
                </span>
                <span class="meta">使用进度 {{ i.used_count || 0 }}/{{ i.max_uses || 1 }} · 过期 {{ i.expires_at ? i.expires_at.slice(0, 10) : '—' }} · 最近使用 {{ i.last_used_at ? i.last_used_at.slice(0, 10) : '未使用' }}</span>
              </div>
              <div>
                <span class="invite-status" :class="i.active ? 'pill ok' : 'pill bad'">{{ i.active ? '可用' : '已禁用' }}</span>
                <button v-if="i.active" class="mini-btn bad" @click="patchInvite(i, false)">禁用</button>
              </div>
            </div>
            <div v-if="!invites.length" class="empty-state">
              <b>暂无邀请码</b>
              生成后把完整邀请码发给需要注册的用户。
            </div>
          </div>
        </div>

        <div v-if="section === 'proxy'" class="set-block">
          <h3>🌐 代理池 ({{ proxyOk }}/{{ proxyTotal }})</h3>
          <div class="proxy-summary-grid">
            <div class="proxy-summary-card">
              <span>池内代理</span>
              <b>{{ proxyTotal }}</b>
            </div>
            <div class="proxy-summary-card">
              <span>当前可用</span>
              <b>{{ proxyOk }}</b>
            </div>
            <div class="proxy-summary-card" :class="{ warn: proxyProblemCount > 0 }">
              <span>健康异常</span>
              <b>{{ proxyProblemCount }}</b>
            </div>
          </div>
          <div class="proxy-actions">
            <button class="mini-btn" :disabled="busy === 'proxy'" @click="refreshProxy">刷新状态</button>
            <a v-if="auth.user?.global_role === 'super_admin'" class="mini-btn proxy-admin-link" href="/admin/proxies" target="_blank" rel="noopener">后台代理管理</a>
          </div>
          <div v-if="proxyHealth?.total" class="proxy-status-line">
            <span>健康 {{ proxyStatusCounts.healthy || 0 }}</span>
            <span>降级 {{ proxyStatusCounts.degraded || 0 }}</span>
            <span>不可用 {{ proxyStatusCounts.down || 0 }}</span>
            <span>阻断 {{ proxyStatusCounts.blocked || 0 }}</span>
          </div>
          <div v-for="p in proxyDetails" :key="p.proxy || p.hash" class="key-row">
            <div class="info">
              <span class="key-prefix">{{ p.proxy || '—' }}</span>
              <span class="meta">成功={{ p.success_count || 0 }} 失败={{ p.failure_count || 0 }} · {{ p.last_failure_code || '无最近失败' }}</span>
            </div>
            <span class="pill" :class="proxyTone(p.status)">{{ formatProxyStatus(p.status) }}</span>
          </div>
          <div v-if="!proxyDetails.length" class="empty-state">代理健康状态未加载</div>
        </div>

        <div v-if="section === 'docs'" class="set-block">
          <h3>📚 文档 · 工具</h3>
          <div class="docs-list">
            <div>📊 <a href="/d/morning_report_2026-05-25.html" target="_blank" rel="noopener">24 小时战报</a></div>
            <div>🎯 <a href="/d/customer_dashboard_v7_unified.html" target="_blank" rel="noopener">客户视角看板</a></div>
            <div>📋 <a href="/d/aosen_80pct_daily_plan.html" target="_blank" rel="noopener">遨森 80% 方案</a></div>
            <div>🌐 <a href="/d/platform_expansion_roadmap.html" target="_blank" rel="noopener">平台扩展路线图</a></div>
            <div>🔬 <a href="/d/scrapling_design_research.html" target="_blank" rel="noopener">Scrapling 研究</a></div>
            <div>📚 <a href="/llms.txt" target="_blank" rel="noopener">AI 入口说明</a></div>
            <div>🧪 <a href="/docs" target="_blank" rel="noopener">接口文档</a></div>
          </div>
        </div>
      </div>
    </div>

    <UModal
      v-model:open="showCreateUser"
      title="创建用户"
      :ui="{
        content: 'settings-dialog',
        header: 'settings-dialog-head',
        body: 'settings-dialog-body',
        footer: 'settings-dialog-foot',
        title: 'settings-dialog-title'
      }"
    >
      <template #body>
        <div class="settings-dialog-grid">
          <UFormField label="用户名" class="settings-dialog-field">
            <input v-model="userForm.username" class="set-inp" placeholder="请输入用户名" />
          </UFormField>
          <UFormField label="邮箱" class="settings-dialog-field">
            <input v-model="userForm.email" class="set-inp" placeholder="请输入邮箱" />
          </UFormField>
          <UFormField label="显示名" class="settings-dialog-field">
            <input v-model="userForm.display_name" class="set-inp" placeholder="请输入显示名" />
          </UFormField>
          <UFormField label="角色" class="settings-dialog-field">
            <USelect v-model="userForm.role" class="set-select" :items="roleItems" value-key="value" />
          </UFormField>
          <UFormField label="密码" class="settings-dialog-field wide">
            <input v-model="userForm.password" class="set-inp" type="password" placeholder="留空自动生成" />
          </UFormField>
        </div>
      </template>
      <template #footer>
        <button type="button" class="settings-dialog-btn ghost" :disabled="busy === 'user'" @click="showCreateUser = false">取消</button>
        <button type="button" class="settings-dialog-btn primary" :disabled="busy === 'user'" @click="saveUser">
          {{ busy === 'user' ? '创建中' : '创建用户' }}
        </button>
      </template>
    </UModal>
  </section>
</template>

<style scoped>
.inline-target {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  color: var(--ui-muted);
  font-size: .75rem;
  flex-wrap: wrap;
}
.inline-target .set-inp.mini {
  width: 112px;
  min-height: 30px;
  padding: 4px 8px;
  font-size: .78rem;
}

.settings-dialog-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.settings-dialog-field {
  min-width: 0;
}

.settings-dialog-field.wide {
  grid-column: 1 / -1;
}

.settings-dialog-field :deep(label) {
  color: var(--ui-heading);
  font-size: .78rem;
  font-weight: 800;
}

.settings-dialog-field :deep(.set-inp),
.settings-dialog-field :deep(.set-select) {
  min-height: 38px;
  margin-top: 6px;
}

:global(.settings-dialog) {
  width: 560px;
  max-width: calc(100vw - 32px);
  max-height: calc(100vh - 32px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--ui-card);
  color: var(--ui-text);
  border: 1px solid var(--ui-border);
  border-radius: 12px;
  box-shadow: 0 24px 70px rgba(37, 29, 61, .22);
}

:global(.settings-dialog-head) {
  flex: 0 0 auto;
  padding: 18px 20px 12px;
  border-bottom: 1px solid var(--ui-border);
}

:global(.settings-dialog-title) {
  color: var(--ui-heading);
  font-size: 1rem;
  font-weight: 900;
}

:global(.settings-dialog-body) {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 18px 20px;
}

:global(.settings-dialog-foot) {
  flex: 0 0 auto;
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 20px 18px;
  border-top: 1px solid var(--ui-border);
}

.settings-dialog-btn {
  min-width: 92px;
  min-height: 36px;
  border-radius: 8px;
  border: 1px solid var(--ui-border);
  padding: 8px 14px;
  font-size: .78rem;
  font-weight: 800;
  cursor: pointer;
}

.settings-dialog-btn:disabled {
  opacity: .62;
  cursor: wait;
}

.settings-dialog-btn.ghost {
  background: var(--ui-card-soft);
  color: var(--ui-muted);
}

.settings-dialog-btn.primary {
  border-color: rgba(167, 139, 250, .42);
  background: rgba(167, 139, 250, .18);
  color: var(--ui-purple);
}

:global(html[data-theme="dark"] .settings-dialog) {
  background: #15101f;
  border-color: #3d2d5a;
  box-shadow: 0 24px 70px rgba(0, 0, 0, .52);
}

:global(html[data-theme="dark"] .settings-dialog-btn.ghost) {
  background: rgba(255, 255, 255, .035);
  color: #b5bfd2;
}

:global(html[data-theme="dark"] .settings-dialog-btn.primary) {
  border-color: rgba(185, 148, 255, .44);
  background: rgba(185, 148, 255, .20);
  color: #dccfff;
}

@media (max-width: 640px) {
  .settings-dialog-grid {
    grid-template-columns: 1fr;
  }

  :global(.settings-dialog-foot) {
    flex-direction: column-reverse;
  }

  .settings-dialog-btn {
    width: 100%;
  }
}
</style>
