import { useAuthStore } from '../stores/auth'

export type Dict<T = unknown> = Record<string, T>

export async function apiJson<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const auth = useAuthStore()
  const headers = new Headers(opts.headers || {})
  if (!headers.has('Content-Type') && opts.body && !(opts.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }
  if (auth.token) headers.set('Authorization', `Bearer ${auth.token}`)

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

export function fmtNumber(value: unknown) {
  const n = Number(value || 0)
  return Number.isFinite(n) ? n.toLocaleString() : '0'
}

function padDatePart(value: number) {
  return String(value).padStart(2, '0')
}

export function fmtDate(value?: string | null) {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return `${d.getFullYear()}-${padDatePart(d.getMonth() + 1)}-${padDatePart(d.getDate())} ${padDatePart(d.getHours())}:${padDatePart(d.getMinutes())}`
}

export function fmtDateOnly(value?: string | null) {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value).slice(0, 10) || value
  return `${d.getFullYear()}-${padDatePart(d.getMonth() + 1)}-${padDatePart(d.getDate())}`
}
