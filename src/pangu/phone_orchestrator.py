from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from .events import EventBus, EventEnvelope, EventPriority
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
    """Bridge delegated calls to the shared PANGU EventBus without leaking sensitive content."""

    def __init__(self, events: EventBus) -> None:
        self.events = events
        self._sessions: dict[str, DelegatedCallSession] = {}
        self._requests: dict[str, OwnerConfirmationRequest] = {}

    def register(self, session: DelegatedCallSession) -> str:
        session_id = uuid4().hex
        self._sessions[session_id] = session
        return session_id

    def get(self, session_id: str) -> DelegatedCallSession:
        try:
            return self._sessions[session_id]
        except KeyError as error:
            raise KeyError(f"unknown phone delegation session: {session_id}") from error

    async def handle_proposal(
        self,
        session_id: str,
        proposal: CounterpartyProposal,
    ) -> PolicyDecision:
        session = self.get(session_id)
        decision = session.handle_proposal(proposal)
        await self.events.publish(
            EventEnvelope(
                "phone.call.proposal",
                {
                    "session_id": session_id,
                    "field": proposal.field.value,
                    "decision": decision.decision.value,
                    "sensitive": proposal.field
                    in {ProposalField.PERSONAL_DATA, ProposalField.MEDICAL_DATA},
                },
                EventPriority.NORMAL,
            )
        )
        if decision.decision != ProposalDecision.ASK_OWNER or decision.confirmation_token is None:
            return decision
        sensitive = proposal.field in {ProposalField.PERSONAL_DATA, ProposalField.MEDICAL_DATA}
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
        session.mark_booked(confirmation_summary)
        await self.events.publish(
            EventEnvelope(
                "phone.call.booked",
                {
                    "session_id": session_id,
                    "state": DelegationState.BOOKED.value,
                    "summary": confirmation_summary[:500],
                },
                EventPriority.HIGH,
            )
        )

    async def cancel(self, session_id: str, reason: str = "owner_cancelled") -> None:
        session = self.get(session_id)
        session.cancel(reason)
        await self.events.publish(
            EventEnvelope(
                "phone.call.cancelled",
                {"session_id": session_id, "reason": reason[:200]},
                EventPriority.HIGH,
            )
        )
