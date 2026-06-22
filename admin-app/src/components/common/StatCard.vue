<script setup lang="ts">
defineProps<{
  label: string
  value: string | number
  note?: string
}>()
</script>

<template>
  <div class="stat-card">
    <p class="stat-label">{{ label }}</p>
    <p class="stat-value">{{ value }}</p>
    <p v-if="note" class="stat-note">{{ note }}</p>
  </div>
</template>

<style scoped>
.stat-card {
  height: 100%;
  min-height: 94px;
  padding: 14px 16px;
  border-radius: var(--admin-radius, 8px);
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.08));
  background: var(--admin-panel, var(--ui-bg-elevated, rgba(255, 255, 255, 0.03)));
  box-shadow: var(--admin-shadow, none);
  min-width: 0;
}

.stat-label {
  color: var(--ui-muted, #64748b);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.35;
  opacity: 1;
}

.stat-value {
  margin-top: 10px;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 25px;
  font-weight: 700;
  line-height: 1.1;
  letter-spacing: 0;
}

.stat-note {
  margin-top: 7px;
  color: var(--ui-muted, #64748b);
  font-size: 12px;
  line-height: 1.35;
  opacity: 1;
}
</style>
