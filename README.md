# PT Media Hub

PT Media Hub is a NAS-first Docker Web App for media discovery, PT download monitoring, statistics, runtime credential management, and diagnostics.

## What Is Implemented

- React + TypeScript + Vite frontend.
- FastAPI backend.
- SQLite WAL, SQLAlchemy models, and automatic Alembic upgrades.
- First-run administrator setup wizard.
- JWT login, admin role, and qB 2 temporary admin grant.
- Runtime credential center with encrypted storage, redacted summaries, audit logs, enable/disable, and connection tests.
- Real adapters for M-Team, qBittorrent, TMDB, AI, and WeChat claw.
- Dashboard, Discover, Search, Downloads, Stats, Notifications, Settings, and Diagnostics UI.

## Docker

For NAS or Docker Compose deployment, create a persistent `data` folder and start the stack:

```bash
mkdir -p data
docker compose up --build
```

Open `http://localhost:8000`.

## Windows 本地一键启动

在项目根目录双击 `启动影视中枢.cmd`，脚本会在后台启动本地后端（8001）和前端（5173），等待健康检查通过后自动打开浏览器。双击 `关闭影视中枢.cmd` 可关闭由该脚本启动的前后端进程，不会按名称批量终止电脑上的其他 Python 或 Node 程序。

首次运行时，如果缺少本地依赖，启动脚本会自动创建 `backend/.venv`、安装后端依赖并执行 `npm ci`。运行状态和启动日志保存在被 Git 忽略的 `.tmp-local-run` 目录。

The app generates its runtime encryption/JWT secrets on first start and stores them in
`data/runtime-secrets.json`. Keep the `data` folder when upgrading or recreating containers.
Advanced users can still create `.env` from `.env.example` to override network/storage defaults.

Browser timestamps, request-time calendar labels, and AI replies use the viewer device's current IANA timezone. Background jobs and WeChat replies without a browser context use `APP_TIMEZONE` (for example, `Asia/Shanghai`). The bundled Compose file defaults both `APP_TIMEZONE` and the container `TZ` to `Asia/Shanghai`; set `APP_TIMEZONE` in `.env` when the NAS should use another timezone. Timestamps remain stored in UTC so changing the display timezone does not rewrite historical data.

## Media Search Proxy

The Settings > Media Search page can route TMDB through an existing Mihomo container. Media Hub and Mihomo join the private external Docker network `media-hub-egress`; Mihomo receives the stable alias `tmdb-egress-proxy`. The UI stores protocol, host, port, optional username, and optional password as separate fields. Proxy routing is controlled by a fixed application allowlist:

- `api.themoviedb.org`: search, discover, details, people, trends, and filters.
- `image.tmdb.org`: posters, backdrops, profiles, and logos.

The proxy switch always routes both allowed domains together. When it is off, the existing DoH path is unchanged. When it is on, both domains must use Mihomo; failures are retried only through the proxy and never silently fall back to direct access.

qBittorrent, M-Team, NAS storage checks, login, and all other app traffic are direct-only. Do not add global `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY` variables to `pt-media-hub`.

The default `docker-compose.yml` does not start Mihomo. Use `docker-compose.tmdb-proxy.yml` only when connecting an existing Mihomo container. Full setup instructions, including the companion Mihomo network fragment, are in [docs/nas-mihomo-tmdb-proxy.md](docs/nas-mihomo-tmdb-proxy.md).

## Media Hub Agent

The in-app assistant and WeChat Claw share one autonomous Media Hub Agent. The model is not routed through a fixed intent classifier: it decides whether to answer directly or call one or more tools for TMDB lookup and details, M-Team search and refreshed release details, dashboard/diagnostics, qB task summaries/details, and confirmed downloads.

Agent sessions retain bounded conversation history and safe result references for 24 hours. Follow-ups such as "show the complete overview for the fourth result" or "how long is the second release free for?" resolve against the prior real result and call the appropriate detail service again. Starting a new web conversation creates an isolated context.

All tool observations and API responses are bounded and redacted before reaching the model. Credentials, cookies, tokens, account identifiers, internal paths, IPs, qB hashes, and tracker URLs are excluded. qB2 task details still require its temporary privacy grant. A download is executed only when a resource is already pending and the current user message explicitly confirms it.


## NAS Storage Mounts

The dashboard storage card scans fixed container paths: `/mnt/storage1`, `/mnt/storage2`, and `/mnt/storage3`. Users only need to replace the left side of the Compose volume mapping with real NAS folders. Keep the right side unchanged:

