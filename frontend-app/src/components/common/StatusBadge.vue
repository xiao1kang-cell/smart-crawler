<script setup lang="ts">
defineProps<{ status?: string | null }>()

function tone(status?: string | null) {
  const s = String(status || '').toLowerCase()
  if (['ok', 'success', 'completed', 'active', 'done', 'on_sale', 'healthy'].includes(s)) return 'ok'
  if (['running', 'queued', 'pending', 'processing', 'warning'].includes(s)) return 'warn'
  if (['failed', 'error', 'disabled', 'out_of_stock', 'critical', 'blocked'].includes(s)) return 'bad'
  return 'pending'
}

function label(status?: string | null) {
  const s = String(status || '').toLowerCase()
  return ({
    success: '成功',
    completed: '完成',
    done: '完成',
    running: '采取中',
    processing: '采取中',
    pending: '待处理',
    queued: '排队中',
    failed: '失败',
    error: '错误',
    blocked: '阻断',
    skipped: '跳过',
    partial: '部分成功',
    active: '启用',
    disabled: '停用',
    healthy: '健康',
    warning: '警告',
    on_sale: '在售',
    out_of_stock: '缺货'
  } as Record<string, string>)[s] || status || '—'
}
</script>

<template>
  <span class="pill" :class="tone(status)">{{ label(status) }}</span>
</template>
