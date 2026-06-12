<script setup lang="ts">
import {
  Activity,
  BarChart3,
  Boxes,
  ClipboardList,
  CreditCard,
  ListChecks,
  LogOut,
  Users
} from 'lucide-vue-next'
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const error = ref('')

const nav = [
  { path: '/', label: '概览', icon: BarChart3 },
  { path: '/tenants', label: '租户用户', icon: Users },
  { path: '/datasets', label: '数据集', icon: Boxes },
  { path: '/queue', label: '队列', icon: ListChecks },
  { path: '/usage', label: '计费', icon: CreditCard },
  { path: '/health', label: '健康', icon: Activity },
  { path: '/audit', label: '审计', icon: ClipboardList }
]

function isActive(path: string) {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

async function bootstrap() {
  try {
    if (!auth.user) await auth.loadMe()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function logout() {
  await auth.logout()
  router.push('/login')
}

onMounted(bootstrap)
</script>

<template>
  <div class="admin-shell">
    <aside class="admin-side">
      <div class="admin-brand">⚡ smart-crawler<span class="admin-brand-sub">管理后台</span></div>
      <nav class="admin-nav">
        <RouterLink
          v-for="item in nav"
          :key="item.path"
          :to="item.path"
          class="admin-nav-item"
          :class="{ active: isActive(item.path) }"
        >
          <component :is="item.icon" class="size-4" />
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>
    </aside>

    <div class="admin-main">
      <header class="admin-top">
        <div class="admin-user">
          <div class="admin-avatar">
            {{ (auth.user?.display_name || auth.user?.username || 'A').charAt(0).toUpperCase() }}
          </div>
          <span>{{ auth.user?.display_name || auth.user?.username || '超级管理员' }}</span>
        </div>
        <button class="admin-logout" title="退出登录" @click="logout">
          <LogOut class="size-4" />
          <span>退出</span>
        </button>
      </header>

      <main class="admin-content">
        <UAlert v-if="error" color="error" variant="soft" :title="error" class="m-6" />
        <RouterView />
      </main>
    </div>
  </div>
</template>

<style scoped>
.admin-shell {
  display: flex;
  min-height: 100vh;
  width: 100%;
}

.admin-side {
  width: 220px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 12px;
  border-right: 1px solid var(--ui-border, rgba(255, 255, 255, 0.08));
  background: var(--ui-bg-muted, rgba(255, 255, 255, 0.02));
}

.admin-brand {
  display: flex;
  flex-direction: column;
  font-weight: 700;
  font-size: 16px;
  padding: 8px 10px;
}

.admin-brand-sub {
  font-size: 11px;
  font-weight: 500;
  opacity: 0.6;
  margin-top: 2px;
}

.admin-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.admin-nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 14px;
  color: inherit;
  text-decoration: none;
  opacity: 0.78;
  transition: background 0.15s, opacity 0.15s;
}

.admin-nav-item:hover {
  opacity: 1;
  background: var(--ui-bg-elevated, rgba(255, 255, 255, 0.05));
}

.admin-nav-item.active {
  opacity: 1;
  background: var(--ui-color-primary-500, #6366f1);
  color: #fff;
}

.admin-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.admin-top {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 16px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--ui-border, rgba(255, 255, 255, 0.08));
}

.admin-user {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}

.admin-avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  background: var(--ui-color-primary-500, #6366f1);
}

.admin-logout {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 8px;
  font-size: 13px;
  cursor: pointer;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
  background: transparent;
  color: inherit;
}

.admin-logout:hover {
  background: var(--ui-bg-elevated, rgba(255, 255, 255, 0.05));
}

.admin-content {
  flex: 1;
  overflow: auto;
}
</style>
