import pytest
from sqlalchemy import create_engine, inspect

from app.db.migrations import upgrade_database
from app.db.session import Base
from app.models import entities  # noqa: F401


def test_new_database_is_created_at_alembic_head():
    engine = create_engine("sqlite:///:memory:")
    upgrade_database(engine, "sqlite:///:memory:")

    tables = set(inspect(engine).get_table_names())
    assert {"users", "media_favorites", "mteam_traffic_rollups", "qb_delete_confirmations"} <= tables
    with engine.connect() as connection:
        assert connection.exec_driver_sql("select version_num from alembic_version").scalar_one() == "20260722_0004"


def test_complete_legacy_database_is_adopted_without_data_loss(tmp_path):
    database_path = tmp_path / "legacy.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "insert into users (username, password_hash, role, is_active, created_at, updated_at) "
            "values ('legacy-admin', 'hash', 'admin', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

    backup = upgrade_database(engine, database_url)

    assert backup is not None and backup.is_file()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("select username from users").scalar_one() == "legacy-admin"
        assert connection.exec_driver_sql("select version_num from alembic_version").scalar_one() == "20260722_0004"
    backup_engine = create_engine(f"sqlite:///{backup.as_posix()}")
    with backup_engine.connect() as connection:
        assert connection.exec_driver_sql("select username from users").scalar_one() == "legacy-admin"


def test_partial_legacy_database_fails_closed():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("create table users (id integer primary key)")

    with pytest.raises(RuntimeError, match="partially initialized"):
        upgrade_database(engine, "sqlite:///:memory:")
