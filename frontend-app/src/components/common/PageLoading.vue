<script setup lang="ts">
import PathLoader from './PathLoader.vue'

withDefaults(defineProps<{
  title?: string
  note?: string
  compact?: boolean
}>(), {
  title: '加载中...',
  note: '',
  compact: false,
})
</script>

<template>
  <div class="page-loading" :class="{ compact }" role="status" aria-live="polite" :aria-label="note || title">
    <span class="sr-only">{{ note || title }}</span>
    <div class="page-loading-mark">
      <PathLoader :size="compact ? 54 : 72" :label="title" />
      <span v-if="note" class="page-loading-note">{{ note }}</span>
    </div>
  </div>
</template>

<style scoped>
.page-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 12px 0;
}
.page-loading.compact {
  margin: 8px 0;
}
.page-loading-mark {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: 100%;
  min-height: 132px;
  padding: 22px 16px;
  border: 1px solid rgba(216, 180, 254, .42);
  border-radius: 8px;
  background:
    radial-gradient(circle at 50% 20%, rgba(167, 139, 250, .10), transparent 46%),
    linear-gradient(180deg, rgba(255, 255, 255, .76), rgba(251, 249, 255, .90));
}
.page-loading.compact .page-loading-mark {
  min-height: 92px;
  padding: 16px 12px;
}
.page-loading-note {
  color: var(--ui-muted, #64748b);
  font-size: .74rem;
  font-weight: 700;
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
:global(html[data-theme="dark"] .page-loading-mark) {
  border-color: rgba(185, 148, 255, .30) !important;
  background-color: #100b1a !important;
  background:
    radial-gradient(circle at 50% 20%, rgba(185, 148, 255, .12), transparent 46%),
    linear-gradient(180deg, rgba(21, 16, 31, .88), rgba(16, 11, 26, .96)) !important;
}
:global(html[data-theme="dark"] .page-loading-note) {
  color: var(--ui-muted, #b5bfd2) !important;
}
</style>
