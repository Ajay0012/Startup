from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from .contracts import Risk, ToolRequest


def canonical_operation(request: ToolRequest) -> str:
    return json.dumps(
        {
            "tool": request.tool_id,
            "operation": request.operation,
            "arguments": request.arguments,
            "actor": request.actor,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def operation_hash(request: ToolRequest) -> str:
    return hashlib.sha256(canonical_operation(request).encode()).hexdigest()


class ApprovalStore:
    def __init__(self) -> None:
        self._items: dict[str, tuple[str, datetime, bool]] = {}

    def issue(self, request: ToolRequest, seconds: int = 300) -> str:
        token = operation_hash(request)
        self._items[token] = (token, datetime.now(UTC) + timedelta(seconds=seconds), False)
        return token

    def consume(self, request: ToolRequest, token: str) -> bool:
        item = self._items.get(token)
        if not item or item[0] != operation_hash(request) or item[1] < datetime.now(UTC) or item[2]:
            return False
        self._items[token] = (item[0], item[1], True)
        return True


class SafetyGateway:
    prohibited: ClassVar[set[str]] = {"disable_antivirus", "extract_credentials", "shell"}

    def classify(self, request: ToolRequest) -> Risk:
        if request.operation in self.prohibited:
            return Risk.PROHIBITED
        if request.operation in {"delete", "shutdown", "restart", "stop_process"}:
            return Risk.HIGH
        if request.operation in {"write", "create_folder", "open_application"}:
            return Risk.LOW
        return Risk.READ_ONLY

    def allowed_path(self, path: str, root: Path) -> bool:
        try:
            Path(path).resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False
