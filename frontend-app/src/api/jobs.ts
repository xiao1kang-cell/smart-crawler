import { apiJson, qs } from './client'

export function listJobs(params: Record<string, unknown> = {}) {
  return apiJson(`/api/jobs${qs(params)}`)
}

export function triggerJob(params: Record<string, unknown> = {}) {
  return apiJson(`/api/jobs/trigger${qs(params)}`, { method: 'POST' })
}

export function crawlDiagnostics(params: Record<string, unknown> = {}) {
  return apiJson(`/api/crawl/diagnostics${qs(params)}`)
}

export function latestDailyDelta() {
  return apiJson('/api/daily-delta/latest')
}
