# Amazon VOC 安克接口对接文档

更新时间：2026-06-30

本文档面向安克侧接口对接，覆盖 Amazon 商品详情和评论采集的提交、查询、回调、OSS 结果文件结构和主要字段含义。

## 1. 基本约定

Base URL 按部署环境提供，下文用 `{BASE_URL}` 表示。

认证方式：

- 如果服务端配置了 `AMAZON_VOC_TOKEN` 或 `AMAZON_VOC_TENANT_TOKENS`，请求需要带 token。
- Header 支持 `X-Token: <token>`。
- 也支持 `Authorization: Bearer <token>`。
- 未配置 token 时，接口不强制鉴权。

通用业务字段：

| 字段 | 类型 | 必填 | 说明                                                                          |
| --- | --- | --- |-----------------------------------------------------------------------------|
| `tenant_id` | string | 是 | 租户 ID。安克建议固定为 `anker_001`。                                                  |
| `app_id` | string | 是 | 应用 ID。建议固定为 `voc` 或双方约定值。                                                   |
| `req_ssn` | string | 是 | 安克侧请求流水号。幂等键为 `tenant_id + app_id + req_ssn + type`，因此同一流水号可以同时提交商品和评论任务。   |
| `type` | string | 是 | `AmazonReviewJob` 评论采集；`AmazonListingJob` 商品详情采集。                           |
| `priority` | string/int | 否 | 优先级。支持 `P0`、`P1`、`P2`、`explore`（p0=0 p1=100 p2=200） 或数字；数值越小优先级越高。默认 `100`。 |
| `biz_source` | string | 否 | 业务来源标识。OSS 结果路径按 `tenant_id` 分区，不依赖该字段。 |
| `payload` | object | 是 | 任务参数，见下文。                                                                   |
| `sla` | int | 否 | SLA 标记。 |
| `callback` | string | 否 | 回调地址。只有显式传该字段时才回调；不传则不回调。                                                   |
| `callback_url` | string | 否 | 回调地址。与 `callback` 二选一；同时传时 `callback` 优先。 |

国家/站点：

- `payload.market` 支持 `US`、`UK`、`DE`、`JP`、`CA`、`FR`、`IT`、`ES` 等。
- 入参大小写不敏感，会统一转大写。
- `GB` 会规范化为 `UK`。

## 2. 提交任务

### 2.1 通用提交接口

`POST {BASE_URL}/job/submit`

Content-Type: `application/json`

成功响应：

```json
{
  "rsp_code": "00000",
  "rsp_msg": "提交成功",
  "req_ssn": "TR1781759443539",
  "data": {
    "task_id": "TR_20260630081100_US_B0D62GMQ3F_84226dfb2a",
    "id": 123,
    "queued": true
  }
}
```

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `rsp_code` | string | `00000` 表示提交成功。 |
| `rsp_msg` | string | 提交结果说明。 |
| `req_ssn` | string | 原样返回请求流水号。 |
| `data.task_id` | string | 采集任务 ID。 |
| `data.id` | int | 新平台数据库任务主键。 |
| `data.queued` | boolean | 是否成功写入 Redis 队列；为 `false` 时任务仍已入库，可由补偿逻辑重新入队。 |

### 2.2 评论采集请求示例

```json
{
  "tenant_id": "anker_001",
  "app_id": "voc",
  "req_ssn": "TR1781759443539",
  "type": "AmazonReviewJob",
  "priority": "P0",
  "biz_source": "CD",
  "payload": {
    "market": "us",
    "asin": "B0D62GMQ3F",
    "limit": 999,
    "max_pages": 10,
    "query_conditions": {
      "date_from": "2026-06-01",
      "stars": [5, 4, 3],
      "sort_by": "recent",
      "all_variants": false
    }
  },
  "sla": 1,
  "callback": "https://anker.example.com/amazon-voc/callback"
}
```

