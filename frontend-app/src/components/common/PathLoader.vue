<script setup lang="ts">
withDefaults(defineProps<{
  size?: number
  label?: string
  compact?: boolean
}>(), {
  size: 44,
  label: '',
  compact: false,
})

const motionPath = 'M8 18 C22 8 34 8 48 18 S74 28 88 18'
</script>

<template>
  <span
    class="path-loader"
    :class="{ compact }"
    :style="{ '--loader-size': `${size}px` }"
    role="status"
    aria-live="polite"
    :aria-label="label || '加载中'"
  >
    <svg class="path-loader-svg" viewBox="0 0 96 36" aria-hidden="true">
      <path class="path-loader-rail" :d="motionPath" pathLength="100" />
      <path class="path-loader-stroke" :d="motionPath" pathLength="100" />
      <circle class="path-loader-dot path-loader-dot-trail" r="2.2">
        <animateMotion :path="motionPath" dur="1.45s" begin="-0.28s" repeatCount="indefinite" rotate="auto" />
      </circle>
      <circle class="path-loader-dot" r="3">
        <animateMotion :path="motionPath" dur="1.45s" repeatCount="indefinite" rotate="auto" />
      </circle>
    </svg>
    <span v-if="label && !compact" class="path-loader-label">{{ label }}</span>
  </span>
</template>

<style scoped>
.path-loader {
  --loader-size: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--ui-purple, #7c3aed);
}
.path-loader.compact {
  gap: 0;
}
.path-loader-svg {
  width: var(--loader-size);
  height: calc(var(--loader-size) * .38);
  overflow: visible;
}
.path-loader-rail,
.path-loader-stroke {
  fill: none;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.path-loader-rail {
  stroke: rgba(124, 58, 237, .14);
  stroke-width: 3.4;
}
.path-loader-stroke {
  stroke: currentColor;
  stroke-width: 3.4;
  stroke-dasharray: 18 82;
  animation: path-loader-run 1.45s cubic-bezier(.62, 0, .28, 1) infinite;
  filter: drop-shadow(0 4px 8px rgba(124, 58, 237, .16));
}
.path-loader-dot {
  fill: currentColor;
  stroke: currentColor;
  stroke-width: 1.8;
  filter: drop-shadow(0 3px 7px rgba(124, 58, 237, .20));
}
.path-loader-dot-trail {
  opacity: .32;
  stroke-width: 0;
}
.path-loader-label {
  color: var(--ui-heading, #0f172a);
  font-size: .78rem;
  font-weight: 850;
  white-space: nowrap;
}
@keyframes path-loader-run {
  0% { stroke-dashoffset: 100; }
  100% { stroke-dashoffset: 0; }
}
:global(html[data-theme="dark"] .path-loader) {
  color: var(--ui-purple, #b994ff);
}
:global(html[data-theme="dark"] .path-loader-rail) {
  stroke: rgba(185, 148, 255, .18);
}
</style>
