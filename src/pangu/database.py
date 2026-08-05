from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .contracts import CommandEnvelope, ToolResult


class Base(DeclarativeBase):
    pass


class CommandRow(Base):
    __tablename__ = "commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    utterance: Mapped[str] = mapped_column(String, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AuditRow(Base):
    __tablename__ = "audit_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command_id: Mapped[str | None] = mapped_column(ForeignKey("commands.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(String, nullable=False)
    evidence: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ApprovalRow(Base):
    __tablename__ = "approvals"
    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tool_id: Mapped[str | None] = mapped_column(String(128))
    tool_version: Mapped[str | None] = mapped_column(String(32))
    operation: Mapped[str | None] = mapped_column(String(128))
    arguments_json: Mapped[dict[str, object] | None] = mapped_column(JSON)
    target: Mapped[str | None] = mapped_column(String(1024))
    risk_level: Mapped[str | None] = mapped_column(String(32))
    permission_scopes: Mapped[list[str] | None] = mapped_column(JSON)
    mission_id: Mapped[str | None] = mapped_column(String(36))
    session_id: Mapped[str | None] = mapped_column(String(128))
    approval_mode: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exact_operation_hash: Mapped[str | None] = mapped_column(String(64))
    reusable: Mapped[bool] = mapped_column(default=False, nullable=False)


class RuntimeHealthRow(Base):
    __tablename__ = "runtime_health"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ApplicationCatalogRow(Base):
    __tablename__ = "applications"
    application_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    body: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    stale: Mapped[bool] = mapped_column(default=False, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApplicationAliasRow(Base):
    __tablename__ = "application_aliases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.application_id"), index=True
    )
    alias: Mapped[str] = mapped_column(String(512), nullable=False, index=True)


class ApplicationEvidenceRow(Base):
    __tablename__ = "application_evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.application_id"), index=True
    )
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)


class ApplicationDiscoveryRunRow(Base):
    __tablename__ = "application_discovery_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DatabaseService:
    """The single SQLAlchemy engine/session owner for PANGU local state."""

    def __init__(self, path: Path, timeout_seconds: float = 5.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._engine: Engine | None = None
        self._sessions: sessionmaker[Session] | None = None
        self._accepting = False
        self.lifecycle_state = "REGISTERED"
        self.last_error: str | None = None
        self.repository_ready = False

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"timeout": self.timeout_seconds}
        )

        @event.listens_for(self._engine, "connect")
        def configure(connection: sqlite3.Connection, _: object) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
            cursor.close()

        assert self._engine is not None
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{self.path.as_posix()}")
        command.upgrade(config, "head")
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        self._accepting = True
        self.repository_ready = True
        self.lifecycle_state = "RUNNING"

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        if self._sessions is None or not self._accepting:
            raise RuntimeError("database is not started")
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def health(self) -> dict[str, str]:
        if self._engine is None:
            return {"status": "stopped"}
        with self._engine.connect() as connection:
            mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
            foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        return {"status": "ready", "journal_mode": str(mode), "foreign_keys": str(foreign_keys)}

    def health_details(self) -> dict[str, object]:
        if self._engine is None:
            return {
                "component": "database",
                "lifecycle_state": self.lifecycle_state,
                "database_ready": False,
                "migration_revision": None,
                "migration_head": "0003_application_catalog",
                "migration_at_head": False,
                "journal_mode": None,
                "foreign_keys_enabled": False,
                "busy_timeout_ms": None,
                "repository_ready": self.repository_ready,
                "last_error": self.last_error,
                "degraded_reason": self.last_error,
            }
        with self._engine.connect() as connection:
            revision = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
            foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        at_head = revision == "0003_application_catalog"
        ready = bool(
            at_head
            and str(mode).lower() == "wal"
            and foreign_keys == 1
            and self.repository_ready
            and self._accepting
        )
        return {
            "component": "database",
            "lifecycle_state": self.lifecycle_state,
            "database_ready": ready,
            "migration_revision": revision,
            "migration_head": "0003_application_catalog",
            "migration_at_head": at_head,
            "journal_mode": str(mode),
            "foreign_keys_enabled": foreign_keys == 1,
            "busy_timeout_ms": int(busy_timeout),
            "repository_ready": self.repository_ready,
            "last_error": self.last_error,
            "degraded_reason": None if ready else "database is not ready",
        }

    def record(self, command: CommandEnvelope, result: ToolResult) -> None:
        with self.transaction() as session:
            session.add(
                CommandRow(
                    id=command.command_id,
                    utterance=command.original_utterance,
                    trace_id=command.trace_id,
                    created_at=command.timestamp,
                )
            )
            session.flush()
            session.add(
                AuditRow(
                    command_id=command.command_id,
                    status=str(result.status),
                    message=result.message,
                    evidence=result.evidence,
                    created_at=command.timestamp,
                )
            )

    def audit_count(self) -> int:
        with self.transaction() as session:
            return session.query(AuditRow).count()

    def stop(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._sessions = None
        self.lifecycle_state = "STOPPED"


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ToolSpecificationRow(Base):
    __tablename__ = "tool_specifications"
    tool_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    body: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)


class ToolExecutionRow(Base):
    __tablename__ = "tool_executions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    command_id: Mapped[str | None] = mapped_column(ForeignKey("commands.id"))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)


class PermissionGrantRow(Base):
    __tablename__ = "permission_grants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(512), nullable=False)


class ApprovalConsumptionRow(Base):
    __tablename__ = "approval_consumptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    approval_id: Mapped[str | None] = mapped_column(
        ForeignKey("approvals.approval_id"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalRevocationRow(Base):
    __tablename__ = "approval_revocations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    approval_id: Mapped[str | None] = mapped_column(ForeignKey("approvals.approval_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MissionRow(Base):
    __tablename__ = "missions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MissionTaskRow(Base):
    __tablename__ = "mission_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)


class MissionCheckpointRow(Base):
    __tablename__ = "mission_checkpoints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
