from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from pangu.phone_call_dossier import CallSpeaker
from pangu.phone_delegation import (
    AppointmentWindow,
    CounterpartyProposal,
    DelegationEnvelope,
    ProposalField,
)
from pangu.runtime_builder import ServiceContainer


class SignedPhoneMessage(BaseModel):
    device_id: str
    sequence: int
    kind: str
    issued_at: int
    expires_at: int
    payload: dict[str, Any]
    signature: str


class PhoneAuthRequest(BaseModel):
    reason: str = "Authorize PANGU phone action"


class PhoneCallRequest(BaseModel):
    target: str


class AppointmentWindowRequest(BaseModel):
    date: str
    start_minute: int = Field(ge=0, lt=1440)
    end_minute: int = Field(gt=0, le=1440)


class DelegationRequest(BaseModel):
    purpose: str
    target: str
    requested_service: str
    acceptable_windows: list[AppointmentWindowRequest] = []
    allowed_locations: list[str] = []
    allowed_providers: list[str] = []
    max_price: float | None = Field(default=None, ge=0)
    currency: str = "INR"
    allow_equivalent_service: bool = False
    transcript_retention: bool = False


class OwnerDecisionRequest(BaseModel):
    approve: bool


class TranscriptTurnRequest(BaseModel):
    speaker: CallSpeaker
    text: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    sensitive: bool = False


class ProposalRequest(BaseModel):
    field: ProposalField
    value: Any
    summary: str
    date: str | None = None
    minute: int | None = Field(default=None, ge=0, le=1439)
    price: float | None = Field(default=None, ge=0)
    currency: str | None = None
    location: str | None = None
    provider: str | None = None


class BookingRequest(BaseModel):
    confirmation_summary: str


def _raw(message: SignedPhoneMessage) -> str:
    return json.dumps(message.model_dump(), ensure_ascii=False, separators=(",", ":"))


