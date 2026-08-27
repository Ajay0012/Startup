from __future__ import annotations

import json
from pathlib import Path

import pytest

from pangu.benchmark_promotion import BenchmarkArtifact
from pangu.evaluation import IntelligenceEvaluationGate


def _metrics() -> dict[str, float]:
    return {
        "wake_false_accept_rate": 0.02,
        "wake_false_reject_rate": 0.08,
        "transcription_word_error_rate": 0.10,
        "command_completion_rate": 0.94,
        "hallucination_rate": 0.02,
        "computer_use_success_rate": 0.90,
        "p95_turn_latency_ms": 1500.0,
        "mission_success_rate": 0.90,
        "mission_recovery_rate": 0.80,
        "memory_retrieval_precision": 0.90,
        "crash_free_session_rate": 0.995,
    }


def test_benchmark_artifact_requires_all_protected_metrics(tmp_path: Path) -> None:
    path = tmp_path / "bench.json"
    path.write_text(
        json.dumps({"revision": "abc", "metrics": {"wake_false_accept_rate": 0.01}}),
        encoding="utf-8",
    )
    artifact = BenchmarkArtifact.load(path)
    with pytest.raises(ValueError, match="missing metrics"):
        artifact.benchmark()


def test_evaluation_rejects_candidate_regression_from_artifacts(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline_path.write_text(
        json.dumps({"revision": "base", "metrics": _metrics()}), encoding="utf-8"
    )
    degraded = _metrics()
    degraded["crash_free_session_rate"] = 0.94
    candidate_path.write_text(
        json.dumps({"revision": "candidate", "metrics": degraded}), encoding="utf-8"
    )
    baseline = BenchmarkArtifact.load(baseline_path).benchmark()
    candidate = BenchmarkArtifact.load(candidate_path).benchmark()
    decision = IntelligenceEvaluationGate().compare(baseline, candidate)
    assert decision.accepted is False
    assert any("crash_free_session_rate" in item for item in decision.regressions)
