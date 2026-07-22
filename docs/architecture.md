# Architecture

## Runtime Shape

The production image is a single container. FastAPI serves `/api/*`, hosts the built React app, and runs the application-level APScheduler collectors independently of login sessions.

## Credential Vault

Deployment-level root secrets are generated into the persistent `/data/runtime-secrets.json` file by default; environment overrides remain available for advanced deployments:

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

Application-level jobs collect M-Team, qB, and NAS snapshots every 10 minutes even when nobody is logged in. M-Team raw snapshots are compacted into rollups, while statistics APIs preserve source, formula, time range, and completeness metadata.


## Database Lifecycle

Alembic owns schema creation and upgrades. A new database is built from the production baseline; a complete pre-Alembic database is stamped at the baseline and upgraded through idempotent compatibility migrations. Partial legacy databases fail closed instead of being altered blindly. SQLite legacy adoption creates a timestamped backup beside the database before any migration.

Database and runtime secrets must be backed up and restored together. Production rollback restores a matching backup and image; destructive schema downgrades are intentionally disabled.
## Diagnostics

Debug traces store trace IDs, event type, timeline, duration, config version, and redacted error summaries. Diagnostic export applies redaction before writing or returning JSON.