def phone_router(container: ServiceContainer) -> APIRouter:
    router = APIRouter(prefix="/api/v1/phone", tags=["phone"])
    link = container.phone_link
    service = container.phone

    @router.get("/status")
    def status() -> dict[str, object]:
        return service.status()

    @router.post("/challenge")
    def challenge() -> dict[str, object]:
        if not link.configured:
            raise HTTPException(503, "Phone link is not configured")
        value = link.issue_pairing_challenge()
        return {"challenge": value.challenge, "expires_at": value.expires_at}

    @router.post("/connect")
    def connect(message: SignedPhoneMessage) -> dict[str, object]:
        verified = link.parse_and_verify(_raw(message))
        if verified is None:
            raise HTTPException(401, "Invalid or replayed phone handshake")
        try:
            phone = link.accept_hello(verified)
        except (ValueError, PermissionError) as error:
            raise HTTPException(401, str(error)) from error
        return {
            "connected": True,
            "device_id": phone.device_id,
            "capabilities": sorted(item.value for item in phone.capabilities),
        }

    @router.post("/poll")
    def poll(message: SignedPhoneMessage) -> dict[str, object]:
        verified = link.parse_and_verify(_raw(message))
        phone = link.phone
        if verified is None or phone is None or verified.device_id != phone.device_id:
            raise HTTPException(401, "Invalid, replayed, or unpaired phone message")
        if verified.kind != "poll":
            raise HTTPException(400, "Expected poll message")
        link.touch()
        return {"command": link.next_wire_command()}

    @router.post("/event")
    async def phone_event(message: SignedPhoneMessage) -> dict[str, object]:
        verified = link.parse_and_verify(_raw(message))
        phone = link.phone
        if verified is None or phone is None or verified.device_id != phone.device_id:
            raise HTTPException(401, "Invalid, replayed, or unpaired phone event")
        link.touch()
        payload = verified.payload
        if verified.kind == "authenticated":
            if payload.get("success") is True:
                link.mark_authenticated(seconds=120)
            return {
                "accepted": True,
                "fresh_device_authentication": link.has_fresh_authentication(),
            }
        if verified.kind == "transcript_turn":
            try:
                service.record_turn(
                    str(payload["session_id"]),
                    CallSpeaker(str(payload["speaker"])),
                    str(payload["text"]),
                    confidence=float(payload.get("confidence", 1.0)),
                    sensitive=bool(payload.get("sensitive", False)),
                )
            except (KeyError, ValueError, TypeError) as error:
                raise HTTPException(400, "Invalid transcript turn") from error
            return {"accepted": True}
        if verified.kind == "proposal":
            try:
                proposal = CounterpartyProposal(
                    ProposalField(str(payload["field"])),
                    payload.get("value"),
                    str(payload["summary"]),
                    date=str(payload["date"]) if payload.get("date") is not None else None,
                    minute=int(payload["minute"]) if payload.get("minute") is not None else None,
                    price=float(payload["price"]) if payload.get("price") is not None else None,
                    currency=str(payload["currency"])
                    if payload.get("currency") is not None
                    else None,
                    location=str(payload["location"])
                    if payload.get("location") is not None
                    else None,
                    provider=str(payload["provider"])
                    if payload.get("provider") is not None
                    else None,
                )
                decision = await service.handle_proposal(str(payload["session_id"]), proposal)
            except (KeyError, ValueError, TypeError) as error:
                raise HTTPException(400, "Invalid phone proposal") from error
            return {
                "accepted": True,
                "decision": decision.decision.value,
                "reason": decision.reason,
            }
        if verified.kind == "booked":
            try:
                dossier = await service.mark_booked(
                    str(payload["session_id"]), str(payload["confirmation_summary"])
                )
            except (KeyError, ValueError, RuntimeError) as error:
                raise HTTPException(400, "Invalid booking completion event") from error
            return {"accepted": True, "briefing": dossier.concise_briefing}
        # Call state/notification events can be added to the world model without granting authority.
        return {"accepted": True, "ignored_kind": verified.kind}

    @router.post("/authenticate")
    def request_authentication(payload: PhoneAuthRequest) -> dict[str, object]:
        result = service.request_authentication(payload.reason)
        if not result.success:
            raise HTTPException(409, result.normalized_error or result.message)
        return {"status": result.message, "data": result.data}

    @router.post("/call")
    def place_call(payload: PhoneCallRequest) -> dict[str, object]:
        result = service.place_call(payload.target)
        if not result.success:
            raise HTTPException(409, result.normalized_error or result.message)
        return {"status": result.message, "data": result.data}

    @router.post("/delegations")
    def start_delegation(payload: DelegationRequest) -> dict[str, object]:
        envelope = DelegationEnvelope(
            purpose=payload.purpose,
            target=payload.target,
            requested_service=payload.requested_service,
            acceptable_windows=tuple(
                AppointmentWindow(item.date, item.start_minute, item.end_minute)
                for item in payload.acceptable_windows
            ),
            allowed_locations=frozenset(payload.allowed_locations),
            allowed_providers=frozenset(payload.allowed_providers),
            max_price=payload.max_price,
            currency=payload.currency,
            allow_equivalent_service=payload.allow_equivalent_service,
            transcript_retention=payload.transcript_retention,
        )
        session_id, result = service.start_delegation(envelope)
        if not result.success or session_id is None:
            raise HTTPException(409, result.normalized_error or result.message)
        return {"session_id": session_id, "status": result.message}

    @router.post("/delegations/{session_id}/turn")
    def record_turn(session_id: str, payload: TranscriptTurnRequest) -> dict[str, object]:
        service.record_turn(
            session_id,
            payload.speaker,
            payload.text,
            confidence=payload.confidence,
            sensitive=payload.sensitive,
        )
        return {"accepted": True}

    @router.post("/delegations/{session_id}/proposal")
    async def proposal(session_id: str, payload: ProposalRequest) -> dict[str, object]:
        decision = await service.handle_proposal(
            session_id,
            CounterpartyProposal(
                payload.field,
                payload.value,
                payload.summary,
                date=payload.date,
                minute=payload.minute,
                price=payload.price,
                currency=payload.currency,
                location=payload.location,
                provider=payload.provider,
            ),
        )
        return {
            "decision": decision.decision.value,
            "reason": decision.reason,
            "owner_confirmation_required": decision.confirmation_token is not None,
        }

    @router.post("/confirmations/{request_id}")
    async def confirmation(request_id: str, payload: OwnerDecisionRequest) -> dict[str, object]:
        accepted = await service.resolve_confirmation(request_id, approve=payload.approve)
        if not accepted:
            raise HTTPException(404, "Confirmation request not found or expired")
        return {"accepted": True, "approved": payload.approve}

    @router.post("/delegations/{session_id}/booked")
    async def mark_booked(session_id: str, payload: BookingRequest) -> dict[str, object]:
        dossier = await service.mark_booked(session_id, payload.confirmation_summary)
        return {"outcome": dossier.outcome, "briefing": dossier.concise_briefing}

    @router.get("/delegations/{session_id}/dossier")
    def dossier(session_id: str) -> dict[str, object]:
        try:
            return service.dossier_public(session_id)
        except KeyError as error:
            raise HTTPException(404, "Delegated call session not found") from error

    return router
