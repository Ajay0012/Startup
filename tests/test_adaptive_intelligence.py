from pathlib import Path

from pangu.adaptive_intelligence import ActionOutcome, AdaptiveLearningRuntime
from pangu.database import DatabaseService
from pangu.memory import MemoryKind, PersistentMemoryRuntime


def test_repeated_verified_outcomes_induce_procedure(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "pangu.db")
    database.start()
    try:
        memory = PersistentMemoryRuntime(database)
        learner = AdaptiveLearningRuntime(memory)
        for _ in range(3):
            learner.record_outcome(
                ActionOutcome("application", "open", {"name": "Notepad"}, True, 20)
            )
        procedures = memory.recall(
            "application open name", kinds=(MemoryKind.PROCEDURAL,), limit=10
        )
        assert procedures
        assert procedures[0].content["operation"] == "open"
    finally:
        database.stop()


def test_repeated_failures_create_capability_gap(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "pangu.db")
    database.start()
    try:
        memory = PersistentMemoryRuntime(database)
        learner = AdaptiveLearningRuntime(memory)
        for _ in range(3):
            learner.record_outcome(
                ActionOutcome(
                    "browser", "navigate", {"url": "https://example.com"}, False, 10, "TIMEOUT"
                )
            )
        gaps = learner.capability_gaps("browser navigate")
        assert gaps
        assert gaps[0].failure_count >= 3
        assert "TIMEOUT" in gaps[0].recent_errors
    finally:
        database.stop()
