from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @classmethod
    def load_root(cls, root: Path) -> PanguSettings:
        values: dict[str, str] = {}
        env_path = root / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    values[key] = value
        return cls(**dict[str, Any](values))
