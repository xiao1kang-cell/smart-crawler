# 按需抓取 Tab 重构:弹窗建任务 + 单条转异步 + 无感刷新

## Context

「🔗 按需抓取」Tab 当前的交互有几处不顺手,需要一次成体系的重构:

1. **历史详情评论平铺** —— 展开行里评论是「评分+正文」两列平表,上千条又长又难读(listing 同样平表)。
2. **冗余的「重试整批失败」按钮** —— 用户只需要单条状态/单条重试,批量级重试是噪声。
3. **单条/批量两个面板上下平铺常驻** —— 占地方,入口不清晰。
4. **单条抓取接口是同步阻塞的** —— `POST /api/ondemand/fetch` 调 `ondemand.fetch()` 同步跑完(含 listing 6 次重试,可能几分钟)才返回,前端一直转圈。批量已是异步(队列+worker)。
5. **状态轮询整表重渲染** —— `loadOndemandJobs()` 每轮 `odJobs.value = d.jobs`,整个数组替换导致 Vue 整表重绘、展开的详情闪烁/塌陷、`odJobsLoading` 触发「加载中…」覆盖。
6. **并发闸太严** —— 同 workspace 有未完成任务时,提交新任务被 `PendingExistsError` 直接 409 拒绝。期望改为允许入队排队(worker 串行消费)。
7. **重复抓取浪费** —— 重抓同一商品时每条评论都触发 `upsert_reviews_into` 的 `(platform, review_id)` 查库去重(数据不会重复,但每条都有一次 DB 往返)。期望抓取前一次性载入已有 review_id 集合,命中的直接跳过,省掉逐条 DB 往返。

> ⚠️ 关于"只抓最新"的真相(已实测):美客多评论接口**不支持时间排序**(`sort/order/criteria/sortType` 全被忽略),默认视角按"相关性"排——老评论(一年多前)在 offset=0,新评论(一周前)散落在后面。因此**无法**实现"翻到第一条已抓过的就停"式增量(会误停漏新)。"增量"的可行含义只能是**去重入库**:仍按桶翻完该翻的页,已有 review_id 跳过。单视角 offset 硬上限 ~300(315 被拒),分桶仍是拿到尽量多评论的唯一手段。

目标:页面只留「+ 新建任务」按钮 + 当前任务卡片 + 历史表;建任务走弹窗;单条与批量统一异步;轮询无感增量刷新;允许排队;评论去重不重复入库。

后端已有完整异步基础设施(`app/ondemand/queue.py` 进程内队列 + 懒启动 worker 线程,`app/api/ondemand_jobs.py` 的 `submit_batch`/`flush_enqueue`/`retry_job`),本次单条转异步**复用**它,不新造。

## 方案

### 后端:`backend/app/api/routes.py` — 单条 fetch 转异步

`ondemand_fetch`(L694)从「同步 `ondemand.fetch()` + `record_job`」改为复用 `submit_batch`(单元素 URL 列表):
- 走 `submit_batch(session, ws_id, username, urls=[url], max_items, review_limit)` → `commit` → `flush_enqueue`。
- 立即返回 `{batch_id, queued, skipped, job_id}`(不再返回 listings/reviews 内容)。
- `detect_platform` 识别不了的 URL 进 `skipped`,前端提示。

> 注:`ondemand_batch`(L733)已是这套,无需改。`fetch` 改完后两个端点行为统一,仅入参 urls 数量不同。

### 后端:`backend/app/api/ondemand_jobs.py` — 并发闸改为允许排队

`submit_batch`(L89)去掉 `has_pending` 的 `PendingExistsError` 拒绝:有未完成任务时不再 409,直接建 queued job 入队,worker 串行消费。`ondemand_fetch`/`ondemand_batch` 路由相应去掉 `except PendingExistsError → 409` 分支(类暂保留,降改动面)。前端去掉 `odHasPending` 的提交拦截与「请等待」提示。

### 后端:增量去重抓取 — `mercadolibre.py` + `runner.py`

利用实测确认的 `order=dateCreated`(严格时间倒序,offset 上限仍 ~300,可与 `rating` 桶叠加):

- **`mercadolibre.py` `fetch_reviews(item_id, url, limit, proxy, known_ids=None)`**:
  - URL 统一加 `&order=dateCreated`(首次全量与增量都用,入库时间有序)。
  - `known_ids` 为空(库里无该商品)→ **首次全量**:5 星级桶 + dateCreated,每桶翻到 ~300 或抓尽。
  - `known_ids` 非空(库里已有)→ **增量**:只翻默认视角(不分桶)dateCreated,offset=0 往后;**碰到第一条 `review_id ∈ known_ids` 即停**(时间倒序保证后面全是旧的)。
- **`runner.py` `_fetch_reviews_safe`**:抓前查库 `SELECT review_id FROM reviews WHERE platform=SITE AND sku=iid` → 传 `known_ids`。

> 增量正确性依赖严格时间倒序——已实测 `order=dateCreated` 满足。`upsert_reviews_into` 的 `(platform, review_id)` 去重保留为兜底。

### 前端:`frontend/index.html` — 五处改动

**A. 页面布局精简(L1646 起 ondemand Tab)**
- 删除常驻的「单条抓取面板」(L1650–1760)和「批量抓取面板」(L1761–1776)。
- 顶部改为:`+ 新建任务` 按钮 + 当前任务卡片(`v-if` 有进行中任务时显示状态)。
- 保留「抓取历史」表为主体。

