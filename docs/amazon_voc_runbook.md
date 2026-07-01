# Amazon VOC 操作手册

这份手册覆盖 Amazon VOC 的本地测试和生产运行：API、daemon、商品详情 worker、评论 worker、日志、停止、重启和常见排查。

## 组件说明

Amazon VOC 主要有四类进程：

| 组件 | 作用 |
| --- | --- |
| API | 提供 `/job/submit`、`/job/result`、本地测试回调接口和 Swagger 文档。 |
| daemon | 队列回填、队列深度采集、账号风险分析、任务表归档、失败回调补发。 |
| 商品详情 worker | 消费 `AmazonListingJob`，抓商品详情，上传 result JSON 和 snapshot HTML。 |
| 评论 worker | 消费 `AmazonReviewJob`，抓评论列表，上传 result JSON。 |

daemon 不负责抓取商品或评论，但回调失败补发依赖 daemon 长期运行。商品详情回调会带 `result.snapshot`，评论回调不会带 `snapshot`。

## 路径

项目根目录：

```bash
/Users/edy/PycharmProjects/PythonProject/smart-crawler
```

常用脚本：

```bash
scripts/amazon_voc_daemon.sh
scripts/start_amazon_voc_listing_prod.sh
scripts/start_amazon_voc_review_us_prod.sh
scripts/start_amazon_voc_review_other_prod.sh
scripts/run_amazon_voc_worker_test.sh
scripts/run_amazon_voc_worker_prod.sh
```

常用日志目录：

```bash
logs/amazon_voc
backend/app/crawlers/amazon_crawler/shuler/logs
```

## 环境

本地测试：

```bash
APP_ENV=test
```

生产：

```bash
APP_ENV=production
```

测试和生产真实配置由根目录 `.env.test` / `.env.production` 加载，并通过 `SC_EXTRA_ENV_FILE` 加载：

```bash
backend/app/crawlers/amazon_crawler/secrets/amazon-voc.test.env
backend/app/crawlers/amazon_crawler/secrets/amazon-voc.prod.env
```

## API

本地 API 端口通常是 `8077`：

```text
http://localhost:8077/docs
```

本地测试回调地址：

```text
http://127.0.0.1:8077/api/v1/test/delivery/receive
```

查看最近一次测试回调：

```text
http://127.0.0.1:8077/api/v1/test/delivery/receive/latest
```

提交任务时，`callback` 优先级最高；如果请求没有传 `callback`，会按 `tenant_id` 使用租户默认回调地址。

## Daemon

daemon 管理脚本：

```bash
cd /Users/edy/PycharmProjects/PythonProject/smart-crawler
./scripts/amazon_voc_daemon.sh help
```

本地测试启动：

```bash
APP_ENV=test API_URL=http://127.0.0.1:8077 ./scripts/amazon_voc_daemon.sh start
```

生产启动：

```bash
APP_ENV=production API_URL=http://127.0.0.1:8077 ./scripts/amazon_voc_daemon.sh start
```

查看状态：

```bash
./scripts/amazon_voc_daemon.sh status
```

停止：

```bash
./scripts/amazon_voc_daemon.sh stop
```

重启：

```bash
APP_ENV=production API_URL=http://127.0.0.1:8077 ./scripts/amazon_voc_daemon.sh restart
```

查看 daemon stdout/stderr 日志：

```bash
./scripts/amazon_voc_daemon.sh logs 200
./scripts/amazon_voc_daemon.sh tail
```

查看 daemon 内部业务日志：

```bash
tail -f backend/app/crawlers/amazon_crawler/shuler/logs/daemon_$(date +%F).log
```

确认 callback 补发线程启动：

```bash
grep "CallbackRetry 已启动" backend/app/crawlers/amazon_crawler/shuler/logs/daemon_$(date +%F).log
```

确认队列回填启动：

```bash
grep "TaskQueueBackfill" backend/app/crawlers/amazon_crawler/shuler/logs/daemon_$(date +%F).log
```

## 商品详情 Worker

生产后台启动商品详情 worker：

```bash
cd /Users/edy/PycharmProjects/PythonProject/smart-crawler
COUNT=3 ./scripts/start_amazon_voc_listing_prod.sh
```

日志：

```bash
tail -f logs/amazon_voc/listing.out.log
tail -f backend/app/crawlers/amazon_crawler/shuler/logs/asin_worker_$(date +%F).log
```

停止：

```bash
kill -TERM "$(cat logs/amazon_voc/listing.pid)"
```

如果没有退出：

```bash
kill -KILL "$(cat logs/amazon_voc/listing.pid)"
rm -f logs/amazon_voc/listing.pid
```

重启：

```bash
kill -TERM "$(cat logs/amazon_voc/listing.pid)" || true
rm -f logs/amazon_voc/listing.pid
COUNT=3 ./scripts/start_amazon_voc_listing_prod.sh
```

