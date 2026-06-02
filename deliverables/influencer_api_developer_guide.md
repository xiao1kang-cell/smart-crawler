# 红人数据采集 API · 开发者接入指南

> Base URL: `https://smartcrawler.io`
> 鉴权:`Authorization: Bearer sck_...`
> 内测期密钥(临时): `sck_EXAMPLE_REPLACE_ME`

## 1. 完整调用流程(Apify-compatible)

```
POST /discover/runs                          创建任务 → 返 runId + datasetId
GET  /discover/runs/{runId}                  轮询状态 (PENDING / RUNNING / SUCCEEDED / FAILED)
GET  /discover/datasets/{datasetId}/items    取结果(成功后)
```

每个 run 同时也是一个 dataset (runId == datasetId · 简化)。

## 2. 支持平台 + 请求体

```
POST /discover/runs
Content-Type: application/json
Authorization: Bearer sck_...

{
  "platform": "youtube_about | instagram | facebook | tiktok",
  "hashtags": ["furniture", "homedecor"],   // 给 instagram / facebook / tiktok
  "urls":     ["https://www.youtube.com/@MrBeast"],  // 给 youtube_about
  "limit":    20                             // 上限 200
}
```

**⚠️ `hashtags` 和 `urls` 在请求体里是顶级字段, 不要嵌套在 `params` 里**

| platform | 输入字段 | 当前可用 | 备注 |
|---|---|---|---|
| `youtube_about` | `urls[]` | ✅ 直接用 | 不需 cookie |
| `instagram` | `hashtags[]` | ⚠️ 需 `IG_COOKIES_PATH` cookie | 见 §4 |
| `facebook` | `hashtags[]`(用作搜索 query) | ⚠️ 需 `FB_COOKIES_PATH` cookie | 见 §4 |
| `tiktok` | `hashtags[]` | ⚠️ 2026-05 起 challenge gate · parser 已就绪, live fetch 阻塞 | 见 §4 |

## 3. 实战示例

### A. YouTube About(立即可用)

**请求**:
```bash
curl -X POST 'https://smartcrawler.io/discover/runs' \
  -H 'Authorization: Bearer sck_EXAMPLE_REPLACE_ME' \
  -H 'Content-Type: application/json' \
  -d '{
    "platform": "youtube_about",
    "urls": ["https://www.youtube.com/@MrBeast", "https://www.youtube.com/@MKBHD"],
    "limit": 2
  }'
```

**返回**:
```json
{"runId":"19abf244...","datasetId":"19abf244...","status":"PENDING"}
```

**轮询 + 取结果**:
```bash
# 状态(等到 SUCCEEDED, 通常 1-3 秒)
curl -H 'Authorization: Bearer sck_...' \
  'https://smartcrawler.io/discover/runs/19abf244...'

# {"status":"SUCCEEDED","itemCount":2,"startedAt":...,"finishedAt":...}

# items
curl -H 'Authorization: Bearer sck_...' \
  'https://smartcrawler.io/discover/datasets/19abf244.../items'
```

**结果格式**:
```json
[
  {"email":null,"websiteUrl":"https://bit.ly/mrbeastbowandarrow"},
  {"email":"business@MKBHD.com","websiteUrl":"http://shop.MKBHD.com/"}
]
```

### B. Instagram hashtag(需 cookie · 暂不可用)

```bash
curl -X POST 'https://smartcrawler.io/discover/runs' \
  -H 'Authorization: Bearer sck_...' \
  -H 'Content-Type: application/json' \
  -d '{
    "platform": "instagram",
    "hashtags": ["furniture", "homedecor"],
    "limit": 10
  }'
```

无 cookie 时返回:
```json
{"status":"FAILED","error":"CookieExpiredError: cookies_expired_instagram: file /app/data/cookies/ig.json not found"}
```

## 4. Cookie 需求(IG / FB / TT)

平台需 cookie 才能抓 hashtag 数据。两条路:

1. **平台方提供** · 用 TGE 指纹浏览器 + 住宅代理登录后导出 `ig.json` / `fb.json` / `tt.json`(Playwright 格式), 落 NAS `/app/data/cookies/`
2. **客户提供** · 自己提供活跃 cookie 文件(同样格式)

详见 `backend/app/influencers/README.md`。

## 5. 数据 schema

成功 items 是平台-specific dict。YT About 返 `{email, websiteUrl}`,IG/FB/TT 返完整 `InfluencerProfile`:

```typescript
{
  platform: "instagram" | "facebook" | "tiktok" | "youtube_about",
  username: string,
  user_id: string | null,
  display_name: string | null,
  bio: string | null,
  avatar_url: string | null,
  is_verified: boolean,
  is_business: boolean,
  category: string | null,
  followers: number | null,
  following: number | null,
  posts_count: number | null,
  likes_total: number | null,    // TikTok 特有
  contact: { email, whatsapp, linktree, website },
  external_url: string | null,
  raw_url: string | null,
  fetched_at: string | null,     // ISO datetime
  fetched_via: string | null,    // 来源标识
  notes: string | null
}
```

(YouTube About 是简化版,仅 `email` + `websiteUrl`)

## 6. 错误码

| 场景 | HTTP | 响应 |
|---|---|---|
| 缺 / 错 API key | 401 | `{"error":"unauthorized"}` |
| 未知 platform | 400 | `{"detail":"unknown platform: ...Supported: [...]"}` |
| runId 不存在 | 404 | `{"detail":"run not found: ..."}` |
| Cookie 过期 | 200 (status=FAILED) | `error` 字段含 `CookieExpiredError` |

## 7. 限制

- `limit` 上限 200
- Run 状态在内存 (run_registry · 单进程)· 容器重启 run 状态丢, 但 dataset items 还在
- 单 IP 调用建议 ≤ 60 / min(平台风控)

## 8. 内测期联系

- API base: https://smartcrawler.io
- 内测期密钥(请勿外传): `sck_EXAMPLE_REPLACE_ME`
- 问题反馈: 直接联系 Bo Yuan (boyuan@solvea.cx)

---

最后更新:2026-06-01 · 内测期间所有调用都计入 billing
