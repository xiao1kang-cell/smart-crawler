import { apiJson, jsonBody, qs } from './client'

export function listTracking(params: Record<string, unknown> = {}) {
  return apiJson(`/api/tracking${qs(params)}`)
}
export function addTracking(payload: { url: string; brand?: string; country?: string }) {
  return apiJson('/api/tracking', { method: 'POST', ...jsonBody(payload) })
}
export function editTracking(site: string, payload: Record<string, unknown>) {
  return apiJson(`/api/tracking/${encodeURIComponent(site)}`, { method: 'PATCH', ...jsonBody(payload) })
}
export function pauseTracking(site: string) {
  return apiJson(`/api/tracking/${encodeURIComponent(site)}/pause`, { method: 'POST' })
}
export function resumeTracking(site: string) {
  return apiJson(`/api/tracking/${encodeURIComponent(site)}/resume`, { method: 'POST' })
}
export function deleteTracking(site: string) {
  return apiJson(`/api/tracking/${encodeURIComponent(site)}`, { method: 'DELETE' })
}
