<script setup lang="ts">
withDefaults(defineProps<{
  title?: string
  note?: string
  compact?: boolean
  rows?: number
}>(), {
  title: '加载中...',
  note: '',
  compact: false,
  rows: 4,
})
</script>

<template>
  <div class="page-skeleton" :class="{ compact }" role="status" aria-live="polite" :aria-label="note || title">
    <span class="sr-only">{{ note || title }}</span>

    <template v-if="compact">
      <div v-for="i in rows" :key="i" class="sk-row">
        <i class="sk-avatar" />
        <div class="sk-copy">
          <i class="sk-line w-lg" />
          <i class="sk-line w-md" />
        </div>
        <i class="sk-pill" />
      </div>
    </template>

    <template v-else>
      <div class="sk-stat-grid">
        <div v-for="i in 3" :key="`stat-${i}`" class="sk-card">
          <i class="sk-line w-sm" />
          <i class="sk-line w-xl tall" />
          <i class="sk-line w-md" />
        </div>
      </div>
      <div class="sk-panel">
        <i class="sk-line w-lg" />
        <i class="sk-line w-full tall" />
        <i class="sk-line w-full" />
        <i class="sk-line w-xxl" />
      </div>
    </template>
  </div>
</template>

<style scoped>
.page-skeleton {
  display: grid;
  gap: 14px;
  margin: 12px 0;
}
.page-skeleton.compact {
  gap: 10px;
  margin: 8px 0;
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
.sk-stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
}
.sk-card,
.sk-panel,
.sk-row {
  border: 1px solid var(--ui-border, rgba(148, 163, 184, .18));
  background: var(--ui-card, rgba(255, 255, 255, .04));
}
.sk-card {
  min-height: 112px;
  border-radius: 8px;
  padding: 16px;
}
.sk-panel {
  min-height: 150px;
  border-radius: 8px;
  padding: 18px;
}
.sk-row {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) 72px;
  align-items: center;
  gap: 12px;
  min-height: 58px;
  border-radius: 7px;
  padding: 12px;
}
.sk-copy {
  display: grid;
  gap: 8px;
}
.sk-avatar,
.sk-pill,
.sk-line {
  display: block;
  overflow: hidden;
  position: relative;
  background: linear-gradient(90deg, rgba(148, 163, 184, .12), rgba(148, 163, 184, .26), rgba(148, 163, 184, .12));
  background-size: 220% 100%;
  animation: skeleton-shimmer 1.15s ease-in-out infinite;
}
.sk-avatar {
  width: 34px;
  height: 34px;
  border-radius: 8px;
}
.sk-pill {
  width: 64px;
  height: 22px;
  border-radius: 999px;
}
.sk-line {
  height: 12px;
  border-radius: 999px;
}
.sk-line.tall {
  height: 26px;
}
.w-sm { width: 38%; }
.w-md { width: 56%; }
.w-lg { width: 72%; }
.w-xl { width: 48%; }
.w-xxl { width: 86%; }
.w-full { width: 100%; }
@keyframes skeleton-shimmer {
  0% { background-position: 120% 0; }
  100% { background-position: -120% 0; }
}
</style>