评论 `payload` 支持参数：

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `market` | string | 是 | `US` | 亚马逊站点国家。 |
| `country` | string | 否 | 同 `market` | 站点国家字段。未传 `market` 时可用该字段。 |
| `asin` | string | 是 | 无 | Amazon ASIN，系统会转大写。 |
| `max_pages` | int | 否 | `10` | 最大翻页数。Amazon 一页通常约 10 条评论，因此 `10` 页通常最多约 100 条。 |
| `query_conditions` | object | 否 | `{}` | 评论筛选条件，见下表。 |
| `limit` | int | 否 | `999` | 期望评论数量上限；实际返回数量会受 Amazon 页面、筛选条件和 `max_pages` 影响。 |
| `last_time` | string | 否 | `null` | 日期过滤字段。建议优先使用 `query_conditions.date_from`。 |
| `star_filter` | array[int] | 否 | 无 | 星级过滤字段。建议优先使用 `query_conditions.stars`。 |

评论 `query_conditions` 支持参数：

| 字段 | 类型 | 示例 | 说明 |
| --- | --- | --- | --- |
| `date_from` | string | `"2026-06-01"` 或 `"30d"` | 只采集该日期之后的评论；支持绝对日期和相对天数。启用后按最近排序并遇到旧评论停止翻页。 |
| `stars` | int/array[int] | `[5,4,3]` | 星级过滤，取值 1-5。 |
| `sort_by` | string | `"recent"` / `"top_reviews"` | 默认按最近评论（请求 URL 带 `sortBy=recent`）。只有传 `top_reviews` 且未设置 `date_from` 时，才不强制最近排序，使用 Amazon 默认的 Top reviews 排序；一旦设置 `date_from`，系统会强制最近排序以便按日期截断。 |
| `all_variants` | boolean/string | `false` | 默认只采当前变体评论；传 true 时采集所有变体评论。 |

### 2.3 商品详情采集请求示例

```json
{
  "tenant_id": "anker_001",
  "app_id": "voc",
  "req_ssn": "TL1781759443539",
  "type": "AmazonListingJob",
  "priority": "100",
  "biz_source": "",
  "payload": {
    "market": "us",
    "asin": "B0F9L1PPPJ",
    "include_ratings_by_feature": false
  },
  "sla": 1,
  "callback": "https://anker.example.com/amazon-voc/callback"
}
```

商品 `payload` 支持参数：

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `market` | string | 是 | `US` | 亚马逊站点国家。 |
| `asin` | string | 是 | 无 | Amazon ASIN，系统会转大写。 |
| `include_ratings_by_feature` | boolean | 否 | `false` | 是否需要采集 feature rating；是否返回取决于 Amazon 页面是否展示该模块。 |

推荐安克正式对接使用 `/job/submit`，便于传 `tenant_id/app_id/req_ssn/type/callback`。

## 3. 查询结果

`POST {BASE_URL}/job/result`

请求：

```json
{
  "tenant_id": "anker_001",
  "app_id": "voc",
  "req_ssn": "TL1781759443539",
  "type": "AmazonListingJob"
}
```

`type` 建议必传。新平台按 `tenant_id + app_id + req_ssn + type` 定位唯一任务；如果同一 `req_ssn` 同时存在商品和评论任务，不传 `type` 只能兼容旧查询逻辑，不能表达明确目标。

响应：

```json
{
  "rsp_code": "00000",
  "rsp_msg": "success",
  "req_ssn": "TL1781759443539",
  "status": "finished",
  "type": "AmazonListingJob",
  "task_id": "TL_20260630080817_US_B0F9L1PPPJ_4975b67213",
  "result": {
    "data": "https://oss.example.com/crawler-data/reviews/anker_001/20260630/TL_xxx.json?OSSAccessKeyId=***&Expires=***&Signature=***",
    "code": 200,
    "snapshot": "https://oss.example.com/crawler-data/reviews/anker_001/20260630/TL_xxx_snapshot.html?OSSAccessKeyId=***&Expires=***&Signature=***"
  },
  "result_count": 1,
  "result_data": {
    "asin": "B0F9L1PPPJ",
    "title": "..."
  },
  "result_url": "https://oss.example.com/...",
  "snapshot_url": "https://oss.example.com/...",
  "reason": null,
  "error_msg": null
}
```

