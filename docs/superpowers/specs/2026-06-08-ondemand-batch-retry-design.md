# 按需抓取:批量上传 + 失败重试 设计

日期:2026-06-08
状态:待评审

## 背景与目标

当前按需抓取(`/api/ondemand/fetch`)是**同步单条**:一次粘贴一个 URL,HTTP 请求阻塞 15–120 秒(真浏览器渲染)直到抓完返回,落一条 `OnDemandJob` 历史记录。

要新增两个能力:

1. **批量上传** —— 一次提交多条 URL(粘贴多行 / 上传 .txt/.csv),后台逐条抓取。
2. **失败重试** —— 对失败的记录单条重试,或一键重试整批的失败项。

## 关键约束

- 单条抓取耗时 15–120 秒,批量量级不固定 → **批量必须异步**:提交后立即返回,后台执行,前端轮询进度。同步循环会超 HTTP 超时。
- 抓取需对反爬友好(住宅 IP 信誉时好时坏、高频易触发验证页)→ 后台 **串行**逐条跑,一次只跑一条。
- 本地开发用 `RUN_SCHEDULER=0`,现有 scheduler/worker 线程不启动 → 批量执行机制**不能依赖**那套基建。

## 架构:专用进程内按需队列 + 单 worker 线程(方案 A)

新增模块 `app/ondemand/queue.py`:

- 模块级 `queue.Queue[int]`(存 job_id)。
- 一个**懒启动的常驻 daemon 线程**:首次入队时启动,循环 `get()` → 执行一条 → 原地更新 job → 取下一条。单线程天然串行。
- `enqueue(job_id)`:入队;`ensure_worker()`:保证 worker 线程已起。
- **崩溃兜底**:`init_db()` 后调用一次 `requeue_pending()`,把库里残留 `queued` **以及 `running`**(进程在抓取中途被杀,job 卡在 running)的 job 重新置 `queued` 并入队,覆盖进程重启时内存队列丢失的情况。

worker 执行一条的逻辑:

1. 读 job(校验存在),置 `status="running"`、`attempts += 1`,提交。
2. 调现有 `app.ondemand.runner.fetch(url, max_items, review_limit, do_persist=True)`。
3. 按结果原地更新该 job 行:`status`(success/partial/failed)、`listing_count`、`review_count`、`notes`、`item_skus`、`error`(失败时填简短原因,成功时清空)。
4. 异常被 `runner.fetch` 内部吞掉并以 notes 返回时,沿用其 `_status_of` 判定;worker 自身异常则置 `failed` + `error`。

不复用现有 `runner.enqueue`/scheduler worker:那套是"按站点定时采集"语义(Job 模型、site-name),与 URL 级按需抓取错配,且 `RUN_SCHEDULER=0` 时不启动。

## 数据模型:`OnDemandJob` 扩展

现有表"一次 fetch 一行"。新增列(经 `app/db.py` 的 `_migrate()` 幂等 `ALTER TABLE ADD COLUMN` 自动补列,SQLite/PG 兼容,无需手写迁移):

| 列 | 类型 | 用途 |
|---|---|---|
| `batch_id` | `String, index` | 同一次批量共享一个 UUID;单条抓取也分配一个。前端按批次显示进度 |
| `max_items` | `Integer` | 重试需复跑,存原始参数 |
| `review_limit` | `Integer` | 同上 |
| `attempts` | `Integer, default 0` | 执行次数,worker 每跑一次 +1,前端显示"已重试 N 次" |
| `error` | `Text, nullable` | 最后一次失败的简短原因(区别于逐条 `notes`),前端红字提示 |

`status` 取值扩展:新增 `queued` / `running`;原 `success` / `partial` / `failed` 不变。

**状态机**:`queued → running → success|partial|failed`;重试 = 任意终态 `→ queued`(原地复用同一行,`attempts` 累加)。落实"重试原地更新、历史表不膨胀"。

## 接口设计

现有 `POST /api/ondemand/fetch`(同步单条)**保留不动**。新增/改造:

