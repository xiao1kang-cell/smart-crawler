import { createRouter, createWebHistory } from 'vue-router'
import OverviewPage from '../pages/OverviewPage.vue'

const router = createRouter({
  history: createWebHistory('/admin/'),
  routes: [{ path: '/', component: OverviewPage }]
})

export default router