状态值：

| 内部状态 | 对外 `status` | 说明 |
| --- | --- | --- |
| `queued` / `pending` | `pending` | 已提交，等待采集。 |
| `running` | `running` | 采集中。 |
| `completed` / `success` / `partial` | `finished` | 终态成功或部分成功。 |
| `failed` / `error` | `failed` | 终态失败。 |

`result.code`：

- `200`：已完成。
- `102`：处理中。
- `500`：失败。

## 4. 回调格式

任务完成后，只有请求里显式传了 `callback` 或 `callback_url`，系统才会向该 URL 发起：

`POST <callback_url>`

Content-Type: `application/json`

如果服务端配置了 `CALLBACK_SECRET`，会额外带：

`X-Callback-Secret: <secret>`

商品详情成功回调：

```json
{
  "reason": null,
  "req_ssn": "TL1781759443539",
  "result": {
    "code": 200,
    "data": "https://oss.example.com/crawler-data/reviews/anker_001/20260630/TL_xxx.json?OSSAccessKeyId=***&Expires=***&Signature=***",
    "snapshot": "https://oss.example.com/crawler-data/reviews/anker_001/20260630/TL_xxx_snapshot.html?OSSAccessKeyId=***&Expires=***&Signature=***"
  },
  "rsp_code": "00000",
  "rsp_msg": "success",
  "status": "finished",
  "type": "AmazonListingJob"
}
```

评论成功回调：

```json
{
  "reason": null,
  "req_ssn": "TR1781759443539",
  "result": {
    "code": 200,
    "data": "https://oss.example.com/crawler-data/reviews/anker_001/20260630/TR_xxx.json?OSSAccessKeyId=***&Expires=***&Signature=***"
  },
  "rsp_code": "00000",
  "rsp_msg": "success",
  "status": "finished",
  "type": "AmazonReviewJob"
}
```

评论空结果回调：

```json
{
  "reason": "no_reviews",
  "req_ssn": "TR1781759443539",
  "result": {
    "code": 200,
    "data": ""
  },
  "rsp_code": "00000",
  "rsp_msg": "success",
  "status": "finished",
  "type": "AmazonReviewJob"
}
```

失败回调：

```json
{
  "reason": "asin_not_found",
  "req_ssn": "TR1781759443539",
  "result": {
    "code": 500,
    "data": ""
  },
  "rsp_code": "E5000",
  "rsp_msg": "failed",
  "status": "failed",
  "type": "AmazonReviewJob"
}
```

回调字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `reason` | string/null | 结果原因。正常有数据时为 `null`；评论无数据为 `no_reviews`；ASIN 不存在为 `asin_not_found`；其他失败可能返回 `error` 或原始错误摘要。 |
| `req_ssn` | string | 安克请求流水号。 |
| `result.code` | int | `200` 成功，`500` 失败。 |
| `result.data` | string | OSS JSON 结果文件签名 URL。评论无数据或 ASIN 不存在时不上传 OSS，该字段为空字符串。 |
| `result.snapshot` | string | 仅商品详情可能返回，商品页 HTML 快照签名 URL。 |
| `rsp_code` | string | `00000` 成功，`E5000` 失败。 |
| `rsp_msg` | string | `success` / `failed`。 |
| `status` | string | `finished` / `failed`。 |
| `type` | string | `AmazonReviewJob` 或 `AmazonListingJob`。 |

注意：

- `result.data` 是签名 URL，有有效期；过期后需要服务端重新签名或重新回调。
- 评论任务只有存在实际评论结果时才上传 OSS；`no_reviews` 和 `asin_not_found` 不上传 OSS，只通过回调和 `/job/result` 返回状态与 `reason`。
- 回调失败会记录并由 daemon 补偿重试，默认只重试未成功的终态任务。
- 不传 `callback/callback_url` 时，任务只落库并可通过 `/job/result` 查询，不触发回调。

