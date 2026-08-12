from __future__ import annotations

import pytest

from pangu.device_ecosystem import DeviceActionResult
from pangu.events import EventBus, EventEnvelope
from pangu.phone_call_dossier import CallDossierBuilder, CallSpeaker
from pangu.phone_delegation import DelegatedCallSession, DelegationEnvelope
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


def test_dossier_preserves_complete_turn_order_and_extracts_key_facts() -> None:
    builder = CallDossierBuilder("Book appointment", "Apollo Hospital")
    builder.record_turn(CallSpeaker.PANGU, "I am calling to book a dermatology appointment.")
    builder.record_turn(
        CallSpeaker.COUNTERPARTY,
        "We can schedule it tomorrow at 11:30 AM for INR 1200.",
    )
    builder.record_turn(CallSpeaker.PANGU, "That is within the approved limits. Please confirm it.")
    builder.record_turn(
        CallSpeaker.COUNTERPARTY,
        "Confirmed. Appointment reference APPT-48291 is booked.",
    )
    dossier = builder.build(outcome="BOOKED")

    assert [turn.speaker for turn in dossier.complete_conversation] == [
        CallSpeaker.PANGU,
        CallSpeaker.COUNTERPARTY,
        CallSpeaker.PANGU,
        CallSpeaker.COUNTERPARTY,
    ]
    facts = {(item.kind, item.value) for item in dossier.facts}
    assert any(kind == "price" and "1200" in value for kind, value in facts)
    assert any(kind == "reference" and "APPT-48291" in value for kind, value in facts)
    assert dossier.commitments
    assert "BOOKED" in dossier.concise_briefing


def test_redacted_transcript_hides_sensitive_turns_and_contact_identifiers() -> None:
    builder = CallDossierBuilder("Book hospital appointment", "Hospital")
    builder.record_turn(CallSpeaker.COUNTERPARTY, "Call me at +91 98765 43210 or test@example.com")
    builder.record_turn(
        CallSpeaker.OWNER,
        "My diagnosis is private-condition",
        sensitive=True,
    )
    redacted = builder.redacted_transcript()

    assert "98765" not in redacted[0].text
    assert "test@example.com" not in redacted[0].text
    assert "private-condition" not in redacted[1].text
    assert redacted[1].text == "[SENSITIVE CONTENT WITHHELD]"


@pytest.mark.asyncio
async def test_orchestrator_owner_event_never_contains_complete_sensitive_transcript() -> None:
    bus = EventBus()
    events: list[EventEnvelope] = []

    async def capture(event: EventEnvelope) -> None:
        events.append(event)

    bus.subscribe("phone.call.owner_briefing", capture)
    await bus.start()
    try:
        session = DelegatedCallSession(
            DelegationEnvelope(
                purpose="Book hospital appointment",
                target="Hospital",
                requested_service="consultation",
            ),
            FakeTransport(),
        )
        session.prepare()
        session.begin()
        orchestrator = PhoneDelegationOrchestrator(bus)
        session_id = orchestrator.register(session)
        orchestrator.record_turn(
            session_id,
            CallSpeaker.COUNTERPARTY,
            "The patient diagnosis is secret-condition.",
            sensitive=True,
        )
        orchestrator.record_turn(
            session_id,
            CallSpeaker.PANGU,
            "Please proceed without disclosing any additional medical information.",
        )
        await orchestrator.mark_booked(
            session_id, "Confirmed for tomorrow at 10:30 AM, reference HSP-4491"
        )
        await bus.stop()

        dossier = orchestrator.dossier(session_id)
        assert any("secret-condition" in turn.text for turn in dossier.complete_conversation)
        assert events
        payload_text = str(events[-1].payload)
        assert "secret-condition" not in payload_text
        assert events[-1].payload["complete_transcript_available_to_owner"] is True
        assert events[-1].payload["raw_audio_retained"] is False
    finally:
        if bus.running:
            await bus.stop()
