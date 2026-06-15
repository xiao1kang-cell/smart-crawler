<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { changePassword, updateMe } from '../api/auth'
import { asList, fmtDate, fmtNumber } from '../api/client'
import { billingUsage, createApiKey, deleteApiKey, listApiKeys, updateApiKey } from '../api/settings'
import { useAuthStore } from '../stores/auth'
import { useWorkspaceStore } from '../stores/workspace'
import PageLoading from '../components/common/PageLoading.vue'

const auth = useAuthStore()
const workspace = useWorkspaceStore()
const section = ref('profile')
const apiKeys = ref<Record<string, any>[]>([])
const usage = ref<Record<string, any> | null>(null)
const profileForm = ref({ display_name: '', email: '' })
const passwordForm = ref({ old_password: '', new_password: '', confirm_password: '' })
const keyForm = ref({ name: '', scopes: 'crawler:read,crawler:crawl' })
const currentWorkspaceId = ref('')
const loading = ref(false)
const message = ref('')
const error = ref('')

const menu = [
  { key: 'profile', label: '个人资料', desc: '名称和身份信息' },
  { key: 'security', label: '密码与会话', desc: '改密码或退出' },
  { key: 'workspace', label: '当前工作区', desc: '账号所在租户' },
  { key: 'api_keys', label: '接口密钥', desc: '外部调用凭证' },
  { key: 'usage', label: '30 天用量', desc: '调用统计' }
]

function formatRole(role?: string) {
  return ({ admin: '管理员', user: '普通用户', viewer: '只读用户', operator: '操作员' } as Record<string, string>)[role || ''] || role || '—'
}

