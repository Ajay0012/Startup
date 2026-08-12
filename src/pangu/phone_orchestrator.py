from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from .events import EventBus, EventEnvelope, EventPriority
from .phone_call_dossier import CallDossier, CallDossierBuilder, CallSpeaker
from .phone_delegation import (
    CounterpartyProposal,
    DelegatedCallSession,
    DelegationState,
    PolicyDecision,
    ProposalDecision,
    ProposalField,
)


@dataclass(frozen=True)
class OwnerConfirmationRequest:
    request_id: str
    session_id: str
    token: str
    summary: str
    field: ProposalField
    sensitive: bool


class PhoneDelegationOrchestrator:
    """Bridge delegated calls to PANGU while retaining owner-only conversation dossiers.

    Shared EventBus payloads remain redacted/minimal. Complete transcript turns live inside the
    session dossier builder and are exposed only through explicit owner-facing retrieval.
    """

    def __init__(self, events: EventBus) -> None:
        self.events = events
        self._sessions: dict[str, DelegatedCallSession] = {}
        self._requests: dict[str, OwnerConfirmationRequest] = {}
        self._dossiers: dict[str, CallDossierBuilder] = {}
        self._completed: dict[str, CallDossier] = {}

    def register(self, session: DelegatedCallSession) -> str:
        session_id = uuid4().hex
        self._sessions[session_id] = session
        self._dossiers[session_id] = CallDossierBuilder(
            session.envelope.purpose,
            session.envelope.target,
        )
        return session_id

    def get(self, session_id: str) -> DelegatedCallSession:
        try:
            return self._sessions[session_id]
        except KeyError as error:
            raise KeyError(f"unknown phone delegation session: {session_id}") from error

    def record_turn(
        self,
        session_id: str,
        speaker: CallSpeaker,
        text: str,
        *,
        confidence: float = 1.0,
        sensitive: bool = False,
    ) -> None:
        """Record one complete conversational turn for immediate owner review.

        Call audio is not stored here. A speech transport/STT layer should call this for every
        finalized turn. Sensitive turns remain inside the owner-only dossier and are never copied
        into normal EventBus payloads.
        """
        self.get(session_id)
        self._dossiers[session_id].record_turn(
            speaker,
            text,
            confidence=confidence,
            sensitive=sensitive,
        )

    def record_fact(self, session_id: str, kind: str, value: str, source_turn: int = -1) -> None:
        self.get(session_id)
        self._dossiers[session_id].record_fact(kind, value, source_turn)

    def dossier(self, session_id: str) -> CallDossier:
        completed = self._completed.get(session_id)
        if completed is not None:
            return completed
        session = self.get(session_id)
        return self._dossiers[session_id].build(outcome=session.state.value)

    async def _publish_owner_briefing(self, session_id: str, outcome: str) -> CallDossier:
        dossier = self._dossiers[session_id].build(outcome=outcome)
        self._completed[session_id] = dossier
        await self.events.publish(
            EventEnvelope(
                "phone.call.owner_briefing",
                {
                    "session_id": session_id,
                    "outcome": dossier.outcome,
                    "briefing": dossier.concise_briefing[:1500],
                    "turn_count": len(dossier.complete_conversation),
                    "commitment_count": len(dossier.commitments),
                    "fact_count": len(dossier.facts),
                    "owner_confirmation_count": len(dossier.owner_confirmations),
                    "unresolved_count": len(dossier.unresolved_items),
                    "complete_transcript_available_to_owner": True,
                    "raw_audio_retained": False,
                    "sensitive_content_in_event": False,
                },
                EventPriority.HIGH,
            )
        )
        return dossier

    async def handle_proposal(
        self,
        session_id: str,
        proposal: CounterpartyProposal,
    ) -> PolicyDecision:
        session = self.get(session_id)
        sensitive = proposal.field in {ProposalField.PERSONAL_DATA, ProposalField.MEDICAL_DATA}
        self.record_turn(
            session_id,
            CallSpeaker.COUNTERPARTY,
            proposal.summary,
            sensitive=sensitive,
        )
        decision = session.handle_proposal(proposal)
        await self.events.publish(
            EventEnvelope(
                "phone.call.proposal",
                {
                    "session_id": session_id,
                    "field": proposal.field.value,
                    "decision": decision.decision.value,
                    "sensitive": sensitive,
                },
                EventPriority.NORMAL,
            )
        )
        if decision.decision != ProposalDecision.ASK_OWNER or decision.confirmation_token is None:
            if decision.decision == ProposalDecision.AUTO_ACCEPT:
                self.record_turn(
                    session_id,
                    CallSpeaker.PANGU,
                    "That works within the approved limits. Please continue.",
                )
            return decision
        request = OwnerConfirmationRequest(
            uuid4().hex,
            session_id,
            decision.confirmation_token,
            "Sensitive information requested during the call. Approve disclosure?"
            if sensitive
            else proposal.summary[:500],
            proposal.field,
            sensitive,
        )
        self._requests[request.request_id] = request
        await self.events.publish(
            EventEnvelope(
                "phone.call.confirmation_required",
                {
                    "request_id": request.request_id,
                    "session_id": session_id,
                    "summary": request.summary,
                    "field": request.field.value,
                    "sensitive": request.sensitive,
                },
                EventPriority.HIGH,
            )
        )
        return decision

    async def resolve(self, request_id: str, *, approve: bool) -> bool:
        request = self._requests.get(request_id)
        if request is None:
            return False
        session = self.get(request.session_id)
        accepted = session.owner_decision(approve=approve, token=request.token)
        if not accepted:
            return False
        del self._requests[request_id]
        summary = (
            f"Owner approved {request.field.value} change."
            if approve
            else f"Owner declined {request.field.value} change."
        )
        self._dossiers[request.session_id].record_owner_confirmation(summary)
        self.record_turn(
            request.session_id, CallSpeaker.OWNER, summary, sensitive=request.sensitive
        )
        self.record_turn(
            request.session_id,
            CallSpeaker.PANGU,
            "My user has approved that change. Please continue with the booking."
            if approve
            else "My user did not approve that change. Please keep the original constraints or offer another option.",
        )
        await self.events.publish(
            EventEnvelope(
                "phone.call.confirmation_resolved",
                {
                    "request_id": request_id,
                    "session_id": request.session_id,
                    "approved": approve,
                    "field": request.field.value,
                },
                EventPriority.HIGH,
            )
        )
        return True

    async def mark_booked(self, session_id: str, confirmation_summary: str) -> None:
        session = self.get(session_id)
        self.record_turn(session_id, CallSpeaker.COUNTERPARTY, confirmation_summary)
        session.mark_booked(confirmation_summary)
        self._dossiers[session_id].record_fact("booking_confirmation", confirmation_summary)
        dossier = await self._publish_owner_briefing(session_id, DelegationState.BOOKED.value)
        await self.events.publish(
            EventEnvelope(
                "phone.call.booked",
                {
                    "session_id": session_id,
                    "state": DelegationState.BOOKED.value,
                    "summary": confirmation_summary[:500],
                    "owner_briefing": dossier.concise_briefing[:1000],
                },
                EventPriority.HIGH,
            )
        )

    async def cancel(self, session_id: str, reason: str = "owner_cancelled") -> None:
        session = self.get(session_id)
        self.record_turn(session_id, CallSpeaker.SYSTEM, f"Call ended: {reason}")
        session.cancel(reason)
        await self._publish_owner_briefing(session_id, DelegationState.CANCELLED.value)
        await self.events.publish(
            EventEnvelope(
                "phone.call.cancelled",
                {"session_id": session_id, "reason": reason[:200]},
                EventPriority.HIGH,
            )
        )
