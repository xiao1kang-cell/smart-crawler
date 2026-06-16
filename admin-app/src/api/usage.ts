import { apiJson, qs, type Dict } from './client'

export const usageSummary = (p: Dict<any> = {}) => apiJson(`/api/admin/spine/usage${qs(p)}`)
export const usageByKey = (p: Dict<any> = {}) => apiJson(`/api/admin/spine/usage/by-key${qs(p)}`)
export const usageByTenant = (p: Dict<any> = {}) => apiJson(`/api/admin/spine/usage/by-tenant${qs(p)}`)
