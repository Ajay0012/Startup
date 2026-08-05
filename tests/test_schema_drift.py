from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from pangu.database import Base, DatabaseService

RUNTIME_TABLES = {
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


def test_alembic_schema_matches_orm_metadata(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "drift.db")
    database.start()
    try:
        assert database._engine is not None
        inspector = inspect(database._engine)
        assert RUNTIME_TABLES == set(Base.metadata.tables).intersection(RUNTIME_TABLES)
        for table_name in RUNTIME_TABLES:
            migrated = {column["name"]: column for column in inspector.get_columns(table_name)}
            model = Base.metadata.tables[table_name]
            assert set(migrated) == {column.name for column in model.columns}
            assert inspector.get_pk_constraint(table_name)["constrained_columns"] == [
                column.name for column in model.primary_key.columns
            ]
            for column in model.columns:
                assert migrated[column.name]["nullable"] == column.nullable
    finally:
        database.stop()


def test_single_production_engine_creation_site() -> None:
    source = Path("src/pangu")
    occurrences = [
        path
        for path in source.rglob("*.py")
        if "create_engine(" in path.read_text(encoding="utf-8")
    ]
    assert occurrences == [source / "database.py"]
