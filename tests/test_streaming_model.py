from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pangu.model_runtime import ModelRequest, ModelRole
from pangu.streaming_model import StreamingGeminiProvider


class FakeStreamingTransport:
    async def generate_text(self, model: str, prompt: str, timeout_seconds: float) -> str:
        return "unused"

    async def stream_text(
        self, model: str, prompt: str, timeout_seconds: float
    ) -> AsyncIterator[str]:
        yield "Hello "
        yield "from "
        yield "PANGU."

    async def structured_output(
        self,
        model: str,
        prompt: str,
        schema: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return {}

    async def vision(
        self,
        model: str,
        prompt: str,
        images: tuple[bytes, ...],
        timeout_seconds: float,
    ) -> str:
        return "unused"


@pytest.mark.asyncio
async def test_streaming_provider_yields_incrementally_and_records_one_call() -> None:
    provider = StreamingGeminiProvider(
        "k" * 40,
        transport=FakeStreamingTransport(),
        models={role: "test-model" for role in ModelRole},
    )
    chunks = [
        chunk
        async for chunk in provider.stream_async(
            ModelRequest("Say hello", role=ModelRole.FAST, mission_id="voice-stream")
        )
    ]
    assert chunks == ["Hello ", "from ", "PANGU."]
    budget = provider.budget.mission("voice-stream")
    assert budget.calls == 1
    assert budget.input_tokens > 0
    assert budget.output_tokens > 0


@pytest.mark.asyncio
async def test_streaming_provider_fails_closed_without_api_key() -> None:
    provider = StreamingGeminiProvider(
        None,
        transport=FakeStreamingTransport(),
        models={role: "test-model" for role in ModelRole},
    )
    with pytest.raises(RuntimeError):
        _ = [
            chunk
            async for chunk in provider.stream_async(
                ModelRequest("hello", role=ModelRole.FAST, mission_id="voice-stream")
            )
        ]
