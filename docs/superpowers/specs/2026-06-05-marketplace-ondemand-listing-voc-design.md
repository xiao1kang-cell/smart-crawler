# 跨境电商平台「指定 URL → listing + VOC」按需抓取 — 设计文档

> 日期:2026-06-05
> 范围:为 smart-crawler 新增 **美客多(MercadoLibre)/ Lazada / 虾皮(Shopee)** 三个平台的
> **按需(on-demand)抓取能力**:用户提交一条 URL,系统抓取该 listing 的商品信息 + VOC(评论原文)。

---

## 0. 实测落地状态(2026-06-05 更新,重要)

设计阶段假设三平台均为「内部 JSON API 可直连」(类比 Costway),**真实落地后该假设被推翻**,各平台实况差异很大:

| 平台 | 状态 | 实测结论 / 下一步 |
|------|------|------|
| **Lazada** | ✅ **已做通(样板)** | listing 必须 **StealthyFetcher 真浏览器渲染**(裸 HTTP 只拿到反爬占位页);解析路径全部按真实结构重写;评论走 `my.<region>` 子域接口。**端到端验证通过**(NAS + ProxyJet 住宅代理:真 URL → 价格 RM114 + 9 图 + 6 评论 → 入库)。 |
| **美客多** | ❌ **不可用,待 OAuth** | `items` API 现强制 OAuth(403 PolicyAgent);商品页 HTML 即便真浏览器也只有无价格占位页。需接官方 OAuth(注册开发者应用拿 token)才能抓。 |
| **Shopee** | ⏳ **未经真实验证** | 仅过假 fixture 单测,从未打真实站点。参照 Lazada 的教训,大概率需同样一轮逆向 + 真浏览器改造;`get_pc`/`get_ratings` 可能已加签名头。 |

**两条贯穿性教训(后续做 Shopee / 复活美客多前必读):**
1. **不要用假 fixture 替代真实结构**:三平台的 `parse_*` 最初都是按臆想结构写的,无一对得上真实站点。正确流程是先 `StealthyFetcher` 抓真实页/接口 → dump 结构 → 按真实路径写解析 + 用真实数据做 fixture(见 Lazada 的做法)。
2. **生产可用的前提是住宅代理**:Lazada listing 在 NAS 机房 IP 会被 `_____tmd_____/punish` 拦截,必须经住宅代理(ProxyJet,已配在 `backend/proxies.txt` 的 `[residential]` 段)。代码读代理依赖 `PROXIES_FILE` / 默认 `/app/backend/proxies.txt`——脱离该路径跑(如临时目录)会取不到代理而误判为「不可用」。

---

## 1. 背景与需求

需求来自甲方表格(截图):

| 抓取平台 | 服务类型 | 服务方式 | 服务内容 |
|----------|----------|----------|----------|
| 美客多 | RPA 抓取 | 指定 URL 抓取 | listing+voc 抓取 |
| lazada | RPA 抓取 | 指定 URL 抓取 | listing+voc 抓取 |
| 虾皮 | RPA 抓取 | 指定 URL 抓取 | listing+voc 抓取 |

**与现有系统的关系**:现有 smart-crawler 是「站点枚举」范式(`sites.yaml` 列出整站,
采集器 `crawl()` 拉全量商品)。本需求是**新范式**——「按 URL 按需抓取」:给一条 URL,
抓一条(或一页)的 listing + 评论。最接近的现有范式是 `app/voc_amazon.py`
(输入 ASIN → 拉评论)。

### 已澄清的需求决策

| 维度 | 决策 |
|------|------|
| URL 粒度 | **两种都支持**:单商品详情页 URL(精抓一条);店铺/类目/搜索页 URL(枚举该页所有商品批量抓) |
| 集成入口 | **四层全做**:CLI 底层命令 + MCP 工具 + Web 控制台 UI(底层一个纯函数,三层薄包装) |
| 抓取方式 | **API 直连优先**(沿用现有 curl_cffi 直连内部 JSON 接口路子),Playwright 仅作兜底 |
| VOC 深度 | **只抓评论原文**(评分/日期/内容/赞同数等)入库,首期不接 NLP 情感分析 |
| 住宅代理 | 已确认基础设施就绪(`proxy.py` + `proxy_pool.py`,分层 + 健康检查 + 粘性会话,对接 static-ip-manager 的 AT&T 美国静态 IP 块) |