### 1. `POST /api/ondemand/batch` — 提交批量(立即返回)

```
请求: { "urls": ["...", ...], "max_items"?: 20, "review_limit"?: 100 }
处理:
  - 去空行 + 去重
  - 数量上限 1000:超过 → 400
  - 并发闸:本 workspace 存在 status in (queued, running) 的 job → 409(有未完成任务,禁止再次提交)
  - 逐条 detect_platform 校验:识别不了的进 skipped,不入队
  - 为可抓的每条建一行 queued job(共享新 batch_id,存 max_items/review_limit),enqueue
返回: { "batch_id": "...", "queued": <int>, "skipped": [{"url":"...","reason":"无法识别平台"}] }
```

文件上传由**前端**解析 .txt/.csv 成 urls 数组后调本端点;后端只认 JSON,不做文件解析(更简单、易测)。

### 2. `GET /api/ondemand/jobs` — 列表(改造)

- 新增可选 query:`batch_id`、`status`。
- 返回项补字段:`batch_id`、`attempts`、`error`、`max_items`、`review_limit`。
- 前端在有 `queued`/`running` 时每 2–3 秒轮询,全终态后停。

### 3. `POST /api/ondemand/jobs/{id}/retry` — 单条重试

```
处理: 校验归属本 ws;仅允许终态(success/partial/failed)重试,queued/running → 409;
      置 queued、清 error、enqueue
返回: { "id": ..., "status": "queued" }
```

### 4. `POST /api/ondemand/batch/{batch_id}/retry-failed` — 一键重试整批失败

```
处理: 该 batch 下所有 status == failed 的 job 批量置 queued + enqueue
返回: { "batch_id": ..., "requeued": <int> }
```

业务逻辑集中在 `app/api/ondemand_jobs.py`(现有薄路由 + 逻辑分层惯例),`routes.py` 只做路由声明。

## 前端(`frontend/index.html` 按需 tab)

- **批量输入区**:现有单 URL 框下方新增多行 textarea(每行一个 URL)+ 文件选择(.txt/.csv,选中后解析填入 textarea)+「批量抓取」按钮。
- **前端校验**(后端兜底,前端做体验):
  - 解析后 URL 数 > 1000 → 提示并阻止提交。
  - 存在 `queued`/`running` job 时,「批量抓取」按钮禁用 + 提示"有未完成任务,请等待完成"。
- **历史表**:
  - `queued`/`running` 显示对应状态(转圈/灰字);展示 `attempts`("重试 N 次")。
  - 失败行加「重试」按钮 → 调端点 3。
  - 批次存在失败项时顶部出「重试全部失败」→ 调端点 4。
  - 有未完成 job 时启用轮询自动刷新。

## 错误处理

- `/batch`:空 urls → 400;>1000 → 400;有未完成任务 → 409;全部 skipped(无可抓)→ 正常返回 `queued:0` + skipped 列表。
- `retry`:越权 → 403;不存在 → 404;非终态 → 409。
- worker:单条异常隔离,置 `failed` + `error`,不影响队列后续条目。
- 进程重启:`requeue_pending()` 恢复残留 `queued`/`running`。

## 测试

- **queue 单测**:enqueue → worker 串行消费 → job 状态流转(注入假 `runner.fetch`,不起真浏览器);`requeue_pending` 恢复逻辑。
- **batch 逻辑单测**:去重/去空、1000 上限、未完成任务并发闸、skipped 分类。
- **retry 逻辑单测**:终态可重试、非终态 409、越权 403、retry-failed 只挑 failed。
- **HTTP 冒烟**(本地手测):提交小批量真实 URL → 轮询到全终态 → 对失败项点重试。

## 不做(YAGNI)

- 不做多 worker 并发(明确选串行)。
- 不做真正的分布式任务队列(Celery/RQ)。
- 不做批次级删除/导出(现有单条删除 + 清空历史已够)。
- 后端不做文件解析(前端解析)。
