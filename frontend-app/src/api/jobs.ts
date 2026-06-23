import { apiJson, qs } from './client'

export function listJobs(params: Record<string, unknown> = {}) {
  return apiJson(`/api/jobs${qs(params)}`)
}

export function triggerJob(params: Record<string, unknown> = {}) {
  return apiJson(`/api/jobs/trigger${qs(params)}`, { method: 'POST' })
}

export function retryJob(id: string | number) {
  return apiJson(`/api/jobs/${id}/retry`, { method: 'POST' })
}

export function listFailedProducts(params: Record<string, unknown> = {}) {
  return apiJson(`/api/crawl/failed-products${qs(params)}`)
}

export function retryFailedProducts(payload: Record<string, unknown> = {}) {
  return apiJson('/api/crawl/failed-products/retry', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function crawlDiagnostics(params: Record<string, unknown> = {}) {
  return apiJson(`/api/crawl/diagnostics${qs(params)}`)
}

export function latestDailyDelta() {
  return apiJson('/api/daily-delta/latest')
}
