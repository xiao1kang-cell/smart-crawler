import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ToastTone = 'error' | 'success' | 'info' | 'warning'

export type AppToast = {
  id: number
  title: string
  description?: string
  tone: ToastTone
  timeout: number
}

export const useToastStore = defineStore('toast', () => {
  const items = ref<AppToast[]>([])
  let nextId = 1

  function dismiss(id: number) {
    items.value = items.value.filter((item) => item.id !== id)
  }

  function show(input: string | { title: string; description?: string; tone?: ToastTone; timeout?: number }) {
    const item: AppToast = {
      id: nextId++,
      title: typeof input === 'string' ? input : input.title,
      description: typeof input === 'string' ? undefined : input.description,
      tone: typeof input === 'string' ? 'info' : input.tone || 'info',
      timeout: typeof input === 'string' ? 3600 : input.timeout ?? 3600,
    }
    items.value = [...items.value, item].slice(-4)
    if (item.timeout > 0) window.setTimeout(() => dismiss(item.id), item.timeout)
    return item.id
  }

  function error(title: string, description?: string) {
    return show({ title, description, tone: 'error', timeout: 4600 })
  }

  function success(title: string, description?: string) {
    return show({ title, description, tone: 'success' })
  }

  return { items, show, error, success, dismiss }
})
