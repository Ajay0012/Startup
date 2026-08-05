from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pytest import MonkeyPatch

from pangu import cli
from pangu.model_runtime import FakeGeminiTransport, ProviderErrorCode, ProviderHealth
from pangu.runtime_builder import RuntimeBuilder, ServiceContainer


class StubBuilder:
    def __init__(self, container: ServiceContainer) -> None:
        self.container = container

    def __call__(self, root: Path) -> StubBuilder:
        return self

    def build(self) -> ServiceContainer:
        return self.container


def configured_container(tmp_path: Path) -> ServiceContainer:
    (tmp_path / ".env").write_text("GEMINI_API_KEY=cli-test-secret\n", encoding="utf-8")
    return RuntimeBuilder(tmp_path).build()


def invoke(
    monkeypatch: MonkeyPatch, capsys: object, container: ServiceContainer, *args: str
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(cli, "RuntimeBuilder", StubBuilder(container))
    monkeypatch.setattr(sys, "argv", ["pangu", *args])
    code = cli.main()
    output = json.loads(capsys.readouterr().out)  # type: ignore[union-attr]
    return code, output


def test_python_module_help_and_model_health() -> None:
    environment = os.environ | {"PYTHONPATH": str(Path("src").resolve())}
    help_result = subprocess.run(
        [sys.executable, "-m", "pangu", "--help"],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    health_result = subprocess.run(
        [sys.executable, "-m", "pangu", "model-health"],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "model-health" in help_result.stdout
    assert health_result.returncode == 0
    assert json.loads(health_result.stdout)["gemini"]["state"] in {"UNCONFIGURED", "INITIALIZING"}


def test_model_health_without_probe_makes_no_transport_request(
    monkeypatch: MonkeyPatch, capsys: object, tmp_path: Path
) -> None:
    container = configured_container(tmp_path)
    transport = FakeGeminiTransport()
    container.gemini_provider.transport = transport
    code, output = invoke(monkeypatch, capsys, container, "model-health")
    assert code == 0
    assert transport.calls == []
    assert output["gemini"]["state"] == "INITIALIZING"  # type: ignore[index]


def test_successful_probe_is_healthy_and_shuts_down(
    monkeypatch: MonkeyPatch, capsys: object, tmp_path: Path
) -> None:
    container = configured_container(tmp_path)

    class TrackingTransport(FakeGeminiTransport):
        def __init__(self) -> None:
            super().__init__()
            self.timeouts: list[float] = []

        async def health_check(self, model: str, timeout_seconds: float) -> None:
            self.timeouts.append(timeout_seconds)
            await super().health_check(model, timeout_seconds)

    transport = TrackingTransport()
    container.gemini_provider.transport = transport
    code, output = invoke(monkeypatch, capsys, container, "model-health", "--probe")
    assert code == 0
    assert transport.calls == [(container.settings.gemini_fast_model, "health")]
    assert transport.timeouts == [container.settings.gemini_timeout_seconds]
    assert output["gemini"]["state"] == "HEALTHY"  # type: ignore[index]
    assert transport.closed
    assert not container.runtime.started


def test_invalid_key_probe_is_normalized_and_keeps_key_private(
    monkeypatch: MonkeyPatch, capsys: object, tmp_path: Path
) -> None:
    container = configured_container(tmp_path)
    transport = FakeGeminiTransport(failures=[PermissionError()])
    container.gemini_provider.transport = transport
    code, output = invoke(monkeypatch, capsys, container, "model-health", "--probe")
    assert code == 1
    gemini = output["gemini"]  # type: ignore[index]
    assert gemini["state"] == ProviderHealth.INVALID_CREDENTIALS  # type: ignore[index]
    assert gemini["last_failure"] == ProviderErrorCode.INVALID_CREDENTIALS  # type: ignore[index]
    assert "cli-test-secret" not in json.dumps(output)


def test_timeout_probe_is_normalized(
    monkeypatch: MonkeyPatch, capsys: object, tmp_path: Path
) -> None:
    container = configured_container(tmp_path)
    transport = FakeGeminiTransport(failures=[TimeoutError()])
    container.gemini_provider.transport = transport
    code, output = invoke(monkeypatch, capsys, container, "model-health", "--probe")
    assert code == 1
    gemini = output["gemini"]  # type: ignore[index]
    assert gemini["state"] == ProviderHealth.OFFLINE  # type: ignore[index]
    assert gemini["last_failure"] == ProviderErrorCode.REQUEST_TIMEOUT  # type: ignore[index]
