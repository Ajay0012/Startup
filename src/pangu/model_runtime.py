from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError


class ProviderHealth(StrEnum):
    UNCONFIGURED = "UNCONFIGURED"
    INITIALIZING = "INITIALIZING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    OFFLINE = "OFFLINE"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class ModelRole(StrEnum):
    FAST = "FAST"
    PRIMARY = "PRIMARY"
    CODING = "CODING"
    VISION = "VISION"


class ProviderErrorCode(StrEnum):
    PROVIDER_UNCONFIGURED = "PROVIDER_UNCONFIGURED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    CANCELLED = "CANCELLED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    STRUCTURED_OUTPUT_INVALID = "STRUCTURED_OUTPUT_INVALID"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    INTERNAL_PROVIDER_ERROR = "INTERNAL_PROVIDER_ERROR"


class PrivacyOutcome(StrEnum):
    ALLOW = "ALLOW"
    ALLOW_WITH_REDACTION = "ALLOW_WITH_REDACTION"
    USER_CONFIRMATION_REQUIRED = "USER_CONFIRMATION_REQUIRED"
    LOCAL_ONLY = "LOCAL_ONLY"
    REJECT = "REJECT"


@dataclass(frozen=True)
class ModelRequest:
    prompt: str
    role: ModelRole = ModelRole.PRIMARY
    request_id: str = field(default_factory=lambda: str(uuid4()))
    trace_id: str | None = None
    mission_id: str = "default"
    timeout_seconds: float = 15.0
    images: tuple[bytes, ...] = ()


@dataclass(frozen=True)
class ModelResult:
    text: str | None
    provider: str
    model: str
    health: ProviderHealth
    error: str | None = None
    retryable: bool = False
    usage: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderError:
    error_code: ProviderErrorCode
    sanitized_message: str
    provider: str
    model: str
    retryable: bool = False
    retry_after: float | None = None
    trace_id: str | None = None
    health_impact: ProviderHealth = ProviderHealth.DEGRADED
    original_exception_type: str | None = None
    api_status_code: int | None = None


class ProviderFailure(Exception):
    def __init__(self, error: ProviderError) -> None:
        self.error = error
        super().__init__(error.error_code)


class GeminiTransport(Protocol):
    async def generate_text(self, model: str, prompt: str, timeout_seconds: float) -> str: ...
    async def generate_structured(self, model: str, prompt: str, timeout_seconds: float) -> str: ...
    def stream_text(
        self, model: str, prompt: str, timeout_seconds: float
    ) -> AsyncIterator[str]: ...
    async def generate_multimodal(
        self, model: str, prompt: str, images: tuple[bytes, ...], timeout_seconds: float
    ) -> str: ...
    async def health_check(self, model: str, timeout_seconds: float) -> None: ...
    async def close(self) -> None: ...


