from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from pangu.self_upgrade import (
    FileReplacement,
    OwnerDirectedSelfUpgradeRuntime,
    UpgradeChangeSet,
    UpgradePlan,
    UpgradePolicy,
)


def test_upgrade_policy_allows_feature_code_and_tests() -> None:
    assert UpgradePolicy.permits("src/pangu/new_capability.py")
    assert UpgradePolicy.permits("tests/test_new_capability.py")
    assert UpgradePolicy.permits("apps/overlay-host/Program.cs")


def test_upgrade_policy_blocks_control_plane_and_secrets() -> None:
    blocked = (
        "src/pangu/security.py",
        "src/pangu/approvals.py",
        "src/pangu/permissions.py",
        "src/pangu/self_upgrade.py",
        "scripts/test.ps1",
        ".github/workflows/ci.yml",
        ".env",
        "models/voice/wake/model.onnx",
        "migrations/versions/9999_weaken_policy.py",
        "../outside.py",
    )
    assert all(not UpgradePolicy.permits(path) for path in blocked)


def test_upgrade_plan_rejects_duplicate_read_paths() -> None:
    with pytest.raises(ValidationError):
        UpgradePlan(summary="inspect", read_paths=["src/pangu/runtime.py", "src/pangu/runtime.py"])


def test_change_set_rejects_duplicate_replacements() -> None:
    replacement = FileReplacement(path="src/pangu/runtime.py", content="x", reason="test")
    with pytest.raises(ValidationError):
        UpgradeChangeSet(summary="duplicate", files=[replacement, replacement])


@pytest.mark.asyncio
async def test_vague_upgrade_request_fails_before_git_or_model(tmp_path: Path) -> None:
    runtime = OwnerDirectedSelfUpgradeRuntime(tmp_path, cast(Any, None))
    result = await runtime.upgrade("add ai")
    assert result.tests_passed is False
    assert result.promoted is False
    assert result.normalized_error == "UPGRADE_REQUEST_TOO_VAGUE"


def test_inventory_never_exposes_protected_files(tmp_path: Path) -> None:
    (tmp_path / "src/pangu").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/pangu/feature.py").write_text("FEATURE = True", encoding="utf-8")
    (tmp_path / "src/pangu/security.py").write_text("SECRET = True", encoding="utf-8")
    (tmp_path / "tests/test_feature.py").write_text("def test_feature(): pass", encoding="utf-8")
    runtime = OwnerDirectedSelfUpgradeRuntime(tmp_path, cast(Any, None))
    inventory = runtime.inventory()
    assert "src/pangu/feature.py" in inventory
    assert "tests/test_feature.py" in inventory
    assert "src/pangu/security.py" not in inventory
