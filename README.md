# PT Media Hub MVP

PT Media Hub is a NAS-first Docker Web App for media discovery, PT download monitoring, statistics, runtime credential management, and diagnostics.

This MVP implements the runnable skeleton with Mock adapters. Real M-Team, qBittorrent, TMDB, AI, and WeChat Claw credentials are entered later in the app Settings page and encrypted in SQLite.

## What Is Implemented

- React + TypeScript + Vite frontend.
- FastAPI backend.
- SQLite WAL, SQLAlchemy models, and Alembic wiring.
- First-run administrator setup wizard.
- JWT login, admin role, and qB 2 temporary admin grant.
- Runtime credential center with encrypted storage, redacted summaries, audit logs, enable/disable, and mock connection tests.
- Mock adapters for M-Team, qB 1/2/3, TMDB, AI, and WeChat Claw.
- Dashboard, Discover, Search, Downloads, Stats, Notifications, Settings, and Debug UI.
- 10 minute snapshot scheduler framework using mock data.
- Safe diagnostics export with redaction.

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

## Deployment Notes

- Keep `APP_CONFIG_ENCRYPTION_KEY` and `JWT_SIGNING_KEY` outside the app UI.
- Do not put M-Team, qB, TMDB, AI, or WeChat Claw business credentials in Docker `.env`.
- For remote access, prefer Tailscale or another private tunnel. Do not expose this app directly to the public internet.

## Default Phase Behavior

All external adapters are Mock implementations. Saving credentials in Settings already exercises encryption, redaction, auditing, and adapter configuration flow. Real integrations should be added in the planned order: qB read monitoring, M-Team snapshots, TMDB discovery/search, then AI and WeChat Claw.