class GoogleGenAITransport:
    """Production-only adapter; importing PANGU never imports the Google SDK."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client: Any | None = None

    def _client_or_create(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def generate_text(self, model: str, prompt: str, timeout_seconds: float) -> str:
        client = self._client_or_create()
        result = await asyncio.wait_for(
            client.aio.models.generate_content(model=model, contents=prompt),
            timeout_seconds,
        )
        return str(getattr(result, "text", ""))

    async def generate_structured(self, model: str, prompt: str, timeout_seconds: float) -> str:
        return await self.generate_text(model, prompt, timeout_seconds)

    async def stream_text(
        self, model: str, prompt: str, timeout_seconds: float
    ) -> AsyncIterator[str]:
        yield await self.generate_text(model, prompt, timeout_seconds)

    async def generate_multimodal(
        self, model: str, prompt: str, images: tuple[bytes, ...], timeout_seconds: float
    ) -> str:
        contents: list[object] = [prompt, *images]
        client = self._client_or_create()
        result = await asyncio.wait_for(
            client.aio.models.generate_content(model=model, contents=contents),
            timeout_seconds,
        )
        return str(getattr(result, "text", ""))

    async def health_check(self, model: str, timeout_seconds: float) -> None:
        client = self._client_or_create()
        await asyncio.wait_for(
            client.aio.models.generate_content(model=model, contents="Reply with exactly OK."),
            timeout_seconds,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aio.aclose()
            self._client = None


class FakeGeminiTransport:
    def __init__(
        self, responses: list[str] | None = None, failures: list[Exception] | None = None
    ) -> None:
        self.responses = responses or ["{}"]
        self.failures = failures or []
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def _request(self, model: str, prompt: str, timeout_seconds: float) -> str:
        self.calls.append((model, prompt))
        if self.failures:
            raise self.failures.pop(0)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    async def generate_text(self, model: str, prompt: str, timeout_seconds: float) -> str:
        return await self._request(model, prompt, timeout_seconds)

    async def generate_structured(self, model: str, prompt: str, timeout_seconds: float) -> str:
        return await self._request(model, prompt, timeout_seconds)

    async def stream_text(
        self, model: str, prompt: str, timeout_seconds: float
    ) -> AsyncIterator[str]:
        yield await self._request(model, prompt, timeout_seconds)

    async def generate_multimodal(
        self, model: str, prompt: str, images: tuple[bytes, ...], timeout_seconds: float
    ) -> str:
        return await self._request(model, prompt, timeout_seconds)

    async def health_check(self, model: str, timeout_seconds: float) -> None:
        await self._request(model, "health", timeout_seconds)

    async def close(self) -> None:
        self.closed = True


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
                lambda match: (
                    str(match.group(1)) + "[REDACTED]" if match.lastindex else "[REDACTED]"
                ),
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


@dataclass(frozen=True)
class SanitizationDecision:
    outcome: PrivacyOutcome
    sanitized_content: str
    redactions: tuple[str, ...]
    original_hash: str
    sanitized_hash: str
    confirmation_reason: str | None = None


class RetryPolicy:
    def __init__(
        self,
        max_retries: int = 2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_retries, self.sleep, self.clock = max_retries, sleep, clock

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        return retry_after if retry_after is not None else float(2**attempt)

    def should_retry(self, error: ProviderError) -> bool:
        return error.retryable and error.error_code in {
            ProviderErrorCode.NETWORK_UNAVAILABLE,
            ProviderErrorCode.REQUEST_TIMEOUT,
            ProviderErrorCode.RATE_LIMITED,
            ProviderErrorCode.INTERNAL_PROVIDER_ERROR,
        }


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(
        self,
        threshold: int = 3,
        open_duration: float = 30.0,
        half_open_probe_limit: int = 1,
        success_threshold: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        (
            self.threshold,
            self.open_duration,
            self.half_open_probe_limit,
            self.success_threshold,
            self.clock,
        ) = threshold, open_duration, half_open_probe_limit, success_threshold, clock
        self.failures = 0
        self.state = CircuitState.CLOSED
        self._opened_at = 0.0
        self._probes = 0
        self._successes = 0

    def allow(self) -> bool:
        if self.state == CircuitState.OPEN:
            if self.clock() - self._opened_at < self.open_duration:
                return False
            self.state = CircuitState.HALF_OPEN
            self._probes = 0
        if self.state == CircuitState.HALF_OPEN:
            if self._probes >= self.half_open_probe_limit:
                return False
            self._probes += 1
        return True

    def success(self) -> None:
        if self.state == CircuitState.HALF_OPEN:
            self._successes += 1
            if self._successes < self.success_threshold:
                return
        self.failures = 0
        self._successes = 0
        self.state = CircuitState.CLOSED

    def failure(self, code: str | ProviderErrorCode) -> None:
        if str(code) in {"CANCELLED", "VALIDATION_ERROR", "ProviderErrorCode.CANCELLED"}:
            return
        self.failures += 1
        if (
            code == ProviderErrorCode.INVALID_CREDENTIALS
            or str(code) == "INVALID_CREDENTIALS"
            or self.state == CircuitState.HALF_OPEN
            or self.failures >= self.threshold
        ):
            self.state = CircuitState.OPEN
            self._opened_at = self.clock()
            self._probes = 0
            self._successes = 0


@dataclass
class MissionBudget:
    calls: int = 0
    retry_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_seconds: float = 0.0
    usage: list[dict[str, object]] = field(default_factory=list)


class ModelBudgetManager:
    def __init__(
        self, max_calls: int = 12, max_input_tokens: int = 120000, max_output_tokens: int = 24000
    ) -> None:
        self.max_calls = max_calls
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self._missions: dict[str, MissionBudget] = {}

    def mission(self, mission_id: str) -> MissionBudget:
        return self._missions.setdefault(mission_id, MissionBudget())

    def permit(
        self, mission_id: str = "default", input_tokens: int = 0, retry: bool = False
    ) -> bool:
        b = self.mission(mission_id)
        return b.calls < self.max_calls and b.input_tokens + input_tokens <= self.max_input_tokens

    def record(
        self,
        mission_id: str = "default",
        input_tokens: int = 0,
        output_tokens: int = 0,
        retry: bool = False,
        elapsed_seconds: float = 0.0,
        usage: dict[str, object] | None = None,
    ) -> None:
        if (
            not self.permit(mission_id, input_tokens, retry)
            or self.mission(mission_id).output_tokens + output_tokens > self.max_output_tokens
        ):
            raise ValueError("BUDGET_EXCEEDED")
        b = self.mission(mission_id)
        b.calls += 1
        b.retry_calls += int(retry)
        b.input_tokens += input_tokens
        b.output_tokens += output_tokens
        b.elapsed_seconds += elapsed_seconds
        if usage:
            b.usage.append(usage)


ModelBudget = ModelBudgetManager


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
    name = "gemini"

    def __init__(
        self,
        api_key: str | None,
        model: str = "gemini-3.6-flash",
        *,
        transport: GeminiTransport | None = None,
        models: dict[ModelRole, str] | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        retry_policy: RetryPolicy | None = None,
        budget_manager: ModelBudgetManager | None = None,
        sanitizer: CloudContextSanitizer | None = None,
    ) -> None:
        self._key = api_key
        self.models = models or {role: model for role in ModelRole}
        self.transport = transport
        self.circuit = circuit_breaker or CircuitBreaker()
        self.retry = retry_policy or RetryPolicy()
        self.budget = budget_manager or ModelBudgetManager()
        self.sanitizer = sanitizer or CloudContextSanitizer()
        self._health = ProviderHealth.UNCONFIGURED if not api_key else ProviderHealth.INITIALIZING
        self.last_failure: ProviderError | None = None
        self.last_success: float | None = None
        self.active_requests = 0
        self.retry_after: float | None = None

    def health(self) -> ProviderHealth:
        return self._health

    def health_details(self) -> dict[str, object]:
        return {
            "provider": self.name,
            "configured": bool(self._key),
            "available": self._health in {ProviderHealth.HEALTHY, ProviderHealth.DEGRADED},
            "models": {role.value: model for role, model in self.models.items()},
            "last_success": self.last_success,
            "last_failure": self.last_failure.error_code if self.last_failure else None,
            "last_failure_exception_type": (
                self.last_failure.original_exception_type if self.last_failure else None
            ),
            "last_failure_api_status_code": (
                self.last_failure.api_status_code if self.last_failure else None
            ),
            "last_failure_retryable": self.last_failure.retryable if self.last_failure else False,
            "circuit_state": self.circuit.state,
            "retry_after": self.retry_after,
            "active_request_count": self.active_requests,
            "state": self._health,
        }

    def _error(self, exc: Exception, model: str, trace_id: str | None) -> ProviderError:
        text = f"{type(exc).__name__} {exc}".lower()
        code = ProviderErrorCode.INTERNAL_PROVIDER_ERROR
        retryable = False
        health = ProviderHealth.FAILED
        retry_after = None
        api_status_code: int | None = None
        from google.genai.errors import APIError

        if isinstance(exc, APIError):
            api_status_code = int(exc.code) if exc.code is not None else None
            message = str(exc.message or "").lower()
            response = exc.response
            headers = getattr(response, "headers", {}) if response is not None else {}
            retry_value = headers.get("retry-after") if hasattr(headers, "get") else None
            try:
                retry_after = float(retry_value) if retry_value is not None else None
            except (TypeError, ValueError):
                retry_after = None
            if api_status_code in {401, 403}:
                code, health = (
                    ProviderErrorCode.INVALID_CREDENTIALS,
                    ProviderHealth.INVALID_CREDENTIALS,
                )
            elif api_status_code == 404:
                code = ProviderErrorCode.MODEL_UNAVAILABLE
            elif api_status_code in {408, 504}:
                code, health, retryable = (
                    ProviderErrorCode.REQUEST_TIMEOUT,
                    ProviderHealth.OFFLINE,
                    True,
                )
            elif api_status_code == 429:
                if "quota" in message or "resource_exhausted" in message:
                    code, health = ProviderErrorCode.QUOTA_EXHAUSTED, ProviderHealth.QUOTA_EXHAUSTED
                else:
                    code, health, retryable = (
                        ProviderErrorCode.RATE_LIMITED,
                        ProviderHealth.RATE_LIMITED,
                        True,
                    )
            elif api_status_code in {500, 502, 503}:
                if "model" in message or "not found" in message:
                    code = ProviderErrorCode.MODEL_UNAVAILABLE
                else:
                    code, health = ProviderErrorCode.NETWORK_UNAVAILABLE, ProviderHealth.OFFLINE
                retryable = True
            elif api_status_code == 400:
                code = (
                    ProviderErrorCode.MODEL_UNAVAILABLE
                    if "model" in message or "not found" in message
                    else ProviderErrorCode.INVALID_RESPONSE
                )
            return ProviderError(
                code,
                "Gemini API request failed.",
                self.name,
                model,
                retryable,
                retry_after,
                trace_id,
                health,
                type(exc).__name__,
                api_status_code,
            )
        if isinstance(exc, asyncio.CancelledError):
            code = ProviderErrorCode.CANCELLED
            health = ProviderHealth.DEGRADED
        elif isinstance(exc, TimeoutError) or "timeout" in text:
            code = ProviderErrorCode.REQUEST_TIMEOUT
            retryable = True
            health = ProviderHealth.OFFLINE
        elif (
            "auth" in text
            or "credential" in text
            or "permission" in text
            or "api key" in text
            or "401" in text
            or "403" in text
        ):
            code = ProviderErrorCode.INVALID_CREDENTIALS
            health = ProviderHealth.INVALID_CREDENTIALS
        elif "quota" in text:
            code = ProviderErrorCode.QUOTA_EXHAUSTED
            health = ProviderHealth.QUOTA_EXHAUSTED
        elif "rate" in text or "429" in text or "resource exhausted" in text:
            code = ProviderErrorCode.RATE_LIMITED
            retryable = True
            health = ProviderHealth.RATE_LIMITED
        elif "network" in text or "connect" in text or "dns" in text or "socket" in text:
            code = ProviderErrorCode.NETWORK_UNAVAILABLE
            retryable = True
            health = ProviderHealth.OFFLINE
        elif "model" in text or "notfound" in text or "not found" in text or "404" in text:
            code = ProviderErrorCode.MODEL_UNAVAILABLE
        return ProviderError(
            code,
            "Gemini request failed.",
            self.name,
            model,
            retryable,
            retry_after,
            trace_id,
            health,
            type(exc).__name__,
            api_status_code,
        )

    async def probe_async(self, timeout_seconds: float) -> ModelResult:
        """Perform one context-free provider health request using the fast model.

        This intentionally bypasses normal generation retries and mission budgets:
        a health probe must make at most one request and must never contain user data.
        """
        model = self.models[ModelRole.FAST]
        if not self._key:
            return ModelResult(
                None,
                self.name,
                model,
                ProviderHealth.UNCONFIGURED,
                ProviderErrorCode.PROVIDER_UNCONFIGURED,
            )
        if self.transport is None:
            self._health = ProviderHealth.OFFLINE
            return ModelResult(
                None,
                self.name,
                model,
                self._health,
                ProviderErrorCode.NETWORK_UNAVAILABLE,
            )
        self.active_requests += 1
        try:
            await self.transport.health_check(model, timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - third-party transport failures must be sanitized here.
            error = self._error(exc, model, None)
            self.last_failure = error
            self._health = error.health_impact
            self.circuit.failure(error.error_code)
            return ModelResult(
                None, self.name, model, self._health, error.error_code, error.retryable
            )
        else:
            self.circuit.success()
            self._health = ProviderHealth.HEALTHY
            self.last_success = time.time()
            return ModelResult("OK", self.name, model, self._health)
        finally:
            self.active_requests -= 1

    async def generate_async(self, request: ModelRequest, structured: bool = False) -> ModelResult:
        model = self.models[request.role]
        if not self._key:
            return ModelResult(
                None,
                self.name,
                model,
                ProviderHealth.UNCONFIGURED,
                ProviderErrorCode.PROVIDER_UNCONFIGURED,
            )
        if self.transport is None:
            return ModelResult(
                None,
                self.name,
                model,
                ProviderHealth.DEGRADED,
                "Gemini transport is not configured.",
            )
        if not self.circuit.allow():
            return ModelResult(None, self.name, model, self._health, ProviderErrorCode.CIRCUIT_OPEN)
        clean = self.sanitizer.sanitize(request.prompt, "screenshot" if request.images else "text")
        if clean.outcome in {
            PrivacyOutcome.REJECT,
            PrivacyOutcome.LOCAL_ONLY,
            PrivacyOutcome.USER_CONFIRMATION_REQUIRED,
        }:
            return ModelResult(
                None, self.name, model, self._health, "Cloud processing blocked by privacy policy."
            )
        tokens = len(clean.sanitized_content) // 4
        if not self.budget.permit(request.mission_id, tokens):
            return ModelResult(
                None, self.name, model, self._health, ProviderErrorCode.BUDGET_EXCEEDED
            )
        self.active_requests += 1
        try:
            for attempt in range(self.retry.max_retries + 1):
                start = time.monotonic()
                self.budget.record(request.mission_id, tokens, retry=attempt > 0)
                try:
                    if request.images:
                        text = await self.transport.generate_multimodal(
                            model, clean.sanitized_content, request.images, request.timeout_seconds
                        )
                    elif structured:
                        text = await self.transport.generate_structured(
                            model, clean.sanitized_content, request.timeout_seconds
                        )
                    else:
                        text = await self.transport.generate_text(
                            model, clean.sanitized_content, request.timeout_seconds
                        )
                    if not text:
                        raise ValueError("invalid response")
                    self.circuit.success()
                    self._health = ProviderHealth.HEALTHY
                    self.last_success = time.time()
                    return ModelResult(
                        text,
                        self.name,
                        model,
                        self._health,
                        usage={"input_tokens": tokens, "elapsed_seconds": time.monotonic() - start},
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - third-party transport failures must be sanitized here.
                    error = self._error(exc, model, request.trace_id)
                    self.last_failure = error
                    self._health = error.health_impact
                    self.circuit.failure(error.error_code)
                    if attempt >= self.retry.max_retries or not self.retry.should_retry(error):
                        return ModelResult(
                            None, self.name, model, self._health, error.error_code, error.retryable
                        )
                    await self.retry.sleep(self.retry.delay(attempt, error.retry_after))
        except asyncio.CancelledError:
            return ModelResult(None, self.name, model, self._health, ProviderErrorCode.CANCELLED)
        finally:
            self.active_requests -= 1
        return ModelResult(
            None, self.name, model, self._health, ProviderErrorCode.INTERNAL_PROVIDER_ERROR
        )

    def generate(self, request: ModelRequest) -> ModelResult:
        return ModelResult(
            None,
            self.name,
            self.models[request.role],
            self.health(),
            "Use generate_async for Gemini.",
        )

    async def close(self) -> None:
        if self.transport:
            await self.transport.close()
        self._health = ProviderHealth.STOPPED


T = TypeVar("T", bound=BaseModel)


class StructuredOutputValidator:
    def extract(self, raw: str) -> dict[str, object]:
        text = raw.strip()
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        text = match.group(1) if match else text
        value = json.loads(text)
        if not isinstance(value, dict):
            raise TypeError("STRUCTURED_OUTPUT_INVALID")
        return value

    def validate(self, raw: str, required_keys: set[str] | type[T]) -> dict[str, object] | T:
        value = self.extract(raw)
        if isinstance(required_keys, set):
            if not required_keys <= set(value):
                raise ValueError("STRUCTURED_OUTPUT_INVALID")
            return value
        try:
            return required_keys.model_validate(value)
        except ValidationError as error:
            raise ValueError("STRUCTURED_OUTPUT_INVALID") from error

    async def validate_with_repair(
        self, provider: GeminiProvider, request: ModelRequest, schema: type[T]
    ) -> T:
        result = await provider.generate_async(request, structured=True)
        if result.text:
            try:
                return self.validate(result.text, schema)  # type: ignore[return-value]
            except ValueError:
                pass
        repair = ModelRequest(
            "Return valid JSON only for the requested schema.",
            request.role,
            trace_id=request.trace_id,
            mission_id=request.mission_id,
            timeout_seconds=request.timeout_seconds,
        )
        fixed = await provider.generate_async(repair, structured=True)
        if not fixed.text:
            raise ValueError("STRUCTURED_OUTPUT_INVALID")
        return self.validate(fixed.text, schema)  # type: ignore[return-value]


@dataclass(frozen=True)
class ModelCapability:
    provider: str
    model_id: str
    role: ModelRole
    text: bool = True
    structured: bool = True
    streaming: bool = True
    vision: bool = False
    context_limit: int = 0
    required_health: ProviderHealth = ProviderHealth.HEALTHY
    privacy_restrictions: tuple[PrivacyOutcome, ...] = ()
    fallback: str = "deterministic"
    latency: str = "standard"


class ModelCapabilityRegistry:
    def __init__(self) -> None:
        self._items: dict[tuple[str, ModelRole], ModelCapability] = {}

    def register(self, capability: ModelCapability) -> None:
        key = (capability.provider, capability.role)
        if key in self._items or not capability.model_id.strip() or capability.context_limit < 0:
            raise ValueError("invalid or duplicate model capability")
        self._items[key] = capability

    def all(self) -> tuple[ModelCapability, ...]:
        return tuple(self._items.values())


@dataclass(frozen=True)
class RoutingDecision:
    provider: str
    model: str
    reason: str
    privacy: PrivacyOutcome
    confirmation_required: bool = False
    role: ModelRole | None = None


class ModelRouter:
    def __init__(
        self,
        deterministic: DeterministicProvider,
        gemini: GeminiProvider,
        sanitizer: CloudContextSanitizer,
    ) -> None:
        self.deterministic, self.gemini, self.sanitizer = deterministic, gemini, sanitizer

    def route(
        self, text: str, deterministic_available: bool = True, kind: str = "text"
    ) -> RoutingDecision:
        privacy = self.sanitizer.sanitize(text, kind)
        if privacy.outcome in {PrivacyOutcome.REJECT, PrivacyOutcome.LOCAL_ONLY}:
            return RoutingDecision(
                "deterministic", "local-rules", "cloud blocked by privacy policy", privacy.outcome
            )
        if privacy.outcome == PrivacyOutcome.USER_CONFIRMATION_REQUIRED:
            return RoutingDecision(
                "gemini",
                self.gemini.models[ModelRole.VISION],
                "user confirmation required",
                privacy.outcome,
                True,
                ModelRole.VISION,
            )
        if deterministic_available:
            return RoutingDecision(
                "deterministic", "local-rules", "known local command path", privacy.outcome
            )
        role = (
            ModelRole.VISION
            if kind == "image"
            else ModelRole.CODING
            if any(word in text.lower() for word in ("repository", "codebase", "code "))
            else ModelRole.PRIMARY
            if any(word in text.lower() for word in ("research", "complex", "analyze"))
            else ModelRole.FAST
        )
        if self.gemini.health() not in {
            ProviderHealth.HEALTHY,
            ProviderHealth.INITIALIZING,
            ProviderHealth.DEGRADED,
        }:
            return RoutingDecision(
                "gemini",
                self.gemini.models[role],
                "Gemini unavailable; work deferred without fabrication",
                privacy.outcome,
                False,
                role,
            )
        return RoutingDecision(
            "gemini",
            self.gemini.models[role],
            "cloud reasoning requested",
            privacy.outcome,
            False,
            role,
        )


class CognitiveDecisionKind(StrEnum):
    DIRECT_TOOL = "DIRECT_TOOL"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    DEFERRED = "DEFERRED"
    UNSUPPORTED = "UNSUPPORTED"
    INFORMATIONAL_RESPONSE = "INFORMATIONAL_RESPONSE"


@dataclass(frozen=True)
class CognitiveDecision:
    kind: CognitiveDecisionKind
    summary: str
    tool: str | None = None


class CognitiveEngine:
    def decide(
        self, normalized_intent: str, route: RoutingDecision | None = None, original_text: str = ""
    ) -> CognitiveDecision:
        if normalized_intent in {
            "create_folder",
            "battery_status",
            "open_application",
            "mute_volume",
            "volume_down",
        }:
            return CognitiveDecision(
                CognitiveDecisionKind.DIRECT_TOOL, "deterministic command", normalized_intent
            )
        if normalized_intent == "delete":
            return CognitiveDecision(
                CognitiveDecisionKind.APPROVAL_REQUIRED, "Deletion requires approval."
            )
        if normalized_intent == "rename":
            return CognitiveDecision(
                CognitiveDecisionKind.CLARIFICATION_REQUIRED, "Specify the file to rename."
            )
        if route and route.provider == "gemini":
            return CognitiveDecision(
                CognitiveDecisionKind.DEFERRED,
                "Cloud reasoning is unavailable; no action was taken.",
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


class MockModelProvider(DeterministicProvider):
    name = "mock"
