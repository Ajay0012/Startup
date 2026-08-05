from __future__ import annotations

import asyncio
from pathlib import Path

from pangu.applications import (
    ApplicationKind,
    ApplicationOperationResult,
    ApplicationRecord,
    ResolutionStatus,
    SimulatedWindowsApplicationAdapter,
    VerificationState,
)
from pangu.cli import _application_exit_code
from pangu.runtime_builder import RuntimeBuilder


def notepad(source: str = "appx") -> ApplicationRecord:
    return ApplicationRecord.create(
        "Windows Notepad",
        executable_name="notepad.exe" if source != "appx" else None,
        app_user_model_id=(
            "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App" if source == "appx" else None
        ),
        package_identity=("Microsoft.WindowsNotepad_8wekyb3d8bbwe" if source == "appx" else None),
        install_source=source,
    )


def test_catalog_persists_across_independent_containers(tmp_path: Path) -> None:
    first = RuntimeBuilder(tmp_path, SimulatedWindowsApplicationAdapter([notepad()])).build()
    asyncio.run(first.runtime.start_async())
    try:
        first.runtime.discover_applications()
    finally:
        asyncio.run(first.runtime.stop_async())

    second = RuntimeBuilder(tmp_path, SimulatedWindowsApplicationAdapter()).build()
    asyncio.run(second.runtime.start_async())
    try:
        resolved = second.runtime.resolve_application("Notepad")
        assert resolved.status == ResolutionStatus.RESOLVED
        assert resolved.selected_application is not None
        assert resolved.selected_application.application_kind == ApplicationKind.USER_APPLICATION
        assert resolved.selected_application.launch_eligible
    finally:
        asyncio.run(second.runtime.stop_async())


def test_empty_catalog_performs_one_bounded_refresh(tmp_path: Path) -> None:
    adapter = SimulatedWindowsApplicationAdapter([notepad()])
    container = RuntimeBuilder(tmp_path, adapter).build()
    asyncio.run(container.runtime.start_async())
    try:
        assert container.runtime.resolve_application("Notepad").status == ResolutionStatus.RESOLVED
        assert container.runtime.resolve_application("Notepad").status == ResolutionStatus.RESOLVED
    finally:
        asyncio.run(container.runtime.stop_async())


def test_notepad_appx_and_executable_evidence_consolidate(tmp_path: Path) -> None:
    adapter = SimulatedWindowsApplicationAdapter([notepad(), notepad("registry_app_paths")])
    container = RuntimeBuilder(tmp_path, adapter).build()
    asyncio.run(container.runtime.start_async())
    try:
        catalog = container.runtime.discover_applications()
        matches = [record for record in catalog if record.normalized_name == "windows notepad"]
        assert len(matches) == 1
        assert {"Notepad", "Windows Notepad", "notepad.exe"} <= set(matches[0].aliases)
    finally:
        asyncio.run(container.runtime.stop_async())


def test_semantic_application_exit_codes() -> None:
    assert _application_exit_code(container_result(ResolutionStatus.NOT_FOUND)) == 3
    assert _application_exit_code(container_result(ResolutionStatus.RESOLVED)) == 0
    denied = ApplicationOperationResult(
        "open", "takeown", "id", "open", "prohibited", VerificationState.DENIED, 0
    )
    assert _application_exit_code(denied) == 5


def test_appx_activation_is_selected_and_requires_visible_postcondition(tmp_path: Path) -> None:
    class RecordingAdapter(SimulatedWindowsApplicationAdapter):
        activated = False

        def activate(self, app: ApplicationRecord):
            self.activated = True
            return super().activate(app)

    adapter = RecordingAdapter([notepad()])
    container = RuntimeBuilder(tmp_path, adapter).build()
    asyncio.run(container.runtime.start_async())
    try:
        container.runtime.discover_applications()
        result = container.runtime.open_application("Notepad")
        assert adapter.activated
        assert result.verification_state == VerificationState.VERIFIED
        assert result.observed_outcome == "running with visible window"
    finally:
        asyncio.run(container.runtime.stop_async())


def container_result(status: ResolutionStatus):
    from pangu.applications import ResolutionResult

    return ResolutionResult(status)
