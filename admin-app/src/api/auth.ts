import { apiJson, jsonBody } from './client'

export function login(payload: { username: string; password: string }) {
  return apiJson('/api/auth/login', { method: 'POST', ...jsonBody(payload) })
}

export function me() {
  return apiJson('/api/me')
}

export function logout() {
  return apiJson('/api/auth/logout', { method: 'POST' })
}
