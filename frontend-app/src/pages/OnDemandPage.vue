<script setup lang="ts">
import { RefreshCw, Trash2 } from 'lucide-vue-next'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { asList, fmtNumber } from '../api/client'
import { batchOndemand, clearOndemandJobs, deleteOndemandJob, fetchOndemand, getOndemandJob, listOndemandJobs, retryOndemandJob } from '../api/ondemand'
import StatusBadge from '../components/common/StatusBadge.vue'

const form = ref({ url: '', max_items: 20, review_limit: 2000, batchText: '' })
const jobs = ref<Record<string, any>[]>([])
const selectedJob = ref<Record<string, any> | null>(null)
const showDetail = ref(false)
const showBatch = ref(false)
const reviewFilter = ref(0)
const reviewPage = ref(1)
const loading = ref(false)
const busy = ref('')
const error = ref('')
const message = ref('')
let timer: number | undefined
const maxBatch = 1000

const detailJob = computed(() => selectedJob.value?.job || selectedJob.value || null)
const detailListings = computed<Record<string, any>[]>(() => selectedJob.value?.listings || selectedJob.value?.products || [])
const detailReviews = computed<Record<string, any>[]>(() => selectedJob.value?.reviews || [])
const hasPendingJobs = computed(() => jobs.value.some((job) => job.status === 'queued' || job.status === 'running'))
const batchUrls = computed(() => form.value.batchText.split(/\r?\n/).map((x) => x.trim()).filter(Boolean))
const reviewStats = computed(() => {
  const dist: Record<number, number> = { 1: 0, 2: 0, 3: 0, 4: 0, 5: 0 }
  let sum = 0
  let rated = 0
  for (const row of detailReviews.value) {
    const rating = Math.round(Number(row.rating || 0))
    if (rating < 1 || rating > 5) continue
    dist[rating] += 1
    sum += rating
    rated += 1
  }
  const total = detailReviews.value.length
  return { total, dist, max: Math.max(1, ...Object.values(dist)), avg: rated ? sum / rated : 0 }
})
const filteredReviews = computed(() => {
  if (!reviewFilter.value) return detailReviews.value
  return detailReviews.value.filter((row) => Math.round(Number(row.rating || 0)) === reviewFilter.value)
})
const reviewPages = computed(() => Math.max(1, Math.ceil(filteredReviews.value.length / 20)))
const reviewPageItems = computed(() => filteredReviews.value.slice((reviewPage.value - 1) * 20, reviewPage.value * 20))
const jobColumns = [
  { accessorKey: 'created_at', header: '创建时间' },
  { accessorKey: 'finished_at', header: '完成时间' },
  { accessorKey: 'platform', header: '平台' },
  { accessorKey: 'url', header: 'URL' },
  { accessorKey: 'listing_count', header: 'listing' },
  { accessorKey: 'review_count', header: '评论' },
  { accessorKey: 'status', header: '状态' },
  { id: 'actions', header: '操作' },
]
const listingColumns = [
  { accessorKey: 'sku', header: 'SKU' },
  { accessorKey: 'title', header: '标题' },
  { accessorKey: 'sale_price', header: '售价' },
  { accessorKey: 'original_price', header: '原价' },
]

async function load(options: { silent?: boolean } = {}) {
  if (!options.silent) loading.value = true
  try {
    mergeJobs(asList(await listOndemandJobs({ page_size: 50 }), ['jobs', 'items']))
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    if (!options.silent) loading.value = false
  }
}

function mergeJobs(incoming: Record<string, any>[]) {
  const byId = new Map(jobs.value.map((job) => [job.id, job]))
  const nextIds = new Set(incoming.map((job) => job.id))
  for (let index = jobs.value.length - 1; index >= 0; index -= 1) {
    if (!nextIds.has(jobs.value[index].id)) jobs.value.splice(index, 1)
  }
  incoming.forEach((nextJob, index) => {
    const existing = byId.get(nextJob.id)
    if (existing) Object.assign(existing, nextJob)
    else jobs.value.splice(Math.min(index, jobs.value.length), 0, nextJob)
  })
}

