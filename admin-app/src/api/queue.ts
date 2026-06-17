import { apiJson, jsonBody, qs, type Dict } from './client'

export const listJobs = (p: Dict<any> = {}) => apiJson(`/api/admin/spine/jobs${qs(p)}`)
export const jobStats = () => apiJson('/api/admin/spine/jobs/stats')
export const jobDetail = (id: number, source = 'spine') =>
  apiJson(`/api/admin/spine/jobs/${id}${qs({ source })}`)
export const retryJob = (id: number, source = 'spine') =>
  apiJson(`/api/admin/spine/jobs/${id}/retry${qs({ source })}`, { method: 'POST' })
export const queueMaintenance = (payload: Dict<any> = {}) =>
  apiJson('/api/admin/spine/jobs/maintenance', { method: 'POST', ...jsonBody(payload) })
export const enqueueJob = (payload: Dict<any>) =>
  apiJson('/api/admin/spine/jobs/enqueue', { method: 'POST', ...jsonBody(payload) })
