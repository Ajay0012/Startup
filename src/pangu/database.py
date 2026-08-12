from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
)
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
        ForeignKey("applications.application_id", ondelete="CASCADE"), index=True
    )
    alias: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    __table_args__ = (UniqueConstraint("application_id", "alias", name="uq_application_alias"),)


class ApplicationEvidenceRow(Base):
    __tablename__ = "application_evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.application_id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    __table_args__ = (
        UniqueConstraint("application_id", "source", name="uq_application_evidence_source"),
    )


class ApplicationDiscoveryRunRow(Base):
    __tablename__ = "application_discovery_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryRow(Base):
    __tablename__ = "memory_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    content: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (
        UniqueConstraint("namespace", "kind", "subject", name="uq_memory_subject"),
    )


class WorldFactRow(Base):
    __tablename__ = "world_facts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    entity: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    attribute: Mapped[str] = mapped_column(String(256), index=True, nullable=False)
    value: Mapped[object] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("entity", "attribute", name="uq_world_fact"),)


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

    def _alembic_config(self) -> Config:
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{self.path.as_posix()}")
        return config

    def _migration_head(self) -> str | None:
        return ScriptDirectory.from_config(self._alembic_config()).get_current_head()

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
        command.upgrade(self._alembic_config(), "head")
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
        head = self._migration_head()
        if self._engine is None:
            return {
                "component": "database",
                "lifecycle_state": self.lifecycle_state,
                "database_ready": False,
                "migration_revision": None,
                "migration_head": head,
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
        at_head = head is not None and revision == head
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
            "migration_head": head,
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
    goal: Mapped[str | None] = mapped_column(String)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resumable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class MissionTaskRow(Base):
    __tablename__ = "mission_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    operation: Mapped[str | None] = mapped_column(String(256))
    arguments: Mapped[dict[str, object] | None] = mapped_column(JSON)
    dependencies: Mapped[list[str] | None] = mapped_column(JSON)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class MissionCheckpointRow(Base):
    __tablename__ = "mission_checkpoints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
