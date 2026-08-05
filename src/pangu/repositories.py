from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypeVar

from sqlalchemy import CursorResult, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .database import (
    ApplicationCatalogRow,
    ApprovalConsumptionRow,
    ApprovalRevocationRow,
    ApprovalRow,
    AuditRow,
    CommandRow,
    EventRow,
    MissionCheckpointRow,
    MissionRow,
    MissionTaskRow,
    PermissionGrantRow,
    RuntimeHealthRow,
    ToolExecutionRow,
    ToolSpecificationRow,
)


class PersistenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandRecord:
    id: str
    utterance: str
    trace_id: str
    created_at: datetime


@dataclass(frozen=True)
class EventRecord:
    id: str
    event_type: str
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True)
class ToolSpecificationRecord:
    tool_id: str
    version: str
    body: dict[str, object]


@dataclass(frozen=True)
class ToolExecutionRecord:
    id: str
    command_id: str | None
    status: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class PermissionGrantRecord:
    id: int | None
    actor: str
    scope: str


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    binding_hash: str
    actor: str
    expires_at: datetime
    tool_id: str | None = None
    tool_version: str | None = None
    operation: str | None = None
    arguments_json: dict[str, object] | None = None
    target: str | None = None
    risk_level: str | None = None
    permission_scopes: list[str] | None = None
    mission_id: str | None = None
    session_id: str | None = None
    approval_mode: str | None = None
    created_at: datetime | None = None
    exact_operation_hash: str | None = None
    reusable: bool = False
    consumed_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class ApprovalConsumptionRecord:
    id: int | None
    approval_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class ApprovalRevocationRecord:
    id: int | None
    approval_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class AuditRecord:
    id: int | None
    command_id: str | None
    status: str
    message: str
    evidence: dict[str, object]
    created_at: datetime


@dataclass(frozen=True)
class RuntimeHealthRecord:
    id: int | None
    component: str | None
    status: str
    updated_at: datetime


@dataclass(frozen=True)
class ApplicationCatalogRecord:
    application_id: str
    display_name: str
    normalized_name: str
    body: dict[str, object]
    stale: bool
    first_seen_at: datetime
    last_seen_at: datetime

    @classmethod
    def from_application(cls, application: Any) -> ApplicationCatalogRecord:
        return cls(
            application.application_id,
            application.display_name,
            application.normalized_name,
            {
                "aliases": list(application.aliases),
                "executable_name": application.executable_name,
                "install_source": application.install_source,
                "source_evidence": list(application.source_evidence),
            },
            application.stale,
            application.discovered_at,
            application.last_seen_at,
        )


@dataclass(frozen=True)
class MissionRecord:
    id: str
    state: str
    created_at: datetime


@dataclass(frozen=True)
class MissionTaskRecord:
    id: str
    mission_id: str
    state: str


@dataclass(frozen=True)
class MissionCheckpointRecord:
    id: str
    mission_id: str
    payload: dict[str, object]


R = TypeVar("R")


class Repository(Protocol[R]):
    def add(self, record: R) -> R: ...


class _Repository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _add(self, row: Any) -> Any:
        try:
            self._session.add(row)
            self._session.flush()
            return row
        except SQLAlchemyError as error:
            raise PersistenceError(str(error)) from error


class CommandRepository(_Repository):
    def add(self, r: CommandRecord) -> CommandRecord:
        self._add(CommandRow(**r.__dict__))
        return r

    def get(self, identifier: str) -> CommandRecord | None:
        x = self._session.get(CommandRow, identifier)
        return None if x is None else CommandRecord(x.id, x.utterance, x.trace_id, x.created_at)


class EventRepository(_Repository):
    def add(self, r: EventRecord) -> EventRecord:
        self._add(EventRow(**r.__dict__))
        return r

    def get(self, identifier: str) -> EventRecord | None:
        x = self._session.get(EventRow, identifier)
        return None if x is None else EventRecord(x.id, x.event_type, x.payload, x.created_at)


class ToolSpecificationRepository(_Repository):
    def add(self, r: ToolSpecificationRecord) -> ToolSpecificationRecord:
        self._add(ToolSpecificationRow(**r.__dict__))
        return r

    def get(self, tool_id: str, version: str) -> ToolSpecificationRecord | None:
        x = self._session.get(ToolSpecificationRow, (tool_id, version))
        return None if x is None else ToolSpecificationRecord(x.tool_id, x.version, x.body)


class ToolExecutionRepository(_Repository):
    def add(self, r: ToolExecutionRecord) -> ToolExecutionRecord:
        self._add(ToolExecutionRow(**r.__dict__))
        return r

    def get(self, i: str) -> ToolExecutionRecord | None:
        x = self._session.get(ToolExecutionRow, i)
        return None if x is None else ToolExecutionRecord(x.id, x.command_id, x.status, x.evidence)


class PermissionRepository(_Repository):
    def add(self, r: PermissionGrantRecord) -> PermissionGrantRecord:
        x = self._add(PermissionGrantRow(actor=r.actor, scope=r.scope))
        return PermissionGrantRecord(x.id, x.actor, x.scope)

    def for_actor(self, actor: str) -> list[PermissionGrantRecord]:
        return [
            PermissionGrantRecord(x.id, x.actor, x.scope)
            for x in self._session.scalars(
                select(PermissionGrantRow).where(PermissionGrantRow.actor == actor)
            )
        ]


