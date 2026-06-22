<script setup lang="ts">
defineProps<{ status?: string | null }>()

function tone(status?: string | null) {
  const s = String(status || '').toLowerCase()
  if (['success', 'completed', 'done', 'ok', 'active', 'healthy', 'online'].includes(s)) return 'ok'
  if (['failed', 'error', 'dead', 'critical', 'blocked'].includes(s)) return 'bad'
  if (['running', 'processing'].includes(s)) return 'run'
  if (['stuck', 'warning', 'warn', 'reclaim', 'partial', 'skipped', 'stale_pending'].includes(s)) return 'warn'
  return 'idle'
}

function label(status?: string | null) {
  const s = String(status || '').toLowerCase()
  return (
    ({
      pending: '待处理',
      running: '采取中',
      processing: '采取中',
      stale_pending: '久排',
      success: '成功',
      completed: '完成',
      done: '完成',
      partial: '部分成功',
      failed: '失败',
      blocked: '阻断',
      skipped: '跳过',
      stuck: '卡住',
      warning: '警告',
      warn: '警告',
      idle: '空闲',
      online: '在线',
      offline: '离线',
      healthy: '健康'
    } as Record<string, string>)[s] || s || '—'
  )
}
</script>

<template>
  <span class="badge" :class="tone(status)">{{ label(status) }}</span>
</template>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.6;
  white-space: nowrap;
}

.ok {
  color: #16a34a;
  background: rgba(34, 197, 94, 0.15);
}

.bad {
  color: #ef4444;
  background: rgba(239, 68, 68, 0.15);
}

.run {
  color: #3b82f6;
  background: rgba(59, 130, 246, 0.15);
}

.warn {
  color: #f59e0b;
  background: rgba(245, 158, 11, 0.15);
}

.idle {
  color: #9ca3af;
  background: rgba(156, 163, 175, 0.15);
}
</style>
