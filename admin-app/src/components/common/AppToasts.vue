<script setup lang="ts">
import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from 'lucide-vue-next'
import { useToastStore, type ToastTone } from '../../stores/toast'

const toast = useToastStore()

const icons: Record<ToastTone, unknown> = {
  error: CircleAlert,
  success: CircleCheck,
  warning: TriangleAlert,
  info: Info,
}
</script>

<template>
  <Teleport to="body">
    <div class="app-toast-stack" aria-live="polite" aria-relevant="additions">
      <TransitionGroup name="app-toast">
        <div
          v-for="item in toast.items"
          :key="item.id"
          class="app-toast-item"
          :class="`is-${item.tone}`"
          role="status"
        >
          <component :is="icons[item.tone]" class="app-toast-icon" :size="18" stroke-width="2.3" />
          <div class="app-toast-copy">
            <div class="app-toast-title">{{ item.title }}</div>
            <div v-if="item.description" class="app-toast-desc">{{ item.description }}</div>
          </div>
          <button class="app-toast-close" type="button" aria-label="关闭提示" @click="toast.dismiss(item.id)">
            <X :size="16" stroke-width="2.4" />
          </button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.app-toast-stack {
  position: fixed;
  top: 76px;
  right: 18px;
  z-index: 1200;
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: min(380px, calc(100vw - 28px));
  pointer-events: none;
}

.app-toast-item {
  pointer-events: auto;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: flex-start;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--ui-border);
  border-left-width: 4px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.96);
  color: var(--ui-text);
  box-shadow: 0 18px 46px rgba(15, 23, 42, 0.18);
  backdrop-filter: blur(12px);
}

.app-toast-icon {
  margin-top: 1px;
  flex: 0 0 auto;
}

.app-toast-copy {
  min-width: 0;
}

.app-toast-title {
  color: var(--ui-text);
  font-size: 0.88rem;
  font-weight: 800;
  line-height: 1.35;
  word-break: break-word;
}

.app-toast-desc {
  margin-top: 3px;
  color: var(--ui-muted);
  font-size: 0.76rem;
  line-height: 1.45;
  word-break: break-word;
}

.app-toast-close {
  width: 24px;
  height: 24px;
  border: 0;
  border-radius: 6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--ui-muted);
  background: transparent;
  cursor: pointer;
}

.app-toast-close:hover {
  background: var(--admin-control-hover);
  color: var(--ui-text);
}

.app-toast-item.is-error {
  border-left-color: #ef4444;
}

.app-toast-item.is-error .app-toast-icon {
  color: #dc2626;
}

.app-toast-item.is-success {
  border-left-color: #10b981;
}

.app-toast-item.is-success .app-toast-icon {
  color: #059669;
}

.app-toast-item.is-warning {
  border-left-color: #f59e0b;
}

.app-toast-item.is-warning .app-toast-icon {
  color: #d97706;
}

.app-toast-item.is-info {
  border-left-color: var(--ui-color-primary-500, #6366f1);
}

.app-toast-item.is-info .app-toast-icon {
  color: var(--ui-color-primary-500, #6366f1);
}

.app-toast-enter-active,
.app-toast-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.app-toast-enter-from,
.app-toast-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}

.app-toast-move {
  transition: transform 0.18s ease;
}

html[data-theme='dark'] .app-toast-item {
  background: rgba(15, 23, 42, 0.96);
  box-shadow: 0 18px 46px rgba(0, 0, 0, 0.46);
}

html[data-theme='dark'] .app-toast-item.is-error .app-toast-icon {
  color: #fca5a5;
}

html[data-theme='dark'] .app-toast-item.is-success .app-toast-icon {
  color: #86efac;
}

html[data-theme='dark'] .app-toast-item.is-warning .app-toast-icon {
  color: #fcd34d;
}

@media (max-width: 720px) {
  .app-toast-stack {
    top: 12px;
    right: 14px;
  }
}
</style>
