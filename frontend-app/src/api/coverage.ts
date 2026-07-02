import { apiJson } from './client'

const CACHE_TTL_MS = 30_000

type CacheEntry = {
  scope: string
  at: number
  data: any
}

type InflightEntry = {
  scope: string
  promise: Promise<any>
}

let coverageCache: CacheEntry | null = null
let coverageInflight: InflightEntry | null = null
let qualityCache: CacheEntry | null = null
let qualityInflight: InflightEntry | null = null

function cacheScope() {
  if (typeof localStorage === 'undefined') return ''
  return [
    localStorage.getItem('sc_workspace_id') || localStorage.getItem('sc_workspace') || '',
    localStorage.getItem('sc_token') || '',
  ].join(':')
}

function cachedJson(path: string, cache: CacheEntry | null, setCache: (entry: CacheEntry) => void, inflight: InflightEntry | null, setInflight: (entry: InflightEntry | null) => void, force = false) {
  const scope = cacheScope()
  const now = Date.now()
  if (!force && cache && cache.scope === scope && now - cache.at < CACHE_TTL_MS) {
    return Promise.resolve(cache.data)
  }
  if (!force && inflight && inflight.scope === scope) {
    return inflight.promise
  }
  const promise = apiJson(path)
    .then((data) => {
      setCache({ scope, at: Date.now(), data })
      return data
    })
    .finally(() => {
      setInflight(null)
    })
  setInflight({ scope, promise })
  return promise
}

export function listCoverage(opts: { force?: boolean } = {}) {
  return cachedJson('/api/coverage', coverageCache, (entry) => { coverageCache = entry }, coverageInflight, (entry) => { coverageInflight = entry }, Boolean(opts.force))
}

export function dataQuality(opts: { force?: boolean } = {}) {
  return cachedJson('/api/data-quality', qualityCache, (entry) => { qualityCache = entry }, qualityInflight, (entry) => { qualityInflight = entry }, Boolean(opts.force))
}