## 5. OSS 结果文件结构

评论任务 OSS JSON：

```json
{
  "task_id": "TR_20260630081100_US_B0D62GMQ3F_84226dfb2a",
  "asin": "B0D62GMQ3F",
  "region": "US",
  "biz_source": "CD",
  "task_type": "review",
  "status": 2,
  "status_desc": "success",
  "fail_reason": "",
  "result_count": 100,
  "result": [
    {
      "review_id": "R3MT8EB76V1FD2",
      "title": "異常なし",
      "score": "5.0",
      "star_rate": "5.0",
      "date": "2026-06-21",
      "region": "日本",
      "review_text": "2週間ぐらい使って何も異常ありません！"
    }
  ],
  "error_msg": "",
  "extra": {
    "req_ssn": "TR1781759443539",
    "job_type": "AmazonReviewJob"
  },
  "created_at": "2026-06-30T16:14:19"
}
```

商品详情任务 OSS JSON：

```json
{
  "task_id": "TL_20260630080817_US_B0F9L1PPPJ_4975b67213",
  "asin": "B0F9L1PPPJ",
  "region": "US",
  "biz_source": "",
  "task_type": "listing",
  "status": 2,
  "status_desc": "success",
  "fail_reason": "",
  "result_count": 1,
  "result": {
    "title": "Anker Prime 3-in-1 Charging Station...",
    "brand": "Anker",
    "page_price": 159.99,
    "star_rate": 4.6
  },
  "error_msg": "",
  "created_at": "2026-06-30T16:08:17"
}
```

商品详情上传时 `task_type` 为 `"listing"`；评论上传时 `task_type` 为 `"review"`。对接侧仍建议优先按回调 `type` 判断任务类型。

`status/status_desc/fail_reason`：

| 字段 | 值 | 说明 |
| --- | --- | --- |
| `status` | `2` | 成功。 |
| `status` | `3` | 失败。 |
| `status` | `-1` | ASIN 不存在。 |
| `status_desc` | `success` / `failed` / `asin_not_found` | 状态描述。 |
| `fail_reason` | `""` / `error` / `no_reviews` / `asin_not_found` | 失败分类。 |

## 6. 评论结果字段

评论结果位于 OSS JSON 的 `result[]`。对外 OSS 结果会转换成安克字段；数据库 `amazon_review_jobs.result_data` 仍保留采集器原始字段，便于内部排查和重试。

真实样例对照来自 2026-06-30 本机 Postgres：`asin=B09BRC1XP6`，`req_ssn=TR1781759443539191`，`result_count=100`。原始字段形如 `reviewId/reviewTitle/helpfulNum/isVP/comment`，对外会标准化为下列安克字段。

```json
{
  "review_id": "R3MT8EB76V1FD2",
  "title": "異常なし",
  "useful_num": 0,
  "score": "5.0",
  "star_rate": "5.0",
  "date": "2026-06-21",
  "region": "日本",
  "is_locale_review": true,
  "author": "なーちゃん",
  "author_id": "AHBMUFE5KDUK65DBOPPCUWSAN66A",
  "is_purchased": true,
  "color": "",
  "asin": "B09BRC1XP6",
  "real_asin": "B09BRC1XP6",
  "variations": [
    {
      "asin": "B09BRC1XP6",
      "attributes": []
    }
  ],
  "is_hall_of_fame": false,
  "is_from_outside": false,
  "review_text": "2週間ぐらい使って何も異常ありません！",
  "comment_num": 0,
  "has_image": false,
  "images": [],
  "has_video": false,
  "is_early_reviewer_rewards": false,
  "is_vine_voice": false,
  "is_vine_customer_review_of_free_product": false
}
```