async function guarded(label: string, fn: () => Promise<void>) {
  busy.value = label
  error.value = ''
  message.value = ''
  try {
    await fn()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = ''
  }
}

async function submitOne() {
  await guarded('fetch', async () => {
    if (!form.value.url.trim()) throw new Error('请填写 URL')
    await fetchOndemand({ url: form.value.url, max_items: form.value.max_items, review_limit: form.value.review_limit })
    form.value.url = ''
    message.value = '按需抓取已提交'
    await load()
  })
}

async function submitBatch() {
  const urls = batchUrls.value
  await guarded('batch', async () => {
    if (!urls.length) throw new Error('请粘贴或上传至少一条 URL')
    if (urls.length > maxBatch) throw new Error(`单批最多 ${maxBatch} 条，当前 ${urls.length} 条`)
    await batchOndemand({ urls, max_items: form.value.max_items, review_limit: form.value.review_limit })
    form.value.batchText = ''
    message.value = '批量任务已提交'
    await load()
  })
}

function onBatchFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => {
    const lines = String(reader.result || '').split(/\r?\n/).map((line) => line.split(',')[0].trim()).filter(Boolean)
    form.value.batchText = lines.join('\n')
  }
  reader.readAsText(file)
  input.value = ''
}

async function openJob(job: Record<string, any>) {
  await guarded('detail', async () => {
    selectedJob.value = await getOndemandJob(job.id)
    reviewFilter.value = 0
    reviewPage.value = 1
    showDetail.value = true
  })
}

function closeDetail() {
  showDetail.value = false
  selectedJob.value = null
}

function setReviewFilter(star: number) {
  reviewFilter.value = star
  reviewPage.value = 1
}

function stars(rating: unknown) {
  const n = Math.max(0, Math.min(5, Math.round(Number(rating || 0))))
  return '★★★★★'.slice(0, n) + '☆☆☆☆☆'.slice(0, 5 - n)
}

async function retry(job: Record<string, any>) {
  await guarded('retry', async () => {
    await retryOndemandJob(job.id)
    await load()
  })
}

async function remove(job: Record<string, any>) {
  if (!window.confirm('删除这条抓取记录？不会删除已入库的商品/评论数据')) return
  await guarded('delete', async () => {
    await deleteOndemandJob(job.id)
    await load()
  })
}

async function clearAll() {
  if (!window.confirm('清空本工作区的全部抓取历史？不会删除已入库的商品/评论数据')) return
  await guarded('clear', async () => {
    await clearOndemandJobs()
    await load()
  })
}

