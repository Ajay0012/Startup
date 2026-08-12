from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from pangu.advanced_realtime_voice import AdvancedRealtimeVoiceTurnCoordinator, FullDuplexPolicy
from pangu.events import EventBus
from pangu.mission_intelligence import IntelligentMissionRuntime
from pangu.missions import MissionSnapshot, MissionState
from pangu.multi_agent import AgentFinding, AgentRole, CouncilDecision
from pangu.multimodal import Modality, MultimodalContextFusion
from pangu.screen_observer import ScreenObservationPolicy, ScreenObservationRuntime
from pangu.screen_perception import ScreenRect, ScreenSnapshot, UIElement
from pangu.speaker_identity import SpeakerRole
from pangu.windows_identity import (
    ContextualIdentitySecurity,
    StrongAuthResult,
    StrongAuthState,
)


def test_advanced_voice_chunks_responses_at_safe_boundaries() -> None:
    text = (
        "First I will inspect the project carefully. Then I will run the tests and verify the result. "
        "Finally I will report only the observed outcome."
    )
    chunks = AdvancedRealtimeVoiceTurnCoordinator._chunk_response(text, 10)
    assert len(chunks) >= 2
    assert " ".join(chunks) == text
    assert all(len(chunk.split()) <= 10 for chunk in chunks)


def test_full_duplex_policy_rejects_unbounded_partial_audio() -> None:
    with pytest.raises(ValueError, match="maximum partial audio"):
        FullDuplexPolicy(maximum_partial_audio_seconds=30)


class _FakePerception:
    def __init__(self, snapshot: ScreenSnapshot) -> None:
        self.snapshot_value = snapshot

    def capture(self) -> ScreenSnapshot:
        return self.snapshot_value


@pytest.mark.asyncio
async def test_screen_observer_suppresses_password_context_from_fusion() -> None:
    password = UIElement(
        "pw",
        "Password",
        "Edit",
        "password-field",
        "Edit",
        ScreenRect(10, 10, 200, 50),
        True,
        True,
        True,
        True,
        42,
    )
    perception = _FakePerception(ScreenSnapshot("fake", "VERIFIED", "Sign in", 42, (password,)))
    fusion = MultimodalContextFusion()
    events = EventBus()
    await events.start()
    try:
        observer = ScreenObservationRuntime(
            perception,  # type: ignore[arg-type]
            fusion,
            events,
            policy=ScreenObservationPolicy(enabled=False, ocr_enabled=False),
        )
        observed = await observer.observe_once()
        assert observed is not None
        assert observed.sensitive is True
        assert observed.element_labels == ()
        assert not fusion.recent()
    finally:
        await events.stop()


@pytest.mark.asyncio
async def test_screen_observer_feeds_only_semantic_context_and_deduplicates() -> None:
    button = UIElement(
        "run",
        "Run tests",
        "Button",
        "run-tests",
        "Button",
        ScreenRect(10, 10, 140, 50),
        True,
        True,
        True,
        False,
        7,
    )
    perception = _FakePerception(
        ScreenSnapshot("fake", "VERIFIED", "Visual Studio Code", 7, (button,))
    )
    fusion = MultimodalContextFusion()
    events = EventBus()
    await events.start()
    try:
        observer = ScreenObservationRuntime(
            perception,  # type: ignore[arg-type]
            fusion,
            events,
            policy=ScreenObservationPolicy(enabled=False, ocr_enabled=False),
        )
        first = await observer.observe_once()
        second = await observer.observe_once()
        assert first is not None
        assert second is None
        signals = fusion.recent()
        assert len(signals) == 1
        assert signals[0].modality == Modality.SCREEN
        assert signals[0].target_id == "window:7"
        assert "Run tests" in str(signals[0].value)
    finally:
        await events.stop()


class _FakeHello:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, message: str = "") -> StrongAuthResult:
        self.calls += 1
        return StrongAuthResult(StrongAuthState.VERIFIED, 100.0)


@pytest.mark.asyncio
async def test_contextual_identity_requires_explicit_strong_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr("pangu.windows_identity.time.monotonic", lambda: clock["now"])
    hello = _FakeHello()
    identity = ContextualIdentitySecurity(hello=hello, strong_auth_ttl_seconds=60)  # type: ignore[arg-type]

    before = identity.assess(
        speaker=SpeakerRole.OWNER,
        windows_session_unlocked=True,
        trusted_device=True,
        local_presence=True,
        consequential=True,
    )
    assert before.strong_auth_fresh is False
    assert before.trust.confirmation_required is True

    result = await identity.require_strong_auth("Confirm privileged action")
    assert result.state == StrongAuthState.VERIFIED
    after = identity.assess(
        speaker=SpeakerRole.OWNER,
        windows_session_unlocked=True,
        trusted_device=True,
        local_presence=True,
        consequential=True,
    )
    assert after.strong_auth_fresh is True
    assert hello.calls == 1

    clock["now"] = 161.0
    expired = identity.assess(
        speaker=SpeakerRole.OWNER,
        windows_session_unlocked=True,
        trusted_device=True,
        local_presence=True,
        consequential=True,
    )
    assert expired.strong_auth_fresh is False


@dataclass
class _FakeCouncil:
    verification_evidence: str | None = None

    async def deliberate(self, goal: str) -> CouncilDecision:
        finding = AgentFinding(AgentRole.PLANNER, "safe plan", 0.9)
        return CouncilDecision(True, "safe plan", (finding,), (), True)

    async def verify_execution(
        self, goal: str, prior: CouncilDecision, execution_evidence: str
    ) -> AgentFinding:
        self.verification_evidence = execution_evidence
        return AgentFinding(AgentRole.VERIFIER, "postcondition verified", 0.95)


class _FakeOrchestrator:
    async def execute_goal(
        self,
        goal: str,
        *,
        grounding: tuple[str, ...] = (),
        priority: int = 50,
    ) -> tuple[MissionSnapshot, ...]:
        return (MissionSnapshot("m1", goal, MissionState.COMPLETED, priority, True, ()),)


@pytest.mark.asyncio
async def test_intelligent_mission_verifies_actual_terminal_evidence(tmp_path: Path) -> None:
    del tmp_path
    council = _FakeCouncil()
    runtime = IntelligentMissionRuntime(
        council,  # type: ignore[arg-type]
        _FakeOrchestrator(),  # type: ignore[arg-type]
    )
    result = await runtime.execute("prepare verified report")
    assert result.verified_success is True
    assert council.verification_evidence is not None
    assert '"state": "COMPLETED"' in council.verification_evidence
    assert '"mission_id": "m1"' in council.verification_evidence
