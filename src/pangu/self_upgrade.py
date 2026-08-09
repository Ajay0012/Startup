from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from .model_runtime import GeminiProvider, ModelRequest, ModelRole, StructuredOutputValidator


class UpgradePlan(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    read_paths: list[str] = Field(min_length=1, max_length=24)
    expected_tests: list[str] = Field(default_factory=list, max_length=24)

    @field_validator("read_paths")
    @classmethod
    def unique_paths(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate read path")
        return value


class FileReplacement(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=120_000)
    reason: str = Field(min_length=1, max_length=1000)


class UpgradeChangeSet(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    files: list[FileReplacement] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique_files(self) -> UpgradeChangeSet:
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate replacement path")
        if sum(len(item.content) for item in self.files) > 300_000:
            raise ValueError("change set is too large")
        return self


@dataclass(frozen=True)
class UpgradeResult:
    request: str
    branch: str
    commit_sha: str | None
    promoted: bool
    tests_passed: bool
    changed_files: tuple[str, ...]
    summary: str
    normalized_error: str | None = None
    backup_branch: str | None = None


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner(Protocol):
    def run(self, args: list[str], cwd: Path, timeout: int = 900) -> ProcessResult: ...


class SubprocessRunner:
    """Argument-vector-only process boundary; no shell expansion is permitted."""

    def run(self, args: list[str], cwd: Path, timeout: int = 900) -> ProcessResult:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        return ProcessResult(completed.returncode, completed.stdout[-50_000:], completed.stderr[-50_000:])


class UpgradePolicy:
    """Fail-closed policy for code that PANGU may modify autonomously.

    Security/approval/permission boundaries and deployment credentials are intentionally
    excluded from self-modification. Those files can still be changed through normal
    owner-reviewed development, but never by this runtime.
    """

    ALLOWED_ROOTS = ("src/pangu/", "tests/", "apps/", "scripts/", "docs/")
    ALLOWED_SUFFIXES = {".py", ".cs", ".csproj", ".ps1", ".md", ".json", ".toml", ".yml", ".yaml"}
    PROTECTED = {
        "src/pangu/security.py",
        "src/pangu/approvals.py",
        "src/pangu/permissions.py",
        "src/pangu/self_upgrade.py",
        "scripts/test.ps1",
    }
    BLOCKED_PREFIXES = (
        ".git/",
        ".github/",
        "runtime-data/",
        "models/",
        "migrations/",
        ".venv/",
    )
    BLOCKED_NAMES = {".env", ".env.example", "secrets.toml"}

    @classmethod
    def normalize(cls, path: str) -> str:
        candidate = path.replace("\\", "/").lstrip("./")
        if not candidate or candidate.startswith("/") or ".." in Path(candidate).parts:
            raise ValueError("unsafe repository path")
        return candidate

    @classmethod
    def permits(cls, path: str) -> bool:
        try:
            candidate = cls.normalize(path)
        except ValueError:
            return False
        if candidate in cls.PROTECTED or Path(candidate).name.casefold() in cls.BLOCKED_NAMES:
            return False
        if candidate.startswith(cls.BLOCKED_PREFIXES):
            return False
        return candidate.startswith(cls.ALLOWED_ROOTS) and Path(candidate).suffix.casefold() in cls.ALLOWED_SUFFIXES


class OwnerDirectedSelfUpgradeRuntime:
    """Plan, implement, verify and optionally promote an explicitly requested upgrade.

    This runtime cannot wake itself up and invent upgrades. It only acts on a direct owner
    request. All edits occur in an isolated git worktree, every path is policy-checked, the
    repository test gate must pass, and production promotion is an explicit option.
    """

    def __init__(
        self,
        root: Path,
        provider: GeminiProvider,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.root = root.resolve()
        self.provider = provider
        self.runner = runner or SubprocessRunner()
        self.validator = StructuredOutputValidator()

    def inventory(self, limit: int = 500) -> tuple[str, ...]:
        items: list[str] = []
        for base in UpgradePolicy.ALLOWED_ROOTS:
            root = self.root / base
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(self.root).as_posix()
                    if UpgradePolicy.permits(relative):
                        items.append(relative)
                        if len(items) >= limit:
                            return tuple(sorted(items))
        return tuple(sorted(items))

    def _read_context(self, paths: list[str]) -> dict[str, str]:
        context: dict[str, str] = {}
        total = 0
        for requested in paths:
            relative = UpgradePolicy.normalize(requested)
            if not UpgradePolicy.permits(relative):
                raise ValueError(f"upgrade plan requested protected path: {relative}")
            path = (self.root / relative).resolve()
            if self.root not in path.parents or not path.is_file():
                raise ValueError(f"upgrade plan requested missing path: {relative}")
            content = path.read_text(encoding="utf-8", errors="replace")
            total += len(content)
            if total > 220_000:
                raise ValueError("upgrade context exceeds bounded size")
            context[relative] = content
        return context

    async def _plan(self, request: str, inventory: tuple[str, ...]) -> UpgradePlan:
        prompt = (
            "You are PANGU's software architect. The owner explicitly requested a feature upgrade. "
            "Inspect the repository inventory and choose the MINIMUM existing files needed to understand "
            "the implementation before editing. Prefer extending existing components; never create a "
            "parallel RuntimeBuilder, EventBus, database owner, microphone owner, safety authority, or "
            "duplicate feature. Return JSON only with summary, read_paths, expected_tests. Do not select "
            "security.py, approvals.py, permissions.py, self_upgrade.py, test.ps1, secrets, migrations, "
            "models, .github, or runtime-data.\n\n"
            f"Owner request: {request}\n"
            f"Repository inventory: {json.dumps(inventory)}"
        )
        result = await self.provider.generate_async(
            ModelRequest(prompt, ModelRole.CODING, mission_id="self-upgrade-plan", timeout_seconds=45),
            structured=True,
        )
        if not result.text:
            raise RuntimeError(f"self-upgrade planning unavailable: {result.error}")
        plan = self.validator.validate(result.text, UpgradePlan)
        assert isinstance(plan, UpgradePlan)
        return plan

    async def _implement(
        self,
        request: str,
        plan: UpgradePlan,
        context: dict[str, str],
    ) -> UpgradeChangeSet:
        prompt = (
            "You are PANGU's coding engineer. Implement the owner's requested feature by improving the "
            "existing architecture. Return JSON only with summary and files. Each file entry must contain "
            "path, COMPLETE replacement content, and reason. Modify only files present in the supplied "
            "context unless a new test/source file is essential; new files must live under src/pangu or "
            "tests. Preserve RuntimeBuilder as the sole composition root and preserve safety/audit/privacy "
            "boundaries. Do not weaken tests, delete unrelated functionality, edit protected files, add "
            "shell execution capabilities, embed credentials, or claim hardware validation. Include or "
            "improve tests for the feature. Keep the change set minimal.\n\n"
            f"Owner request: {request}\n"
            f"Plan: {plan.model_dump_json()}\n"
            f"Current files: {json.dumps(context, ensure_ascii=False)}"
        )
        result = await self.provider.generate_async(
            ModelRequest(prompt, ModelRole.CODING, mission_id="self-upgrade-implementation", timeout_seconds=90),
            structured=True,
        )
        if not result.text:
            raise RuntimeError(f"self-upgrade implementation unavailable: {result.error}")
        changes = self.validator.validate(result.text, UpgradeChangeSet)
        assert isinstance(changes, UpgradeChangeSet)
        for item in changes.files:
            if not UpgradePolicy.permits(item.path):
                raise ValueError(f"self-upgrade attempted protected path: {item.path}")
        return changes

    def _git(self, args: list[str], cwd: Path | None = None, timeout: int = 120) -> ProcessResult:
        return self.runner.run(["git", *args], cwd or self.root, timeout)

    def _require_clean_repository(self) -> str:
        status = self._git(["status", "--porcelain"])
        if status.returncode != 0:
            raise RuntimeError("git repository is unavailable")
        if status.stdout.strip():
            raise RuntimeError("self-upgrade requires a clean working tree")
        head = self._git(["rev-parse", "HEAD"])
        if head.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40}", head.stdout.strip()):
            raise RuntimeError("unable to resolve current git head")
        return head.stdout.strip()

    def _test_command(self, worktree: Path) -> ProcessResult:
        if os.name == "nt":
            powershell = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
            if not powershell:
                return ProcessResult(127, "", "POWERSHELL_UNAVAILABLE")
            return self.runner.run([powershell, "-NoProfile", "-File", "scripts/test.ps1"], worktree, 1800)
        return self.runner.run(["python", "-m", "pytest", "-q"], worktree, 1800)

    async def upgrade(self, request: str, *, promote: bool = False) -> UpgradeResult:
        feature = " ".join(request.strip().split())
        if len(feature) < 8:
            return UpgradeResult(feature, "", None, False, False, (), "", "UPGRADE_REQUEST_TOO_VAGUE")
        try:
            base_sha = self._require_clean_repository()
            plan = await self._plan(feature, self.inventory())
            context = self._read_context(plan.read_paths)
            changes = await self._implement(feature, plan, context)
        except (RuntimeError, ValueError) as error:
            return UpgradeResult(feature, "", None, False, False, (), "", str(error))

        token = uuid4().hex[:12]
        branch = f"pangu-self/{token}"
        worktree = Path(tempfile.gettempdir()) / f"pangu-self-{token}"
        try:
            created = self._git(["worktree", "add", "-b", branch, str(worktree), base_sha], timeout=180)
            if created.returncode != 0:
                return UpgradeResult(feature, branch, None, False, False, (), changes.summary, "WORKTREE_CREATE_FAILED")
            changed: list[str] = []
            for replacement in changes.files:
                relative = UpgradePolicy.normalize(replacement.path)
                if not UpgradePolicy.permits(relative):
                    raise ValueError(f"protected path rejected: {relative}")
                target = (worktree / relative).resolve()
                if worktree.resolve() not in target.parents:
                    raise ValueError("replacement escaped upgrade worktree")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(replacement.content, encoding="utf-8", newline="\n")
                changed.append(relative)

            diff = self.runner.run(["git", "diff", "--check"], worktree, 120)
            if diff.returncode != 0:
                return UpgradeResult(feature, branch, None, False, False, tuple(changed), changes.summary, "DIFF_VALIDATION_FAILED")
            tests = self._test_command(worktree)
            if tests.returncode != 0:
                return UpgradeResult(
                    feature,
                    branch,
                    None,
                    False,
                    False,
                    tuple(changed),
                    changes.summary,
                    f"TEST_GATE_FAILED: {tests.stderr[-2000:] or tests.stdout[-2000:]}",
                )
            self.runner.run(["git", "add", "--", *changed], worktree, 120)
            commit = self.runner.run(
                ["git", "commit", "-m", f"feat(self-upgrade): {feature[:72]}"], worktree, 180
            )
            if commit.returncode != 0:
                return UpgradeResult(feature, branch, None, False, True, tuple(changed), changes.summary, "COMMIT_FAILED")
            sha = self.runner.run(["git", "rev-parse", "HEAD"], worktree, 60).stdout.strip()
            backup: str | None = None
            promoted = False
            if promote:
                current = self._git(["rev-parse", "HEAD"]).stdout.strip()
                if current != base_sha:
                    return UpgradeResult(feature, branch, sha, False, True, tuple(changed), changes.summary, "BASE_CHANGED_DURING_UPGRADE")
                backup = f"pangu-backup/{token}"
                if self._git(["branch", backup, base_sha]).returncode != 0:
                    return UpgradeResult(feature, branch, sha, False, True, tuple(changed), changes.summary, "BACKUP_BRANCH_FAILED")
                merge = self._git(["merge", "--ff-only", branch], timeout=180)
                if merge.returncode != 0:
                    return UpgradeResult(feature, branch, sha, False, True, tuple(changed), changes.summary, "PROMOTION_FAILED", backup)
                promoted = True
            return UpgradeResult(feature, branch, sha, promoted, True, tuple(changed), changes.summary, backup_branch=backup)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            return UpgradeResult(feature, branch, None, False, False, (), changes.summary, f"UPGRADE_FAILED: {error}")
        finally:
            if worktree.exists():
                self._git(["worktree", "remove", "--force", str(worktree)], timeout=180)
