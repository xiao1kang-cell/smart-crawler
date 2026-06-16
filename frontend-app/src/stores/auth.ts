import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as authApi from '../api/auth'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('sc_token') || '')
  const workspaceId = ref(localStorage.getItem('sc_workspace_id') || localStorage.getItem('sc_workspace') || '')
  const user = ref<Record<string, any> | null>(null)

  const isAuthed = computed(() => Boolean(token.value))

  function setToken(nextToken: string, nextWorkspaceId?: string | number | null) {
    token.value = nextToken
    localStorage.setItem('sc_token', nextToken)
    if (nextWorkspaceId !== undefined && nextWorkspaceId !== null && nextWorkspaceId !== '') {
      setWorkspace(String(nextWorkspaceId))
    }
  }

  function setWorkspace(nextWorkspaceId: string) {
    if (String(nextWorkspaceId || '') !== String(workspaceId.value || '')) {
      user.value = null
    }
    workspaceId.value = nextWorkspaceId
    if (nextWorkspaceId) {
      localStorage.setItem('sc_workspace_id', nextWorkspaceId)
      localStorage.setItem('sc_workspace', nextWorkspaceId)
    } else {
      localStorage.removeItem('sc_workspace_id')
      localStorage.removeItem('sc_workspace')
    }
  }

  function clear() {
    token.value = ''
    workspaceId.value = ''
    user.value = null
    localStorage.removeItem('sc_token')
    localStorage.removeItem('sc_workspace_id')
    localStorage.removeItem('sc_workspace')
  }

  async function login(payload: { username: string; password: string }) {
    const data = await authApi.login(payload)
    setToken(data.access_token || data.token, data.workspace_id || data.workspace?.id)
    return data
  }

  async function register(payload: { invite_code: string; username: string; password: string; email?: string; display_name?: string; confirm_password?: string }) {
    const data = await authApi.register(payload)
    setToken(data.access_token || data.token, data.workspace_id || data.workspace?.id)
    return data
  }

  async function logout() {
    try {
      await authApi.logout()
    } finally {
      clear()
    }
  }

  async function loadMe() {
    user.value = await authApi.me()
    return user.value
  }

  return { token, workspaceId, user, isAuthed, setToken, setWorkspace, clear, login, register, logout, loadMe }
})
