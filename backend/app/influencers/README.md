# Influencers — native discover adapters

Native replacement for Apify + ScraperAPI. Exposed via HTTP:

- `POST /discover/runs`                       create run
- `GET  /discover/runs/{runId}`               poll status
- `GET  /discover/datasets/{datasetId}/items` fetch items

## Supported platforms

| platform string | input slot      | live status (2026-05-28)                   |
|-----------------|-----------------|--------------------------------------------|
| `youtube_about` | `urls[]`        | ✅ live (replaces ScraperAPI)               |
| `instagram`     | `hashtags[]`    | ✅ live with IG_COOKIES_PATH                |
| `facebook`      | `hashtags[]` (used as search queries) | ✅ live with FB_COOKIES_PATH |
| `tiktok`        | `hashtags[]`    | ⚠️ parser ready, live fetch blocked — see below |
| `tiktok_phone`  | (push only)     | ✅ live via `POST /discover/ingest` from matrix-mvp phone driver |

### TikTok status

TikTok's `/tag/{hashtag}` page no longer ships SSR JSON to unauthenticated
HTTP clients (as of 2026-05). Two lanes available:

1. **`platform=tiktok`** — HTTP fetch + parser. Currently returns `[]`
   (challenge shell). Will work again behind a Playwright lane (Phase 2).
2. **`platform=tiktok_phone`** — phone-pushed ingest. A real device runs the
   TikTok app via Appium, harvests creator handles from the Users-search page,
   and POSTs them to `POST /discover/ingest`. Driver lives in
   `matrix-mvp/poc-tiktok/phone_driver.py`. See its README for setup.

The two share the same CreatorRecord output shape and dataset items API; the
caller just queries `GET /discover/datasets/{runId}/items`.

## Cookie runbook (IG / FB)

Adapters read cookies from JSON files pointed to by env vars:

```
IG_COOKIES_PATH=/app/data/cookies/ig.json    # in-container
FB_COOKIES_PATH=/app/data/cookies/fb.json
```

NAS host path: `/volume1/docker/smart-crawler/app/data/cookies/`.

### How to refresh a cookie jar

**Preferred — TGE 指纹浏览器（免费额度够用）：**

1. 在 TGE 里新建一个干净 profile（指纹独立、IP 走住宅代理），登录
   instagram.com / facebook.com，完成所有人机校验。
2. 用 TGE 的"导出 cookies"功能，导出为 JSON 数组（Playwright 兼容格式）。
   如果 TGE 只能导出 cookies.txt，用下面一行转 JSON：

   ```python
   import json, http.cookiejar as cj
   jar = cj.MozillaCookieJar(); jar.load("cookies.txt", ignore_discard=True)
   open("ig.json","w").write(json.dumps([
       {"name":c.name,"value":c.value,"domain":c.domain,"path":c.path}
       for c in jar
   ]))
   ```

3. `scp ig.json solvea@192.168.1.80:/volume1/docker/smart-crawler/app/data/cookies/`
4. `chmod 600` the file. **No container restart needed** — adapters reload
   on next 401/403.

**Fallback — local Playwright（只在 TGE 不可用时用）：**

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=False)
    ctx = b.new_context()
    page = ctx.new_page()
    page.goto("https://www.instagram.com/")
    input("log in, press Enter...")
    import json
    open("ig.json","w").write(json.dumps(ctx.cookies()))
```

Local Playwright is more prone to IG/FB risk-control loops; TGE +
residential proxy combo has higher session survival.

## CreatorRecord output shape

```python
{
  "channelId": "@sellerjoe",          # platform-prefixed primary key
  "name": "Seller Joe",
  "platform": "TikTok",               # "TikTok"|"Instagram"|"Facebook"|"YouTube"
  "profileUrl": "https://www.tiktok.com/@sellerjoe",
  "handle": "sellerjoe",
  "followerCount": 12345,
  "email": "biz@sellerjoe.com",
  "websiteUrl": "https://sellerjoe.com"
}
```

`youtube_about` returns a thinner shape (it's enrichment, not discovery):

```python
{ "email": "...", "websiteUrl": "..." }
```

## Example calls

```bash
# Instagram hashtag discovery
curl -X POST http://192.168.1.80:8077/discover/runs \
  -H 'Content-Type: application/json' \
  -d '{"platform":"instagram","hashtags":["amazonfba","amazonseller"],"limit":38}'

# Facebook pages search
curl -X POST http://192.168.1.80:8077/discover/runs \
  -H 'Content-Type: application/json' \
  -d '{"platform":"facebook","hashtags":["amazon fba"],"limit":20}'

# YouTube About enrichment
curl -X POST http://192.168.1.80:8077/discover/runs \
  -H 'Content-Type: application/json' \
  -d '{"platform":"youtube_about","urls":["https://www.youtube.com/@MrBeast/about"]}'

# Phone-pushed TikTok (driven from matrix-mvp/poc-tiktok/phone_driver.py)
curl -X POST http://192.168.1.80:8077/discover/ingest \
  -H 'Content-Type: application/json' \
  -d '{"platform":"tiktok_phone","hashtag":"amazonfba","items":[
        {"authorMeta":{"uniqueId":"sellerjoe","nickName":"Seller Joe","fans":12345}}
      ]}'

# Poll + fetch
RID=$(... | jq -r .runId)
curl http://192.168.1.80:8077/discover/runs/$RID            # check status
curl http://192.168.1.80:8077/discover/datasets/$RID/items  # fetch items
```

## Failure modes

| `run.error` value                  | What happened                                            | Action                              |
|------------------------------------|----------------------------------------------------------|-------------------------------------|
| `cookies_expired_instagram: ...`   | IG cookie file missing / 401 / login redirect            | refresh ig.json (see runbook)       |
| `cookies_expired_facebook: ...`    | FB cookie file missing / login or checkpoint redirect    | refresh fb.json                     |
| `unknown platform: ...`            | bad `platform` value in POST body                        | use one of the 4 supported strings  |
| (none, but `itemCount == 0`)       | network call returned challenge page or empty JSON       | check proxy health / TikTok lockdown |
