from __future__ import annotations

import asyncio

from pangu.events import EventBus, EventEnvelope


def test_event_envelope_topic_alias_matches_event_type() -> None:
    event = EventEnvelope("voice.response.completed", {"ok": True})
    assert event.topic == event.event_type


def test_event_bus_isolates_unexpected_subscriber_exception() -> None:
    async def scenario() -> None:
        bus = EventBus(handler_timeout=0.5)
        handled: list[str] = []

        async def broken(_: EventEnvelope) -> None:
            raise AttributeError("subscriber contract mismatch")

        async def healthy(event: EventEnvelope) -> None:
            handled.append(event.event_type)

        bus.subscribe("test.event", broken)
        bus.subscribe("test.event", healthy)
        await bus.start()
        event = EventEnvelope("test.event", {"value": 1})
        await bus.publish(event)
        await bus.stop()

        assert handled == ["test.event"]
        assert bus.dead_letters == [event]

    asyncio.run(scenario())