async function guarded(fn: () => Promise<void>) {
  error.value = ''
  message.value = ''
  try {
    await fn()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    await Promise.all([auth.loadMe(), workspace.load()])
    currentWorkspaceId.value = String(auth.workspaceId || workspace.currentWorkspace?.id || '')
    profileForm.value.display_name = auth.user?.display_name || ''
    profileForm.value.email = auth.user?.email || ''
    const [keysData, usageData] = await Promise.all([
      listApiKeys().catch(() => ({ keys: [] })),
      billingUsage().catch(() => null)
    ])
    apiKeys.value = asList(keysData, ['keys', 'items'])
    usage.value = usageData
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function switchWorkspace() {
  auth.setWorkspace(currentWorkspaceId.value)
  await load()
}

async function logout() {
  await auth.logout()
  window.location.href = '/login'
}

async function saveProfile() {
  await guarded(async () => {
    auth.user = await updateMe(profileForm.value)
    message.value = '资料已保存'
  })
}

async function savePassword() {
  await guarded(async () => {
    await changePassword(passwordForm.value)
    passwordForm.value = { old_password: '', new_password: '', confirm_password: '' }
    message.value = '密码已更新'
  })
}

async function saveApiKey() {
  await guarded(async () => {
    const scopes = keyForm.value.scopes.split(',').map((x) => x.trim()).filter(Boolean)
    const data = await createApiKey({ name: keyForm.value.name, scopes })
    keyForm.value.name = ''
    await load()
    message.value = data?.key ? `新密钥：${data.key}` : 'API Key 已创建'
  })
}

async function toggleKey(key: Record<string, any>) {
  await guarded(async () => {
    await updateApiKey(key.id, { active: !key.active })
    await load()
  })
}

function formatScope(scope: string) {
  return ({ 'crawler:read': '读取', 'crawler:crawl': '采集', 'crawler:scrape': '抓单页' } as Record<string, string>)[scope] || scope
}

async function removeKey(key: Record<string, any>) {
  await guarded(async () => {
    await deleteApiKey(key.id)
    await load()
  })
}

onMounted(load)
</script>

<template>
  <section>
    <div class="lead">账号</div>
    <div class="sub">个人资料 · 密码安全 · 接口密钥 · 用量 · 当前工作区</div>
    <UAlert v-if="error" color="error" variant="soft" :title="error" class="mb-4" />
    <UAlert v-if="message" color="success" variant="soft" :title="message" class="mb-4" />

    <PageLoading v-if="loading && !auth.user" title="加载账号信息..." note="正在同步个人资料、工作区和接口密钥" />

    <div v-else class="subnav-layout">
      <nav class="subnav" aria-label="账号二级菜单">
        <button v-for="item in menu" :key="item.key" class="subnav-item" :class="{ active: section === item.key }" @click="section = item.key">
          <span>{{ item.label }}</span>
          <small>{{ item.desc }}</small>
        </button>
      </nav>

      <div class="subnav-content">
        <div v-if="section === 'profile'" class="set-block">
          <h3>👤 个人资料</h3>
          <div class="key-row">
            <div class="info">
              <b>{{ auth.user?.display_name || auth.user?.username }}</b>
              <span class="key-prefix">{{ auth.user?.email || auth.user?.username }}</span>
              <span class="meta">角色={{ formatRole(auth.user?.role) }} · 状态={{ auth.user?.status || 'active' }} · 全局角色={{ formatRole(auth.user?.global_role) }}</span>
            </div>
          </div>
          <div class="form-grid one">
            <input class="set-inp" v-model="profileForm.display_name" placeholder="显示名" />
            <input class="set-inp" v-model="profileForm.email" placeholder="邮箱" />
            <button class="mini-btn" @click="saveProfile">保存资料</button>
          </div>
        </div>

        <div v-if="section === 'security'" class="set-block">
          <h3>🔒 密码与会话</h3>
          <div class="form-grid">
            <input class="set-inp" type="password" v-model="passwordForm.old_password" placeholder="旧密码" />
            <input class="set-inp" type="password" v-model="passwordForm.new_password" placeholder="新密码" />
            <input class="set-inp" type="password" v-model="passwordForm.confirm_password" placeholder="确认新密码" />
            <button class="mini-btn" @click="savePassword">修改密码</button>
          </div>
          <div class="key-row">
            <div class="info">
              <b>退出当前账号</b>
              <span class="meta">结束当前浏览器会话，返回登录页</span>
            </div>
            <button class="mini-btn bad" @click="logout">退出登录</button>
          </div>
        </div>

        <div v-if="section === 'workspace'" class="set-block">
          <h3>🏢 当前工作区</h3>
          <div class="key-row">
            <div class="info">
              <b>{{ workspace.currentWorkspace?.name || workspace.currentWorkspace?.slug || '当前工作区' }}</b>
              <span class="meta">站点、报告、导出、接口密钥和用量按工作区隔离</span>
            </div>
          </div>
          <div v-if="workspace.workspaces.length > 1" class="form-grid one">
            <select class="set-sel" v-model="currentWorkspaceId" @change="switchWorkspace">
              <option v-for="w in workspace.workspaces" :key="w.id" :value="String(w.id)">{{ w.name || w.slug || `工作区 #${w.id}` }}</option>
            </select>
          </div>
        </div>

        <div v-if="section === 'api_keys'" class="set-block">
          <h3>🔑 接口密钥</h3>
          <div class="form-grid">
            <input class="set-inp" v-model="keyForm.name" placeholder="Key 名称" />
            <input class="set-inp" v-model="keyForm.scopes" placeholder="scopes, 逗号分隔" />
            <button class="mini-btn wide" @click="saveApiKey">创建密钥</button>
          </div>
          <div v-for="key in apiKeys" :key="key.id" class="key-row">
            <div class="info">
              <b>{{ key.name || 'API Key' }}</b>
              <span class="key-prefix">{{ key.key_prefix || key.prefix }}</span>
              <span class="meta">{{ fmtNumber(key.request_count) }} 次调用 · {{ (key.scopes || []).map(formatScope).join('、') || '默认权限' }} · 最近 {{ fmtDate(key.last_used) }}</span>
            </div>
            <div style="display:flex;gap:6px">
              <button class="mini-btn" @click="toggleKey(key)">{{ key.active === false ? '启用' : '禁用' }}</button>
              <button class="mini-btn bad" @click="removeKey(key)">删除</button>
            </div>
          </div>
        </div>

        <div v-if="section === 'usage'" class="set-block">
          <h3>💰 30 天用量</h3>
          <div v-for="key in (usage?.keys || [])" :key="key.id" class="key-row">
            <div class="info">
              <b>{{ key.name }}</b>
              <span class="key-prefix">{{ key.key_prefix }}</span>
              <span class="meta">{{ fmtNumber(key.total_calls ?? key.request_count) }} 次调用 · {{ fmtNumber(key.total_api_calls || 0) }} API · {{ fmtNumber(key.total_browser_opens || 0) }} 浏览器 · {{ fmtNumber(key.total_pages_fetched || 0) }} 页 · {{ fmtNumber(key.total_bytes ?? key.bytes) }} bytes</span>
            </div>
          </div>
          <div v-if="!(usage?.keys || []).length" class="empty-state">暂无用量记录</div>
        </div>
      </div>
    </div>
  </section>
</template>
