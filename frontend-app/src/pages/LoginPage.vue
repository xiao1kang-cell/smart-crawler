<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const busy = ref('')
const error = ref('')
const mode = ref<'login' | 'register'>('login')
const loginForm = ref({ identifier: '', password: '' })
const registerForm = ref({ invite_code: '', username: '', email: '', display_name: '', password: '', confirm_password: '' })

function redirectAfterAuth() {
  const redirect = route.query.redirect
  if (typeof redirect === 'string' && redirect.startsWith('/') && !redirect.startsWith('//')) {
    return redirect
  }
  return '/app/overview'
}

async function submitLogin() {
  busy.value = 'login'
  error.value = ''
  try {
    await auth.login({ username: loginForm.value.identifier, password: loginForm.value.password })
    router.push(redirectAfterAuth())
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = ''
  }
}

async function submitRegister() {
  busy.value = 'register'
  error.value = ''
  try {
    await auth.register(registerForm.value)
    router.push(redirectAfterAuth())
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <main class="login-wrap">
    <section class="login-card">
      <div class="login-brand">⚡ smart-crawler</div>
      <div class="login-sub">遨森竞品情报 · 授权访问</div>
      <div class="login-tabs">
        <button class="login-tab" :class="{ active: mode === 'login' }" @click="mode = 'login'">登录</button>
        <button class="login-tab" :class="{ active: mode === 'register' }" @click="mode = 'register'">注册</button>
      </div>

      <template v-if="mode === 'login'">
        <div class="login-field">
          <label>邮箱或用户名</label>
          <input v-model="loginForm.identifier" placeholder="请输入邮箱或用户名" autocomplete="username" @keyup.enter="submitLogin" />
        </div>
        <div class="login-field">
          <label>密码</label>
          <input v-model="loginForm.password" type="password" placeholder="密码" autocomplete="current-password" @keyup.enter="submitLogin" />
        </div>
        <button class="login-btn" :disabled="busy === 'login'" @click="submitLogin">{{ busy === 'login' ? '登录中…' : '登录' }}</button>
      </template>

      <template v-else>
        <div class="login-field"><label>用户名</label><input v-model="registerForm.username" placeholder="3-32 位用户名" /></div>
        <div class="login-field"><label>邮箱</label><input v-model="registerForm.email" placeholder="请输入邮箱" /></div>
        <div class="login-field"><label>显示名</label><input v-model="registerForm.display_name" placeholder="可选" /></div>
        <div class="login-field"><label>邀请码</label><input v-model="registerForm.invite_code" placeholder="内部邀请码" /></div>
        <div class="login-field"><label>密码</label><input v-model="registerForm.password" type="password" placeholder="至少 8 位，含字母和数字" /></div>
        <div class="login-field">
          <label>确认密码</label>
          <input v-model="registerForm.confirm_password" type="password" placeholder="再次输入密码" @keyup.enter="submitRegister" />
        </div>
        <button class="login-btn" :disabled="busy === 'register'" @click="submitRegister">{{ busy === 'register' ? '注册中…' : '注册并登录' }}</button>
        <div class="login-note">邀请码只能由内部管理员生成。注册后可在账号页创建自己的接口密钥。</div>
      </template>
      <div class="login-err">{{ error }}</div>
    </section>
  </main>
</template>
