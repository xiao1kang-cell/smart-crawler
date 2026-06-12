<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const busy = ref(false)
const error = ref('')
const form = ref({ username: '', password: '' })

async function submit() {
  busy.value = true
  error.value = ''
  try {
    await auth.login({ username: form.value.username, password: form.value.password })
    await auth.loadMe()
    if (auth.user?.global_role !== 'super_admin') {
      auth.clear()
      throw new Error('该账号无管理后台权限')
    }
    router.push('/')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="login-wrap">
    <section class="login-card">
      <div class="login-brand">⚡ smart-crawler</div>
      <div class="login-sub">管理后台 · 超级管理员</div>

      <div class="login-field">
        <label>用户名</label>
        <input
          v-model="form.username"
          placeholder="请输入用户名"
          autocomplete="username"
          @keyup.enter="submit"
        />
      </div>
      <div class="login-field">
        <label>密码</label>
        <input
          v-model="form.password"
          type="password"
          placeholder="密码"
          autocomplete="current-password"
          @keyup.enter="submit"
        />
      </div>
      <button class="login-btn" :disabled="busy" @click="submit">
        {{ busy ? '登录中…' : '登录' }}
      </button>
      <div v-if="error" class="login-err">{{ error }}</div>
    </section>
  </main>
</template>

<style scoped>
.login-wrap {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 24px;
}

.login-card {
  width: 100%;
  max-width: 360px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 32px;
  border-radius: 16px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.1));
  background: var(--ui-bg-elevated, rgba(255, 255, 255, 0.03));
}

.login-brand {
  font-size: 22px;
  font-weight: 700;
}

.login-sub {
  font-size: 13px;
  opacity: 0.6;
  margin-bottom: 8px;
}

.login-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.login-field label {
  font-size: 13px;
  opacity: 0.8;
}

.login-field input {
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--ui-border, rgba(255, 255, 255, 0.12));
  background: var(--ui-bg, rgba(0, 0, 0, 0.2));
  color: inherit;
  font-size: 14px;
}

.login-field input:focus {
  outline: none;
  border-color: var(--ui-color-primary-500, #6366f1);
}

.login-btn {
  margin-top: 8px;
  padding: 11px;
  border-radius: 8px;
  border: none;
  cursor: pointer;
  font-size: 14px;
  font-weight: 600;
  color: #fff;
  background: var(--ui-color-primary-500, #6366f1);
}

.login-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.login-err {
  font-size: 13px;
  color: #ef4444;
}
</style>
