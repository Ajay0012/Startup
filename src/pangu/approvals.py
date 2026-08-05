from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import cast
from uuid import uuid4

from .database import DatabaseService
from .repositories import ApprovalRecord, ApprovalRepository


class ApprovalDenial(str, Enum):
    NOT_FOUND = "not_found"
    BINDING_MISMATCH = "binding_mismatch"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CONSUMED = "consumed"
    SESSION_MISMATCH = "session_mismatch"


def _normalise(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return str(value.resolve()).replace("/", "\\").casefold()
    if isinstance(value, set | frozenset):
        return sorted(
            (_normalise(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True)
        )
    if isinstance(value, dict):
        return {
            str(key): _normalise(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list | tuple):
        return [_normalise(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    return json.dumps(_normalise(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ApprovalBinding:
    actor: str
    tool_id: str
    tool_version: str
    operation: str
    arguments: dict[str, object]
    target: str
    risk_level: str
    permission_scopes: frozenset[str]
    mission_id: str | None
    session_id: str | None
    expires_at: datetime
    approval_mode: str = "one_time"

    def payload(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "tool_id": self.tool_id,
            "tool_version": self.tool_version,
            "operation": self.operation,
            "arguments": self.arguments,
            "target": self.target,
            "risk_level": self.risk_level,
            "permission_scopes": sorted(self.permission_scopes),
            "mission_id": self.mission_id,
            "session_id": self.session_id,
            "expires_at": self.expires_at,
            "approval_mode": self.approval_mode,
        }

    def hash(self) -> str:
        return hashlib.sha256(canonical_json(self.payload()).encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class PersistentApprovalService:
    """The single persistent exact-operation approval authority."""

    def __init__(self, database: DatabaseService) -> None:
        self._database = database

    def issue(self, binding: ApprovalBinding) -> str:
        now = datetime.now(UTC)
        token = str(uuid4())
        digest = binding.hash()
        with self._database.transaction() as session:
            ApprovalRepository(session).add(
                ApprovalRecord(
                    approval_id=token,
                    binding_hash=digest,
                    exact_operation_hash=digest,
                    actor=binding.actor,
                    tool_id=binding.tool_id,
                    tool_version=binding.tool_version,
                    operation=binding.operation,
                    arguments_json=cast(dict[str, object], _normalise(binding.arguments)),
                    target=str(_normalise(Path(binding.target))),
                    risk_level=binding.risk_level,
                    permission_scopes=sorted(binding.permission_scopes),
                    mission_id=binding.mission_id,
                    session_id=binding.session_id,
                    approval_mode=binding.approval_mode,
                    created_at=now,
                    expires_at=binding.expires_at,
                    reusable=binding.approval_mode == "reusable",
                )
            )
        return token

    def consume(self, approval_id: str, binding: ApprovalBinding) -> ApprovalDenial | None:
        now = datetime.now(UTC)
        with self._database.transaction() as session:
            repo = ApprovalRepository(session)
            record = repo.get(approval_id)
            if record is None:
                return ApprovalDenial.NOT_FOUND
            if record.revoked_at is not None:
                return ApprovalDenial.REVOKED
            if _as_utc(record.expires_at) <= now:
                return ApprovalDenial.EXPIRED
            if record.session_id != binding.session_id:
                return ApprovalDenial.SESSION_MISMATCH
            if record.exact_operation_hash != binding.hash():
                return ApprovalDenial.BINDING_MISMATCH
            if record.reusable:
                return None
            return None if repo.consume_once(approval_id, now) else ApprovalDenial.CONSUMED

    def revoke(self, approval_id: str) -> bool:
        with self._database.transaction() as session:
            return ApprovalRepository(session).revoke(approval_id, datetime.now(UTC))


def expires_in(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)