onMounted(() => {
  load()
  timer = window.setInterval(() => {
    if (hasPendingJobs.value) load({ silent: true })
  }, 5000)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <section>
    <div class="lead">按需抓取</div>
    <div class="sub">指定 URL → listing + VOC（美客多 / Lazada / 虾皮）</div>
    <UAlert v-if="error" color="error" variant="soft" :title="error" />
    <UAlert v-if="message" color="success" variant="soft" :title="message" />

    <div class="inf-panel od-fetch-panel">
      <div class="od-fetch-row">
        <div class="inf-form od-fetch-form">
          <input v-model="form.url" class="inf-inp od-url-input" placeholder="粘贴商品页或店铺/类目页 URL" @keyup.enter="submitOne" />
          <label class="od-limit-label">列表上限<input v-model.number="form.max_items" class="inf-inp od-limit-input" type="number" title="列表枚举上限" /></label>
          <label class="od-limit-label">评论上限<input v-model.number="form.review_limit" class="inf-inp od-limit-input" type="number" title="评论抓取上限（美客多约可达 1500）" /></label>
          <button class="btn-prim" :disabled="busy === 'fetch'" @click="submitOne">{{ busy === 'fetch' ? '提交中…' : '开始抓取' }}</button>
        </div>
        <button class="btn-prim btn-soft no-wrap-btn od-batch-btn" @click="showBatch = true">批量抓取</button>
      </div>
      <div v-if="hasPendingJobs" class="inf-empty-note pending-note">
        ⚙️ 有 {{ jobs.filter((j) => j.status === 'queued' || j.status === 'running').length }} 个任务进行中…
      </div>
    </div>

    <div v-if="showBatch" class="od-modal" @click.self="showBatch = false">
      <div class="od-modal-card">
        <div class="od-modal-head">
          <h3 style="margin:0">批量抓取（最多 {{ maxBatch }} 条）</h3>
          <button class="btn-prim btn-plain" @click="showBatch = false">✕</button>
        </div>
        <textarea v-model="form.batchText" class="inf-inp" placeholder="每行粘贴一个商品/店铺/类目 URL，或选择 .txt/.csv 文件导入" style="width:100%;min-height:160px;resize:vertical;font-family:inherit"></textarea>
        <div class="inf-form" style="flex-wrap:wrap;margin-top:10px;align-items:center">
          <input type="file" accept=".txt,.csv" style="font-size:0.8rem" @change="onBatchFile" />
          <span class="inf-empty-note" style="margin:0">已识别 {{ batchUrls.length }} 条</span>
          <button class="btn-prim" :disabled="busy === 'batch' || !batchUrls.length || batchUrls.length > maxBatch" @click="submitBatch">{{ busy === 'batch' ? '提交中…' : '批量抓取' }}</button>
        </div>
      </div>
    </div>

    <div class="inf-panel" style="margin-top:16px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <h3>抓取历史</h3>
        <div style="display:flex;gap:8px">
          <button class="btn-prim btn-soft no-wrap-btn" :disabled="loading" @click="load()"><RefreshCw class="size-4" /><span>刷新</span></button>
          <button v-if="jobs.length" class="btn-prim btn-danger no-wrap-btn" @click="clearAll"><Trash2 class="size-4" /><span>清空历史</span></button>
        </div>
      </div>
      <div v-if="loading" class="inf-empty-note">加载中…</div>
      <div v-else-if="!jobs.length" class="inf-empty-note">暂无抓取记录</div>
      <UTable v-else class="od-table ui-table" :data="jobs" :columns="jobColumns" sticky="header" empty="暂无抓取记录">
        <template #created_at-cell="{ row }">{{ (row.original.created_at || '').replace('T', ' ').slice(0, 16) }}</template>
        <template #finished_at-cell="{ row }">{{ row.original.finished_at ? row.original.finished_at.replace('T', ' ').slice(0, 16) : '—' }}</template>
        <template #platform-cell="{ row }">{{ row.original.platform || row.original.marketplace }}</template>
        <template #url-cell="{ row }"><button class="table-link url-cell" @click="openJob(row.original)">{{ row.original.url }}</button></template>
        <template #listing_count-cell="{ row }">{{ fmtNumber(row.original.listing_count || row.original.product_count || row.original.products_count || row.original.items_count) }}</template>
        <template #review_count-cell="{ row }">{{ fmtNumber(row.original.review_count || row.original.reviews_count) }}</template>
        <template #status-cell="{ row }"><StatusBadge :status="row.original.status" /></template>
        <template #actions-cell="{ row }">
          <div class="actions-cell">
            <button v-if="row.original.status === 'failed' || row.original.status === 'partial'" class="btn-prim btn-mini btn-retry" @click.stop="retry(row.original)">重试</button>
            <button class="btn-prim btn-mini btn-danger" @click.stop="remove(row.original)">删除</button>
          </div>
        </template>
      </UTable>
    </div>

    <div v-if="showDetail" class="od-modal" @click.self="closeDetail">
      <div class="od-modal-card" style="max-width:760px">
        <div class="od-modal-head">
          <h3 style="margin:0">抓取详情</h3>
          <button class="btn-prim btn-plain" @click="closeDetail">✕</button>
        </div>
        <div v-if="busy === 'detail'" class="inf-empty-note">加载详情中…</div>
        <div v-else-if="selectedJob">
          <ul v-if="detailJob?.notes && detailJob.notes.length" style="margin:0 0 8px;padding-left:18px;color:var(--ui-muted);font-size:0.8rem">
            <li v-for="(note, index) in detailJob.notes" :key="index">{{ note }}</li>
          </ul>

          <UTable v-if="detailListings.length" class="od-table ui-table detail-listing-table" :data="detailListings" :columns="listingColumns" empty="暂无 listing">
            <template #sku-cell="{ row }">{{ row.original.sku || row.original.item_id }}</template>
            <template #title-cell="{ row }">{{ row.original.title || row.original.name }}</template>
            <template #sale_price-cell="{ row }">{{ row.original.sale_price || row.original.price || '—' }}</template>
            <template #original_price-cell="{ row }">{{ row.original.original_price || '—' }}</template>
          </UTable>

          <div v-if="detailReviews.length" style="margin-top:6px">
            <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap">
              <div style="text-align:center;min-width:90px">
                <div style="font-size:1.8rem;font-weight:700;color:var(--ui-text);line-height:1.1">{{ reviewStats.avg.toFixed(1) }}</div>
                <div style="color:#f5b301;letter-spacing:1px">{{ stars(reviewStats.avg) }}</div>
                <div class="inf-empty-note" style="margin-top:2px">共 {{ reviewStats.total }} 条</div>
              </div>
              <div style="flex:1;min-width:200px;display:flex;flex-direction:column;gap:3px">
                <div
                  v-for="star in [5, 4, 3, 2, 1]"
                  :key="star"
                  :style="{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', padding: '2px 4px', borderRadius: '5px', background: reviewFilter === star ? 'var(--ui-card-soft)' : 'transparent' }"
                  @click="setReviewFilter(reviewFilter === star ? 0 : star)"
                >
                  <span style="width:26px;color:var(--ui-muted);font-size:0.76rem">{{ star }}★</span>
                  <span style="flex:1;height:8px;background:var(--ui-border);border-radius:5px;overflow:hidden">
                    <span :style="{ display: 'block', height: '100%', width: (reviewStats.dist[star] / reviewStats.max * 100) + '%', background: '#f5b301' }"></span>
                  </span>
                  <span style="width:42px;text-align:right;color:var(--ui-muted);font-size:0.76rem">{{ reviewStats.dist[star] }}</span>
                </div>
              </div>
            </div>
            <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:12px">
              <button
                v-for="opt in [{ v: 0, t: '全部' }, { v: 5, t: '5★' }, { v: 4, t: '4★' }, { v: 3, t: '3★' }, { v: 2, t: '2★' }, { v: 1, t: '1★' }]"
                :key="opt.v"
                class="review-filter-btn"
                :style="{ padding: '3px 11px', borderRadius: '13px', border: '1px solid var(--ui-border)', cursor: 'pointer', fontSize: '0.78rem', background: reviewFilter === opt.v ? 'var(--ui-purple)' : 'var(--ui-card)', color: reviewFilter === opt.v ? '#fff' : 'var(--ui-text)' }"
                @click="setReviewFilter(opt.v)"
              >
                {{ opt.t }}
              </button>
              <span class="inf-empty-note" style="margin:0 0 0 auto">{{ filteredReviews.length }} 条 · 第 {{ reviewPage }}/{{ reviewPages }} 页</span>
            </div>
            <div style="margin-top:8px;max-height:48vh;overflow:auto">
              <div v-for="(review, index) in reviewPageItems" :key="review.review_id || index" style="padding:9px 0;border-bottom:1px solid var(--ui-border)">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
                  <span style="color:#f5b301;letter-spacing:1px;font-size:0.88rem">{{ stars(review.rating) }}</span>
                  <span class="inf-empty-note" style="margin:0">{{ review.review_date || '' }}</span>
                </div>
                <div style="margin-top:4px;color:var(--ui-text);font-size:0.84rem;line-height:1.5">{{ review.content }}</div>
              </div>
            </div>
            <div v-if="reviewPages > 1" style="display:flex;justify-content:center;align-items:center;gap:12px;margin-top:12px">
              <button class="btn-prim" :disabled="reviewPage <= 1" style="padding:4px 12px" @click="reviewPage--">上一页</button>
              <span class="inf-empty-note" style="margin:0">{{ reviewPage }} / {{ reviewPages }}</span>
              <button class="btn-prim" :disabled="reviewPage >= reviewPages" style="padding:4px 12px" @click="reviewPage++">下一页</button>
            </div>
          </div>
          <div v-if="!detailListings.length && !detailReviews.length" class="inf-empty-note">本次未抓到数据</div>
        </div>
      </div>
    </div>
  </section>
</template>
