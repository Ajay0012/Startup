from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypeVar

from sqlalchemy import CursorResult, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .database import (
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
    def add(self, r: ApprovalRecord) -> ApprovalRecord:
        self._add(ApprovalRow(**r.__dict__))
        return r

    def get(self, i: str) -> ApprovalRecord | None:
        x = self._session.get(ApprovalRow, i)
        return (
            None
            if x is None
            else ApprovalRecord(
                x.approval_id, x.binding_hash, x.actor, x.expires_at, x.consumed_at, x.revoked_at
            )
        )

    def consume_once(self, approval_id: str, now: datetime) -> bool:
        from sqlalchemy import update

        result = self._session.execute(
            update(ApprovalRow)
            .where(
                ApprovalRow.approval_id == approval_id,
                ApprovalRow.consumed_at.is_(None),
                ApprovalRow.revoked_at.is_(None),
                ApprovalRow.expires_at > now,
            )
            .values(consumed_at=now)
        )
        return isinstance(result, CursorResult) and result.rowcount == 1
