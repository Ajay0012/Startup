from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class Risk(StrEnum):
    READ_ONLY = "READ_ONLY"
    LOW = "LOW_RISK_REVERSIBLE"
    MODERATE = "MODERATE_RISK"
    HIGH = "HIGH_RISK"
    PRIVILEGED = "PRIVILEGED"
    PROHIBITED = "PROHIBITED"


class Status(StrEnum):
    REQUESTED = "REQUESTED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"
    DENIED = "DENIED"


@dataclass(frozen=True)
class CommandEnvelope:
    original_utterance: str
    source: str = "cli"
    command_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = "local"
    user_id: str = "default"
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    privacy_classification: str = "PRIVATE"


@dataclass(frozen=True)
class NormalizedIntent:
    intent_name: str
    canonical_english: str
    original_text: str
    entities: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    detected_language: str = "en"


@dataclass(frozen=True)
class ToolRequest:
    tool_id: str
    operation: str
    arguments: dict[str, Any]
    actor: str = "default"
    request_id: str = field(default_factory=lambda: str(uuid4()))
    idempotency_key: str = field(default_factory=lambda: str(uuid4()))
    mission_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    request_id: str
    status: Status
    message: str
    observations: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