评论字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `review_id` | string | Amazon 评论 ID。由原始 `reviewId/review_id` 映射。 |
| `title` | string | 评论标题。由原始 `reviewTitle/title` 映射。 |
| `useful_num` | int | helpful vote 数。由原始 `helpfulNum` 转整数。 |
| `score` | string | 星级评分，保留一位小数，如 `"5.0"`。 |
| `star_rate` | string | 星级评分，当前与 `score` 一致。 |
| `date` | string | 评论日期，格式通常为 `YYYY-MM-DD`。 |
| `region` | string | 本地化国家/地区名。`JP/Japan` 输出 `日本`；其他站点按映射或原值输出。 |
| `is_locale_review` | boolean | 是否本站本地评论。由原始 `isReviewLocal` 转布尔。 |
| `author` | string | 评论人展示名。 |
| `author_id` | string | 评论人 Amazon profile ID。 |
| `is_purchased` | boolean | 是否 Verified Purchase。由原始 `isVP` 转布尔。 |
| `color` | string | 颜色属性。优先原始 `color`；否则从 `dimension` 中的 `Color:` / `カラー:` / `色:` 提取；没有则为空字符串。 |
| `asin` | string | 评论对应 ASIN；多变体时可能不同于请求 ASIN。 |
| `real_asin` | string | 实际 ASIN。当前缺失时复制 `asin`。 |
| `variations` | array | 变体结构。当前由 `asin` 和 `dimension` 组装为 `[{ "asin": "...", "attributes": [...] }]`。 |
| `is_hall_of_fame` | boolean | Hall of Fame reviewer 标记。当前页面未解析时默认 `false`。 |
| `is_from_outside` | boolean | 是否外站评论。当前由 `is_locale_review` 反向推导。 |
| `review_text` | string | 评论正文。由原始 `comment/review_text/content` 映射。 |
| `comment_num` | int | 评论回复数。当前未解析时默认 `0`。 |
| `has_image` | boolean | 是否有图片。由 `images` 推导。 |
| `images` | array[string] | 评论图片 URL 列表。若原始数据把多个 URL 拼成一个字符串，输出时会拆成多个 URL。 |
| `has_video` | boolean | 是否包含视频。由原始 `hasVideo` 或 `videos` 推导。 |
| `is_early_reviewer_rewards` | boolean | 是否 Early Reviewer Rewards。由原始 `earlyReviewer` 转布尔。 |
| `is_vine_voice` | boolean | 是否 Vine Voice 评论。由原始 `isVineVoice` 转布尔。 |
| `is_vine_customer_review_of_free_product` | boolean | 是否 Vine 免费产品评论。当前页面未解析时默认 `false`。 |

- 安克侧建议以 `review_id` 作为评论去重主键。

原始采集字段到安克字段映射：

