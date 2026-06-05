# 按需抓取历史记录 — 设计文档

> 日期:2026-06-05
> 范围:为 smart-crawler 的「按需抓取」(ondemand)功能新增**历史记录**——把每次
> `fetch()` 调用记成一条任务,在控制台「🔗 按需抓取」Tab 内以列表展示,点开可看该次
> 抓到的 listing + 评论。

---

## 1. 背景与问题

ondemand 抓取的数据已入库(listing → `Product` 表 `site=ondemand_*`,评论 → `Review` 表
`platform=ondemand_*`),但:

1. **前端面板是一次性的**:`odResult` 只在抓取那一刻显示,刷新即失,无历史。
2. **现有商品库 Tab 看不到 ondemand 数据**:`GET /api/products` 按 workspace 的
   `allowed_sites` 过滤(`Product.site.in_(allowed_sites)`),而 `ondemand_*` 这几个 site
   不在任何 workspace 的站点列表里 → 被过滤挡掉。

所以用户「看不到按需抓取记录」。本设计补上「任务记录」这一层。

### 已澄清的需求决策

| 维度 | 决策 |
|------|------|
| 记录粒度 | **按抓取任务**(每次 fetch 一条),非按商品列表 |
| 详情数据 | **只存摘要**,详情现查 Product/Review 表(不存完整快照) |
| 放置位置 | **「🔗 按需抓取」Tab 内**,抓取框下方加历史列表 |
| 详情查看 | 点开一条记录,**展示该次抓到的 listing + 评论** |

---

## 2. 数据模型:新增 `OnDemandJob` 表

`backend/app/models.py` 新增:

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | Integer PK | |
| `url` | String | 抓取的 URL |
| `platform` | String, index | mercadolibre / lazada / shopee |
| `kind` | String | product / listing |
| `listing_count` | Integer | 抓到的 listing 数 |
| `review_count` | Integer | 抓到的评论数 |
| `status` | String, index | success / partial / failed |
| `notes` | JSON | `res.notes`(失败原因/截断提示) |
| `item_skus` | JSON | 本次抓到的 sku 列表(详情查库依据) |
| `workspace_id` | Integer, index | 发起的工作区(多租户隔离) |
| `created_by` | String | 发起用户(username) |
| `created_at` | DateTime, index | 抓取时间(默认 utcnow) |

**status 判定**:
- `failed`:`listing_count == 0 and review_count == 0`(没抓到任何东西)
- `partial`:抓到数据但 `notes` 非空(有截断/部分失败)
- `success`:抓到数据且 notes 为空

**`item_skus` 的作用**:详情查看时按这批 sku 精确取 Product/Review,**绕过 workspace 的
`allowed_sites` 过滤**(按 sku 精确取,不按 site 列举),从而能查到 ondemand 数据。

建表方式:沿用现有 `db.py::_migrate()` 的幂等 `CREATE TABLE`(`Base.metadata.create_all`
建新表,生产部署时自动建)。

---

## 3. 接口设计(挂在登录保护的 `router`,`/api` 前缀,按 workspace 隔离)

### 3.1 写入(改造现有 fetch 链路)

不新增写接口。在 `POST /api/ondemand/fetch` 的处理中,`runner.fetch()` 完成后**落一条
`OnDemandJob`**:

- 入参 workspace_id(来自 `X-Workspace-ID` header / `_current_workspace`)、created_by(user)
- 从 `OnDemandResult` 取 listing_count/review_count/notes,从 listings 取 sku 列表
- platform 用 `detect_platform(url)`,kind 用 `classify_url(url)`
- 按 §2 规则定 status

实现上:`runner.fetch()` 已返回 `OnDemandResult`;在 `routes.py` 的 `ondemand_fetch`
端点里,fetch 后构造并写入 OnDemandJob(单独一个 `_record_ondemand_job(...)` helper,
保持端点函数简洁)。

### 3.2 `GET /api/ondemand/jobs` — 历史列表

