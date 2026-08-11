from __future__ import annotations

from pathlib import Path

from pangu.readiness import PanguReadinessInspector, ReadinessState
from pangu.settings import PanguSettings


def test_readiness_reports_missing_required_models_and_api(tmp_path: Path) -> None:
    settings = PanguSettings(
        gemini_api_key=None,
        pangu_media_enabled=False,
        pangu_browser_enabled=False,
        pangu_screen_observation_ocr_enabled=False,
    )
    report = PanguReadinessInspector(tmp_path, settings).inspect()
    by_name = {item.name: item for item in report.checks}
    assert by_name["gemini_api"].state == ReadinessState.MISSING
    assert by_name["whisper_model"].state == ReadinessState.MISSING
    assert by_name["wake_model"].state == ReadinessState.MISSING
    assert report.ready is False


def test_phone_enabled_without_secret_is_blocked(tmp_path: Path) -> None:
    (tmp_path / "models" / "voice" / "whisper").mkdir(parents=True)
    (tmp_path / "models" / "voice" / "wake" / "sherpa-kws").mkdir(parents=True)
    settings = PanguSettings(
        gemini_api_key="x" * 40,
        pangu_phone_enabled=True,
        pangu_phone_pairing_secret=None,
        pangu_media_enabled=False,
        pangu_browser_enabled=False,
        pangu_screen_observation_ocr_enabled=False,
    )
    report = PanguReadinessInspector(tmp_path, settings).inspect()
    by_name = {item.name: item for item in report.checks}
    assert by_name["phone_pairing_secret"].state == ReadinessState.BLOCKED
    assert report.ready is False
