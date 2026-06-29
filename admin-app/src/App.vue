<script setup lang="ts">
import AppToasts from './components/common/AppToasts.vue'
</script>

<template>
  <UApp>
    <RouterView />
    <AppToasts />
  </UApp>
</template>
