from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from pangu.database import DatabaseService

REQUIRED_TABLES = {
    "commands",
    "events",
    "tool_specifications",
    "tool_executions",
    "permission_grants",
    "approvals",
    "approval_consumptions",
    "approval_revocations",
    "audit_records",
    "runtime_health",
    "missions",
    "mission_tasks",
    "mission_checkpoints",
}


def test_empty_database_migrates_to_head_with_sqlite_guards(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "database" / "pangu.db", timeout_seconds=3)
    database.start()
    try:
        assert database.health() == {"status": "ready", "journal_mode": "wal", "foreign_keys": "1"}
        assert REQUIRED_TABLES <= set(inspect(database._engine).get_table_names())
        with database._engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "0002_persistent_exact_approval"
            )
    finally:
        database.stop()


def test_migration_startup_is_idempotent_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "database" / "pangu.db"
    first = DatabaseService(path)
    first.start()
    first.stop()
    second = DatabaseService(path)
    second.start()
    try:
        assert second.health()["status"] == "ready"
    finally:
        second.stop()
        second.stop()
