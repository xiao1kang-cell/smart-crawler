<script setup lang="ts">
withDefaults(defineProps<{
  loading?: boolean
  hasData?: boolean
  label?: string
  skeletonRows?: number
  skeletonVariant?: 'list' | 'cards'
}>(), {
  loading: false,
  hasData: false,
  label: '正在更新数据',
  skeletonRows: 4,
  skeletonVariant: 'list',
})
</script>

<template>
  <div class="data-loading-panel" :class="{ 'is-loading': loading && hasData }" :aria-busy="loading ? 'true' : 'false'">
    <div v-if="loading && hasData" class="data-loading-overlay" role="status" aria-live="polite">
      <UProgress animation="carousel" color="primary" class="data-loading-progress" />
      <span class="data-loading-label">{{ label }}</span>
    </div>
    <div v-if="loading && !hasData" class="data-loading-skeleton" :class="`is-${skeletonVariant}`" role="status" aria-live="polite">
      <div v-for="i in skeletonRows" :key="i" class="data-loading-row">
        <USkeleton class="data-loading-avatar" />
        <div class="data-loading-copy">
          <USkeleton class="data-loading-line wide" />
          <USkeleton class="data-loading-line" />
        </div>
        <USkeleton class="data-loading-pill" />
      </div>
    </div>
    <slot />
  </div>
</template>

<style scoped>
.data-loading-panel { position:relative; }
.data-loading-panel.is-loading { cursor:progress; }
.data-loading-overlay { position:absolute; inset:0; z-index:8; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:8px; min-height:86px; background:rgba(255,255,255,.62); backdrop-filter:blur(2px); pointer-events:none; }
.data-loading-progress { position:absolute; inset:0 0 auto; height:2px; border-radius:0; }
.data-loading-label { display:inline-flex; align-items:center; padding:8px 12px; border:1px solid var(--ui-border); border-radius:999px; background:var(--ui-card); color:var(--ui-heading); font-size:.78rem; font-weight:800; box-shadow:0 10px 24px rgba(37,29,61,.16); }
.data-loading-skeleton { display:grid; gap:10px; padding:12px; }
.data-loading-row { display:grid; grid-template-columns:34px minmax(0,1fr) 72px; align-items:center; gap:12px; min-height:58px; border:1px solid var(--ui-border, rgba(148,163,184,.18)); border-radius:7px; padding:12px; background:var(--ui-card, rgba(255,255,255,.04)); }
.data-loading-copy { display:grid; gap:8px; }
.data-loading-avatar { width:34px; height:34px; border-radius:8px; }
.data-loading-line { width:56%; height:12px; border-radius:999px; }
.data-loading-line.wide { width:72%; }
.data-loading-pill { width:64px; height:22px; border-radius:999px; }
.data-loading-skeleton.is-cards { display:contents; padding:0; }
.data-loading-skeleton.is-cards .data-loading-row { grid-template-columns:minmax(0,1fr) 64px; grid-template-areas:"copy pill" "bar bar"; align-content:start; min-height:178px; border-radius:11px; padding:14px; box-shadow:0 14px 32px rgba(37,29,61,.10); }
.data-loading-skeleton.is-cards .data-loading-avatar { display:none; }
.data-loading-skeleton.is-cards .data-loading-copy { grid-area:copy; align-self:start; gap:10px; padding-top:2px; }
.data-loading-skeleton.is-cards .data-loading-line { width:64%; height:10px; }
.data-loading-skeleton.is-cards .data-loading-line.wide { width:86%; height:14px; }
.data-loading-skeleton.is-cards .data-loading-pill { grid-area:pill; align-self:start; justify-self:end; width:58px; height:22px; }
.data-loading-skeleton.is-cards .data-loading-row::after { content:""; grid-area:bar; align-self:end; height:5px; border-radius:999px; background:var(--ui-card-soft, rgba(148,163,184,.14)); }
:global(html[data-theme="dark"]) .data-loading-overlay { background:rgba(7,5,13,.56); }
:global(html[data-theme="dark"]) .data-loading-label { background:#15101f; border-color:#3d2d5a; color:#edf0fb; box-shadow:0 10px 24px rgba(0,0,0,.36); }
</style>
