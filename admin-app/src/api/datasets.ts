import { apiJson, qs, type Dict } from './client'

export const listDatasets = () => apiJson('/api/admin/spine/datasets')
export const datasetRecords = (id: number, p: Dict<any> = {}) =>
  apiJson(`/api/admin/spine/datasets/${id}/records${qs(p)}`)
export const recordDetail = (id: number) => apiJson(`/api/admin/spine/records/${id}`)
export const promoteRecord = (id: number) =>
  apiJson(`/api/admin/spine/records/${id}/promote`, { method: 'POST' })
export const deleteRecord = (id: number) =>
  apiJson(`/api/admin/spine/records/${id}`, { method: 'DELETE' })
