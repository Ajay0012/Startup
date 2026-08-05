from __future__ import annotations

from pangu.applications import (
    ApplicationCatalog,
    ApplicationKind,
    ApplicationRecord,
    RealWindowsApplicationAdapter,
    ResolutionResult,
    ResolutionStatus,
    SimulatedWindowsApplicationAdapter,
    VerificationState,
)
from pangu.database import DatabaseService


def chrome() -> ApplicationRecord:
    return ApplicationRecord.create(
        "Google Chrome", executable_name="chrome.exe", process_names=("chrome.exe",)
    )


def test_standard_aliases_are_registered_without_paths() -> None:
    app = chrome()
    assert {"Chrome", "Google Chrome", "chrome browser"} <= set(app.aliases)
    assert "executable_path" not in app.public()


def test_standard_vscode_and_notepad_aliases() -> None:
    assert "VS Code" in ApplicationRecord.create("Visual Studio Code").aliases
    assert "Windows Notepad" in ApplicationRecord.create("Notepad").aliases


def test_stable_id_uses_identity_evidence() -> None:
    assert (
        chrome().application_id
        == ApplicationRecord.create("Chrome", executable_name="chrome.exe").application_id
    )


def test_simulated_open_has_observed_process_and_window() -> None:
    adapter = SimulatedWindowsApplicationAdapter([chrome()])
    result = adapter.launch(chrome())
    assert result.succeeded and result.pid is not None
    assert [item.pid for item in adapter.processes()] == [result.pid]
    assert adapter.windows()[0].pid == result.pid


def test_simulated_window_state_operations_are_observable() -> None:
    adapter = SimulatedWindowsApplicationAdapter([chrome()])
    pid = adapter.launch(chrome()).pid
    assert pid is not None
    handle = adapter.windows()[0].handle
    assert adapter.window_action(handle, "minimize").succeeded
    assert adapter.windows()[0].minimized
    assert adapter.window_action(handle, "restore").succeeded
    assert not adapter.windows()[0].minimized
    assert adapter.window_action(handle, "maximize").succeeded
    assert adapter.windows()[0].maximized


def test_simulated_graceful_close_verifies_window_disappearance() -> None:
    adapter = SimulatedWindowsApplicationAdapter([chrome()])
    adapter.launch(chrome())
    assert adapter.graceful_close(adapter.windows()[0].handle).succeeded
    assert adapter.windows() == []


def test_simulated_termination_removes_only_requested_process() -> None:
    adapter = SimulatedWindowsApplicationAdapter([chrome()])
    first, second = adapter.launch(chrome()).pid, adapter.launch(chrome()).pid
    assert first is not None and second is not None
    assert adapter.terminate(first).succeeded
    assert [item.pid for item in adapter.processes()] == [second]


def test_real_adapter_reports_empty_windows_off_platform(monkeypatch: object) -> None:
    adapter = RealWindowsApplicationAdapter()
    monkeypatch.setattr(adapter, "_supported", lambda: False)  # type: ignore[attr-defined]
    assert adapter.windows() == []
    assert adapter.launch(chrome()).supported is False


def test_verification_state_has_no_implicit_success() -> None:
    assert VerificationState.FAILED != VerificationState.VERIFIED


def test_shells_interpreters_and_administrative_utilities_are_classified() -> None:
    for name in ("cmd.exe", "powershell.exe", "python.exe", "node.exe", "taskkill.exe"):
        app = ApplicationRecord.create(name, executable_name=name, install_source="path")
        assert app.application_kind == ApplicationKind.COMMAND_LINE_TOOL
        assert not app.launch_eligible
    assert not ApplicationRecord.create("takeown", executable_name="takeown.exe").launch_eligible
    vssadmin = ApplicationRecord.create("vssadmin", executable_name="vssadmin.exe")
    assert vssadmin.protected and vssadmin.requires_elevation


def test_path_only_executables_are_not_user_applications() -> None:
    app = ApplicationRecord.create(
        "system helper", executable_name="helper.exe", install_source="path"
    )
    assert app.application_kind == ApplicationKind.UNKNOWN
    assert not app.launch_eligible


def test_process_only_services_are_not_user_applications() -> None:
    for name in ("wslservice.exe", "wlanext.exe", "worker.exe"):
        app = ApplicationRecord.create(name, executable_name=name, install_source="running_process")
        assert app.application_kind == ApplicationKind.BACKGROUND_PROCESS
        assert not app.launch_eligible and not app.control_eligible and app.protected


def test_visual_studio_developer_command_prompts_are_not_launchable() -> None:
    app = ApplicationRecord.create(
        "Visual Studio 2022 x64 Native Tools Command Prompt",
        executable_name="cmd.exe",
        install_source="start_menu",
    )
    assert app.application_kind == ApplicationKind.COMMAND_LINE_TOOL
    assert not app.launch_eligible


def test_resolution_public_output_nests_safe_application_object() -> None:
    result = ResolutionResult(ResolutionStatus.RESOLVED, chrome())
    encoded = result.public()
    assert isinstance(encoded["selected_application"], dict)
    assert "executable_path" not in encoded["selected_application"]


def test_catalog_hides_excluded_records_by_default(tmp_path: object) -> None:
    path = tmp_path / "pangu.db"  # type: ignore[operator]
    db = DatabaseService(path)
    db.start()
    try:
        notepad = ApplicationRecord.create(
            "Notepad", executable_name="notepad.exe", install_source="start_menu"
        )
        blocked = ApplicationRecord.create(
            "taskkill", executable_name="taskkill.exe", install_source="path"
        )
        catalog = ApplicationCatalog(db, SimulatedWindowsApplicationAdapter([notepad, blocked]))
        catalog.refresh()
        assert catalog.list() == [notepad]
        assert {x.display_name for x in catalog.list(include_non_user=True)} == {
            "Notepad",
            "taskkill",
        }
    finally:
        db.stop()
