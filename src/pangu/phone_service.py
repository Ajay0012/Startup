from __future__ import annotations

from dataclasses import asdict

from .device_ecosystem import DeviceActionResult
from .events import EventBus
from .phone_call_dossier import CallDossier, CallSpeaker
from .phone_delegation import (
    CounterpartyProposal,
    DelegatedCallSession,
    DelegationEnvelope,
    PolicyDecision,
)
from .phone_link import PhoneCapability, PhoneCommand, PhoneLinkRuntime
from .phone_orchestrator import PhoneDelegationOrchestrator
from .phone_transport import PhoneLinkCallTransport


class PhoneIntelligenceService:
    """One production owner for paired-phone control and delegated call missions."""

    def __init__(self, link: PhoneLinkRuntime, events: EventBus) -> None:
        self.link = link
        self.events = events
        self.orchestrator = PhoneDelegationOrchestrator(events)
        self.transport = PhoneLinkCallTransport(link)

    def status(self) -> dict[str, object]:
        phone = self.link.phone
        return {
            "configured": self.link.configured,
            "connected": phone is not None,
            "device_id": phone.device_id if phone else None,
            "capabilities": sorted(item.value for item in phone.capabilities) if phone else [],
            "fresh_device_authentication": self.link.has_fresh_authentication(),
        }

    def request_authentication(self, reason: str = "Authorize PANGU phone action") -> DeviceActionResult:
        try:
            lease = self.link.queue_command(
                PhoneCommand.AUTHENTICATE,
                {"reason": " ".join(reason.split())[:300]},
                capability=PhoneCapability.AUTHENTICATE,
            )
        except (RuntimeError, PermissionError) as error:
            return DeviceActionResult(False, "Phone authentication unavailable.", normalized_error=str(error))
        return DeviceActionResult(
            True,
            "Authentication request queued on the paired phone.",
            {"command_id": lease.command_id},
        )

    def place_call(self, target: str) -> DeviceActionResult:
        return self.transport.start(target)

    def answer_call(self, call_id: str) -> DeviceActionResult:
        if not call_id.strip():
            return DeviceActionResult(False, "Invalid call id.", normalized_error="INVALID_CALL_ID")
        try:
            lease = self.link.queue_command(
                PhoneCommand.ANSWER_CALL,
                {"call_id": call_id.strip()[:200]},
                capability=PhoneCapability.ANSWER_CALL,
                requires_device_auth=True,
            )
        except (RuntimeError, PermissionError) as error:
            return DeviceActionResult(False, "Call could not be answered.", normalized_error=str(error))
        return DeviceActionResult(True, "Answer-call command queued.", {"command_id": lease.command_id})

    def end_call(self, call_id: str | None = None) -> DeviceActionResult:
        payload = {"call_id": call_id.strip()[:200]} if call_id and call_id.strip() else {}
        try:
            lease = self.link.queue_command(
                PhoneCommand.END_CALL,
                payload,
                capability=PhoneCapability.END_CALL,
                requires_device_auth=True,
            )
        except (RuntimeError, PermissionError) as error:
            return DeviceActionResult(False, "Call could not be ended.", normalized_error=str(error))
        return DeviceActionResult(True, "End-call command queued.", {"command_id": lease.command_id})

    def start_delegation(self, envelope: DelegationEnvelope) -> tuple[str | None, DeviceActionResult]:
        session = DelegatedCallSession(envelope, self.transport)
        session.prepare()
        session_id = self.orchestrator.register(session)
        result = session.begin()
        if not result.success:
            return None, result
        self.orchestrator.record_turn(
            session_id,
            CallSpeaker.PANGU,
            (
                "Hello. I am PANGU, an automated assistant calling on behalf of my user. "
                "I can discuss scheduling within approved limits, and I will ask my user before any material change."
            ),
        )
        return session_id, result

    def record_turn(
        self,
        session_id: str,
        speaker: CallSpeaker,
        text: str,
        *,
        confidence: float = 1.0,
        sensitive: bool = False,
    ) -> None:
        self.orchestrator.record_turn(
            session_id,
            speaker,
            text,
            confidence=confidence,
            sensitive=sensitive,
        )

    async def handle_proposal(
        self, session_id: str, proposal: CounterpartyProposal
    ) -> PolicyDecision:
        return await self.orchestrator.handle_proposal(session_id, proposal)

    async def resolve_confirmation(self, request_id: str, *, approve: bool) -> bool:
        return await self.orchestrator.resolve(request_id, approve=approve)

    async def mark_booked(self, session_id: str, confirmation_summary: str) -> CallDossier:
        await self.orchestrator.mark_booked(session_id, confirmation_summary)
        return self.orchestrator.dossier(session_id)

    async def cancel(self, session_id: str, reason: str = "owner_cancelled") -> CallDossier:
        await self.orchestrator.cancel(session_id, reason)
        return self.orchestrator.dossier(session_id)

    def dossier(self, session_id: str) -> CallDossier:
        return self.orchestrator.dossier(session_id)

    def dossier_public(self, session_id: str) -> dict[str, object]:
        dossier = self.dossier(session_id)
        value = asdict(dossier)
        value["complete_conversation"] = [
            {
                "speaker": turn.speaker.value,
                "text": turn.text,
                "at_monotonic": turn.at_monotonic,
                "confidence": turn.confidence,
                "sensitive": turn.sensitive,
            }
            for turn in dossier.complete_conversation
        ]
        return value
