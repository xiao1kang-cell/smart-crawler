import { apiJson, jsonBody } from './client'

export function listWorkspaces() {
  return apiJson('/api/workspaces')
}

export function createWorkspace(payload: Record<string, unknown>) {
  return apiJson('/api/admin/workspaces', { method: 'POST', ...jsonBody(payload) })
}

export function listWorkspaceSites(workspaceId: string | number) {
  return apiJson(`/api/admin/workspaces/${workspaceId}/sites`)
}

export function addWorkspaceSite(workspaceId: string | number, payload: Record<string, unknown>) {
  return apiJson(`/api/admin/workspaces/${workspaceId}/sites`, { method: 'POST', ...jsonBody(payload) })
}

export function updateWorkspaceSite(workspaceId: string | number, siteId: string | number, payload: Record<string, unknown>) {
  return apiJson(`/api/admin/workspaces/${workspaceId}/sites/${siteId}`, { method: 'PATCH', ...jsonBody(payload) })
}

export function listUsers() {
  return apiJson('/api/admin/users')
}

export function createUser(payload: Record<string, unknown>) {
  return apiJson('/api/admin/users', { method: 'POST', ...jsonBody(payload) })
}

export function updateUser(id: string | number, payload: Record<string, unknown>) {
  return apiJson(`/api/admin/users/${id}`, { method: 'PATCH', ...jsonBody(payload) })
}

export function resetUserPassword(id: string | number, payload: Record<string, unknown>) {
  return apiJson(`/api/admin/users/${id}/reset-password`, { method: 'POST', ...jsonBody(payload) })
}

export function listInvites() {
  return apiJson('/api/admin/invites')
}

export function createInvite(payload: Record<string, unknown>) {
  return apiJson('/api/admin/invites', { method: 'POST', ...jsonBody(payload) })
}

export function updateInvite(id: string | number, payload: Record<string, unknown>) {
  return apiJson(`/api/admin/invites/${id}`, { method: 'PATCH', ...jsonBody(payload) })
}

export function listApiKeys() {
  return apiJson('/api/keys')
}

export function createApiKey(payload: Record<string, unknown>) {
  return apiJson('/api/keys', { method: 'POST', ...jsonBody(payload) })
}

export function updateApiKey(id: string | number, payload: Record<string, unknown>) {
  return apiJson(`/api/keys/${id}`, { method: 'PATCH', ...jsonBody(payload) })
}

export function deleteApiKey(id: string | number) {
  return apiJson(`/api/keys/${id}`, { method: 'DELETE' })
}

export function billingUsage() {
  return apiJson('/api/billing/usage')
}

export function proxyStatus() {
  return apiJson('/api/proxy/status')
}

export function reloadProxy() {
  return apiJson('/api/proxy/reload', { method: 'POST' })
}

export function getWebhookConfig() {
  return apiJson('/api/settings/webhook')
}

export function saveWebhookConfig(payload: Record<string, unknown>) {
  return apiJson('/api/settings/webhook', { method: 'PUT', ...jsonBody(payload) })
}

export function deleteWebhookConfig() {
  return apiJson('/api/settings/webhook', { method: 'DELETE' })
}
