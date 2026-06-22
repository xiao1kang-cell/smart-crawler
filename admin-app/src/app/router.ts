import { createRouter, createWebHistory } from 'vue-router'
import AdminLayout from '../components/layout/AdminLayout.vue'
import AuditPage from '../pages/AuditPage.vue'
import DataQualityPage from '../pages/DataQualityPage.vue'
import DatasetDetailPage from '../pages/DatasetDetailPage.vue'
import DatasetsPage from '../pages/DatasetsPage.vue'
import HealthPage from '../pages/HealthPage.vue'
import LoginPage from '../pages/LoginPage.vue'
import OverviewPage from '../pages/OverviewPage.vue'
import ProxiesPage from '../pages/ProxiesPage.vue'
import QueuePage from '../pages/QueuePage.vue'
import TenantsPage from '../pages/TenantsPage.vue'
import UsagePage from '../pages/UsagePage.vue'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory('/admin/'),
  routes: [
    { path: '/login', component: LoginPage },
    {
      path: '/',
      component: AdminLayout,
      children: [
        { path: '', component: OverviewPage },
        { path: 'tenants', component: TenantsPage },
        { path: 'datasets', component: DatasetsPage },
        { path: 'datasets/:id', component: DatasetDetailPage },
        { path: 'queue', component: QueuePage },
        { path: 'data-quality', component: DataQualityPage },
        { path: 'usage', component: UsagePage },
        { path: 'proxies', component: ProxiesPage },
        { path: 'health', component: HealthPage },
        { path: 'audit', component: AuditPage }
      ]
    },
    { path: '/:pathMatch(.*)*', redirect: '/' }
  ]
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (to.path === '/login') return
  if (!auth.token) return '/login'
  if (!auth.user) {
    try {
      await auth.loadMe()
    } catch {
      return '/login'
    }
  }
  if (!auth.canAccessAdmin) return '/login'
})

export default router
