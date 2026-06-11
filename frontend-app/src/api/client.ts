import { useAuthStore } from '../stores/auth'

export type Dict<T = unknown> = Record<string, T>

export async function apiJson<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const auth = useAuthStore()
  const headers = new Headers(opts.headers || {})
  if (!headers.has('Content-Type') && opts.body && !(opts.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }
  if (auth.token) headers.set('Authorization', `Bearer ${auth.token}`)
  if (auth.workspaceId) headers.set('X-Workspace-Id', auth.workspaceId)

  const res = await fetch(path, { ...opts, headers })
  if (res.status === 401) {
    auth.clear()
    throw new Error('登录已过期，请重新登录')
  }
  const text = await res.text()
  const data = text ? JSON.parse(text) : null
  if (!res.ok) {
    const message = data?.detail || data?.error || data?.message || `${res.status} ${res.statusText}`
    throw new Error(typeof message === 'string' ? message : JSON.stringify(message))
  }
  return data as T
}

export function jsonBody(payload: unknown): RequestInit {
  return { body: JSON.stringify(payload) }
}

export function qs(params: Dict<any>) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  }
  const s = search.toString()
  return s ? `?${s}` : ''
}

export function asList(data: any, keys = ['items', 'sites', 'jobs', 'products', 'rows', 'data']) {
  if (Array.isArray(data)) return data
  for (const key of keys) if (Array.isArray(data?.[key])) return data[key]
  return []
}

export function shortUrl(url?: string) {
  if (!url) return ''
  try {
    const u = new URL(url)
    return `${u.hostname}${u.pathname}`.replace(/\/$/, '')
  } catch {
    return url
  }
}

export function fmtNumber(value: unknown) {
  const n = Number(value || 0)
  return Number.isFinite(n) ? n.toLocaleString() : '0'
}

// 货币符号:商品自带 currency 字段(按站点国家映射 USD/EUR/GBP/PLN…),
// 不要硬编码 $——非美元站点(如 _de/_uk/_pl)会显示错误符号。
const CURRENCY_SYMBOL: Record<string, string> = {
  USD: '$', EUR: '€', GBP: '£', PLN: 'zł', MXN: '$', BRL: 'R$',
  JPY: '¥', CNY: '¥', CAD: 'C$', AUD: 'A$', SEK: 'kr', CHF: 'CHF',
}
export function fmtPrice(amount: unknown, currency?: string | null) {
  if (amount === null || amount === undefined || amount === '') return '--'
  const n = Number(amount)
  if (!Number.isFinite(n)) return '--'
  const sym = currency ? (CURRENCY_SYMBOL[currency.toUpperCase()] || `${currency} `) : '$'
  return `${sym}${n.toLocaleString()}`
}

// 代理池可用数:后端 /proxy/status 顶层只有 total,可用数在 by_tier.<tier>.available。
// 兼容顶层 available(若后端将来补上)。
export function proxyAvailable(status: any): number {
  if (!status) return 0
  if (typeof status.available === 'number') return status.available
  return Object.values(status.by_tier || {}).reduce(
    (sum: number, tier: any) => sum + Number(tier?.available || 0),
    0,
  )
}

export function fmtDate(value?: string | null) {
  if (!value) return '-'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString()
}

export function badgeTone(status?: string) {
  const s = String(status || '').toLowerCase()
  if (['ok', 'success', 'completed', 'active', 'done'].includes(s)) return 'success'
  if (['running', 'queued', 'pending'].includes(s)) return 'info'
  if (['failed', 'error', 'disabled'].includes(s)) return 'error'
  return 'neutral'
}
