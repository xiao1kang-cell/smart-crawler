import tailwindcss from '@tailwindcss/vite'
import ui from '@nuxt/ui/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  base: '/admin/',
  plugins: [vue(), ui(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    port: 5174,
    proxy: {
      '/api': 'http://127.0.0.1:8077',
      '/d': 'http://127.0.0.1:8077',
      '/favicon.svg': 'http://127.0.0.1:8077'
    }
  }
})