- 参数:`page=1` / `page_size=20` / 可选 `platform`
- 过滤:`workspace_id == 当前工作区`;可选 `platform`;按 `created_at` 倒序
- 返回:`{total, page, page_size, jobs: [{id, url, platform, kind, listing_count,
  review_count, status, created_at}]}`

### 3.3 `GET /api/ondemand/jobs/{id}` — 单条详情

- 取 job;若 `job.workspace_id != 当前工作区` → 403;不存在 → 404
- 按 `job.item_skus`:
  - listings:`Product` where `site LIKE 'ondemand_%' AND sku IN (item_skus)`
  - reviews:`Review` where `platform LIKE 'ondemand_%' AND sku IN (item_skus)`
- 返回:`{job: {...全字段...}, listings: [...], reviews: [...]}`
- **关键**:按 sku 精确取,不走 `allowed_sites` 过滤 → ondemand 数据可查

---

## 4. 前端(「🔗 按需抓取」Tab 内,抓取框下方)

### 4.1 历史列表区

- Tab 切到 ondemand 时自动 `loadOndemandJobs()`(调 `/api/ondemand/jobs`)
- 抓取成功后(`runOndemand` 完成)除显示当次 `odResult`,也刷新历史列表(新任务置顶)
- 表格列:时间 / 平台 / URL(截断)/ listing 数 / 评论数 / 状态
- 状态用色:success=绿、partial=黄、failed=红(复用现有 `.badge` 样式)
- 复用 `.inf-panel` 容器 + 现有表格样式,主题自适应(`--ui-*` 变量)

### 4.2 点开详情

- 点一行 → 调 `/api/ondemand/jobs/{id}`,展开显示:
  - listing 表格:SKU / 标题 / 售价 / 原价(复用 odResult 表格样式)
  - 评论表格:评分 / 评论内容
  - 失败任务:展开显示 `notes`(失败原因)
- 展开状态用一个 `expandedJobId` ref 控制(点已展开的行则收起)

### 4.3 新增响应式状态

`odJobs`(列表)、`odJobsLoading`、`expandedJobId`、`odJobDetail`(当前展开详情);
函数 `loadOndemandJobs()`、`toggleJobDetail(id)`。均加入 setup() return。

---

## 5. 数据流

```
用户在 Tab 粘 URL → 点抓取
  → POST /api/ondemand/fetch
      → runner.fetch(url) → OnDemandResult(listings/reviews/notes)
      → _record_ondemand_job(ws, user, url, result) → 写 OnDemandJob
      → 返回 odResult(当次结果)
  → 前端显示当次结果 + 刷新历史列表(loadOndemandJobs)

用户点历史某行
  → GET /api/ondemand/jobs/{id}
      → 校验 workspace → 按 item_skus 查 Product/Review
      → 返回 job + listings + reviews
  → 前端展开显示
```

---

## 6. 测试

- **单测**(`tests/test_ondemand_jobs.py`):
  - `_record_ondemand_job` 按 OnDemandResult 正确生成 OnDemandJob(status 判定:
    success/partial/failed 三种)
  - `GET /jobs` 按 workspace 过滤、倒序、分页(用 in-memory sqlite + 造数据)
  - `GET /jobs/{id}` 越权返回 403、不存在 404、正常按 sku 取 listings/reviews
- 前端为单文件 Vue,无自动化测试,手动验证 + 部署后公网验证。

---

## 7. 验收标准

1. 抓取一条 URL 后,历史列表出现一条新记录(置顶),含正确的平台/数量/状态/时间。
2. 点开记录,展示该次抓到的 listing(SKU/标题/价格)+ 评论(评分/内容)。
3. 失败的抓取(0 listing 0 评论)记为 failed,展开显示 notes 原因。
4. 历史列表按 workspace 隔离:A 工作区看不到 B 工作区的记录。
5. 详情接口越权访问(他人 workspace 的 job)返回 403。
6. 刷新页面后历史记录仍在(已落库,非一次性)。
