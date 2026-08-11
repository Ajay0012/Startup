from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CallerTrust(StrEnum):
    VIP = "VIP"
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    PRIVATE = "PRIVATE"
    SPAM_SUSPECT = "SPAM_SUSPECT"


class IncomingCallAction(StrEnum):
    RING_OWNER = "RING_OWNER"
    ASK_OWNER = "ASK_OWNER"
    ASSISTANT_HANDLE = "ASSISTANT_HANDLE"
    SILENCE = "SILENCE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class IncomingCallContext:
    caller_id: str | None
    caller_trust: CallerTrust
    owner_present: bool
    device_locked: bool
    fresh_device_auth: bool
    quiet_hours: bool = False
    owner_in_meeting: bool = False
    driving: bool = False
    sensitive_context: bool = False
    call_media_available: bool = False


@dataclass(frozen=True)
class IncomingCallRules:
    assistant_disclosure_required: bool = True
    assistant_may_handle_known_callers: bool = False
    assistant_may_handle_vip_callers: bool = False
    assistant_may_screen_when_owner_absent: bool = False
    silence_spam_suspect: bool = True
    reject_spam_suspect: bool = False
    require_fresh_auth_to_auto_answer: bool = True
    never_auto_answer_private: bool = True
    never_auto_answer_unknown: bool = True
    allow_assistant_during_meeting: bool = True
    allow_assistant_while_driving: bool = True


@dataclass(frozen=True)
class IncomingCallDecision:
    action: IncomingCallAction
    reason: str
    may_speak_as_assistant: bool


class IncomingCallPolicyEngine:
    """Deterministic incoming-call authority; language models cannot override it."""

    def __init__(self, rules: IncomingCallRules | None = None) -> None:
        self.rules = rules or IncomingCallRules()

    def decide(self, context: IncomingCallContext) -> IncomingCallDecision:
        rules = self.rules
        if context.sensitive_context:
            return IncomingCallDecision(
                IncomingCallAction.ASK_OWNER,
                "sensitive device context blocks autonomous call handling",
                False,
            )
        if context.caller_trust == CallerTrust.SPAM_SUSPECT:
            if rules.reject_spam_suspect:
                return IncomingCallDecision(IncomingCallAction.REJECT, "spam-suspect caller", False)
            if rules.silence_spam_suspect:
                return IncomingCallDecision(IncomingCallAction.SILENCE, "spam-suspect caller", False)
            return IncomingCallDecision(IncomingCallAction.ASK_OWNER, "spam classification is uncertain", False)
        if context.caller_trust == CallerTrust.PRIVATE and rules.never_auto_answer_private:
            return IncomingCallDecision(IncomingCallAction.RING_OWNER, "private caller cannot be auto-answered", False)
        if context.caller_trust == CallerTrust.UNKNOWN and rules.never_auto_answer_unknown:
            return IncomingCallDecision(IncomingCallAction.RING_OWNER, "unknown caller cannot be auto-answered", False)
        if rules.require_fresh_auth_to_auto_answer and (context.device_locked or not context.fresh_device_auth):
            return IncomingCallDecision(
                IncomingCallAction.ASK_OWNER,
                "fresh device authentication is required for autonomous answering",
                False,
            )
        if not context.call_media_available:
            return IncomingCallDecision(
                IncomingCallAction.RING_OWNER,
                "call control is available but assistant conversation media is not",
                False,
            )

        allowed = (
            context.caller_trust == CallerTrust.VIP and rules.assistant_may_handle_vip_callers
        ) or (
            context.caller_trust == CallerTrust.KNOWN and rules.assistant_may_handle_known_callers
        )
        if not allowed and not (not context.owner_present and rules.assistant_may_screen_when_owner_absent):
            return IncomingCallDecision(IncomingCallAction.RING_OWNER, "caller is outside assistant delegation rules", False)
        if context.owner_in_meeting and not rules.allow_assistant_during_meeting:
            return IncomingCallDecision(IncomingCallAction.SILENCE, "owner meeting policy blocks assistant handling", False)
        if context.driving and not rules.allow_assistant_while_driving:
            return IncomingCallDecision(IncomingCallAction.RING_OWNER, "driving policy blocks assistant handling", False)
        if context.quiet_hours and context.caller_trust != CallerTrust.VIP:
            return IncomingCallDecision(IncomingCallAction.SILENCE, "quiet-hours policy", False)
        return IncomingCallDecision(
            IncomingCallAction.ASSISTANT_HANDLE,
            "caller and context are inside explicit assistant delegation rules",
            rules.assistant_disclosure_required,
        )
