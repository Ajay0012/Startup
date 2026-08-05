from __future__ import annotations

from pangu.applications import (
    ApplicationRecord,
    RealWindowsApplicationAdapter,
    SimulatedWindowsApplicationAdapter,
    VerificationState,
)


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
