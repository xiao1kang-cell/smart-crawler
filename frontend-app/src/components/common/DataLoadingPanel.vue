<script setup lang="ts">
import PathLoader from './PathLoader.vue'

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
      <PathLoader :size="52" :label="label" />
    </div>
    <div v-if="loading && !hasData" class="data-loading-empty" role="status" aria-live="polite">
      <div class="data-loading-mark">
        <PathLoader :size="64" :label="label" />
      </div>
    </div>
    <slot />
  </div>
</template>

<style scoped>
.data-loading-panel { position:relative; }
.data-loading-panel.is-loading { cursor:progress; }
.data-loading-overlay { position:absolute; inset:0; z-index:8; display:flex; align-items:center; justify-content:center; min-height:86px; background:rgba(255,255,255,.68); backdrop-filter:blur(3px); pointer-events:none; }
.data-loading-empty { display:flex; align-items:center; justify-content:center; grid-column:1 / -1; width:100%; padding:12px; }
.data-loading-mark { display:flex; align-items:center; justify-content:center; width:100%; min-height:128px; border:1px solid rgba(216,180,254,.42); border-radius:8px; background:radial-gradient(circle at 50% 20%,rgba(167,139,250,.10),transparent 46%),linear-gradient(180deg,rgba(255,255,255,.76),rgba(251,249,255,.90)); }
:global(html[data-theme="dark"] .data-loading-overlay) { background:rgba(7,5,13,.72) !important; }
:global(html[data-theme="dark"] .data-loading-empty) { background:transparent !important; }
:global(html[data-theme="dark"] .data-loading-mark) { border-color:rgba(185,148,255,.30) !important; background-color:#100b1a !important; background:radial-gradient(circle at 50% 20%,rgba(185,148,255,.12),transparent 46%),linear-gradient(180deg,rgba(21,16,31,.88),rgba(16,11,26,.96)) !important; }
</style>
