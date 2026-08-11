from pangu.incoming_call_policy import (
    CallerTrust,
    IncomingCallAction,
    IncomingCallContext,
    IncomingCallPolicyEngine,
    IncomingCallRules,
)


def test_unknown_caller_is_not_auto_answered() -> None:
    engine = IncomingCallPolicyEngine(
        IncomingCallRules(assistant_may_screen_when_owner_absent=True)
    )
    decision = engine.decide(
        IncomingCallContext(
            caller_id="unknown",
            caller_trust=CallerTrust.UNKNOWN,
            owner_present=False,
            device_locked=False,
            fresh_device_auth=True,
            call_media_available=True,
        )
    )
    assert decision.action == IncomingCallAction.RING_OWNER


def test_sensitive_context_blocks_assistant_handling() -> None:
    engine = IncomingCallPolicyEngine(
        IncomingCallRules(assistant_may_handle_vip_callers=True)
    )
    decision = engine.decide(
        IncomingCallContext(
            caller_id="vip-1",
            caller_trust=CallerTrust.VIP,
            owner_present=True,
            device_locked=False,
            fresh_device_auth=True,
            sensitive_context=True,
            call_media_available=True,
        )
    )
    assert decision.action == IncomingCallAction.ASK_OWNER
    assert decision.may_speak_as_assistant is False


def test_known_caller_can_be_handled_only_with_media_and_fresh_auth() -> None:
    rules = IncomingCallRules(assistant_may_handle_known_callers=True)
    engine = IncomingCallPolicyEngine(rules)
    no_media = engine.decide(
        IncomingCallContext(
            caller_id="contact-1",
            caller_trust=CallerTrust.KNOWN,
            owner_present=True,
            device_locked=False,
            fresh_device_auth=True,
            call_media_available=False,
        )
    )
    assert no_media.action == IncomingCallAction.RING_OWNER

    with_media = engine.decide(
        IncomingCallContext(
            caller_id="contact-1",
            caller_trust=CallerTrust.KNOWN,
            owner_present=True,
            device_locked=False,
            fresh_device_auth=True,
            call_media_available=True,
        )
    )
    assert with_media.action == IncomingCallAction.ASSISTANT_HANDLE
    assert with_media.may_speak_as_assistant is True


def test_locked_device_without_fresh_auth_requires_owner() -> None:
    engine = IncomingCallPolicyEngine(
        IncomingCallRules(assistant_may_handle_vip_callers=True)
    )
    decision = engine.decide(
        IncomingCallContext(
            caller_id="vip-1",
            caller_trust=CallerTrust.VIP,
            owner_present=True,
            device_locked=True,
            fresh_device_auth=False,
            call_media_available=True,
        )
    )
    assert decision.action == IncomingCallAction.ASK_OWNER