---

## 2. 可行性评估

| 平台 | listing 抓取路径 | VOC(评论)抓取路径 | 反爬强度 | 代理档位 |
|------|------------------|--------------------|----------|----------|
| **美客多 MercadoLibre** | REST API `api.mercadolibre.com/items/{id}`,或商品页解析 | 评论 API `/reviews/item/{id}` | 🟢 低-中 | `none`(直连) |
| **Lazada** | 商品页内嵌 JSON(`__moduleData__` / pdp data)解析 | 内部接口 `/pdp/review/getReviewList`(需 itemId + 参数) | 🟡 中-高 | `residential` |
| **虾皮 Shopee** | 内部 API `/api/v4/pdp/get_pc`(需 shopid+itemid) | 评论 API `/api/v2/item/get_ratings`(需 shopid+itemid+翻页) | 🔴 高 | `residential`(强制) |

**结论:三平台均可行**,难度递增 美客多 < Lazada < Shopee。三者共同范式:
**从 URL 解析出商品/店铺 ID → 调内部 JSON 接口**,与现有 Costway(`/api/*` JSON 直连)同构,
可复用采集器基类、代理池、antiban 熔断、snapshot 归档。

**主要风险与对策**:
- *URL → ID 解析*:三平台 URL 格式各异(Shopee `i.{shopid}.{itemid}`、美客多 `MLM-123456789`、
  Lazada `...-i{itemid}-s{skuid}.html`),每平台需独立解析器。
- *风控*:Shopee 对数据中心 IP 几乎即时封锁,**强制**住宅代理 + 拟人限速;Lazada 次之;美客多最宽松。
  代理池基础设施已就绪,可控。
- *ToS/合规*:三平台 ToS 均禁止自动抓取,属采集类项目固有风险,沿用现有合规边界
  (限速、不绕验证码、仅抓公开数据)处理。

---

## 3. 架构设计

### 3.1 方案选型

- **方案 A**(否决):塞进 `sites.yaml` + 整站采集器。语义不匹配(整站 `crawl()` vs 单 URL),
  污染整站调度。
- **方案 B**(采纳):新增独立 `ondemand/` 子系统,自己的 registry 与 `fetch(url)` 接口,
  复用底层(代理池/antiban/snapshot/Product/Review 模型),不碰整站 runner/scheduler。
- **方案 C**(否决):纯 MCP 工具、不落库。不满足 Web UI + 数据沉淀 + 复用看板的要求。

**采纳方案 B**:与 `voc_amazon.py` 解耦思路一致;按需抓取与整站调度生命周期不同,分离后
新增平台只需加一个解析器,不影响整站逻辑。

### 3.2 模块结构

```
backend/app/ondemand/
  __init__.py
  base.py          # OnDemandCrawler 基类:fetch(url) -> FetchResult
                   #   复用 BaseCrawler 的 proxy / antiban(guard) / snapshot / sleep
  registry.py      # detect_platform(url) 按域名识别;classify_url(url) 判 product/listing
  mercadolibre.py  # URL→itemId;items API + reviews API
  lazada.py        # URL→itemId;pdp JSON 解析 + getReviewList
  shopee.py        # URL→(shopid,itemid);get_pc + get_ratings;强制住宅代理
```

### 3.3 数据归属(复用现有表,零额外 UI 成本)

- **listing → 现有 `Product` 表**,平台标记 `source=ondemand`,经现有 `pipeline` upsert
  (按平台 + itemId 去重)。
- **评论 → 现有 `Review` 表**,复用 `(platform, review_id)` 唯一约束。
- 因复用现有表,**现有看板/Excel 导出自动能看到这些数据**。

