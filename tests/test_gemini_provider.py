from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from pangu.model_runtime import (
    CircuitBreaker,
    CircuitState,
    FakeGeminiTransport,
    GeminiProvider,
    ModelBudgetManager,
    ModelRequest,
    ProviderHealth,
    StructuredOutputValidator,
)


@pytest.mark.asyncio
async def test_fake_transport_success_and_missing_key_never_calls() -> None:
    transport = FakeGeminiTransport(["hello"])
    provider = GeminiProvider("key", transport=transport)
    assert (await provider.generate_async(ModelRequest("safe"))).text == "hello"
    missing = GeminiProvider(None, transport=transport)
    result = await missing.generate_async(ModelRequest("safe"))
    assert result.health == ProviderHealth.UNCONFIGURED
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_network_failure_retries_exactly_and_invalid_credentials_open_circuit() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    from pangu.model_runtime import RetryPolicy

    network = GeminiProvider(
        "key",
        transport=FakeGeminiTransport(
            failures=[ConnectionError(), ConnectionError(), ConnectionError()]
        ),
        retry_policy=RetryPolicy(2, sleep=sleep),
    )
    result = await network.generate_async(ModelRequest("safe"))
    assert result.retryable and len(network.transport.calls) == 3  # type: ignore[union-attr]
    assert sleeps == [1.0, 2.0]
    invalid = GeminiProvider("key", transport=FakeGeminiTransport(failures=[PermissionError()]))
    await invalid.generate_async(ModelRequest("safe"))
    assert invalid.circuit.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_budget_is_per_mission_and_structured_repair_is_one_attempt() -> None:
    budget = ModelBudgetManager(max_calls=1)
    provider = GeminiProvider(
        "key", transport=FakeGeminiTransport(["ok", "ok"]), budget_manager=budget
    )
    assert (await provider.generate_async(ModelRequest("a", mission_id="one"))).text == "ok"
    assert (
        await provider.generate_async(ModelRequest("a", mission_id="one"))
    ).error == "BUDGET_EXCEEDED"
    assert (await provider.generate_async(ModelRequest("a", mission_id="two"))).text == "ok"

    class Reply(BaseModel):
        answer: str

    repaired = GeminiProvider("key", transport=FakeGeminiTransport(["not json", '{"answer":"ok"}']))
    assert (
        await StructuredOutputValidator().validate_with_repair(repaired, ModelRequest("x"), Reply)
    ).answer == "ok"
    assert len(repaired.transport.calls) == 2  # type: ignore[union-attr]


def test_circuit_half_open_recovers_and_cancellation_does_not_count() -> None:
    now = [0.0]
    circuit = CircuitBreaker(1, open_duration=2, clock=lambda: now[0])
    circuit.failure("NETWORK_UNAVAILABLE")
    assert not circuit.allow()
    now[0] = 3
    assert circuit.allow() and circuit.state == CircuitState.HALF_OPEN
    circuit.success()
    assert circuit.state == CircuitState.CLOSED
    circuit.failure("CANCELLED")
    assert circuit.failures == 0


@pytest.mark.asyncio
async def test_cancellation_returns_safe_error() -> None:
    class Blocking(FakeGeminiTransport):
        async def generate_text(self, model: str, prompt: str, timeout_seconds: float) -> str:
            await asyncio.sleep(10)
            return "never"

    task = asyncio.create_task(
        GeminiProvider("key", transport=Blocking()).generate_async(ModelRequest("x"))
    )
    await asyncio.sleep(0)
    task.cancel()
    assert (await task).error == "CANCELLED"
