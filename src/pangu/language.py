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

        direct_media = re.fullmatch(r"play\s+(https?://\S+)", clean, re.IGNORECASE)
        if direct_media:
            return NormalizedIntent(
                "play_media",
                clean,
                clean,
                {"query": direct_media.group(1), "url": direct_media.group(1), "source": "direct"},
                0.99,
            )

        latest_channel_youtube = re.fullmatch(
            r"play\s+(?:the\s+)?latest\s+(?:video|upload)\s+(?:from|of|by)\s+(.+?)\s+on\s+youtube",
            clean,
            re.IGNORECASE,
        )
        if latest_channel_youtube:
            channel = latest_channel_youtube.group(1).strip()
            return NormalizedIntent(
                "play_media",
                clean,
                clean,
                {"query": f"latest video from {channel}", "source": "youtube"},
                0.995,
            )

        latest_channel = re.fullmatch(
            r"play\s+(?:the\s+)?latest\s+(?:video|upload)\s+(?:from|of|by)\s+(.+?)(?:\s+channel)?",
            clean,
            re.IGNORECASE,
        )
        if latest_channel:
            channel = latest_channel.group(1).strip()
            return NormalizedIntent(
                "play_media",
                clean,
                clean,
                {"query": f"latest video from {channel}", "source": "youtube"},
                0.995,
            )

        provider_media = re.fullmatch(
            r"play\s+(.+?)\s+(?:on|from)\s+(youtube|yt|vimeo|dailymotion|daily motion|archive(?:\.org)?)",
            clean,
            re.IGNORECASE,
        )
        if provider_media:
            return NormalizedIntent(
                "play_media",
                clean,
                clean,
                {"query": provider_media.group(1), "source": provider_media.group(2)},
                0.98,
            )

        specific_channel_youtube = re.fullmatch(
            r"play\s+(.+?)\s+(?:video\s+)?(?:from|of|by)\s+(.+?)(?:\s+channel)?\s+on\s+youtube",
            clean,
            re.IGNORECASE,
        )
        if specific_channel_youtube:
            video = specific_channel_youtube.group(1).strip()
            channel = specific_channel_youtube.group(2).strip()
            return NormalizedIntent(
                "play_media",
                clean,
                clean,
                {"query": f"{video} from {channel}", "source": "youtube"},
                0.99,
            )

        specific_channel = re.fullmatch(
            r"play\s+(.+?)\s+(?:video\s+)?(?:from|of|by)\s+(.+?)(?:\s+channel)?",
            clean,
            re.IGNORECASE,
        )
        if specific_channel:
            video = specific_channel.group(1).strip()
            channel = specific_channel.group(2).strip()
            if channel.casefold() not in {
                "youtube",
                "yt",
                "vimeo",
                "dailymotion",
                "daily motion",
                "archive",
                "archive.org",
            }:
                return NormalizedIntent(
                    "play_media",
                    clean,
                    clean,
                    {"query": f"{video} from {channel}", "source": "youtube"},
                    0.99,
                )

        tanglish_latest = re.fullmatch(
            r"(.+?)(?:\s+channel)?(?:\s+oda|\s+la)?\s+latest\s+(?:video|upload)\s+play\s+pannu",
            lower,
        )
        if tanglish_latest:
            channel = tanglish_latest.group(1).strip()
            return NormalizedIntent(
                "play_media",
                clean,
                clean,
                {"query": f"latest video from {channel}", "source": "youtube"},
                0.99,
                "ta-en",
            )

        tanglish_specific = re.fullmatch(
            r"(.+?)(?:\s+channel)?(?:\s+oda|\s+la)\s+(.+?)\s+play\s+pannu",
            lower,
        )
        if tanglish_specific:
            channel = tanglish_specific.group(1).strip()
            video = tanglish_specific.group(2).strip()
            return NormalizedIntent(
                "play_media",
                clean,
                clean,
                {"query": f"{video} from {channel}", "source": "youtube"},
                0.99,
                "ta-en",
            )

        tanglish_media = re.fullmatch(
            r"(?:youtube|yt|vimeo|dailymotion|archive)(?:\s+la)?\s+(.+?)\s+play\s+pannu",
            lower,
        )
        if tanglish_media:
            provider = lower.split()[0]
            return NormalizedIntent(
                "play_media",
                clean,
                clean,
                {"query": tanglish_media.group(1), "source": provider},
                0.98,
                "ta-en",
            )
        generic_media = re.fullmatch(r"play\s+(.+)", clean, re.IGNORECASE)
        if generic_media:
            return NormalizedIntent(
                "play_media",
                clean,
                clean,
                {"query": generic_media.group(1), "source": "auto"},
                0.92,
            )

        if lower in {"what's on screen", "what is on screen", "describe screen", "read screen"}:
            return NormalizedIntent("screen_snapshot", "Describe the active screen", clean, confidence=0.98)
        desktop = re.fullmatch(r"(?:click|press|invoke)\s+(?:the\s+)?(.+?)(?:\s+(?:button|control))?", lower)
        if desktop:
            return NormalizedIntent(
                "invoke_control", clean, clean, {"target": desktop.group(1)}, 0.9
            )
        desktop = re.fullmatch(r"(?:focus)\s+(?:the\s+)?(.+?)(?:\s+control)?", lower)
        if desktop:
            return NormalizedIntent(
                "focus_control", clean, clean, {"target": desktop.group(1)}, 0.9
            )
        desktop = re.fullmatch(r"type\s+(.+?)\s+(?:in|into)\s+(?:the\s+)?(.+)", clean, re.IGNORECASE)
        if desktop:
            return NormalizedIntent(
                "set_control_text",
                clean,
                clean,
                {"text": desktop.group(1), "target": desktop.group(2)},
                0.9,
            )

        browse = re.fullmatch(r"(?:browse|navigate|go)\s+(?:to\s+)?(https?://\S+)", clean, re.IGNORECASE)
        if browse:
            return NormalizedIntent("browser_navigate", clean, clean, {"url": browse.group(1)}, 0.97)
        if lower in {"read browser", "read browser page", "what's on this webpage", "what is on this webpage"}:
            return NormalizedIntent("browser_read", "Read the current browser page", clean, confidence=0.97)
        web_click = re.fullmatch(r"click\s+browser\s+(button|link)\s+(.+)", lower)
        if web_click:
            return NormalizedIntent(
                "browser_click",
                clean,
                clean,
                {"role": web_click.group(1), "target": web_click.group(2)},
                0.96,
            )
        web_fill = re.fullmatch(r"fill\s+browser\s+(?:field|textbox)\s+(.+?)\s+with\s+(.+)", clean, re.IGNORECASE)
        if web_fill:
            return NormalizedIntent(
                "browser_fill",
                clean,
                clean,
                {"role": "textbox", "target": web_fill.group(1), "text": web_fill.group(2)},
                0.96,
            )
        if lower in {"browser back", "go back in browser"}:
            return NormalizedIntent("browser_back", clean, clean, confidence=0.97)
        if lower in {"browser forward", "go forward in browser"}:
            return NormalizedIntent("browser_forward", clean, clean, confidence=0.97)

        memory = re.fullmatch(r"remember(?: that)?\s+(.+)", lower)
        if memory:
            return NormalizedIntent(
                "remember",
                f"Remember {memory.group(1)}",
                clean,
                {"memory": clean[clean.lower().find(memory.group(1)) :]},
                0.97,
            )
        recall = re.fullmatch(r"(?:what do you remember about|recall|remember anything about)\s+(.+)", lower)
        if recall:
            return NormalizedIntent(
                "recall_memory", clean, clean, {"query": recall.group(1)}, 0.96
            )

        app = re.fullmatch(
            r"(focus|minimize|maximize|restore|close|restart)\s+(.+?)(?:\s+(?:app|application))?",
            lower,
        )
        if app:
            return NormalizedIntent(
                f"{app.group(1)}_application",
                clean,
                clean,
                {"application": clean[len(app.group(1)) + 1 :]},
                0.95,
            )
        tanglish_app = re.fullmatch(r"(.+?)\s+ah\s+(focus|minimize|maximize|restore|close)\s+pannu", lower)
        if tanglish_app:
            return NormalizedIntent(
                f"{tanglish_app.group(2)}_application",
                clean,
                clean,
                {"application": tanglish_app.group(1)},
                0.96,
                "ta-en",
            )

        system = re.fullmatch(r"(?:set )?volume\s+(\d{1,3})(?:\s+(?:ku )?set(?: pannu)?)?", lower)
        if system:
            return NormalizedIntent(
                "set_volume",
                f"Set volume to {system.group(1)}",
                clean,
                {"value": system.group(1)},
                0.97,
                "ta-en" if "pannu" in lower or "ku" in lower else "en",
            )
        if lower in {"volume", "volume level", "what is the volume"}:
            return NormalizedIntent("get_volume", "Read system volume", clean, confidence=0.96)
        system = re.fullmatch(
            r"(?:increase|raise|decrease|lower)\s+volume(?:\s+(?:by )?(\d{1,3}))?", lower
        )
        if system:
            name = (
                "decrease_volume"
                if system.group(0).startswith(("decrease", "lower"))
                else "increase_volume"
            )
            return NormalizedIntent(
                name, name.replace("_", " "), clean, {"step": system.group(1) or "5"}, 0.96
            )
        system = re.fullmatch(
            r"(?:set )?brightness\s+(\d{1,3})(?:\s+(?:ku )?set(?: pannu)?)?", lower
        )
        if system:
            return NormalizedIntent(
                "set_brightness",
                f"Set brightness to {system.group(1)}",
                clean,
                {"value": system.group(1)},
                0.97,
                "ta-en" if "pannu" in lower or "ku" in lower else "en",
            )
        if lower in {"brightness", "brightness level", "what is the brightness"}:
            return NormalizedIntent("get_brightness", "Read display brightness", clean, confidence=0.96)
        system = re.fullmatch(
            r"(?:increase|raise|decrease|lower)\s+brightness(?:\s+(?:by )?(\d{1,3}))?", lower
        )
        if system:
            name = (
                "decrease_brightness"
                if system.group(0).startswith(("decrease", "lower"))
                else "increase_brightness"
            )
            return NormalizedIntent(
                name, name.replace("_", " "), clean, {"step": system.group(1) or "5"}, 0.96
            )
        if re.fullmatch(r"(?:mute|mute pannu)", lower):
            return NormalizedIntent(
                "mute",
                "Mute system audio",
                clean,
                confidence=0.97,
                detected_language="ta-en" if "pannu" in lower else "en",
            )
        if lower in {"unmute", "toggle mute"}:
            return NormalizedIntent(lower.replace(" ", "_"), lower.title(), clean, confidence=0.97)
        if lower in {"mute state", "are you muted", "is volume muted"}:
            return NormalizedIntent("get_mute_state", "Read mute state", clean, confidence=0.96)

        for pattern, name, english in self._rules:
            match = re.fullmatch(pattern, lower)
            if match:
                entities = {"name": match.group(1)} if name == "create_folder" else {}
                if name == "open_application":
                    application = "Google Chrome" if "chrome" in lower else "Visual Studio Code"
                    entities = {"application": application}
                return NormalizedIntent(
                    name,
                    english if not entities or name == "open_application" else f"Create folder {match.group(1)}",
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
