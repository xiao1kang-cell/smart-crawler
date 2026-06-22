<script setup lang="ts">
import {
  Activity,
  BarChart3,
  Boxes,
  ClipboardList,
  CreditCard,
  DatabaseZap,
  ListChecks,
  LogOut,
  Network,
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
  { path: '/datasets', label: '通用数据集', icon: Boxes },
  { path: '/queue', label: 'spine 队列', icon: ListChecks },
  { path: '/data-quality', label: '数据质量', icon: DatabaseZap },
  { path: '/usage', label: '计费', icon: CreditCard },
  { path: '/proxies', label: '代理池', icon: Network },
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
  background: var(--admin-shell-bg, var(--ui-bg));
}

.admin-side {
  position: sticky;
  top: 0;
  width: 232px;
  height: 100vh;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 18px 12px;
  border-right: 1px solid var(--ui-border, rgba(255, 255, 255, 0.08));
  background: var(--ui-panel, rgba(255, 255, 255, 0.02));
}

.admin-brand {
  display: flex;
  flex-direction: column;
  font-weight: 700;
  font-size: 16px;
  line-height: 1.25;
  padding: 6px 10px 12px;
  border-bottom: 1px solid var(--ui-border, rgba(255, 255, 255, 0.08));
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
  gap: 4px;
}

.admin-nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 38px;
  padding: 0 12px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  color: inherit;
  text-decoration: none;
  opacity: 0.72;
  transition: background 0.15s, color 0.15s, opacity 0.15s;
}

.admin-nav-item:hover {
  opacity: 1;
  background: var(--admin-control-hover, rgba(255, 255, 255, 0.05));
}

.admin-nav-item.active {
  opacity: 1;
  background: rgba(99, 102, 241, 0.14);
  color: var(--ui-color-primary-500, #6366f1);
  box-shadow: inset 3px 0 0 var(--ui-color-primary-500, #6366f1);
}

.admin-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.admin-top {
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 16px;
  min-height: 56px;
  padding: 10px 24px;
  border-bottom: 1px solid var(--ui-border, rgba(255, 255, 255, 0.08));
  background: color-mix(in srgb, var(--ui-panel) 92%, transparent);
  backdrop-filter: blur(12px);
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
  min-height: 34px;
  padding: 0 12px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
  background: var(--admin-control-bg, transparent);
  color: inherit;
}

.admin-logout:hover {
  background: var(--admin-control-hover, rgba(255, 255, 255, 0.05));
}

.admin-content {
  flex: 1;
  overflow: auto;
  min-width: 0;
}

.admin-content :deep(.page) {
  width: 100%;
  min-width: 0;
}

.admin-content :deep(.table-wrap) {
  max-width: 100%;
}

@media (max-width: 760px) {
  .admin-shell {
    flex-direction: column;
  }

  .admin-side {
    position: sticky;
    top: 0;
    z-index: 40;
    width: 100%;
    height: auto;
    gap: 8px;
    padding: 10px 12px;
    border-right: 0;
    border-bottom: 1px solid var(--ui-border, rgba(255, 255, 255, 0.08));
  }

  .admin-brand {
    padding: 2px 4px;
    font-size: 15px;
  }

  .admin-nav {
    display: flex;
    flex-direction: row;
    gap: 6px;
    overflow-x: auto;
    padding-bottom: 2px;
    scrollbar-width: none;
  }

  .admin-nav::-webkit-scrollbar {
    display: none;
  }

  .admin-nav-item {
    flex: 0 0 auto;
    min-width: max-content;
    justify-content: center;
    min-height: 34px;
    padding: 0 10px;
    font-size: 12px;
    white-space: nowrap;
  }

  .admin-nav-item.active {
    box-shadow: inset 0 -2px 0 var(--ui-color-primary-500, #6366f1);
  }

  .admin-main {
    width: 100%;
  }

  .admin-top {
    justify-content: space-between;
    padding: 10px 14px;
    min-height: 52px;
  }

  .admin-user {
    min-width: 0;
  }

  .admin-user span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .admin-content {
    overflow-x: hidden;
  }

  .admin-content :deep(.page) {
    padding: 16px 14px;
  }

  .admin-content :deep(.page-head),
  .admin-content :deep(.head-left),
  .admin-content :deep(.toolbar),
  .admin-content :deep(.enqueue),
  .admin-content :deep(.pager) {
    min-width: 0;
    flex-wrap: wrap;
  }

  .admin-content :deep(.page-head) {
    align-items: flex-start;
  }

  .admin-content :deep(.page-title) {
    max-width: 100%;
    overflow-wrap: anywhere;
    line-height: 1.25;
  }

  .admin-content :deep(.ctl),
  .admin-content :deep(.btn) {
    max-width: 100%;
  }

  .admin-content :deep(.filter-input),
  .admin-content :deep(.grow) {
    width: 100%;
    min-width: 0;
  }

  .admin-content :deep(.table-wrap) {
    border-radius: 10px;
  }
}
</style>
