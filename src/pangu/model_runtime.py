from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import uuid4


class ProviderHealth(StrEnum):
    UNCONFIGURED = "UNCONFIGURED"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    STOPPED = "STOPPED"


class PrivacyOutcome(StrEnum):
    ALLOW = "ALLOW"
    ALLOW_WITH_REDACTION = "ALLOW_WITH_REDACTION"
    USER_CONFIRMATION_REQUIRED = "USER_CONFIRMATION_REQUIRED"
    LOCAL_ONLY = "LOCAL_ONLY"
    REJECT = "REJECT"


@dataclass(frozen=True)
class ModelRequest:
    prompt: str
    role: str = "general"
    request_id: str = field(default_factory=lambda: str(uuid4()))
    trace_id: str | None = None
    mission_id: str | None = None
    timeout_seconds: float = 15.0


@dataclass(frozen=True)
class ModelResult:
    text: str | None
    provider: str
    model: str
    health: ProviderHealth
    error: str | None = None
    retryable: bool = False


class AIModelProvider(Protocol):
    name: str

    def health(self) -> ProviderHealth: ...
    def generate(self, request: ModelRequest) -> ModelResult: ...


@dataclass(frozen=True)
class SanitizationDecision:
    outcome: PrivacyOutcome
    sanitized_content: str
    redactions: tuple[str, ...]
    original_hash: str
    sanitized_hash: str
    confirmation_reason: str | None = None


class CloudContextSanitizer:
    _rules = (
        ("api_key", r"(?i)(api[_-]?key\s*[=:]\s*)[^\s]+"),
        ("token", r"(?i)(bearer\s+)[^\s]+"),
        ("password", r"(?i)(password\s*[=:]\s*)[^\s]+"),
        ("connection_string", r"(?i)(?:postgres|mysql|mongodb)://[^\s]+"),
    )

    def sanitize(self, content: str, kind: str = "text") -> SanitizationDecision:
        original_hash = hashlib.sha256(content.encode()).hexdigest()
        if "-----BEGIN PRIVATE KEY-----" in content:
            return SanitizationDecision(
                PrivacyOutcome.REJECT,
                "[private key blocked]",
                ("private_key",),
                original_hash,
                hashlib.sha256(b"[private key blocked]").hexdigest(),
                "Private keys never leave the device.",
            )
        redactions: list[str] = []
        sanitized = content
        for category, pattern in self._rules:
            sanitized, count = re.subn(
                pattern,
                lambda match: match.group(1) + "[REDACTED]" if match.lastindex else "[REDACTED]",
                sanitized,
            )
            if count:
                redactions.append(category)
        outcome = PrivacyOutcome.ALLOW_WITH_REDACTION if redactions else PrivacyOutcome.ALLOW
        if kind in {"screenshot", "document"} and content:
            outcome = PrivacyOutcome.USER_CONFIRMATION_REQUIRED
        return SanitizationDecision(
            outcome,
            sanitized,
            tuple(redactions),
            original_hash,
            hashlib.sha256(sanitized.encode()).hexdigest(),
        )


class DeterministicProvider:
    name = "deterministic"

    def health(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY

    def generate(self, request: ModelRequest) -> ModelResult:
        return ModelResult(
            None,
            self.name,
            "local-rules",
            ProviderHealth.DEGRADED,
            "No deterministic answer for open-ended reasoning.",
        )


class GeminiProvider:
    """Safe adapter boundary: no SDK import or network call occurs without a configured key."""

    name = "gemini"

    def __init__(self, api_key: str | None, model: str = "gemini-3.6-flash") -> None:
        self._key, self._model = api_key, model

    def health(self) -> ProviderHealth:
        return ProviderHealth.UNCONFIGURED if not self._key else ProviderHealth.DEGRADED

    def generate(self, request: ModelRequest) -> ModelResult:
        return ModelResult(
            None,
            self.name,
            self._model,
            self.health(),
            "Gemini adapter requires SDK transport configuration.",
        )


class MockModelProvider:
    name = "mock"

    def __init__(self, result: ModelResult | None = None) -> None:
        self.result = result

    def health(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY

    def generate(self, request: ModelRequest) -> ModelResult:
        return self.result or ModelResult("{}", self.name, "mock", self.health())


@dataclass(frozen=True)
class RoutingDecision:
    provider: str
    model: str
    reason: str
    privacy: PrivacyOutcome
    confirmation_required: bool = False


class ModelRouter:
    def __init__(
        self,
        deterministic: AIModelProvider,
        gemini: AIModelProvider,
        sanitizer: CloudContextSanitizer,
    ) -> None:
        self.deterministic, self.gemini, self.sanitizer = deterministic, gemini, sanitizer

    def route(self, text: str, deterministic_available: bool = True) -> RoutingDecision:
        decision = self.sanitizer.sanitize(text)
        if decision.outcome in {PrivacyOutcome.REJECT, PrivacyOutcome.LOCAL_ONLY}:
            return RoutingDecision(
                "deterministic", "local-rules", "cloud blocked by privacy policy", decision.outcome
            )
        if deterministic_available:
            return RoutingDecision(
                "deterministic", "local-rules", "known local command path", decision.outcome
            )
        return RoutingDecision(
            "gemini",
            "configured",
            "cloud reasoning requested",
            decision.outcome,
            decision.outcome == PrivacyOutcome.USER_CONFIRMATION_REQUIRED,
        )


class CognitiveDecisionKind(StrEnum):
    DIRECT_TOOL = "DIRECT_TOOL"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"
    INFORMATIONAL_RESPONSE = "INFORMATIONAL_RESPONSE"


@dataclass(frozen=True)
class CognitiveDecision:
    kind: CognitiveDecisionKind
    summary: str
    tool: str | None = None


class CognitiveEngine:
    def decide(self, normalized_intent: str) -> CognitiveDecision:
        if normalized_intent in {"create_folder", "battery_status"}:
            return CognitiveDecision(
                CognitiveDecisionKind.DIRECT_TOOL, "deterministic command", normalized_intent
            )
        return CognitiveDecision(
            CognitiveDecisionKind.UNSUPPORTED, "No verified local action selected."
        )


class ContextAssembler:
    def assemble(self, command: str, recent: tuple[str, ...] = ()) -> dict[str, object]:
        return {
            "command": command[:2000],
            "recent": list(recent[-5:]),
            "hash": hashlib.sha256(command.encode()).hexdigest(),
        }
