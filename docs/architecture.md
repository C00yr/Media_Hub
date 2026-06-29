# Architecture

## Runtime Shape

The MVP is a single container. FastAPI serves `/api/*` and hosts the built React app as static files. APScheduler runs inside the same process for the first phase.

## Credential Vault

Deployment-level root secrets live in `.env`:

- `APP_CONFIG_ENCRYPTION_KEY`
- `JWT_SIGNING_KEY`

Service-level credentials are entered by administrators in the Settings page. The backend normalizes the payload, encrypts it with Fernet-compatible encryption, stores it in `integration_configs`, and returns only a redacted summary to the frontend.

Adapters must read the latest enabled config from the backend vault. Config changes write audit rows and do not require a container restart.

## Permissions

- First run creates the initial administrator.
- JWT authenticates API access.
- qB 2 requires a temporary admin verification grant for the current session.
- qB write operations are explicit API calls from user actions and are logged in `download_actions`.
- Background collectors are read-only.

## Statistics

Mock snapshot jobs write M-Team, qB, and NAS records every 10 minutes. Statistics APIs include source, formula, time range, and completeness status so future real data remains traceable.

## Diagnostics

Debug traces store trace IDs, event type, timeline, duration, config version, and redacted error summaries. Diagnostic export applies redaction before writing or returning JSON.

