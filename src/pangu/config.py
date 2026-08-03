from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _read_root_env(root: Path) -> dict[str, str]:
    path = root / ".env"
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.lstrip().startswith("#"):
                key, value = raw.split("=", 1)
                values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class Settings:
    root: Path
    runtime_root: Path
    gemini_key_present: bool
    provider: str
    cloud_reasoning: bool

    @classmethod
    def load(cls, root: Path) -> Settings:
        env = _read_root_env(root)

        def get(key: str, default: str = "") -> str:
            return os.environ.get(key) or env.get(key) or default

        runtime = Path(
            get("PANGU_RUNTIME_ROOT") or (os.environ.get("LOCALAPPDATA") or str(root)) + "\\PanguAI"
        )
        return cls(
            root,
            runtime,
            bool(get("GEMINI_API_KEY")),
            get("PANGU_AI_PROVIDER", "gemini"),
            get("PANGU_CLOUD_REASONING_ENABLED", "true").lower() == "true",
        )