class AuditRepository(_Repository):
    def add(self, r: AuditRecord) -> AuditRecord:
        x = self._add(
            AuditRow(
                command_id=r.command_id,
                status=r.status,
                message=r.message,
                evidence=r.evidence,
                created_at=r.created_at,
            )
        )
        return AuditRecord(x.id, x.command_id, x.status, x.message, x.evidence, x.created_at)


class RuntimeHealthRepository(_Repository):
    def add(self, r: RuntimeHealthRecord) -> RuntimeHealthRecord:
        x = self._add(
            RuntimeHealthRow(component=r.component, status=r.status, updated_at=r.updated_at)
        )
        return RuntimeHealthRecord(x.id, x.component, x.status, x.updated_at)


class ApplicationCatalogRepository(_Repository):
    def upsert(self, r: ApplicationCatalogRecord) -> ApplicationCatalogRecord:
        row = self._session.get(ApplicationCatalogRow, r.application_id)
        if row is None:
            self._add(ApplicationCatalogRow(**r.__dict__))
        else:
            row.display_name, row.normalized_name, row.body, row.stale, row.last_seen_at = (
                r.display_name,
                r.normalized_name,
                r.body,
                r.stale,
                r.last_seen_at,
            )
            self._session.flush()
        return r


class MissionRepository(_Repository):
    def add(self, r: MissionRecord) -> MissionRecord:
        self._add(MissionRow(**r.__dict__))
        return r

    def get(self, i: str) -> MissionRecord | None:
        x = self._session.get(MissionRow, i)
        return None if x is None else MissionRecord(x.id, x.state, x.created_at)


class MissionTaskRepository(_Repository):
    def add(self, r: MissionTaskRecord) -> MissionTaskRecord:
        self._add(MissionTaskRow(**r.__dict__))
        return r

    def get(self, i: str) -> MissionTaskRecord | None:
        x = self._session.get(MissionTaskRow, i)
        return None if x is None else MissionTaskRecord(x.id, x.mission_id, x.state)


class MissionCheckpointRepository(_Repository):
    def add(self, r: MissionCheckpointRecord) -> MissionCheckpointRecord:
        self._add(MissionCheckpointRow(**r.__dict__))
        return r

    def get(self, i: str) -> MissionCheckpointRecord | None:
        x = self._session.get(MissionCheckpointRow, i)
        return None if x is None else MissionCheckpointRecord(x.id, x.mission_id, x.payload)


class ApprovalRepository(_Repository):
    def add(self, record: ApprovalRecord) -> ApprovalRecord:
        self._add(ApprovalRow(**record.__dict__))
        return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        row = self._session.get(ApprovalRow, approval_id)
        return None if row is None else self._record(row)

    def active_for_actor(self, actor: str, now: datetime) -> list[ApprovalRecord]:
        rows = self._session.scalars(
            select(ApprovalRow).where(
                ApprovalRow.actor == actor,
                ApprovalRow.expires_at > now,
                ApprovalRow.revoked_at.is_(None),
            )
        )
        return [self._record(row) for row in rows]

    def consume_once(self, approval_id: str, now: datetime) -> bool:
        from sqlalchemy import update

        result = self._session.execute(
            update(ApprovalRow)
            .where(
                ApprovalRow.approval_id == approval_id,
                ApprovalRow.consumed_at.is_(None),
                ApprovalRow.revoked_at.is_(None),
                ApprovalRow.expires_at > now,
                ApprovalRow.reusable.is_(False),
            )
            .values(consumed_at=now)
        )
        consumed = isinstance(result, CursorResult) and result.rowcount == 1
        if consumed:
            self._add(ApprovalConsumptionRow(approval_id=approval_id, created_at=now))
        return consumed

    def revoke(self, approval_id: str, now: datetime) -> bool:
        from sqlalchemy import update

        result = self._session.execute(
            update(ApprovalRow)
            .where(ApprovalRow.approval_id == approval_id, ApprovalRow.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        revoked = isinstance(result, CursorResult) and result.rowcount == 1
        if revoked:
            self._add(ApprovalRevocationRow(approval_id=approval_id, created_at=now))
        return revoked

    def consumptions(self, approval_id: str) -> list[ApprovalConsumptionRecord]:
        return [
            ApprovalConsumptionRecord(row.id, row.approval_id, row.created_at)
            for row in self._session.scalars(
                select(ApprovalConsumptionRow).where(
                    ApprovalConsumptionRow.approval_id == approval_id
                )
            )
        ]

    def revocations(self, approval_id: str) -> list[ApprovalRevocationRecord]:
        return [
            ApprovalRevocationRecord(row.id, row.approval_id, row.created_at)
            for row in self._session.scalars(
                select(ApprovalRevocationRow).where(
                    ApprovalRevocationRow.approval_id == approval_id
                )
            )
        ]

    def _record(self, row: ApprovalRow) -> ApprovalRecord:
        return ApprovalRecord(
            **{column.name: getattr(row, column.name) for column in ApprovalRow.__table__.columns}
        )
