from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .device_ecosystem import DeviceActionResult


class DelegationState(StrEnum):
    CREATED = "CREATED"
    READY = "READY"
    CALLING = "CALLING"
    NEGOTIATING = "NEGOTIATING"
    AWAITING_OWNER = "AWAITING_OWNER"
    BOOKED = "BOOKED"
    DECLINED = "DECLINED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class ProposalDecision(StrEnum):
    AUTO_ACCEPT = "AUTO_ACCEPT"
    ASK_OWNER = "ASK_OWNER"
    REJECT = "REJECT"


class ProposalField(StrEnum):
    DATE = "date"
    TIME = "time"
    LOCATION = "location"
    PROVIDER = "provider"
    PRICE = "price"
    SERVICE = "service"
    PAYMENT = "payment"
    PERSONAL_DATA = "personal_data"
    MEDICAL_DATA = "medical_data"
    CANCELLATION = "cancellation"
    OTHER = "other"


@dataclass(frozen=True)
class AppointmentWindow:
    date: str
    start_minute: int
    end_minute: int

    def __post_init__(self) -> None:
        if not self.date.strip():
            raise ValueError("appointment date is required")
        if not 0 <= self.start_minute < 24 * 60:
            raise ValueError("start_minute is invalid")
        if not 0 < self.end_minute <= 24 * 60 or self.end_minute <= self.start_minute:
            raise ValueError("end_minute is invalid")

    def contains(self, date: str, minute: int) -> bool:
        return self.date == date and self.start_minute <= minute <= self.end_minute


@dataclass(frozen=True)
class DelegationEnvelope:
    purpose: str
    target: str
    requested_service: str
    acceptable_windows: tuple[AppointmentWindow, ...] = ()
    allowed_locations: frozenset[str] = frozenset()
    allowed_providers: frozenset[str] = frozenset()
    max_price: float | None = None
    currency: str = "INR"
    allow_equivalent_service: bool = False
    allow_time_change_within_window: bool = True
    auto_accept_free_reschedule: bool = True
    require_owner_for_payment: bool = True
    require_owner_for_personal_data: bool = True
    require_owner_for_medical_data: bool = True
    assistant_disclosure_required: bool = True
    transcript_retention: bool = False

    def __post_init__(self) -> None:
        if not self.purpose.strip() or not self.target.strip() or not self.requested_service.strip():
            raise ValueError("delegation purpose, target and requested service are required")
        if self.max_price is not None and self.max_price < 0:
            raise ValueError("max_price cannot be negative")
        if len(self.purpose) > 500 or len(self.target) > 300 or len(self.requested_service) > 500:
            raise ValueError("delegation fields are too long")


@dataclass(frozen=True)
class CounterpartyProposal:
    field: ProposalField
    value: object
    summary: str
    date: str | None = None
    minute: int | None = None
    price: float | None = None
    currency: str | None = None
    location: str | None = None
    provider: str | None = None

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("proposal summary is required")
        if self.price is not None and self.price < 0:
            raise ValueError("proposal price cannot be negative")


@dataclass(frozen=True)
class PolicyDecision:
    decision: ProposalDecision
    reason: str
    proposal: CounterpartyProposal
    confirmation_token: str | None = None


@dataclass
class _ConfirmationRecord:
    digest: str
    expires_at: float
    consumed: bool = False


