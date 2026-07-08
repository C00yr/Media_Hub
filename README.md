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

Backend:

```bash
cd backend
py -m pip install -e ".[test]"
py -m uvicorn app.main:app --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

## Docker

Create `.env` from `.env.example` and replace both root secrets:

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8000`.

## TMDB Network Modes

TMDB supports exactly two network modes:

- `direct`: default. The backend connects to TMDB directly and uses DoH + IPv4 fallback.
- `proxy`: only TMDB requests use the Mihomo sidecar proxy, usually `http://mihomo:7890`.

qBittorrent, M-Team, NAS storage checks, login, and all other app traffic are direct-only. Do not add global `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY` variables to `pt-media-hub`.

For unattended NAS/Compose deployment, `.env` can optionally provide fallback values:

```bash
TMDB_MODE=direct
TMDB_PROXY_URL=http://mihomo:7890
```

Values saved in the Settings page take priority over `.env`. To use proxy mode, copy `nas-mihomo/config.example.yaml` to `nas-mihomo/config.yaml`, add your own nodes from Clash Verge or your provider, and keep the final `MATCH,DIRECT` rule.

## NAS Storage Mounts

The dashboard storage card scans fixed container paths: `/mnt/storage1`, `/mnt/storage2`, and `/mnt/storage3`. Users only need to replace the left side of the Compose volume mapping with real NAS folders. Keep the right side unchanged:

```yaml
- /volume1/qb1-downloads:/mnt/storage1:ro
- /volume1/qb2-downloads:/mnt/storage2:ro
- /volume1/qb3-downloads:/mnt/storage3:ro
```

If the three folders are on the same NAS storage pool, the backend deduplicates them by device ID and counts the capacity only once. The Settings page shows the detected pool and folder count.

## Deployment Notes

- Keep `APP_CONFIG_ENCRYPTION_KEY` and `JWT_SIGNING_KEY` outside the app UI.
- Do not put M-Team, qB, TMDB, AI, or WeChat Claw business credentials in Docker `.env`.
- For remote access, prefer Tailscale or another private tunnel. Do not expose this app directly to the public internet.
