from __future__ import annotations

import json
from pathlib import Path

from pytest import MonkeyPatch

from pangu import cli
from pangu.runtime_builder import RuntimeBuilder
from pangu.settings import PanguSettings

FAKE_KEY = "AIza" + "A" * 35


def write_env(root: Path, key: str) -> None:
    root.mkdir(exist_ok=True)
    (root / ".env").write_text(f"GEMINI_API_KEY={key}\n", encoding="utf-8")


def test_runtime_builder_loads_explicit_root_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    write_env(tmp_path, FAKE_KEY)
    container = RuntimeBuilder(tmp_path).build()
    assert container.gemini_provider.health_details()["configured"] is True
    assert container.settings.gemini_api_key is not None


def test_process_environment_overrides_root_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    root_key = "AIza" + "B" * 35
    process_key = "AIza" + "C" * 35
    write_env(tmp_path, root_key)
    monkeypatch.setenv("GEMINI_API_KEY", process_key)
    assert PanguSettings.load_root(tmp_path).gemini_api_key.get_secret_value() == process_key  # type: ignore[union-attr]


def test_unrelated_working_directory_env_is_ignored(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    application_root, unrelated = tmp_path / "application", tmp_path / "unrelated"
    write_env(application_root, FAKE_KEY)
    write_env(unrelated, "AIza" + "D" * 35)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(unrelated)
    settings = RuntimeBuilder(application_root).build().settings
    assert settings.gemini_api_key.get_secret_value() == FAKE_KEY  # type: ignore[union-attr]


def test_missing_and_placeholder_keys_are_unconfigured(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert RuntimeBuilder(tmp_path).build().gemini_provider.health().value == "UNCONFIGURED"
    write_env(tmp_path, "x")
    assert RuntimeBuilder(tmp_path).build().gemini_provider.health().value == "UNCONFIGURED"


def test_health_serialization_never_exposes_loaded_key(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    write_env(tmp_path, FAKE_KEY)
    assert FAKE_KEY not in json.dumps(
        RuntimeBuilder(tmp_path).build().gemini_provider.health_details()
    )


def test_cli_and_runtime_builder_use_same_explicit_root(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    write_env(tmp_path, FAKE_KEY)
    monkeypatch.setattr(cli, "resolve_application_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["pangu", "model-health"])
    assert cli.main() == 0
    output = json.loads(capsys.readouterr().out)  # type: ignore[union-attr]
    assert output["gemini"]["configured"] is True  # type: ignore[index]
