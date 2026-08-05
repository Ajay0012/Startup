from __future__ import annotations

import re

from .contracts import NormalizedIntent


class LanguageRuntime:
    _rules = (
        (r"chrome\s+ah\s+open\s+pannu", "open_application", "Open Google Chrome"),
        (r"volume\s+konjam\s+kammi\s+pannu", "volume_down", "Reduce the system volume."),
        (r"battery\s+evlo\s+iruku", "battery_status", "Show the battery percentage."),
        (r"vs\s+code\s+open\s+pannu", "open_application", "Open Visual Studio Code."),
        (r"pangu\s+hide\s+aagu", "hide_overlay", "Hide the PANGU overlay."),
        (r"indha\s+folder\s+create\s+pannu", "create_folder", "Create this folder."),
        (r"create\s+(?:a\s+)?folder\s+(.+)", "create_folder", "Create folder"),
    )

    def normalize(self, text: str) -> NormalizedIntent:
        clean = " ".join(text.strip().split())
        lower = clean.lower()
        for pattern, name, english in self._rules:
            match = re.fullmatch(pattern, lower)
            if match:
                entities = {"name": match.group(1)} if name == "create_folder" else {}
                return NormalizedIntent(
                    name,
                    english if not entities else f"Create folder {match.group(1)}",
                    clean,
                    entities,
                    0.98,
                    "ta-en" if "pannu" in lower else "en",
                )
        if lower.startswith("open "):
            return NormalizedIntent(
                "open_application", clean, clean, {"application": clean[5:]}, 0.9
            )
        if lower in {"battery", "battery status"}:
            return NormalizedIntent("battery_status", "Read battery status", clean, confidence=0.95)
        if lower in {"mute volume", "mute the volume"}:
            return NormalizedIntent(
                "mute_volume", "Mute the system volume.", clean, confidence=0.95
            )
        if lower.startswith("delete "):
            return NormalizedIntent("delete", clean, clean, confidence=0.9)
        if lower.startswith("rename "):
            return NormalizedIntent("rename", clean, clean, confidence=0.8)
        return NormalizedIntent("informational", clean, clean, confidence=0.3)
