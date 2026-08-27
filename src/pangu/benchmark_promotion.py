from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .evaluation import (
    EvaluationDecision,
    IntelligenceBenchmark,
    IntelligenceEvaluationGate,
    standard_benchmark,
)
from .self_upgrade import UpgradeResult


@dataclass(frozen=True)
class BenchmarkArtifact:
    revision: str
    label: str
    values: dict[str, float]

    @classmethod
    def load(cls, path: Path) -> BenchmarkArtifact:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("benchmark artifact must be an object")
        revision = str(payload.get("revision", "")).strip()
        label = str(payload.get("label", "pangu")).strip() or "pangu"
        values = payload.get("metrics")
        if not revision or not isinstance(values, dict):
            raise ValueError("benchmark artifact requires revision and metrics")
        parsed: dict[str, float] = {}
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"benchmark metric {key} must be numeric")
            parsed[str(key)] = float(value)
        return cls(revision, label, parsed)

    def benchmark(self) -> IntelligenceBenchmark:
        required = {
            "wake_false_accept_rate",
            "wake_false_reject_rate",
            "transcription_word_error_rate",
            "command_completion_rate",
            "hallucination_rate",
            "computer_use_success_rate",
            "p95_turn_latency_ms",
            "mission_success_rate",
            "mission_recovery_rate",
            "memory_retrieval_precision",
            "crash_free_session_rate",
        }
        missing = sorted(required - self.values.keys())
        if missing:
            raise ValueError(f"benchmark artifact missing metrics: {', '.join(missing)}")
        return standard_benchmark(
            wake_far=self.values["wake_false_accept_rate"],
            wake_frr=self.values["wake_false_reject_rate"],
            wer=self.values["transcription_word_error_rate"],
            command_completion=self.values["command_completion_rate"],
            hallucination_rate=self.values["hallucination_rate"],
            computer_use_success=self.values["computer_use_success_rate"],
            p95_turn_latency_ms=self.values["p95_turn_latency_ms"],
            mission_success=self.values["mission_success_rate"],
            recovery_rate=self.values["mission_recovery_rate"],
            memory_precision=self.values["memory_retrieval_precision"],
            crash_free_rate=self.values["crash_free_session_rate"],
            label=self.label,
        )


@dataclass(frozen=True)
class PromotionResult:
    promoted: bool
    evaluation: EvaluationDecision | None
    backup_branch: str | None = None
    normalized_error: str | None = None


class BenchmarkVerifiedPromoter:
    """Fail closed unless candidate metrics match the tested upgrade revision."""

    def __init__(self, root: Path, gate: IntelligenceEvaluationGate | None = None) -> None:
        self.root = root.resolve()
        self.gate = gate or IntelligenceEvaluationGate(allowed_relative_regression=0.02)

    def _git(self, args: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
        no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
        return subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=no_window,
        )

    def current_head(self) -> str:
        result = self._git(["rev-parse", "HEAD"], 60)
        if result.returncode != 0:
            raise RuntimeError("unable to resolve repository head")
        return result.stdout.strip()

    def promote(
        self,
        upgrade: UpgradeResult,
        *,
        expected_base_sha: str,
        baseline_path: Path,
        candidate_path: Path,
    ) -> PromotionResult:
        if not upgrade.tests_passed or not upgrade.commit_sha or not upgrade.branch:
            return PromotionResult(False, None, normalized_error="UPGRADE_NOT_TEST_VERIFIED")
        try:
            baseline = BenchmarkArtifact.load(baseline_path)
            candidate = BenchmarkArtifact.load(candidate_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return PromotionResult(False, None, normalized_error="BENCHMARK_ARTIFACT_INVALID")
        if baseline.revision != expected_base_sha:
            return PromotionResult(False, None, normalized_error="BASELINE_REVISION_MISMATCH")
        if candidate.revision != upgrade.commit_sha:
            return PromotionResult(False, None, normalized_error="CANDIDATE_REVISION_MISMATCH")
        try:
            evaluation = self.gate.compare(baseline.benchmark(), candidate.benchmark())
        except ValueError:
            return PromotionResult(False, None, normalized_error="BENCHMARK_METRICS_INCOMPLETE")
        if not evaluation.accepted:
            return PromotionResult(False, evaluation, normalized_error="BENCHMARK_REGRESSION")
        if self.current_head() != expected_base_sha:
            return PromotionResult(
                False, evaluation, normalized_error="BASE_CHANGED_DURING_EVALUATION"
            )
        if shutil.which("git") is None:
            return PromotionResult(False, evaluation, normalized_error="GIT_UNAVAILABLE")
        token = upgrade.commit_sha[:12]
        backup = f"pangu-backup/{token}"
        if self._git(["branch", backup, expected_base_sha]).returncode != 0:
            return PromotionResult(False, evaluation, normalized_error="BACKUP_BRANCH_FAILED")
        merge = self._git(["merge", "--ff-only", upgrade.branch])
        if merge.returncode != 0:
            return PromotionResult(
                False,
                evaluation,
                backup_branch=backup,
                normalized_error="PROMOTION_FAILED",
            )
        return PromotionResult(True, evaluation, backup_branch=backup)
