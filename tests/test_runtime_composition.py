from __future__ import annotations

import importlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from apps.backend.main import create_app
from pangu.cli import main
from pangu.model_runtime import CognitiveDecisionKind, ProviderHealth
from pangu.runtime_builder import RuntimeBuilder


def test_builder_injects_shared_decision_services(tmp_path: Path) -> None:
    container = RuntimeBuilder(tmp_path).build()
    runtime = container.runtime
    assert runtime.language is container.language
    assert runtime.context is container.context
    assert runtime.model_router is container.model_router
    assert runtime.cognitive_engine is container.cognitive_engine


def test_decisions_are_safe_without_gemini(tmp_path: Path) -> None:
    runtime = RuntimeBuilder(tmp_path).build().runtime
    assert runtime.decide("Mute volume").kind == CognitiveDecisionKind.DIRECT_TOOL
    assert (
        runtime.decide("Delete this important folder").kind
        == CognitiveDecisionKind.APPROVAL_REQUIRED
    )
    assert runtime.decide("Rename that file").kind == CognitiveDecisionKind.CLARIFICATION_REQUIRED
    assert (
        runtime.decide("research several competing battery technologies").kind
        == CognitiveDecisionKind.DEFERRED
    )


def test_api_uses_supplied_container_and_redacts(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    container = RuntimeBuilder(tmp_path).build()
    app = create_app(container)
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        normalized = client.post(
            "/api/v1/language/normalize", json={"text": "VS Code open pannu"}
        ).json()
        assert normalized["canonical_english"] == "Open Visual Studio Code."
        sanitized = client.post(
            "/api/v1/context/sanitize", json={"text": "Bearer secret-value"}
        ).json()
        assert "secret-value" not in sanitized["sanitized_content"]
    assert container.gemini_provider.health() == ProviderHealth.UNCONFIGURED


def test_importing_backend_does_not_build_runtime() -> None:
    module = importlib.import_module("apps.backend.main")
    assert not hasattr(module, "runtime")


def test_cli_normalize_and_decide(monkeypatch: object, capsys: object, tmp_path: Path) -> None:
    monkeypatch.setattr("sys.argv", ["pangu", "normalize", "Chrome ah open pannu"])
    monkeypatch.setattr("pathlib.Path.cwd", lambda: tmp_path)
    main()
    assert json.loads(capsys.readouterr().out)["canonical_english"] == "Open Google Chrome"
    monkeypatch.setattr("sys.argv", ["pangu", "decide", "Mute volume"])
    main()
    assert json.loads(capsys.readouterr().out)["kind"] == "DIRECT_TOOL"
