from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from .benchmark_promotion import BenchmarkVerifiedPromoter
from .runtime_builder import RuntimeBuilder
from .self_upgrade import OwnerDirectedSelfUpgradeRuntime
from .settings import resolve_application_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pangu-upgrade",
        description=(
            "Owner-directed PANGU self-upgrade. Changes are generated in an isolated git worktree, "
            "validated, tested, committed to a pangu-self/* branch, and benchmark-gated before promotion."
        ),
    )
    parser.add_argument("feature", help="explicit feature or improvement requested by the owner")
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "after the repository test gate passes, require baseline/candidate intelligence benchmarks, "
            "create a backup branch, and fast-forward only when protected metrics do not regress"
        ),
    )
    parser.add_argument(
        "--baseline-benchmark",
        type=Path,
        help="JSON benchmark artifact for the exact base revision; required with --apply",
    )
    parser.add_argument(
        "--candidate-benchmark",
        type=Path,
        help="JSON benchmark artifact for the exact generated candidate revision; required with --apply",
    )
    return parser.parse_args()


async def run(
    feature: str,
    apply: bool = False,
    baseline_benchmark: Path | None = None,
    candidate_benchmark: Path | None = None,
) -> int:
    container = RuntimeBuilder(resolve_application_root()).build()
    upgrader = OwnerDirectedSelfUpgradeRuntime(container.root, container.gemini_provider)
    promoter = BenchmarkVerifiedPromoter(container.root)
    try:
        base_sha = promoter.current_head()
        result = await upgrader.upgrade(feature, promote=False)
        if not apply:
            print(json.dumps({"upgrade": asdict(result), "promotion": None}, default=str))
            return 0 if result.tests_passed else 2 if result.normalized_error else 1

        if baseline_benchmark is None or candidate_benchmark is None:
            print(
                json.dumps(
                    {
                        "upgrade": asdict(result),
                        "promotion": {
                            "promoted": False,
                            "normalized_error": "BENCHMARK_ARTIFACTS_REQUIRED",
                        },
                    },
                    default=str,
                )
            )
            return 2

        promotion = promoter.promote(
            result,
            expected_base_sha=base_sha,
            baseline_path=baseline_benchmark,
            candidate_path=candidate_benchmark,
        )
        print(
            json.dumps(
                {"upgrade": asdict(result), "promotion": asdict(promotion)},
                default=str,
            )
        )
        return 0 if promotion.promoted else 2
    finally:
        await container.gemini_provider.close()


def main() -> int:
    args = parse_args()
    return asyncio.run(
        run(
            args.feature,
            args.apply,
            args.baseline_benchmark,
            args.candidate_benchmark,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