| 安克字段 | 原始字段来源 | 补齐/转换规则 |
| --- | --- | --- |
| `review_id` | `reviewId` / `review_id` | 原样字符串。 |
| `title` | `reviewTitle` / `title` / `review_title` | 原样字符串。 |
| `useful_num` | `helpfulNum` / `useful_num` / `helpful_num` | 转 int，空值默认 0。 |
| `score` | `rating` / `star_rating` / `score` | 转一位小数字符串，如 `5` -> `"5.0"`。 |
| `star_rate` | 同 `score` | 当前与 `score` 一致。 |
| `date` | `reviewDate` / `date` / `review_date` | 原样字符串。 |
| `region` | `region` / `countryCode` / `country` | `JP/Japan` 转 `日本`；其他国家按内置映射或原值输出。 |
| `is_locale_review` | `isReviewLocal` / `is_locale_review` | 转 boolean，空值默认 true。 |
| `author` | `reviewerName` / `author` / `reviewer_name` | 原样字符串。 |
| `author_id` | `reviewerId` / `author_id` / `reviewer_id` | 原样字符串。 |
| `is_purchased` | `isVP` / `is_purchased` / `verified_purchase` | 转 boolean。 |
| `color` | `color` / `dimension` | 优先 `color`；否则从 `dimension` 的 `Color:`、`Colour:`、`カラー:`、`色:` 提取；无则空字符串。 |
| `asin` | `asin` / `real_asin` | 优先 `asin`。 |
| `real_asin` | `real_asin` / `asin` | 原始无 `real_asin` 时复制 `asin`。 |
| `variations` | `variations` / `dimension` / `asin` | 原始无 `variations` 时输出 `[{ "asin": real_asin 或 asin, "attributes": dimension }]`。 |
| `is_hall_of_fame` | `is_hall_of_fame` | 当前未解析时默认 false。 |
| `is_from_outside` | `is_from_outside` / `isReviewLocal` | 原始无值时用 `not is_locale_review` 推导。 |
| `review_text` | `comment` / `review_text` / `review_body` / `content` | 原样字符串。 |
| `comment_num` | `comment_num` | 当前未解析时默认 0。 |
| `has_image` | `has_image` / `images` | 原始无值时由 `images` 是否非空推导。 |
| `images` | `images` / `image_urls` | 输出 URL 数组；若原始把多个 URL 拼成一个字符串，会按 `http(s)://` 自动拆分。 |
| `has_video` | `hasVideo` / `has_video` / `videos` | 原始无值时由 `videos` 是否非空推导。 |
| `is_early_reviewer_rewards` | `earlyReviewer` / `is_early_reviewer_rewards` | 转 boolean。 |
| `is_vine_voice` | `isVineVoice` / `is_vine_voice` | 转 boolean。 |
| `is_vine_customer_review_of_free_product` | `is_vine_customer_review_of_free_product` | 当前未解析时默认 false。 |

## 7. 商品详情结果字段

商品详情结果位于 OSS JSON 的 `result`，也会写入 `amazon_listing_jobs.result_data`。

真实样例来自 2026-06-30 本机 Postgres：`asin=B0F9L1PPPJ`，`req_ssn=TL17817594435393`，`result_count=1`。

节选样例：

```json
{
  "http_code": 200,
  "title": "Anker Prime 3-in-1 Charging Station, Qi2.2-Certified 25W Wireless Charger Dock Stand...",
  "brand": "Anker",
  "page_locale": "en-us",
  "regular_price": 229.99,
  "selling_price": 229.99,
  "selling_price_raw": "$159.99",
  "was_price": 229.99,
  "list_price": 229.99,
  "with_deal_price": 159.99,
  "page_price": 159.99,
  "prime_member_price": 159.99,
  "prime_exclusive_discount": 70.0,
  "exist_price": true,
  "sale_statuses": "In Stock",
  "image_url_list": [
    "https://m.media-amazon.com/images/I/711DjCVtUkL._AC_SL1500_.jpg"
  ],
  "main_image_url": "https://m.media-amazon.com/images/I/711DjCVtUkL._AC_SL1500_.jpg",
  "review_num": 673,
  "star_rate": 4.6,
  "bbx_sellerid": "A294P4X9EWVXLJ",
  "seller_name": "AnkerDirect",
  "ranks": [
    {
      "is_main_category": true,
      "sales_rank": 942,
      "links": [
        {
          "link_text": "#942 in Cell Phones & Accessories",
          "main_category": "wireless",
          "sub_category": null
        }
      ]
    }
  ],
  "real_asin": "B0F9L1PPPJ",
  "feature": "Charge at the Highest Wireless Standard Available...",
  "product_information": {
    "Brand": "Anker",
    "Model Number": "A25X7",
    "ASIN": "B0F9L1PPPJ"
  },
  "last_month_sales_raw": "3K+ bought in past month",
  "last_month_sales": 3000,
  "ships_from": "Amazon",
  "add_to_cart": true,
  "product_document": [
    "https://m.media-amazon.com/images/I/B1nUnRJP63L.pdf"
  ],
  "product_description": {
    "images": ["https://m.media-amazon.com/images/S/aplus-media-library-service-media/...jpg"],
    "texts": ["Product description"]
  },
  "aplus_brand_story": {
    "images": ["https://m.media-amazon.com/images/S/aplus-media-library-service-media/...jpg"],
    "texts": ["From the brand"]
  },
  "review_star": {
    "1": 0.04,
    "2": 0.01,
    "3": 0.04,
    "4": 0.1,
    "5": 0.81
  },
  "reviews": []
}
```

