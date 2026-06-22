<script setup lang="ts">
import { ref } from 'vue'
import { influencerFull } from '../api/influencers'
import PageLoading from '../components/common/PageLoading.vue'

const form = ref({ platform: 'instagram', username: 'cristiano' })
const data = ref<Record<string, any> | null>(null)
const loading = ref(false)
const error = ref('')
const quickTargets = [
  { platform: 'instagram', username: 'cristiano', label: 'Cristiano', meta: 'Instagram' },
  { platform: 'youtube', username: 'MrBeast', label: 'MrBeast', meta: 'YouTube' },
  { platform: 'tiktok', username: 'khaby.lame', label: 'Khaby Lame', meta: 'TikTok' },
  { platform: 'twitter', username: 'elonmusk', label: 'Elon Musk', meta: 'X' }
]
const platformItems = [
  { label: 'Instagram', value: 'instagram' },
  { label: 'TikTok', value: 'tiktok' },
  { label: 'YouTube', value: 'youtube' },
  { label: 'Twitter / X', value: 'twitter' },
]

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await influencerFull({ platform: form.value.platform, username: form.value.username, posts_limit: 12 })
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function setTarget(item: Record<string, string>) {
  form.value.platform = item.platform
  form.value.username = item.username
}
</script>

<template>
  <section>
    <div class="lead">🌟 多平台红人采集</div>
    <div class="sub">替代第三方红人采集服务 · 支持 Instagram、TikTok、YouTube、X · 输入账号名直接抓取</div>

    <div class="inf-workbench">
      <div class="inf-panel">
        <h3>账号采集</h3>
        <div class="inf-form">
          <USelect v-model="form.platform" class="inf-select" :items="platformItems" value-key="value" />
          <input v-model="form.username" class="inf-inp" placeholder="账号名（不带 @）" @keyup.enter="load" />
          <button class="btn-prim" :disabled="loading" @click="load">{{ loading ? '采集中…' : '🚀 抓取' }}</button>
        </div>
        <div class="inf-empty-note">当前目标：{{ form.platform }} / @{{ form.username || '—' }}</div>
      </div>
      <div class="inf-panel">
        <h3>常用账号</h3>
        <div class="inf-quick-grid">
          <button v-for="item in quickTargets" :key="item.platform + item.username" class="inf-chip" @click="setTarget(item)">
            <b>{{ item.label }}</b>
            <span>{{ item.meta }} · @{{ item.username }}</span>
          </button>
        </div>
      </div>
    </div>

    <div v-if="error" class="inf-err">❌ {{ error }}</div>

    <PageLoading v-if="loading && !data" title="采集红人数据..." note="正在读取公开资料、联系方式和近期帖子" />

    <div v-else-if="data?.profile" class="inf-card">
      <div class="inf-hero">
        <img v-if="data.profile.avatar_url" :src="data.profile.avatar_url" class="inf-avatar" referrerpolicy="no-referrer" />
        <div class="inf-meta">
          <div class="inf-name">
            {{ data.profile.display_name || data.profile.username }}
            <span v-if="data.profile.is_verified" class="b ok">✓ 认证</span>
            <span v-if="data.profile.is_business" class="b warn">商业</span>
          </div>
          <div class="inf-handle">@{{ data.profile.username }} · <a :href="data.profile.raw_url" target="_blank" rel="noopener">平台主页 ↗</a></div>
          <div class="inf-bio">{{ data.profile.bio || '—' }}</div>
        </div>
      </div>
      <div class="inf-stats">
        <div class="stat"><div class="lbl">粉丝</div><div class="val">{{ (data.profile.followers || 0).toLocaleString() }}</div></div>
        <div class="stat"><div class="lbl">关注</div><div class="val">{{ (data.profile.following || 0).toLocaleString() }}</div></div>
        <div class="stat"><div class="lbl">帖子</div><div class="val">{{ (data.profile.posts_count || 0).toLocaleString() }}</div></div>
        <div v-if="data.profile.likes_total" class="stat"><div class="lbl">总点赞</div><div class="val">{{ data.profile.likes_total.toLocaleString() }}</div></div>
        <div class="stat"><div class="lbl">分类</div><div class="val small">{{ data.profile.category || '—' }}</div></div>
        <div class="stat"><div class="lbl">采集源</div><div class="val small">{{ data.profile.fetched_via || '—' }}</div></div>
      </div>
      <div class="inf-contact">
        <h4>📬 联系方式</h4>
        <div>📧 邮箱：<b>{{ data.profile.contact?.email || '—' }}</b></div>
        <div>📱 WhatsApp：<b>{{ data.profile.contact?.whatsapp || '—' }}</b></div>
        <div>🌐 主页链接：<b><a v-if="data.profile.external_url" :href="data.profile.external_url" target="_blank" rel="noopener">{{ data.profile.external_url }}</a><span v-else>—</span></b></div>
        <div>🔗 linktree：<b>{{ data.profile.contact?.linktree || '—' }}</b></div>
      </div>
      <div v-if="data.posts && data.posts.length" class="inf-posts">
        <h4>📝 近期 {{ data.posts.length }} 条帖子</h4>
        <div class="post-grid">
          <a v-for="p in data.posts" :key="p.post_id" :href="p.post_url" target="_blank" rel="noopener" class="post">
            <img v-if="p.thumbnail_url" :src="p.thumbnail_url" referrerpolicy="no-referrer" />
            <div class="post-info">
              <div class="cap">{{ (p.caption || '').slice(0, 100) }}</div>
              <div class="metrics"><span v-if="p.likes">❤️ {{ p.likes.toLocaleString() }}</span><span v-if="p.comments">💬 {{ p.comments.toLocaleString() }}</span><span v-if="p.views">👁 {{ p.views.toLocaleString() }}</span></div>
            </div>
          </a>
        </div>
      </div>
    </div>
  </section>
</template>
