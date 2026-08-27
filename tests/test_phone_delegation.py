from __future__ import annotations

from pangu.device_ecosystem import DeviceActionResult
from pangu.phone_delegation import (
    AppointmentWindow,
    CounterpartyProposal,
    DelegatedCallSession,
    DelegationEnvelope,
    DelegationPolicyEngine,
    DelegationState,
    ExactConfirmationGate,
    ProposalDecision,
    ProposalField,
)


class FakeCallTransport:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.spoken: list[str] = []
        self.pauses = 0
        self.ends = 0

    def start(self, target: str) -> DeviceActionResult:
        self.started.append(target)
        return DeviceActionResult(True, "started", {"call_id": "call-1"})

    def say(self, text: str) -> DeviceActionResult:
        self.spoken.append(text)
        return DeviceActionResult(True, "spoken")

    def pause(self) -> DeviceActionResult:
        self.pauses += 1
        return DeviceActionResult(True, "paused")

    def end(self) -> DeviceActionResult:
        self.ends += 1
        return DeviceActionResult(True, "ended")


def envelope() -> DelegationEnvelope:
    return DelegationEnvelope(
        purpose="Book a hospital appointment",
        target="Example Hospital",
        requested_service="general physician consultation",
        acceptable_windows=(AppointmentWindow("2026-08-12", 600, 720),),
        allowed_locations=frozenset({"Main Branch"}),
        allowed_providers=frozenset({"Dr Rao"}),
        max_price=1500,
        currency="INR",
    )


def test_time_inside_window_can_be_auto_accepted() -> None:
    policy = DelegationPolicyEngine(envelope())
    proposal = CounterpartyProposal(
        ProposalField.TIME,
        "11:00",
        "Move the appointment to 11:00",
        date="2026-08-12",
        minute=660,
    )
    decision = policy.evaluate(proposal)
    assert decision.decision == ProposalDecision.AUTO_ACCEPT
    assert decision.confirmation_token is None


def test_time_outside_window_requires_owner_confirmation() -> None:
    policy = DelegationPolicyEngine(envelope())
    proposal = CounterpartyProposal(
        ProposalField.TIME,
        "15:00",
        "Move the appointment to 15:00",
        date="2026-08-12",
        minute=900,
    )
    decision = policy.evaluate(proposal)
    assert decision.decision == ProposalDecision.ASK_OWNER
    assert decision.confirmation_token is not None


def test_payment_always_requires_owner_confirmation() -> None:
    policy = DelegationPolicyEngine(envelope())
    proposal = CounterpartyProposal(
        ProposalField.PAYMENT,
        "pay now",
        "Pay a deposit now",
        price=500,
        currency="INR",
    )
    assert policy.evaluate(proposal).decision == ProposalDecision.ASK_OWNER


def test_exact_confirmation_token_is_one_time_and_bound_to_proposal() -> None:
    gate = ExactConfirmationGate(ttl_seconds=60)
    original = CounterpartyProposal(
        ProposalField.LOCATION,
        "South Branch",
        "Use South Branch",
        location="South Branch",
    )
    changed = CounterpartyProposal(
        ProposalField.LOCATION,
        "North Branch",
        "Use North Branch",
        location="North Branch",
    )
    token = gate.issue(original)
    assert not gate.consume(changed, token)
    assert gate.consume(original, token)
    assert not gate.consume(original, token)


def test_delegated_call_discloses_assistant_and_pauses_for_material_change() -> None:
    transport = FakeCallTransport()
    session = DelegatedCallSession(envelope(), transport)
    session.prepare()
    result = session.begin()
    assert result.success
    assert session.state == DelegationState.NEGOTIATING
    assert transport.started == ["Example Hospital"]
    assert any("automated assistant" in line.casefold() for line in transport.spoken)

    proposal = CounterpartyProposal(
        ProposalField.TIME,
        "15:00",
        "Only 15:00 is available",
        date="2026-08-12",
        minute=900,
    )
    decision = session.handle_proposal(proposal)
    assert decision.decision == ProposalDecision.ASK_OWNER
    assert session.state == DelegationState.AWAITING_OWNER
    assert transport.pauses == 1

    assert decision.confirmation_token is not None
    assert session.owner_decision(approve=True, token=decision.confirmation_token)
    assert session.state == DelegationState.NEGOTIATING
    assert any("approved" in line.casefold() for line in transport.spoken)


def test_sensitive_call_content_is_redacted_when_retention_disabled() -> None:
    transport = FakeCallTransport()
    session = DelegatedCallSession(envelope(), transport)
    session.prepare()
    session.begin()
    proposal = CounterpartyProposal(
        ProposalField.MEDICAL_DATA,
        "private diagnosis",
        "Caller requested disclosure of a private diagnosis",
    )
    session.handle_proposal(proposal)
    proposal_events = [item for item in session.events if item.event_type == "proposal"]
    assert proposal_events
    assert proposal_events[-1].summary == "Sensitive call content withheld by privacy policy."


def test_price_within_preapproved_limit_can_continue_without_interrupting_owner() -> None:
    policy = DelegationPolicyEngine(envelope())
    proposal = CounterpartyProposal(
        ProposalField.PRICE,
        1200,
        "Consultation fee is 1200 INR",
        price=1200,
        currency="INR",
    )
    assert policy.evaluate(proposal).decision == ProposalDecision.AUTO_ACCEPT
