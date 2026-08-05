from __future__ import annotations

import json

import pytest
from google.genai.errors import APIError

from pangu.model_runtime import (
    FakeGeminiTransport,
    GeminiProvider,
    GoogleGenAITransport,
    ModelRole,
    ProviderErrorCode,
    ProviderHealth,
)


def api_error(status: int, message: str) -> APIError:
    return APIError(status, {"error": {"code": status, "message": message}})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (api_error(400, "Malformed request"), ProviderErrorCode.INVALID_RESPONSE),
        (api_error(401, "Unauthenticated"), ProviderErrorCode.INVALID_CREDENTIALS),
        (api_error(403, "Permission denied"), ProviderErrorCode.INVALID_CREDENTIALS),
        (api_error(404, "Model not found"), ProviderErrorCode.MODEL_UNAVAILABLE),
        (api_error(429, "Rate limit exceeded"), ProviderErrorCode.RATE_LIMITED),
        (api_error(429, "Quota exhausted"), ProviderErrorCode.QUOTA_EXHAUSTED),
        (api_error(500, "Service unavailable"), ProviderErrorCode.NETWORK_UNAVAILABLE),
        (TimeoutError(), ProviderErrorCode.REQUEST_TIMEOUT),
        (TypeError("adapter contract mismatch"), ProviderErrorCode.INTERNAL_PROVIDER_ERROR),
    ],
)
async def test_probe_normalizes_sdk_and_adapter_errors(
    error: Exception, expected: ProviderErrorCode
) -> None:
    provider = GeminiProvider("probe-test-key", transport=FakeGeminiTransport(failures=[error]))
    result = await provider.probe_async(3)
    details = provider.health_details()
    assert result.error == expected
    assert details["last_failure"] == expected
    assert details["last_failure_exception_type"] == type(error).__name__
    assert "probe-test-key" not in json.dumps(details)


@pytest.mark.asyncio
async def test_google_transport_uses_async_minimal_request_and_closes_client() -> None:
    calls: list[dict[str, object]] = []

    class Models:
        async def generate_content(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return object()

    class Aio:
        def __init__(self) -> None:
            self.models = Models()
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class Client:
        def __init__(self) -> None:
            self.aio = Aio()

    transport = GoogleGenAITransport("probe-test-key")
    client = Client()
    transport._client = client  # type: ignore[assignment]
    await transport.health_check("gemini-3.5-flash-lite", 1)
    await transport.close()
    await transport.close()
    assert calls == [{"model": "gemini-3.5-flash-lite", "contents": "Reply with exactly OK."}]
    assert client.aio.closed


@pytest.mark.asyncio
async def test_provider_close_is_idempotent_and_closes_transport_once() -> None:
    class CountingTransport(FakeGeminiTransport):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    transport = CountingTransport()
    provider = GeminiProvider("probe-test-key", transport=transport)
    await provider.close()
    await provider.close()
    assert transport.closed
    assert transport.close_calls == 1


@pytest.mark.asyncio
async def test_probe_uses_fast_model_and_one_request() -> None:
    transport = FakeGeminiTransport()
    provider = GeminiProvider(
        "probe-test-key",
        transport=transport,
        models={role: "gemini-3.6-flash" for role in ModelRole}
        | {ModelRole.FAST: "gemini-3.5-flash-lite"},
    )
    result = await provider.probe_async(2)
    assert result.health == ProviderHealth.HEALTHY
    assert transport.calls == [("gemini-3.5-flash-lite", "health")]
