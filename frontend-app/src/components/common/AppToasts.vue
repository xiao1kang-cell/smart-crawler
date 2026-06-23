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
