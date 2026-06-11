import { createRouter, createWebHistory } from 'vue-router'
import AppLayout from '../components/layout/AppLayout.vue'
import AccountPage from '../pages/AccountPage.vue'
import AskPage from '../pages/AskPage.vue'
import CatalogPage from '../pages/CatalogPage.vue'
import CoveragePage from '../pages/CoveragePage.vue'
import InfluencersPage from '../pages/InfluencersPage.vue'
import JobsPage from '../pages/JobsPage.vue'
import LoginPage from '../pages/LoginPage.vue'
import OnDemandPage from '../pages/OnDemandPage.vue'
import OverviewPage from '../pages/OverviewPage.vue'
import ReportsPage from '../pages/ReportsPage.vue'
import SettingsPage from '../pages/SettingsPage.vue'
import SiteReportPage from '../pages/SiteReportPage.vue'
import TrackingPage from '../pages/TrackingPage.vue'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: LoginPage },
    { path: '/report', component: SiteReportPage },
    {
      path: '/app',
      component: AppLayout,
      meta: { requiresAuth: true },
      children: [
        { path: '', redirect: '/app/overview' },
        { path: 'overview', component: OverviewPage },
        { path: 'reports', component: ReportsPage },
        { path: 'tracking', component: TrackingPage },
        { path: 'ask', component: AskPage },
        { path: 'catalog', component: CatalogPage },
        { path: 'coverage', component: CoveragePage },
        { path: 'jobs', component: JobsPage },
        { path: 'ondemand', component: OnDemandPage },
        { path: 'influencers', component: InfluencersPage },
        { path: 'settings', component: SettingsPage },
        { path: 'account', component: AccountPage }
      ]
    },
    { path: '/:pathMatch(.*)*', redirect: '/app/overview' }
  ]
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.requiresAuth && !auth.isAuthed) return '/login'
  if (to.path === '/login' && auth.isAuthed) return '/app/overview'
})

export default router
