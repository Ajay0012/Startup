from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

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
    command_id: Mapped[str] = mapped_column(ForeignKey("commands.id"), index=True)
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


class RuntimeHealthRow(Base):
    __tablename__ = "runtime_health"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class DatabaseService:
    """The single SQLAlchemy engine/session owner for PANGU local state."""

    def __init__(self, path: Path, timeout_seconds: float = 5.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._engine: Engine | None = None
        self._sessions: sessionmaker[Session] | None = None

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
        Base.metadata.create_all(self._engine)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        if self._sessions is None:
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
