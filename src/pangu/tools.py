from __future__ import annotations

from pathlib import Path

from .contracts import Status, ToolRequest, ToolResult
from .security import SafetyGateway


class ToolRuntime:
    def __init__(self, allowed_root: Path, safety: SafetyGateway) -> None:
        self.root = allowed_root.resolve()
        self.safety = safety

    def execute(self, request: ToolRequest, approval: str | None = None) -> ToolResult:
        risk = self.safety.classify(request)
        if risk.value == "PROHIBITED":
            return ToolResult(request.request_id, Status.DENIED, "Operation is prohibited.")
        if request.operation == "create_folder":
            target = (self.root / str(request.arguments["name"])).resolve()
            if not self.safety.allowed_path(str(target), self.root):
                return ToolResult(
                    request.request_id, Status.DENIED, "Path escapes permitted workspace."
                )
            target.mkdir(parents=True, exist_ok=True)
            exists = target.is_dir()
            return ToolResult(
                request.request_id,
                Status.VERIFIED if exists else Status.FAILED,
                "Folder created and verified." if exists else "Folder verification failed.",
                {"path": str(target)},
                {"exists": exists},
            )
        if request.operation == "battery_status":
            return ToolResult(
                request.request_id,
                Status.UNVERIFIED,
                "Battery adapter is unavailable in this process.",
            )
        return ToolResult(
            request.request_id,
            Status.DENIED,
            "No registered deterministic tool supports this operation.",
        )