class ExactConfirmationGate:
    """One-time, exact-proposal confirmation tokens for live call changes."""

    def __init__(self, ttl_seconds: float = 180.0) -> None:
        if not 30 <= ttl_seconds <= 900:
            raise ValueError("confirmation TTL must be between 30 and 900 seconds")
        self.ttl_seconds = ttl_seconds
        self._records: dict[str, _ConfirmationRecord] = {}

    @staticmethod
    def _digest(proposal: CounterpartyProposal) -> str:
        payload = {
            "field": proposal.field.value,
            "value": proposal.value,
            "summary": proposal.summary,
            "date": proposal.date,
            "minute": proposal.minute,
            "price": proposal.price,
            "currency": proposal.currency,
            "location": proposal.location,
            "provider": proposal.provider,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def issue(self, proposal: CounterpartyProposal) -> str:
        token = secrets.token_urlsafe(24)
        self._records[token] = _ConfirmationRecord(
            self._digest(proposal),
            time.monotonic() + self.ttl_seconds,
        )
        return token

    def consume(self, proposal: CounterpartyProposal, token: str) -> bool:
        record = self._records.get(token)
        if record is None or record.consumed or record.expires_at < time.monotonic():
            return False
        if record.digest != self._digest(proposal):
            return False
        record.consumed = True
        return True


class DelegationPolicyEngine:
    """Deterministic policy boundary for live phone negotiations.

    A language model may extract/word proposals, but cannot overrule this engine. Material
    changes, payments and sensitive disclosures always escalate to the owner unless an
    explicit envelope already permits the exact change.
    """

    _always_confirm = frozenset(
        {
            ProposalField.PAYMENT,
            ProposalField.PERSONAL_DATA,
            ProposalField.MEDICAL_DATA,
            ProposalField.CANCELLATION,
        }
    )

    def __init__(
        self,
        envelope: DelegationEnvelope,
        confirmations: ExactConfirmationGate | None = None,
    ) -> None:
        self.envelope = envelope
        self.confirmations = confirmations or ExactConfirmationGate()

    def _time_allowed(self, proposal: CounterpartyProposal) -> bool:
        if proposal.date is None or proposal.minute is None:
            return False
        return any(window.contains(proposal.date, proposal.minute) for window in self.envelope.acceptable_windows)

    def evaluate(self, proposal: CounterpartyProposal) -> PolicyDecision:
        if proposal.field in self._always_confirm:
            token = self.confirmations.issue(proposal)
            return PolicyDecision(
                ProposalDecision.ASK_OWNER,
                f"{proposal.field.value} changes always require owner confirmation.",
                proposal,
                token,
            )
        if proposal.field == ProposalField.TIME:
            if self.envelope.allow_time_change_within_window and self._time_allowed(proposal):
                return PolicyDecision(ProposalDecision.AUTO_ACCEPT, "time remains inside the approved window", proposal)
            token = self.confirmations.issue(proposal)
            return PolicyDecision(ProposalDecision.ASK_OWNER, "time is outside the pre-approved window", proposal, token)
        if proposal.field == ProposalField.DATE:
            if proposal.date is not None and any(window.date == proposal.date for window in self.envelope.acceptable_windows):
                return PolicyDecision(ProposalDecision.AUTO_ACCEPT, "date is already approved", proposal)
            token = self.confirmations.issue(proposal)
            return PolicyDecision(ProposalDecision.ASK_OWNER, "date was not pre-approved", proposal, token)
        if proposal.field == ProposalField.LOCATION:
            location = (proposal.location or str(proposal.value)).casefold().strip()
            allowed = {item.casefold().strip() for item in self.envelope.allowed_locations}
            if location and location in allowed:
                return PolicyDecision(ProposalDecision.AUTO_ACCEPT, "location is approved", proposal)
            token = self.confirmations.issue(proposal)
            return PolicyDecision(ProposalDecision.ASK_OWNER, "location change requires confirmation", proposal, token)
        if proposal.field == ProposalField.PROVIDER:
            provider = (proposal.provider or str(proposal.value)).casefold().strip()
            allowed = {item.casefold().strip() for item in self.envelope.allowed_providers}
            if provider and provider in allowed:
                return PolicyDecision(ProposalDecision.AUTO_ACCEPT, "provider is approved", proposal)
            token = self.confirmations.issue(proposal)
            return PolicyDecision(ProposalDecision.ASK_OWNER, "provider change requires confirmation", proposal, token)
        if proposal.field == ProposalField.PRICE:
            if proposal.price is None:
                token = self.confirmations.issue(proposal)
                return PolicyDecision(ProposalDecision.ASK_OWNER, "price could not be verified", proposal, token)
            if proposal.currency and proposal.currency.casefold() != self.envelope.currency.casefold():
                token = self.confirmations.issue(proposal)
                return PolicyDecision(ProposalDecision.ASK_OWNER, "currency changed", proposal, token)
            if self.envelope.max_price is not None and proposal.price <= self.envelope.max_price:
                return PolicyDecision(ProposalDecision.AUTO_ACCEPT, "price is within the approved maximum", proposal)
            token = self.confirmations.issue(proposal)
            return PolicyDecision(ProposalDecision.ASK_OWNER, "price exceeds or lacks an approved maximum", proposal, token)
        if proposal.field == ProposalField.SERVICE:
            candidate = " ".join(str(proposal.value).casefold().split())
            requested = " ".join(self.envelope.requested_service.casefold().split())
            if candidate == requested or self.envelope.allow_equivalent_service:
                return PolicyDecision(ProposalDecision.AUTO_ACCEPT, "service is allowed by the delegation", proposal)
            token = self.confirmations.issue(proposal)
            return PolicyDecision(ProposalDecision.ASK_OWNER, "service changed", proposal, token)
        token = self.confirmations.issue(proposal)
        return PolicyDecision(ProposalDecision.ASK_OWNER, "unclassified material change requires confirmation", proposal, token)

    def approve(self, proposal: CounterpartyProposal, token: str) -> bool:
        return self.confirmations.consume(proposal, token)


@dataclass(frozen=True)
class DelegatedCallEvent:
    event_type: str
    summary: str
    at_monotonic: float = field(default_factory=time.monotonic)
    sensitive: bool = False


class ConversationalCallTransport(Protocol):
    def start(self, target: str) -> DeviceActionResult: ...

    def say(self, text: str) -> DeviceActionResult: ...

    def pause(self) -> DeviceActionResult: ...

    def end(self) -> DeviceActionResult: ...


class DelegatedCallSession:
    """Stateful, privacy-first call mission for appointments/reservations.

    It intentionally stores only semantic event summaries by default. Raw call audio and
    full transcripts are not retained unless the owner explicitly enables retention.
    """

    def __init__(
        self,
        envelope: DelegationEnvelope,
        transport: ConversationalCallTransport,
        policy: DelegationPolicyEngine | None = None,
    ) -> None:
        self.envelope = envelope
        self.transport = transport
        self.policy = policy or DelegationPolicyEngine(envelope)
        self.state = DelegationState.CREATED
        self.events: list[DelegatedCallEvent] = []
        self.pending: CounterpartyProposal | None = None
        self.pending_token: str | None = None

    def _record(self, event_type: str, summary: str, *, sensitive: bool = False) -> None:
        if sensitive and not self.envelope.transcript_retention:
            summary = "Sensitive call content withheld by privacy policy."
        self.events.append(DelegatedCallEvent(event_type, summary[:1000], sensitive=sensitive))

    def prepare(self) -> None:
        if self.state != DelegationState.CREATED:
            raise RuntimeError("delegation session has already been prepared")
        self.state = DelegationState.READY
        self._record("prepared", self.envelope.purpose)

    def begin(self) -> DeviceActionResult:
        if self.state != DelegationState.READY:
            raise RuntimeError("delegation session is not ready")
        result = self.transport.start(self.envelope.target)
        if not result.success:
            self.state = DelegationState.FAILED
            self._record("call_failed", result.normalized_error or result.message)
            return result
        self.state = DelegationState.CALLING
        self._record("call_started", f"Calling {self.envelope.target}")
        if self.envelope.assistant_disclosure_required:
            disclosure = (
                "Hello. I am PANGU, an automated assistant calling on behalf of my user. "
                "I can discuss scheduling within approved limits, and I will ask my user before any material change."
            )
            spoken = self.transport.say(disclosure)
            if not spoken.success:
                self.state = DelegationState.FAILED
                self._record("disclosure_failed", spoken.normalized_error or spoken.message)
                return spoken
        self.state = DelegationState.NEGOTIATING
        self._record("negotiation_started", self.envelope.requested_service)
        return result

    def handle_proposal(self, proposal: CounterpartyProposal) -> PolicyDecision:
        if self.state != DelegationState.NEGOTIATING:
            raise RuntimeError("session is not negotiating")
        decision = self.policy.evaluate(proposal)
        self._record("proposal", proposal.summary, sensitive=proposal.field in {ProposalField.PERSONAL_DATA, ProposalField.MEDICAL_DATA})
        if decision.decision == ProposalDecision.ASK_OWNER:
            self.pending = proposal
            self.pending_token = decision.confirmation_token
            self.state = DelegationState.AWAITING_OWNER
            self.transport.pause()
            self._record("owner_confirmation_required", decision.reason)
        elif decision.decision == ProposalDecision.REJECT:
            self.transport.say("That change is outside my user's approved limits. Please offer another option.")
            self._record("proposal_rejected", decision.reason)
        else:
            self.transport.say("That works within the approved limits. Please continue.")
            self._record("proposal_auto_accepted", decision.reason)
        return decision

    def owner_decision(self, *, approve: bool, token: str) -> bool:
        if self.state != DelegationState.AWAITING_OWNER or self.pending is None or self.pending_token is None:
            raise RuntimeError("no owner confirmation is pending")
        if token != self.pending_token:
            return False
        proposal = self.pending
        if approve and not self.policy.approve(proposal, token):
            return False
        self.pending = None
        self.pending_token = None
        self.state = DelegationState.NEGOTIATING
        if approve:
            self.transport.say("My user has approved that change. Please continue with the booking.")
            self._record("owner_approved", proposal.summary, sensitive=proposal.field in {ProposalField.PERSONAL_DATA, ProposalField.MEDICAL_DATA})
        else:
            self.transport.say("My user did not approve that change. Please keep the original constraints or offer another option.")
            self._record("owner_declined", proposal.summary)
        return True

    def mark_booked(self, confirmation_summary: str) -> None:
        if self.state != DelegationState.NEGOTIATING:
            raise RuntimeError("booking can only complete while negotiating")
        self.state = DelegationState.BOOKED
        self._record("booked", confirmation_summary)
        self.transport.end()

    def cancel(self, reason: str = "owner_cancelled") -> None:
        if self.state in {DelegationState.BOOKED, DelegationState.CANCELLED, DelegationState.DECLINED}:
            return
        self.state = DelegationState.CANCELLED
        self._record("cancelled", reason)
        self.transport.end()
