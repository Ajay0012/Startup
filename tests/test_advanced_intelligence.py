from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from pangu.conversation_intelligence import (
    ConversationAct,
    ConversationActClassifier,
    EchoReferenceGate,
    PartialTranscriptStabilizer,
    SemanticEndOfTurnDetector,
)
from pangu.evaluation import IntelligenceEvaluationGate, standard_benchmark
from pangu.multi_agent import AgentFinding, AgentRole, MultiAgentCouncil
from pangu.multimodal import ContextSignal, Modality, MultimodalContextFusion
from pangu.resilience import CircuitBreaker, CircuitState, ResilientLoadManager
from pangu.speaker_identity import (
    IdentityTrustEngine,
    SpeakerIdentityRuntime,
    SpeakerProfile,
    SpeakerRole,
    TrustContext,
)


def test_multimodal_deictic_reference_prefers_recent_gesture_target() -> None:
    fusion = MultimodalContextFusion()
    fusion.observe(
        ContextSignal(Modality.SCREEN, "control", "Chrome window", 0.95, target_id="window:chrome")
    )
    fusion.observe(
        ContextSignal(Modality.GESTURE, "point", "second error", 0.94, target_id="screen:error:2")
    )
    context = fusion.fuse("open that")
    assert context.referent is not None
    assert context.referent.target_id == "screen:error:2"


def test_sensitive_multimodal_signal_is_not_cloud_grounded_by_default() -> None:
    fusion = MultimodalContextFusion()
    fusion.observe(ContextSignal(Modality.SCREEN, "text", "secret", sensitive=True))
    assert fusion.fuse("what is this").prompt_context == ()


def test_streaming_transcript_stabilizes_repeated_prefix() -> None:
    stabilizer = PartialTranscriptStabilizer(stable_repetitions=2)
    assert stabilizer.update("open chrome").stable_prefix == ""
    assert stabilizer.update("open chrome and").stable_prefix == "open chrome"
    final = stabilizer.update("open chrome and youtube", is_final=True)
    assert final.stable_prefix == "open chrome and youtube"


def test_semantic_end_of_turn_does_not_cut_unfinished_sentence() -> None:
    detector = SemanticEndOfTurnDetector()
    incomplete = detector.decide("open chrome and", silence_ms=650, asr_final=False)
    complete = detector.decide("open chrome", silence_ms=900, asr_final=True)
    assert not incomplete.should_end
    assert complete.should_end


def test_conversation_acts_cover_backchannel_continue_repair() -> None:
    classifier = ConversationActClassifier()
    assert classifier.classify("uh huh") == ConversationAct.BACKCHANNEL
    assert classifier.classify("go on") == ConversationAct.CONTINUE
    assert classifier.classify("no, I mean the other one") == ConversationAct.REPAIR


def test_echo_reference_gate_rejects_speaker_leakage() -> None:
    gate = EchoReferenceGate(multiplier=1.5, absolute_floor=0.02)
    gate.observe_output_energy(0.20)
    assert not gate.admit_near_end(0.25, vad_speech=True)
    assert gate.admit_near_end(0.40, vad_speech=True)


def test_circuit_breaker_opens_and_recovers_half_open() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=1.0)
    breaker.failure(now=10.0)
    breaker.failure(now=10.1)
    assert breaker.state == CircuitState.OPEN
    assert not breaker.allow(now=10.5)
    assert breaker.allow(now=11.2)
    assert breaker.state == CircuitState.HALF_OPEN


@pytest.mark.asyncio
async def test_load_manager_retries_on_second_endpoint() -> None:
    manager: ResilientLoadManager[str] = ResilientLoadManager(
        ["a", "b"], max_concurrency=2, retry=None
    )
    seen: list[str] = []

    async def operation(endpoint: str) -> str:
        seen.append(endpoint)
        if endpoint == "a":
            raise OSError("temporary")
        return "ok"

    assert await manager.execute(operation, timeout_seconds=1) == "ok"
    assert "a" in seen and "b" in seen


def test_speaker_identity_and_contextual_trust() -> None:
    identity = SpeakerIdentityRuntime()
    identity.enroll(SpeakerProfile("owner", SpeakerRole.OWNER, (1.0,) + (0.0,) * 7, 0.8))
    match = identity.identify((0.99, 0.01) + (0.0,) * 6)
    assert match.accepted and match.role == SpeakerRole.OWNER
    trust = IdentityTrustEngine().assess(
        TrustContext(SpeakerRole.OWNER, True, True, True, recent_strong_auth=True),
        consequential=True,
    )
    assert trust.privileged_allowed
    assert not trust.confirmation_required


def test_evaluation_gate_rejects_reliability_regression() -> None:
    baseline = standard_benchmark(
        wake_far=0.02,
        wake_frr=0.08,
        wer=0.10,
        command_completion=0.94,
        hallucination_rate=0.03,
        computer_use_success=0.90,
        p95_turn_latency_ms=1600,
        mission_success=0.88,
        recovery_rate=0.80,
        memory_precision=0.90,
        crash_free_rate=0.995,
        label="baseline",
    )
    metrics = list(baseline.metrics)
    index = next(i for i, item in enumerate(metrics) if item.name == "crash_free_session_rate")
    metrics[index] = replace(metrics[index], value=0.94)
    candidate = type(baseline)("candidate", tuple(metrics))
    decision = IntelligenceEvaluationGate().compare(baseline, candidate)
    assert not decision.accepted
    assert any("crash_free_session_rate" in item for item in decision.regressions)


@pytest.mark.asyncio
async def test_multi_agent_safety_block_is_not_outvoted() -> None:
    async def runner(role: AgentRole, goal: str, context: tuple[AgentFinding, ...]) -> AgentFinding:
        await asyncio.sleep(0)
        if role == AgentRole.SAFETY:
            return AgentFinding(role, "unsafe consequential step", 0.98, True)
        if role == AgentRole.CRITIC:
            return AgentFinding(role, "assumptions reviewed", 0.9)
        return AgentFinding(role, f"{role.value} result for {goal}", 0.9)

    decision = await MultiAgentCouncil(runner).deliberate("complete the task")
    assert not decision.accepted
    assert decision.blockers[0].role == AgentRole.SAFETY
