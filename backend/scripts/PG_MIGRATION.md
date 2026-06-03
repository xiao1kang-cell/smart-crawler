# SQLite → PostgreSQL 迁移说明（NAS 部署）

把 smart-crawler 的生产库从容器内 SQLite 迁到 PostgreSQL。
全程在 NAS 上的项目目录执行。**先备份再操作。**

## 0. 前置

- `docker-compose.yml` 已新增 `postgres` 服务（`postgres:16-alpine`），
  数据持久化在 `./data/pgdata`。
- `smart-crawler` 服务已通过 `DATABASE_URL` 连 postgres，并 `depends_on` 它。
- 默认凭据（可在 `.env` 覆盖）：
  - `POSTGRES_USER=smart_crawler`
  - `POSTGRES_PASSWORD=change-me-strong-password`
  - `POSTGRES_DB=smart_crawler`

> 生产环境建议在 `.env` 里改掉默认密码。

## 1. 备份现有 SQLite

```bash
cp ./data/smart_crawler.db ./data/smart_crawler.db.bak-$(date +%Y%m%d)
```

## 2. 启动 PostgreSQL 容器

```bash
docker compose up -d postgres
docker compose ps          # 等 postgres 状态变 healthy
```

## 3. 跑迁移脚本

迁移脚本在 `smart-crawler` 容器里跑（容器内能同时访问 SQLite 文件和 postgres 网络）。
先确保镜像是最新代码：

```bash
docker compose build smart-crawler
```

然后用一次性容器执行迁移（不启动 web 进程）：

```bash
docker compose run --rm smart-crawler \
  python scripts/migrate_to_pg.py \
    --source sqlite:////app/data/smart_crawler.db \
    --target "$DATABASE_URL"
```

- `--source` 指向容器内挂载的 SQLite（`./data` 挂到 `/app/data`）。
- `--target` 直接用容器里已注入的 `DATABASE_URL`（指向 postgres 服务）。
- 目标表必须为空；若需重跑覆盖，加 `--truncate`。

脚本会：建表 → 逐表 copy（自动转 datetime / date / JSON）→ 修正自增序列。
输出每张表的迁移行数与总计。

预期行数参考：products ~5.4 万 / promotions ~3.6 万 / reviews ~1346 / sites 46。

## 4. 切换并重建

`DATABASE_URL` 已在 `docker-compose.yml` 默认指向 postgres，无需手动改。
直接重建 `smart-crawler`：

```bash
docker compose up -d --build smart-crawler
docker compose logs -f smart-crawler   # 确认启动无 DB 报错
```

启动时 `init_db()` 会再跑一次 `create_all` + `_migrate()`（幂等，安全）。

## 5. 验证

- 打开 `http://<NAS-IP>:8077` 看板，确认商品 / 促销 / 评论数量与迁移前一致。
- 用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录（`_seed_users()` 若用户已迁移则不会重复创建）。
- 触发一次采集任务，确认能正常写入 PostgreSQL。

## 回滚

把 `docker-compose.yml` 里 `smart-crawler` 的 `DATABASE_URL` 临时改成
`sqlite:////app/data/smart_crawler.db`（或在 `.env` 设 `DATABASE_URL`），
再 `docker compose up -d smart-crawler` 即可退回 SQLite。
SQLite 库文件未被迁移脚本修改。

## 兼容性备注

- `db.py` 的 `PRAGMA` 监听、`check_same_thread` 仅在 SQLite 时生效。
- `_migrate()` 用 ANSI `ALTER TABLE ADD COLUMN`，SQLite / PostgreSQL 均兼容。
- `models.py` 全部使用通用类型（JSON / DateTime / Date / Boolean…），无 SQLite 专属类型。
- 迁移脚本保留原始主键 id，并在结束时 `setval` 修正 PostgreSQL 序列，
  避免迁移后新插入数据主键冲突。
