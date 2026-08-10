from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def resolve_application_root(start: Path | None = None) -> Path:
    """Find the editable repository root without relying on the current directory."""
    location = (start or Path(__file__)).resolve()
    current = location if location.is_dir() else location.parent
    for candidate in (current, *current.parents):
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / "src").is_dir()
            and (candidate / ".env").exists()
        ):
            return candidate
    return current


def _valid_api_key(value: str | SecretStr | None) -> str | None:
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    if raw is None:
        return None
    candidate = raw.strip()
    placeholders = {"changeme", "example", "placeholder", "your_api_key", "none", "null"}
    if len(candidate) < 20 or candidate.lower() in placeholders:
        return None
    return candidate


class PanguSettings(BaseSettings):
    """Root-scoped settings; secrets are intentionally excluded from repr."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    gemini_api_key: SecretStr | None = None
    pangu_ai_provider: str = "gemini"
    gemini_primary_model: str = "gemini-3.6-flash"
    gemini_fast_model: str = "gemini-3.5-flash-lite"
    gemini_coding_model: str = "gemini-3.5-flash"
    gemini_vision_model: str = "gemini-3.6-flash"
    gemini_timeout_seconds: float = 45.0
    gemini_max_retries: int = 2
    gemini_max_concurrent_requests: int = 3
    gemini_max_model_calls_per_mission: int = 12
    gemini_max_input_tokens_per_mission: int = 120000
    gemini_max_output_tokens_per_mission: int = 24000
    pangu_cloud_reasoning_enabled: bool = True
    pangu_allow_screenshot_upload: bool = False
    pangu_allow_document_upload: bool = False
    pangu_redact_sensitive_data: bool = True
    pangu_wake_cooldown_seconds: float = 2.0
    pangu_awareness_enabled: bool = True
    pangu_awareness_interval_seconds: float = 5.0
    pangu_media_enabled: bool = True
    pangu_browser_enabled: bool = False
    pangu_browser_headless: bool = False
    pangu_computer_use_enabled: bool = False
    pangu_gestures_enabled: bool = False
    pangu_gesture_camera_index: int = 0
    pangu_gesture_model_path: str = "models/vision/hand_landmarker.task"

    @field_validator("gemini_api_key", mode="before")
    @classmethod
    def configured_key(cls, value: str | SecretStr | None) -> str | None:
        return _valid_api_key(value)

    @field_validator("pangu_ai_provider")
    @classmethod
    def supported_provider(cls, value: str) -> str:
        if value not in {"gemini", "deterministic"}:
            raise ValueError("unsupported provider")
        return value

    @field_validator("gemini_timeout_seconds")
    @classmethod
    def timeout_bounds(cls, value: float) -> float:
        if not 1 <= value <= 120:
            raise ValueError("timeout must be between 1 and 120 seconds")
        return value

    @field_validator("gemini_max_retries")
    @classmethod
    def retry_bounds(cls, value: int) -> int:
        if not 0 <= value <= 5:
            raise ValueError("retries must be between 0 and 5")
        return value

    @field_validator("pangu_wake_cooldown_seconds")
    @classmethod
    def wake_cooldown_bounds(cls, value: float) -> float:
        if not 0.5 <= value <= 15:
            raise ValueError("wake cooldown must be between 0.5 and 15 seconds")
        return value

    @field_validator("pangu_awareness_interval_seconds")
    @classmethod
    def awareness_interval_bounds(cls, value: float) -> float:
        if not 1 <= value <= 300:
            raise ValueError("awareness interval must be between 1 and 300 seconds")
        return value

    @field_validator("pangu_gesture_camera_index")
    @classmethod
    def camera_index_bounds(cls, value: int) -> int:
        if not 0 <= value <= 32:
            raise ValueError("gesture camera index must be between 0 and 32")
        return value

    @classmethod
    def load_root(cls, root: Path) -> PanguSettings:
        """Load root .env values, overridden by explicitly-set process variables."""
        values: dict[str, Any] = {}
        env_path = root.resolve() / ".env"
        field_names = {name.upper(): name for name in cls.model_fields}
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, env_value = line.split("=", 1)
                    field_name = field_names.get(key.strip())
                    if field_name is not None:
                        values[field_name] = env_value.strip()
        for name in cls.model_fields:
            process_value = os.environ.get(name.upper())
            if process_value is not None:
                if name == "gemini_api_key" and _valid_api_key(process_value) is None:
                    continue
                values[name] = process_value
        return cls(**values)