```yaml
- /volume1/qb1-downloads:/mnt/storage1:ro
- /volume1/qb2-downloads:/mnt/storage2:ro
- /volume1/qb3-downloads:/mnt/storage3:ro
```

If the three folders are on the same NAS storage pool, the backend deduplicates them by device ID and counts the capacity only once. The Settings page shows the detected pool and folder count.

## 首次部署与初始化

1. 在 NAS 上创建一个仅供本应用使用的目录，将仓库内容放入其中。
2. 确认 `docker-compose.yml` 中三个媒体目录左侧路径对应真实 NAS 目录，右侧 `/mnt/storage1~3` 保持不变。
3. 执行 `docker compose up -d --build`，健康检查通过后访问 `http://NAS地址:8000`。
4. 按首次启动向导创建管理员账号，再到设置页填写 M-Team、qBittorrent、TMDB、AI 和 WeChat claw 配置。

首次启动会在持久化的 `data` 目录中创建 SQLite 数据库和 `runtime-secrets.json`。两者是一套数据，缺少其中任意一个都可能导致已保存的服务凭据无法解密。

### 超级密码

首次启动时，应用会自动生成一次超级密码，并在容器日志中输出以下提示：

```text
Recovery super password generated. Store it securely: <超级密码>
```

可通过 `docker compose logs pt-media-hub` 查看首次启动日志。超级密码无需匹配用户名即可登录管理员账号，也可以在账号设置的“当前密码”位置代替原密码，用于重新设置管理员用户名和密码。

本应用默认运行在个人 NAS 或私有云环境中，超级密码会保留在首次启动日志里，便于设备所有者找回账号。请将 NAS 管理权限、项目 `data` 目录和容器日志视为同等级别的敏感资源，不要向外分享日志全文；远程访问时应使用绿联云、Tailscale 等受控的私有访问方式。获取超级密码后建议单独妥善保存，因为数据库中只保存密码哈希，无法从数据库还原原文。


## 升级、备份与回滚

升级前先停止容器，并同时备份数据库、运行时密钥和 Compose 配置：

```bash
docker compose down
cp -a data data-backup-$(date +%Y%m%d-%H%M%S)
cp docker-compose.yml docker-compose.yml.backup
docker compose up -d --build
```

应用启动时会执行 Alembic 迁移。首次接管旧版本数据库时，会在数据库旁额外生成一个 `.pre-alembic-时间.bak` 文件。升级完成后检查 `/health`、仪表盘、诊断页以及各下载器连接。

如需回滚，先停止容器，恢复与旧镜像同一时刻备份的整个 `data` 目录和 Compose 文件，再启动旧镜像。不要只恢复数据库或只恢复 `runtime-secrets.json`，也不要对正式数据库手工执行 Alembic downgrade。

## 时区

浏览器页面、请求时的日期标签和网页内 AI 回复使用当前设备报告的 IANA 时区；无人访问时的后台任务和 WeChat claw 回复使用 `APP_TIMEZONE`。Compose 默认把 `APP_TIMEZONE` 和容器 `TZ` 都设为 `Asia/Shanghai`。更换地区时在 `.env` 中设置同一个 IANA 时区名称，例如 `Asia/Tokyo`，然后重建容器。历史时间始终按 UTC 存储。

## Mihomo 连接

Mihomo 不由本项目自动启动。已有 Mihomo 的用户先创建 `media-hub-egress` 外部 bridge 网络，让 Mihomo 以别名 `tmdb-egress-proxy` 加入，再用 `docker-compose.tmdb-proxy.yml` 部署 Media Hub。设置页默认填写主机 `tmdb-egress-proxy` 和端口 `7890`，点击“测试并启用”后会逐层验证容器名称解析、TCP 端口、认证、TMDB API 与图片 CDN。完整操作见 [NAS Mihomo 接入 TMDB](docs/nas-mihomo-tmdb-proxy.md)。


## Deployment Notes

- Keep the `data` folder private and persistent. It contains the app database and generated runtime secrets.
- Never commit the `data` directory, databases, runtime secrets, logs, exports, or packaged archives. The repository ignores these files by default.
- You do not need to create `.env` for a normal single-NAS deployment.
- Do not put M-Team, qB, TMDB, AI, or WeChat Claw business credentials in Docker `.env`.
- For remote access, prefer Tailscale or another private tunnel. Do not expose this app directly to the public internet.
