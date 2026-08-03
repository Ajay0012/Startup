from __future__ import annotations

from pathlib import Path

from .config import Settings
from .contracts import CommandEnvelope, Status, ToolRequest, ToolResult
from .language import LanguageRuntime
from .persistence import Database
from .security import SafetyGateway
from .tools import ToolRuntime


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.runtime_root / "database" / "pangu.db")
        self.language = LanguageRuntime()
        self.safety = SafetyGateway()
        self.tools = ToolRuntime(settings.root, self.safety)
        self.started = False

    def start(self) -> None:
        self.db.start()
        self.started = True

    def stop(self) -> None:
        self.db.close()
        self.started = False

    def command(self, text: str, source: str = "cli") -> ToolResult:
        if not self.started:
            raise RuntimeError("runtime not started")
        command = CommandEnvelope(text, source)
        intent = self.language.normalize(text)
        if intent.intent_name == "create_folder":
            request = ToolRequest("filesystem", "create_folder", {"name": intent.entities["name"]})
        elif intent.intent_name == "battery_status":
            request = ToolRequest("system", "battery_status", {})
        else:
            result = ToolResult(
                command.command_id,
                Status.UNVERIFIED,
                "No deterministic action selected; Gemini reasoning is unavailable."
                if not self.settings.gemini_key_present
                else "No local deterministic action selected.",
            )
            self.db.record(command, result)
            return result
        result = self.tools.execute(request)
        self.db.record(command, result)
        return result


def build_runtime(root: Path) -> Runtime:
    return Runtime(Settings.load(root))
