import { reactive, onUnmounted } from 'vue'
import { asList } from '../api/client'
import { listJobs, triggerJob } from '../api/jobs'

type JobStatus = 'idle' | 'submitting' | 'queued' | 'running' | 'success' | 'failed' | 'blocked' | 'skipped' | 'finished' | 'unknown'

type TriggerState = {
  jobId?: number
  status: JobStatus
  message: string
  detail?: string
}

type TriggerOptions = {
  onDone?: (site: string, state: TriggerState) => void | Promise<void>
}

const TERMINAL = new Set(['success', 'failed', 'blocked', 'skipped', 'finished'])

function keyFor(site?: string | null) {
  return String(site || '').trim()
}

function normalizeStatus(status?: string): JobStatus {
  const value = String(status || '').toLowerCase()
  if (value === 'pending') return 'queued'
  if (value === 'completed') return 'success'
  if (['queued', 'running', 'success', 'failed', 'blocked', 'skipped'].includes(value)) return value as JobStatus
  return value ? 'unknown' : 'idle'
}

function statusMessage(status: JobStatus) {
  return ({
    idle: '',
    submitting: '提交中',
    queued: '已入队',
    running: '抓取中',
    success: '已完成',
    failed: '抓取失败',
    blocked: '被反爬拦截',
    skipped: '已跳过',
    finished: '已结束',
    unknown: '状态同步中',
  } as Record<JobStatus, string>)[status]
}

function jobDetail(job: Record<string, any>) {
  const products = Number(job.products_count ?? 0)
  const promos = Number(job.promotion_count ?? 0)
  const duration = job.duration_sec != null ? ` · ${job.duration_sec}s` : ''
  if (job.status === 'success' || job.status === 'completed') {
    return `${products.toLocaleString()} 商品 · ${promos.toLocaleString()} 促销${duration}`
  }
  if (job.failure_code) {
    const action = job.suggested_action ? ` · ${String(job.suggested_action).slice(0, 64)}` : ''
    return `${job.failure_code}${action}`
  }
  if (job.error) return String(job.error).slice(0, 90)
  return job.id ? `Job #${job.id}` : ''
}

export function useJobTrigger(options: TriggerOptions = {}) {
  const states = reactive<Record<string, TriggerState>>({})
  const timers = new Map<string, number>()

  function ensure(site: string) {
    const key = keyFor(site)
    if (!states[key]) states[key] = { status: 'idle', message: '' }
    return states[key]
  }

  function setState(site: string, patch: Partial<TriggerState>) {
    const state = ensure(site)
    Object.assign(state, patch)
    if (patch.status) state.message = statusMessage(patch.status)
    return state
  }

  async function refreshJob(site: string) {
    const state = ensure(site)
    if (!state.jobId) return
    const data = await listJobs({ ids: state.jobId, limit: 1 })
    const job = asList(data, ['jobs', 'items']).find((item) => Number(item.id) === state.jobId)
    if (!job) {
      setState(site, { status: 'finished', detail: '任务已离开运行队列，正在刷新数据' })
      window.clearTimeout(timers.get(site))
      timers.delete(site)
      await options.onDone?.(site, states[site])
      return
    }
    const status = normalizeStatus(job.status)
    setState(site, { status, detail: jobDetail(job) })
    if (TERMINAL.has(status)) {
      window.clearTimeout(timers.get(site))
      timers.delete(site)
      await options.onDone?.(site, states[site])
    }
  }

  function schedule(site: string, attempt = 0) {
    window.clearTimeout(timers.get(site))
    if (attempt > 120) {
      setState(site, { status: 'unknown', detail: '任务仍在队列中，可到任务页继续查看' })
      return
    }
    const delay = attempt < 5 ? 1500 : 3000
    const timer = window.setTimeout(async () => {
      try {
        await refreshJob(site)
      } catch (err) {
        setState(site, { status: 'unknown', detail: err instanceof Error ? err.message : String(err) })
      }
      if (!TERMINAL.has(states[site]?.status)) schedule(site, attempt + 1)
    }, delay)
    timers.set(site, timer)
  }

  async function trigger(site?: string | null) {
    const key = keyFor(site)
    if (!key) return null
    setState(key, { status: 'submitting', detail: '' })
    try {
      const result = await triggerJob({ site: key })
      const jobId = Number(result?.jobs?.[0])
      const reused = Array.isArray(result?.existing_jobs) && result.existing_jobs.some((id: unknown) => Number(id) === jobId)
      setState(key, {
        jobId: Number.isFinite(jobId) ? jobId : undefined,
        status: 'queued',
        detail: Number.isFinite(jobId)
          ? `${reused ? '已有任务' : '新任务'} · Job #${jobId}`
          : '等待 worker 领取',
      })
      if (Number.isFinite(jobId)) schedule(key)
      return states[key]
    } catch (err) {
      setState(key, { status: 'failed', detail: err instanceof Error ? err.message : String(err) })
      return states[key]
    }
  }

  function stateFor(site?: string | null) {
    return states[keyFor(site)]
  }

  function isBusy(site?: string | null) {
    const status = stateFor(site)?.status
    return ['submitting', 'queued', 'running', 'unknown'].includes(String(status))
  }

  function labelFor(site?: string | null, idle = '触发抓取') {
    const state = stateFor(site)
    if (!state || state.status === 'idle') return idle
    return state.message || idle
  }

  function classFor(site?: string | null) {
    const status = stateFor(site)?.status
    return status ? `trigger-${status}` : ''
  }

  function detailFor(site?: string | null) {
    const state = stateFor(site)
    if (!state || state.status === 'idle') return ''
    return state.detail || state.message
  }

  onUnmounted(() => {
    for (const timer of timers.values()) window.clearTimeout(timer)
    timers.clear()
  })

  return { states, trigger, stateFor, isBusy, labelFor, classFor, detailFor }
}
