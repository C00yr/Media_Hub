from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect
from sqlalchemy.engine import make_url


BASELINE_REVISION = "20260629_0001"
BASELINE_TABLES = {
    "users",
    "user_sessions",
    "wechat_claw_bindings",
    "integration_configs",
    "config_audit_logs",
    "debug_traces",
    "download_actions",
    "mteam_snapshots",
    "qb_snapshots",
    "qb_torrent_snapshots",
    "nas_disk_snapshots",
    "stat_rules",
    "stat_results",
    "notifications",
    "settings",
}


def _alembic_config(connection) -> Config:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.attributes["connection"] = connection
    return config


def _backup_legacy_sqlite(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    path = Path(url.database).expanduser().resolve()
    if not path.is_file():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.pre-alembic-{stamp}.bak")
    with sqlite3.connect(path) as source:
        with sqlite3.connect(backup) as target:
            source.backup(target)
    return backup


def upgrade_database(engine: Engine, database_url: str) -> Path | None:
    """Upgrade a new database or safely adopt a pre-Alembic installation."""

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    has_version_table = "alembic_version" in tables
    app_tables = tables - {"alembic_version"}
    backup: Path | None = None

    if app_tables:
        missing = BASELINE_TABLES - app_tables
        if missing:
            names = ", ".join(sorted(missing))
            raise RuntimeError(
                "The existing database is only partially initialized and cannot be adopted automatically. "
                f"Missing baseline tables: {names}. Restore a complete backup before upgrading."
            )
        if not has_version_table:
            backup = _backup_legacy_sqlite(database_url)

    with engine.begin() as connection:
        config = _alembic_config(connection)
        if app_tables and not has_version_table:
            command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, "head")
    return backup