商品核心字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `crawl_date` | string/null | 采集日期，当前多为 null。 |
| `http_code` | int | 商品页 HTTP 解析状态，成功通常为 200。 |
| `title` | string | 商品标题。 |
| `brand` | string | 品牌。 |
| `page_locale` | string | 页面语言区域，如 `en-us`。 |
| `real_asin` | string | 页面实际 ASIN。 |
| `sale_statuses` | string | 库存/售卖状态，如 `In Stock`。 |
| `add_to_cart` | boolean | 页面是否可加入购物车。 |
| `is_unavailable` | boolean | 是否不可售或当前不可用。 |
| `asin_variation_values` | object | 变体序号到 ASIN 的映射。 |

价格字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `raw_price` | string/number/null | 原始价格文本或值。 |
| `regular_price` | number/null | 常规价/标价。 |
| `selling_price` | number/null | 销售价字段。 |
| `selling_price_raw` | string/null | 页面展示销售价文本。 |
| `was_price` | number/null | 划线价。 |
| `list_price` | number/null | List Price。 |
| `with_deal_price` | number/null | Deal 价。 |
| `page_price` | number/null | 页面当前可见主价格。 |
| `dotd_price` | number/null | Deal of the Day 价，若有。 |
| `prime_member_price` | number/null | Prime 会员价。 |
| `prime_exclusive_discount` | number/null | Prime 专享折扣金额。 |
| `is_prime_exclusive` | boolean | 是否 Prime 专享。 |
| `exist_price` | boolean | 是否解析到价格。 |
| `is_used_price` | boolean | 是否二手价。 |

评价和排名字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `review_num` | int/null | 评论数或评分数。 |
| `review_num_raw` | string/null | 评论数原始文本。 |
| `star_rate` | number/null | 平均星级。 |
| `star_rate_raw` | string/null | 平均星级原始文本。 |
| `review_star` | object | 星级占比，key 为 1-5，value 为占比。 |
| `ratings_share` | array | 评分分布兼容结构。 |
| `ranks` | array | BSR/类目排名列表。 |
| `cellphones_rank` | string | 排名文本拼接。字段名沿用历史命名，不限手机品类。 |
| `cellphones_rank_format` | string | 格式化排名文本。 |
| `exist_ranks` | boolean | 是否解析到排名。 |

卖家和配送字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `bbx_num` | int/null | Buy Box/卖家数量解析值，页面结构不同可能为空或异常大。 |
| `bbx_num_raw` | string | Buy Box 数原始文本。 |
| `bbx_sellerid` | string/null | Buy Box seller ID。 |
| `seller_name` | string/null | 卖家名称。 |
| `sold_by` | string/null | Sold by 文本。 |
| `sold_bys` | array | 卖家名称列表。 |
| `ships_from` | string/null | Ships from 文本，如 `Amazon`。 |

图片、A+、文档字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `image_url_list` | array | 商品主图/图库大图 URL 列表。 |
| `is_big_image` | boolean | 是否解析到大图。 |
| `main_image_url` | string/null | 主图 URL。 |
| `feature` | string/null | 五点描述/feature bullets 拼接文本。 |
| `product_description` | object | A+ 描述区，包含 `images` 和 `texts`。 |
| `aplus_brand_story` | object | A+ brand story，包含 `images` 和 `texts`。 |
| `product_document` | array | 页面文档链接，如 PDF。 |
| `product_comparison` | object/null | 商品比较模块，当前常为空。 |

