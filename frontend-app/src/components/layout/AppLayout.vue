<script setup lang="ts">
import {
  BarChart3,
  Bot,
  Boxes,
  BriefcaseBusiness,
  Database,
  LogOut,
  Settings,
  Sparkles
} from 'lucide-vue-next'
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listCoverage } from '../../api/coverage'
import { useAuthStore } from '../../stores/auth'
import { useWorkspaceStore } from '../../stores/workspace'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const workspace = useWorkspaceStore()

const busy = ref('')
const message = ref('')
const error = ref('')
const coverageSummary = ref<Record<string, any>>({})

const nav = [
  { path: '/app/overview', label: '📊 总览', icon: BarChart3 },
  { path: '/app/reports', label: '📄 报告', icon: BriefcaseBusiness },
  { path: '/app/tracking', label: '🎯 标杆维护', icon: BarChart3 },
  { path: '/app/ask', label: '💬 问答', icon: Sparkles },
  { path: '/app/catalog', label: '📦 商品库', icon: Boxes },
  { path: '/app/coverage', label: '🌐 覆盖率', icon: Database },
  { path: '/app/ondemand', label: '🔗 按需抓取', icon: Bot },
  { path: '/app/influencers', label: '🌟 红人', icon: Sparkles },
  { path: '/app/settings', label: '🔧 设置', icon: Settings }
]

const totalSku = computed(() => Number(coverageSummary.value.total_current_sku ?? 0))
const coveragePct = computed(() => Number(coverageSummary.value.overall_coverage_pct ?? 0))

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

async function bootstrap() {
  await guarded('load', async () => {
    await Promise.all([auth.loadMe(), workspace.load()])
    const coverage = await listCoverage().catch(() => null)
    coverageSummary.value = coverage?.summary || {}
  })
}

async function logout() {
  await auth.logout()
  router.push('/login')
}

onMounted(bootstrap)
</script>

<template>
  <main>
    <div class="topbar">
      <div class="brand">⚡ smart-crawler</div>
      <nav class="tab-row">
        <RouterLink v-for="item in nav" :key="item.path" :to="item.path" class="tab" :class="{ active: route.path.startsWith(item.path) }">
          {{ item.label }}
        </RouterLink>
      </nav>
      <div class="usage-strip">覆盖SKU <b>{{ totalSku.toLocaleString() }}</b> · 覆盖 <b>{{ coveragePct }}%</b></div>
      <RouterLink class="acct" :class="{ active: route.path.startsWith('/app/account') }" to="/app/account">
        <div class="avatar">{{ (auth.user?.display_name || auth.user?.username || 'A').charAt(0).toUpperCase() }}</div>
        <span>{{ auth.user?.display_name || auth.user?.username || '管理员' }}</span>
      </RouterLink>
      <button class="icon-btn" title="退出" @click="logout">
        <LogOut class="size-4" />
      </button>
    </div>

    <div class="page">
      <UAlert v-if="error" color="error" variant="soft" :title="error" class="mb-4" />
      <UAlert v-if="message" color="success" variant="soft" :title="message" class="mb-4" />
      <RouterView />
    </div>
  </main>
</template>
