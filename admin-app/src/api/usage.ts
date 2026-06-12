import { apiJson, qs, type Dict } from './client'

export const usageSummary = (p: Dict<any> = {}) => apiJson(`/api/admin/spine/usage${qs(p)}`)
export const usageByKey = () => apiJson('/api/admin/spine/usage/by-key')
export const usageByTenant = () => apiJson('/api/admin/spine/usage/by-tenant')