商品信息和促销字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `product_information` | object | Product Information/详情表键值对，key 跟随 Amazon 页面。 |
| `item_model_number` | string/null | 型号。 |
| `model_name` | string/null | Model Name。 |
| `product_dimensions` | object/string/null | 商品尺寸。 |
| `item_dimensions` | object/string/null | Item Dimensions。 |
| `item_weight` | number/null | 商品重量，当前为解析到的数值。 |
| `shipping_weight` | number/null | 运输重量。 |
| `last_month_sales_raw` | string/null | 近一个月销量原始文本。 |
| `last_month_sales` | int/null | 近一个月销量数值化结果。 |
| `proportion_coupon` | string/null | 页面优惠券文本。 |
| `code_coupon` | string/null | 优惠码/券文本。 |
| `promo_code` | string/null | 促销码。 |
| `promo_code_desc_raw` | string | 促销码原始描述。 |
| `promotion` | string/null | 促销文本。 |
| `direct_promotion` | string/null | 直接促销文本。 |
| `direct_discount_coupon` | string/null | 直接折扣券。 |
| `direct_code_coupon` | string/null | 直接优惠码。 |
| `exist_sale` | boolean | 是否存在促销。 |
| `is_prime_day` | boolean | 是否 Prime Day 标记。 |
| `is_frequently_returned` | boolean | 是否出现 frequently returned 标记。 |
| `is_customer_usually_keep` | boolean | 是否出现 customers usually keep 标记。 |

其他兼容字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `has_bundle` | boolean | 是否 bundle。 |
| `helpful_reviews` | array | 有帮助评论摘要，当前通常为空。 |
| `also_boughts` | array | Also bought 商品，当前通常为空。 |
| `also_bought_asins` | array | Also bought ASIN 列表。 |
| `pd` | any/null | 历史兼容字段。 |
| `choice_message` | string/null | Amazon's Choice 等提示。 |
| `stock_on_hand` | int/null | 库存数，当前通常无法解析。 |
| `sp_asin` | array | Sponsored product ASIN，当前通常为空。 |
| `sb_brand` | array | Sponsored brand 信息，当前通常为空。 |
| `compare_similar_asins` | array | 相似商品 ASIN，当前通常为空。 |
| `bsr_tag` / `nr_tag` / `cpf_tag` | string/null | 历史标签字段。 |
| `reviews` | array | listing 页直接解析到的少量评论，字段类似评论结果但更轻量。 |

## 8. 错误响应

常见 HTTP 错误：

| HTTP 状态 | 场景 | 响应示例 |
| --- | --- | --- |
| 401 | token 错误或缺失 | `{"detail":"unauthorized"}` |
| 404 | 查询结果时任务不存在 | `{"detail":"job not found"}` |
| 422 | 参数校验失败，如缺少 ASIN 或 `star_filter` 非数组 | FastAPI/Pydantic 校验错误 |

业务失败通常通过查询或回调返回：

```json
{
  "rsp_code": "E5000",
  "rsp_msg": "failed",
  "req_ssn": "TR1781759443539",
  "status": "failed",
  "type": "AmazonReviewJob",
  "result": {
    "data": "",
    "code": 500
  },
  "reason": "error message"
}
```

## 9. 对接建议

1. 安克侧用 `req_ssn` 做幂等和结果关联，不要依赖内部 `task_id`。
2. 评论去重建议用 `review_id`；商品详情主键建议用 `market + real_asin` 或 `market + asin`。
3. 评论时间过滤使用 `payload.query_conditions.date_from`，不要使用 `last_time`。
4. 星级过滤使用 `payload.query_conditions.stars`，不要使用顶层兼容字段 `star_filter`。
5. 回调只返回 OSS URL，不内联完整结果；安克侧收到回调后下载 `result.data`。
6. OSS 签名 URL 有有效期，安克侧建议收到回调后尽快下载并落库。
7. 商品详情字段受 Amazon 页面结构影响，允许字段为空；对接时应按 nullable 处理。
8. 文档中的真实样例已脱敏 OSS 签名参数，实际回调会带完整签名 URL。
9. 当前 Amazon VOC 接口面向安克对接，评论 OSS 结果统一输出安克字段格式；代码没有再按 `tenant_id` 单独分支识别安克。
