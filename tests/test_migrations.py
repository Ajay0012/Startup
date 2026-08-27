from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from pangu.database import (
    ApplicationAliasRow,
    ApplicationCatalogRow,
    ApplicationEvidenceRow,
    DatabaseService,
)

CURRENT_HEAD = "0008_reconcile_persistent_intelligence"

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
    "applications",
    "application_aliases",
    "application_evidence",
    "application_discovery_runs",
    "memory_records",
    "world_facts",
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
                == CURRENT_HEAD
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


def test_existing_0003_database_upgrades_to_current_head(tmp_path: Path) -> None:
    path = tmp_path / "database" / "pangu.db"
    path.parent.mkdir(parents=True, exist_ok=True)

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(config, "0003_application_catalog")
    upgraded = DatabaseService(path)
    upgraded.start()
    try:
        assert upgraded.health_details()["migration_revision"] == CURRENT_HEAD
    finally:
        upgraded.stop()


def test_application_catalog_constraints_and_cascade(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "catalog.db")
    database.start()
    try:
        assert database._engine is not None
        inspector = inspect(database._engine)
        assert {item["name"] for item in inspector.get_indexes("applications")} >= {
            "ix_applications_normalized_name"
        }
        assert {item["name"] for item in inspector.get_indexes("application_aliases")} >= {
            "ix_application_aliases_alias",
            "ix_application_aliases_application_id",
        }
        assert (
            inspector.get_foreign_keys("application_evidence")[0]["options"].get("ondelete")
            == "CASCADE"
        )
        with database.transaction() as session:
            session.add(
                ApplicationCatalogRow(
                    application_id="app",
                    display_name="Application",
                    normalized_name="application",
                    body={},
                    stale=False,
                    first_seen_at=datetime.now(UTC),
                    last_seen_at=datetime.now(UTC),
                )
            )
            session.flush()
            session.add(ApplicationAliasRow(application_id="app", alias="Alias"))
            session.add(ApplicationEvidenceRow(application_id="app", source="test", evidence={}))
        with pytest.raises(IntegrityError), database.transaction() as session:
            session.add(ApplicationAliasRow(application_id="app", alias="Alias"))
        with pytest.raises(IntegrityError), database.transaction() as session:
            session.add(ApplicationEvidenceRow(application_id="app", source="test", evidence={}))
        with pytest.raises(IntegrityError), database.transaction() as session:
            session.add(ApplicationAliasRow(application_id="missing", alias="Alias"))
        with pytest.raises(IntegrityError), database.transaction() as session:
            session.add(
                ApplicationEvidenceRow(application_id="missing", source="test", evidence={})
            )
        with database.transaction() as session:
            row = session.get(ApplicationCatalogRow, "app")
            assert row is not None
            session.delete(row)
        with database.transaction() as session:
            assert session.query(ApplicationAliasRow).count() == 0
            assert session.query(ApplicationEvidenceRow).count() == 0
    finally:
        database.stop()
