# PT Media Hub MVP

PT Media Hub is a NAS-first Docker Web App for media discovery, PT download monitoring, statistics, runtime credential management, and diagnostics.

## What Is Implemented

- React + TypeScript + Vite frontend.
- FastAPI backend.
- SQLite WAL, SQLAlchemy models, and Alembic wiring.
- First-run administrator setup wizard.
- JWT login, admin role, and qB 2 temporary admin grant.
- Runtime credential center with encrypted storage, redacted summaries, audit logs, enable/disable, and connection tests.
- Real adapters for M-Team, qBittorrent, and TMDB, with mock fallbacks for unfinished areas.
- Dashboard, Discover, Search, Downloads, Stats, Notifications, Settings, and Diagnostics UI.

## Local Development

### Windows one-click start (recommended)

1. Double-click `start-dev.cmd` in the project root.
2. Open `http://127.0.0.1:5173`.
3. Double-click `stop-dev.cmd` when finished.

The launcher always stores local development data in `data/local`, while NAS/Docker stores its data in the mounted `/data` directory. It refuses to replace an existing frontend on port 5173, automatically selects a backend port from 18001-18010, and does not use Uvicorn `--reload`. This prevents a local development process from sharing the NAS SQLite database or generating an uncontrolled Windows reload log.

On the first run, the launcher creates `backend/.venv` and installs the backend and frontend dependencies automatically. If Python 3.12+ or Node.js LTS is missing, it uses Windows `winget` to install it and continues in the same launch. The first run therefore needs an internet connection. Windows 10/11 with App Installer (`winget`) is required only when the prerequisite is not already installed.

For manual advanced startup, use the same `APP_RUNTIME_PROFILE=local`, `APP_DATA_DIR=<project>/data/local`, database, and secret-file values used by `scripts/start-dev.ps1`. Do not use the NAS `/data` path and do not add `--reload` on Windows.

## Docker

For NAS or Docker Compose deployment, create a persistent `data` folder and start the stack:

```bash
mkdir -p data
docker compose up --build
```

Open `http://localhost:8000`.

The app generates its runtime encryption/JWT secrets on first start and stores them in
`data/runtime-secrets.json`. Keep the `data` folder when upgrading or recreating containers.
Advanced users can still create `.env` from `.env.example` to override network/storage defaults.

Browser timestamps and request-time calendar labels use the viewer device's current system timezone. Background jobs and WeChat replies without a browser context use `APP_TIMEZONE` (an IANA name such as `Asia/Shanghai`); the bundled Compose file defaults to `Asia/Shanghai`. Set this value in `.env` when the NAS uses another timezone. Timestamps remain stored in UTC so changing the display timezone does not rewrite historical data.


Source-code development and NAS deployment are separate application instances. Do not copy the NAS database into `data/local`, and do not point local `DATABASE_URL` at a NAS share. The backend validates this at startup and rejects accidental cross-profile SQLite paths.

## Media Search Proxy

The Settings > Media Search page can connect TMDB to an existing HTTP/HTTPS proxy such as Mihomo. Use `http://mihomo:7890` for a private unauthenticated Docker-only endpoint, or an authenticated URL such as `http://mediahub:PASSWORD@mihomo:7890` with the bundled example. Proxy routing is controlled by a fixed application allowlist:

- `api.themoviedb.org`: search, discover, details, people, trends, and filters.
- `image.tmdb.org`: posters, backdrops, profiles, and logos.

Each domain can be enabled independently. Unselected domains connect directly with DoH + IPv4 fallback, even while the proxy switch is on.

qBittorrent, M-Team, NAS storage checks, login, and all other app traffic are direct-only. Do not add global `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY` variables to `pt-media-hub`.

For unattended NAS/Compose deployment, `.env` can optionally provide fallback values:

```bash
TMDB_MODE=direct
TMDB_PROXY_URL=http://mihomo:7890
```

Values saved in the Settings page take priority over `.env`; legacy `TMDB_MODE=proxy` enables both allowed TMDB domains. The default `docker-compose.yml` does not start Mihomo. Direct mode needs no proxy files, while proxy mode requires an existing proxy that the Media Hub container can reach. Domain choices are managed in the Media Hub UI and do not need to be duplicated in Media Hub's Compose YAML.

## NAS Storage Mounts

The dashboard storage card scans fixed container paths: `/mnt/storage1`, `/mnt/storage2`, and `/mnt/storage3`. Users only need to replace the left side of the Compose volume mapping with real NAS folders. Keep the right side unchanged:

```yaml
- /volume1/qb1-downloads:/mnt/storage1:ro
- /volume1/qb2-downloads:/mnt/storage2:ro
- /volume1/qb3-downloads:/mnt/storage3:ro
```

If the three folders are on the same NAS storage pool, the backend deduplicates them by device ID and counts the capacity only once. The Settings page shows the detected pool and folder count.

## Deployment Notes

- Keep the `data` folder private and persistent. It contains the app database and generated runtime secrets.
- You do not need to create `.env` for a normal single-NAS deployment.
- Do not put M-Team, qB, TMDB, AI, or WeChat Claw business credentials in Docker `.env`.
- For remote access, prefer Tailscale or another private tunnel. Do not expose this app directly to the public internet.
