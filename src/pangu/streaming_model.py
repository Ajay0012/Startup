from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from .model_runtime import (
    GeminiProvider,
    GoogleGenAITransport,
    ModelRequest,
    PrivacyOutcome,
    ProviderErrorCode,
    ProviderHealth,
)


class StreamingGoogleGenAITransport(GoogleGenAITransport):
    """Use the google-genai asynchronous streaming API instead of buffering a full reply."""

    async def stream_text(
        self, model: str, prompt: str, timeout_seconds: float
    ) -> AsyncIterator[str]:
        client = self._client_or_create()
        async with asyncio.timeout(timeout_seconds):
            stream = await client.aio.models.generate_content_stream(
                model=model,
                contents=prompt,
            )
            async for chunk in stream:
                text = str(getattr(chunk, "text", "") or "")
                if text:
                    yield text


class StreamingGeminiProvider(GeminiProvider):
    """Gemini provider with privacy/budget/circuit-aware incremental text generation."""

    async def stream_async(self, request: ModelRequest) -> AsyncIterator[str]:
        model = self.models[request.role]
        if not self._key:
            raise RuntimeError(ProviderErrorCode.PROVIDER_UNCONFIGURED)
        if self.transport is None:
            raise RuntimeError(ProviderErrorCode.NETWORK_UNAVAILABLE)
        if request.images:
            raise ValueError("streaming image requests are not enabled")
        if not self.circuit.allow():
            raise RuntimeError(ProviderErrorCode.CIRCUIT_OPEN)

        clean = self.sanitizer.sanitize(request.prompt, "text")
        if clean.outcome in {
            PrivacyOutcome.REJECT,
            PrivacyOutcome.LOCAL_ONLY,
            PrivacyOutcome.USER_CONFIRMATION_REQUIRED,
        }:
            raise PermissionError("CLOUD_PROCESSING_BLOCKED")
        input_tokens = max(1, len(clean.sanitized_content) // 4)
        if not self.budget.permit(request.mission_id, input_tokens):
            raise RuntimeError(ProviderErrorCode.BUDGET_EXCEEDED)

        started = time.monotonic()
        self.budget.record(request.mission_id, input_tokens)
        self.active_requests += 1
        output_characters = 0
        produced = False
        try:
            async for text in self.transport.stream_text(
                model,
                clean.sanitized_content,
                request.timeout_seconds,
            ):
                if not text:
                    continue
                produced = True
                output_characters += len(text)
                yield text
            if not produced:
                raise ValueError("invalid empty streaming response")
            self.circuit.success()
            self._health = ProviderHealth.HEALTHY
            self.last_success = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - third-party transport is normalized here.
            error = self._error(exc, model, request.trace_id)
            self.last_failure = error
            self._health = error.health_impact
            self.circuit.failure(error.error_code)
            raise RuntimeError(str(error.error_code)) from None
        finally:
            self.active_requests -= 1
            mission = self.budget.mission(request.mission_id)
            output_tokens = max(0, output_characters // 4)
            if mission.output_tokens + output_tokens <= self.budget.max_output_tokens:
                mission.output_tokens += output_tokens
            mission.elapsed_seconds += time.monotonic() - started