本地前台调试商品详情 worker：

```bash
cd /Users/edy/PycharmProjects/PythonProject/smart-crawler/backend
APP_ENV=test ../.venv/bin/python -m app.crawlers.amazon_worker --listing --workers 1
```

如果上次 `Ctrl+C` 后留下 stop signal，启动商品 worker 时加清理参数：

```bash
APP_ENV=test ../.venv/bin/python app/crawlers/amazon_crawler/shuler/services/amazon/asin_worker.py --clear-stop-signal
```

商品详情成功后会上传：

```text
result.data     -> result JSON
result.snapshot -> snapshot HTML
```

如果 snapshot HTML 上传 OSS 失败，任务会保留 `snapshot_html`，daemon 后续会重新上传并补发回调。

## 评论 Worker

生产后台启动美国评论 worker：

```bash
cd /Users/edy/PycharmProjects/PythonProject/smart-crawler
COUNT=3 ./scripts/start_amazon_voc_review_us_prod.sh
```

生产后台启动非美国评论 worker：

```bash
COUNT=3 ./scripts/start_amazon_voc_review_other_prod.sh
```

日志：

```bash
tail -f logs/amazon_voc/review-us.out.log
tail -f logs/amazon_voc/review-other.out.log
tail -f backend/app/crawlers/amazon_crawler/shuler/logs/single_worker_$(date +%F).log
```

停止：

```bash
kill -TERM "$(cat logs/amazon_voc/review-us.pid)"
kill -TERM "$(cat logs/amazon_voc/review-other.pid)"
```

重启美国评论 worker：

```bash
kill -TERM "$(cat logs/amazon_voc/review-us.pid)" || true
rm -f logs/amazon_voc/review-us.pid
COUNT=3 ./scripts/start_amazon_voc_review_us_prod.sh
```

本地前台调试评论 worker：

```bash
cd /Users/edy/PycharmProjects/PythonProject/smart-crawler
APP_ENV=test ./scripts/run_amazon_voc_worker_test.sh
```

评论任务回调只包含：

```json
{
  "result": {
    "data": "...reviews.json",
    "code": 200
  }
}
```

## 生产整体启动顺序

建议顺序：

```bash
cd /Users/edy/PycharmProjects/PythonProject/smart-crawler
APP_ENV=production API_URL=http://127.0.0.1:8077 ./scripts/amazon_voc_daemon.sh start
COUNT=3 ./scripts/start_amazon_voc_listing_prod.sh
COUNT=3 ./scripts/start_amazon_voc_review_us_prod.sh
COUNT=3 ./scripts/start_amazon_voc_review_other_prod.sh
```

API 也要保持运行在 `8077`，否则 Swagger、提交任务、任务查询和本地测试回调不可用。

## 生产整体停止

```bash
cd /Users/edy/PycharmProjects/PythonProject/smart-crawler
./scripts/amazon_voc_daemon.sh stop
kill -TERM "$(cat logs/amazon_voc/listing.pid)" 2>/dev/null || true
kill -TERM "$(cat logs/amazon_voc/review-us.pid)" 2>/dev/null || true
kill -TERM "$(cat logs/amazon_voc/review-other.pid)" 2>/dev/null || true
```

清理 stale pid：

```bash
rm -f logs/amazon_voc/listing.pid logs/amazon_voc/review-us.pid logs/amazon_voc/review-other.pid
```

## 常见排查

查看所有 Amazon VOC 后台进程：

```bash
ps aux | grep -E "amazon_daemon|amazon_worker|asin_worker|get_reviews_main" | grep -v grep
```

查看 daemon 状态：

```bash
./scripts/amazon_voc_daemon.sh status
```

查看最近回调：

```text
http://127.0.0.1:8077/api/v1/test/delivery/receive/latest
```

商品详情没有 `snapshot` 时，查商品任务表字段：

```text
snapshot_url
snapshot_object_key
snapshot_html
callback_status
callback_last_error
```

含义：

| 字段 | 含义 |
| --- | --- |
| `snapshot_url` | 已上传 OSS 后生成的访问 URL。 |
| `snapshot_object_key` | OSS object key，daemon 可用它重新生成 URL。 |
| `snapshot_html` | snapshot 上传失败时暂存的大 HTML，daemon 后续重传成功后会清空。 |
| `callback_status` | `pending` / `failed` 会被 daemon 补发；`success` 不再补发。 |
| `callback_last_error` | 最近一次 OSS 上传或 callback 失败原因。 |

如果商品 worker 报 `sleep length must be non-negative`，说明旧代码还在运行；重启 worker 后会加载修复。

如果清理 stop signal 失败并提示 Redis 域名无法解析，说明当前机器网络访问不到测试 Redis。可以等 10 分钟 TTL 自动过期，或切换到可访问 Redis 的网络。
