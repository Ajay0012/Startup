from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pangu.contextual_nlu import ContextualLanguageResolver
from pangu.database import DatabaseService
from pangu.memory import PersistentMemoryRuntime
from pangu.multimodal import ContextSignal, Modality, MultimodalContextFusion
from pangu.offline_intelligence import DeterministicOfflineIntelligence
from pangu.predictive_intelligence import PredictiveBehaviorRuntime
from pangu.procedure_learning import (
    DemonstrationAction,
    DemonstrationStep,
    ProcedureLearningRuntime,
)
from pangu.proactive_intelligence import (
    AttentionContext,
    ContextualInterruptionPolicy,
    ProactiveCandidate,
)
from pangu.research_intelligence import ResearchEvidence, ResearchIntelligenceRuntime
from pangu.screen_vision import OcrTextRegion, ScreenVisionRuntime, TemporalVisualTracker, VisualRegion
from pangu.windows_extended import ExtendedWindowsRuntime, WindowsExtendedResult, WindowsExtendedState
from pangu.world_graph import PersonalWorldGraph
from pangu.world_model import PersonalWorldModel


def test_world_graph_links_project_repo_bug_deadline_and_person(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "graph.db")
    database.start()
    try:
        world = PersonalWorldModel(database)
        graph = PersonalWorldGraph(world)
        graph.connect("project:pangu", "repository", "repo:startup", source="owner")
        graph.connect("project:pangu", "current_bug", "bug:voice-latency", source="runtime")
        graph.connect("bug:voice-latency", "owner", "person:ajay", source="owner")
        graph.set_property("bug:voice-latency", "deadline", "2026-08-12", source="owner")
        neighborhood = graph.neighbors("project:pangu", depth=2)
        assert "repo:startup" in neighborhood.entities
        assert "bug:voice-latency" in neighborhood.entities
        assert "person:ajay" in neighborhood.entities
        assert any(fact.attribute == "deadline" for fact in neighborhood.facts)
    finally:
        database.stop()


def test_procedure_learning_requires_owner_verification_and_parameters(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "procedures.db")
    database.start()
    try:
        memory = PersistentMemoryRuntime(database)
        learning = ProcedureLearningRuntime(memory)
        learning.begin("monthly report")
        learning.record(DemonstrationStep(DemonstrationAction.OPEN_APP, "Excel"))
        learning.record(
            DemonstrationStep(
                DemonstrationAction.SET_TEXT,
                "report month field",
                {"text": "{{month}}"},
            )
        )
        learned = learning.finish()
        assert not learned.verified
        try:
            learning.instantiate("monthly report", {"month": "August"})
            raise AssertionError("unverified procedure should not instantiate")
        except RuntimeError:
            pass
        verified = learning.verify("monthly report")
        assert verified.verified
        instantiated = learning.instantiate("monthly report", {"month": "August"})
        assert instantiated[1].arguments["text"] == "August"
    finally:
        database.stop()


def test_procedure_learning_rejects_password_and_raw_coordinates(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "procedure-safety.db")
    database.start()
    try:
        learning = ProcedureLearningRuntime(PersistentMemoryRuntime(database))
        learning.begin("unsafe")
        try:
            learning.record(
                DemonstrationStep(
                    DemonstrationAction.SET_TEXT,
                    "password field",
                    {"text": "secret"},
                )
            )
            raise AssertionError("password learning must be blocked")
        except ValueError:
            pass
    finally:
        database.stop()


def test_research_synthesis_preserves_citations_and_flags_contradictions() -> None:
    runtime = ResearchIntelligenceRuntime()
    result = runtime.synthesize(
        (
            ResearchEvidence(
                "a",
                "https://learn.microsoft.com/example",
                "Microsoft docs",
                "The feature is enabled by default",
                confidence=0.95,
            ),
            ResearchEvidence(
                "b",
                "https://example.com/article",
                "Article",
                "The feature is not enabled by default",
                confidence=0.75,
            ),
        )
    )
    assert result.citation_map["a"].startswith("https://learn.microsoft.com")
    assert result.contradictions
    assert result.confidence < 0.95


def test_predictive_behavior_requires_repeated_support_and_never_executes_directly() -> None:
    runtime = PredictiveBehaviorRuntime(minimum_support=3)
    for _ in range(4):
        runtime.observe("activity", "start coding")
        runtime.observe("open_app", "Visual Studio Code")
    runtime.observe("activity", "start coding")
    prediction = runtime.predict_next()
    assert prediction is not None
    assert "Visual Studio Code" in prediction.action
    assert prediction.execute_directly is False


def test_contextual_reference_resolves_recent_screen_target() -> None:
    fusion = MultimodalContextFusion()
    resolver = ContextualLanguageResolver(fusion)
    fusion.observe(
        ContextSignal(
            Modality.SCREEN,
            "error",
            "second compiler error",
            0.96,
            target_id="screen:error:2",
        )
    )
    resolved = resolver.resolve("click that")
    assert resolved.referent is not None
    assert resolved.referent.target_id == "screen:error:2"


def test_visual_tracker_preserves_target_across_small_movement() -> None:
    tracker = TemporalVisualTracker(iou_threshold=0.2)
    first = tracker.update((VisualRegion("r", "Save", 100, 100, 80, 30, 0.9, "vision"),))
    second = tracker.update((VisualRegion("r2", "Save", 104, 102, 80, 30, 0.92, "vision"),))
    assert first[0].target_id == second[0].target_id
    assert second[0].age_frames == 2


def test_ocr_target_resolution_rejects_ambiguity() -> None:
    regions = (
        OcrTextRegion("Build error", 0, 0, 100, 30, 0.9),
        OcrTextRegion("Build error", 0, 50, 100, 30, 0.89),
    )
    assert ScreenVisionRuntime.resolve_text_target("build error", regions) is None


def test_offline_deterministic_summary_works_without_cloud() -> None:
    result = DeterministicOfflineIntelligence.summarize(
        "PANGU tracks system health. PANGU recovers failed services. Weather is sunny today.",
        max_sentences=2,
    )
    assert result.available
    assert result.provider == "deterministic"
    assert "PANGU" in result.text


def test_contextual_interruption_policy_defers_low_urgency_during_presentation() -> None:
    policy = ContextualInterruptionPolicy()
    decision = policy.decide(
        ProactiveCandidate("build", "Build finished", 0.75, 0.3, 0.8),
        AttentionContext(datetime.now(UTC), presentation_mode=True),
    )
    assert not decision.interrupt_now


class FakePowerShellRunner:
    def run(self, script: str, args: tuple[str, ...] = (), timeout: int = 15) -> WindowsExtendedResult:
        if "Get-Printer" in script:
            return WindowsExtendedResult(
                "powershell",
                WindowsExtendedState.VERIFIED,
                [{"Name": "Office Printer", "PrinterStatus": "Normal"}],
            )
        if "Set-Clipboard" in script:
            return WindowsExtendedResult("powershell", WindowsExtendedState.VERIFIED, args[0])
        return WindowsExtendedResult("powershell", WindowsExtendedState.VERIFIED, [])


def test_extended_windows_runtime_uses_typed_fixed_runner() -> None:
    runtime = ExtendedWindowsRuntime(FakePowerShellRunner())  # type: ignore[arg-type]
    printers = runtime.printers()
    assert printers.state == WindowsExtendedState.VERIFIED
    assert printers.data == [{"Name": "Office Printer", "PrinterStatus": "Normal"}]
    clipboard = runtime.clipboard_write("hello")
    assert clipboard.state == WindowsExtendedState.VERIFIED
