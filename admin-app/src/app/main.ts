import { VueQueryPlugin, QueryClient } from '@tanstack/vue-query'
import ui from '@nuxt/ui/vue-plugin'
import { createPinia } from 'pinia'
import { createApp } from 'vue'
import App from '../App.vue'
import router from './router'
import '../styles/theme.css'

try {
  const theme = localStorage.getItem('sc_theme') || 'dark'
  if (theme === 'dark') document.documentElement.setAttribute('data-theme', 'dark')
  else document.documentElement.removeAttribute('data-theme')
} catch {
  document.documentElement.setAttribute('data-theme', 'dark')
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 20_000
    }
  }
})

const app = createApp(App)
const pinia = createPinia()

app.use(ui)
app.use(pinia)
app.use(router)
app.use(VueQueryPlugin, { queryClient })
app.mount('#app')
