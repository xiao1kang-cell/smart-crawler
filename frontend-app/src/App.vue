<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AppToasts from './components/common/AppToasts.vue'

const isDark = ref(false)

function syncThemeState() {
  isDark.value = document.documentElement.getAttribute('data-theme') === 'dark'
}

function toggleTheme() {
  const nextDark = !isDark.value
  if (nextDark) document.documentElement.setAttribute('data-theme', 'dark')
  else document.documentElement.removeAttribute('data-theme')
  try {
    localStorage.setItem('sc_theme', nextDark ? 'dark' : 'light')
  } catch {
    // Ignore storage failures; the current page still updates.
  }
  syncThemeState()
}

onMounted(syncThemeState)
</script>

<template>
  <UApp>
    <RouterView />
    <AppToasts />
    <button id="theme-toggle" type="button" aria-label="切换主题" @click="toggleTheme">
      <span class="ico">{{ isDark ? '☀️' : '🌙' }}</span>
      <span class="txt">{{ isDark ? '浅色' : '暗色' }}</span>
    </button>
  </UApp>
</template>
