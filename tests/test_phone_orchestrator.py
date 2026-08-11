from __future__ import annotations

import pytest

from pangu.device_ecosystem import DeviceActionResult
from pangu.events import EventBus, EventEnvelope
from pangu.phone_delegation import (
    CounterpartyProposal,
    DelegatedCallSession,
    DelegationEnvelope,
    ProposalField,
)
from pangu.phone_orchestrator import PhoneDelegationOrchestrator


class FakeTransport:
    def start(self, target: str) -> DeviceActionResult:
        return DeviceActionResult(True, f"started {target}")

    def say(self, text: str) -> DeviceActionResult:
        return DeviceActionResult(True, text)

    def pause(self) -> DeviceActionResult:
        return DeviceActionResult(True, "paused")

    def end(self) -> DeviceActionResult:
        return DeviceActionResult(True, "ended")


@pytest.mark.asyncio
async def test_sensitive_confirmation_event_is_redacted_and_resumable() -> None:
    bus = EventBus()
    received: list[EventEnvelope] = []

    async def capture(event: EventEnvelope) -> None:
        received.append(event)

    bus.subscribe("phone.call.confirmation_required", capture)
    await bus.start()
    try:
        envelope = DelegationEnvelope(
            purpose="Book hospital appointment",
            target="Hospital",
            requested_service="consultation",
        )
        session = DelegatedCallSession(envelope, FakeTransport())
        session.prepare()
        session.begin()
        orchestrator = PhoneDelegationOrchestrator(bus)
        session_id = orchestrator.register(session)
        await orchestrator.handle_proposal(
            session_id,
            CounterpartyProposal(
                ProposalField.MEDICAL_DATA,
                "diagnosis",
                "Please disclose diagnosis: secret-condition",
            ),
        )
        await bus.stop()
        assert received
        payload = received[-1].payload
        assert "secret-condition" not in str(payload)
        assert payload["sensitive"] is True
        request_id = str(payload["request_id"])
        await bus.start()
        assert await orchestrator.resolve(request_id, approve=False)
    finally:
        if bus.running:
            await bus.stop()