**B. 新建任务弹窗(新增 modal)**
- 页面无现成 modal 组件(仅有 `.toast`),新建一个简单弹窗:`position:fixed` 全屏半透明遮罩 + 居中卡片(`.inf-panel` 风格),点遮罩或 ✕ 关闭。新增 `.od-modal`/`.od-modal-card` 两条 CSS;`odShowNewTask` ref 控制开关。
- 弹窗顶部子 tab:`单个 URL` / `批量`(`odModalTab` ref)。
  - 单个:URL + 列表上限(`odMaxItems`)+ 评论上限(`odReviewLimit`)。复用现有 `runOndemand`(改为提交后关弹窗、刷新历史、启动轮询,不再 set `odResult`)。
  - 批量:多行 textarea(`odBatchText`)+ 文件导入(`odOnFile`)。复用 `submitBatch`(提交后关弹窗)。
- 提交后:`odShowNewTask=false` → `loadOndemandJobs()` → `startOdPolling()`。

**C. 历史表瘦身(L1777–1847)**
- 删除「重试整批失败」按钮(L1814–1816)及其 `retryFailedBatch`/`batchHasFailed` 调用;保留单条「重试」(L1811)。
- 详情展开区(L1821–1843):评论改用新「概览组件」(平均分+星级柱状图+筛选 chips+分页),即把当前 `odReviews`/`odRvStats`/`odRvPageItems`/`odSetRvFilter`/`odRvStars` 那套抽出来,数据源换成 `odJobDetail.reviews`。listing 保持小表。

**D. 抽出可复用的评论概览组件**
- 当前评论概览逻辑绑定在 `odResult.reviews`(单条同步结果)。改为接受任意 reviews 数组:把 `odReviews` computed 的来源参数化,或为详情单独建一组 `odDetailRv*` computed(数据源 `odJobDetail.reviews`)。优先后者(隔离清晰,避免单条结果面板与详情面板状态打架)。
- 由于单条转异步后不再有 `odResult` 同步结果面板(决策:只在详情里看评论),**删除** L1694–1759 那段 `v-if="odResult"` 概览面板及其 `odRv*`(挂在 odResult 上的那组),概览组件只服务历史详情。

**E. 无感增量刷新(`loadOndemandJobs` L774)**
- 不再 `odJobs.value = d.jobs`,改为按 `id` merge:
  - 已存在的 job:逐字段更新(status/listing_count/review_count/notes/attempts/error),保持数组对象引用稳定(Vue 只 patch 变化的行)。
  - 新增的 job:prepend 到数组头。
  - 服务端已不存在的:从数组移除。
- 轮询路径(`startOdPolling` L889)调用 merge 版,**不**设 `odJobsLoading`(避免「加载中…」覆盖);仅首次进 Tab 显式加载时显示 loading。
- 展开详情行 `expandedJobId`/`odJobDetail` 不受 merge 影响。

## 取舍

- **并发闸改为排队**:去掉 `has_pending` 的 409 拒绝,单条/批量都允许入队排队,worker 串行消费。代价是任务可能堆积,但本地无代理时也只是排队等,不丢任务。
- **增量靠 `order=dateCreated` + 碰到已有 id 停**:依赖接口的严格时间倒序(已实测)。若美客多哪天改了排序语义,增量会失效退化成"翻几页就停",但 `(platform, review_id)` 去重兜底保证不脏数据。首次全量仍分桶 + dateCreated。
- **不保留单条同步结果面板**:决策为「只在详情里看评论」。少一处重复展示,评论概览组件单一数据源(`odJobDetail.reviews`),更易维护。代价:单条任务跑完要点历史行展开看——可接受(异步语义下本就该如此)。
- **merge 刷新按 id 对象稳定**:避免整表重绘。代价是 merge 逻辑比直接赋值复杂十几行,但这是「无感刷新」的根因修复,值得。
- **删 retryFailedBatch**:后端 `ondemand_batch_retry_failed` 端点先保留(不删后端,降风险),仅前端不再暴露入口。

## 验证

1. **后端单测**:`cd backend && .venv/bin/python -m pytest tests/test_ondemand_jobs.py tests/test_ondemand_api.py -q` —— 确认 fetch 改异步后仍返回 job、入队、状态流转;补一个「fetch 单条进 queued」断言。
2. **浏览器端到端**(Playwright 驱动已就绪,服务在 :8077,admin/Verify2026):
   - 点「+ 新建任务」→ 弹窗出现 → 单个 tab 填美客多 URL + review_limit=250 → 提交 → 弹窗关闭、历史表顶部出现 queued 行。
   - 轮询:queued → running → success,**展开的详情行不闪、整表不重绘**(对比 merge 前后截图)。
   - 展开 success 行 → 详情里评论是「概览+星级柱状图+筛选+分页」,点 1★ 筛选生效、翻页生效。
   - 确认「重试整批失败」按钮已消失,单条「重试」仍在。
3. **无感刷新核验**:轮询期间在 console 观察 `odJobs` 数组对象引用——已存在行的对象引用不变(仅字段 patch),新行 prepend。

注:本任务改 `frontend/index.html`、`backend/app/api/routes.py`、`backend/app/api/ondemand_jobs.py`、`backend/app/ondemand/mercadolibre.py`、`backend/app/ondemand/runner.py`(+ 补测试)。不碰并行会话的 worker memory-gate 相关文件。