### 3.4 四层入口

| 层 | 形态 |
|----|------|
| 底层 | `ondemand.fetch(url, *, max_items, review_limit) -> FetchResult` 纯函数 |
| CLI | `python -m app.cli fetch-url --url "..." [--max-items N] [--review-limit N]` |
| MCP | 工具 `fetch_listing_voc(url)` —— 包装同一个 `fetch()` |
| Web | 控制台新增输入框:粘 URL → 点抓取 → 展示 listing 卡片 + 评论列表 |

---

## 4. 数据流与处理细节

### 4.1 统一入口(单品 / 列表页殊途同归)

```
fetch(url):
  platform = detect_platform(url)        # mercadolibre | lazada | shopee
  kind     = classify_url(url)           # 'product' | 'listing'
  if kind == 'product':
      item_ids = [parse_id(url)]
  else:                                  # 店铺/类目/搜索页
      item_ids = enumerate_listing(url)  # 翻页收集 ID,受 max_items 上限约束
  result = FetchResult()
  for id in item_ids:
      try:
          listing = fetch_listing(id)        # → Product upsert
          reviews = fetch_reviews(id, review_limit)  # → Review upsert(只抓原文)
          result.add(listing, reviews)
      except BlockedError:
          切代理重试;超阈值 → 记 notes,跳过该 id,继续
      except Exception as e:
          result.notes.append(f"{id}: {e}")  # 失败隔离,不中断整批
      sleep()                                # 拟人限速
  return result
```

### 4.2 关键设计点

1. **单品 / 列表共用 `fetch_listing` / `fetch_reviews`**,差异仅在「拿到几个 ID」。
2. **列表枚举上限** `max_items`(默认 100),超限时 `log()` 明确告知截断条数 —— 不静默截断。
3. **评论翻页** `review_limit`(默认 100 条/商品),够 VOC 分析即可,不追全量。
4. **风控分层**(复用 `antiban.py`):
   - 平台默认代理档:美客多 `none`、Lazada `residential`、Shopee `residential`。
   - 命中 403/429/验证码页 → `guard()` 抛 `BlockedError` → 切代理重试,超阈值放弃该 ID。
   - `snapshot()` 归档每次原始响应,便于排查接口变更。
5. **失败隔离**:列表中单个 ID 失败不影响其余;`FetchResult.notes` 汇总成功/失败/截断,四层入口均可展示。
6. **幂等入库**:listing 按平台 + itemId upsert;评论按 `(platform, review_id)` 唯一约束去重。

---

## 5. MVP 范围(YAGNI)

**做**:
- 三平台单品 + 列表页抓取
- 评论原文入库(复用 Review 表)
- 四层入口(底层 / CLI / MCP / Web)
- 风控兜底(代理切换 / 熔断 / 失败隔离 / 截断告知)

**不做(首期)**:
- NLP 情感分析(只抓原文;现有 NLP 能力后续可接)
- 定时调度(按需触发即可,不接 scheduler)
- 跨平台同款匹配、价格曲线(整站体系已有,ondemand 首期不接)

---

## 6. 默认参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_items` | 100 | 列表页枚举商品数上限 |
| `review_limit` | 100 | 每商品抓取评论条数上限 |
| 美客多 `proxy_tier` | none | 直连 |
| Lazada `proxy_tier` | residential | 被封切住宅 |
| Shopee `proxy_tier` | residential | 强制住宅 |

---

## 7. 验收标准

1. 给三平台各一条**单品 URL**,能抓到 listing 字段(标题/价格/图片等)+ ≥1 页评论,入库成功。
2. 给三平台各一条**列表页 URL**,能枚举出多个商品并批量抓取,受 `max_items` 约束。
3. CLI / MCP / Web 三入口均可触发并返回结构化结果与 notes。
4. 单个商品失败不中断整批;被封时能切代理重试;截断有明确日志。
5. 重复抓取同一 URL 不产生脏数据(幂等)。
