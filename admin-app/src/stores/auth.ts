import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as authApi from '../api/auth'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('sc_admin_token') || '')
  const user = ref<Record<string, any> | null>(null)

  const isAuthed = computed(() => Boolean(token.value))

  function setToken(nextToken: string) {
    token.value = nextToken
    localStorage.setItem('sc_admin_token', nextToken)
  }

  function clear() {
    token.value = ''
    user.value = null
    localStorage.removeItem('sc_admin_token')
  }

  async function login(payload: { username: string; password: string }) {
    const data = await authApi.login(payload)
    setToken(data.access_token || data.token)
    return data
  }

  async function loadMe() {
    user.value = await authApi.me()
    return user.value
  }

  async function logout() {
    try {
      await authApi.logout()
    } finally {
      clear()
    }
  }

  return { token, user, isAuthed, setToken, clear, login, loadMe, logout }
})
