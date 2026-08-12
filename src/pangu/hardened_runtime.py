from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from .approvals import PersistentApprovalService
from .contracts import CommandEnvelope, Status, ToolResult
from .model_runtime import ModelRequest, ModelRole
from .permissions import PermissionGrant, PermissionStore
from .runtime import Runtime
from .streaming_model import StreamingGeminiProvider
from .tools import ToolRuntime


class HardenedRuntime(Runtime):
    """Production Runtime with one DB-backed exact approval authority."""

    def __init__(
        self,
        *args: object,
        persistent_approvals: PersistentApprovalService,
        root: Path,
        **kwargs: object,
    ) -> None:
        super().__init__(root, *args, **kwargs)  # type: ignore[arg-type]
        grants = PermissionStore((PermissionGrant("filesystem.write:*", "default"),))
        self.approvals = persistent_approvals  # type: ignore[assignment]
        self.tools = ToolRuntime(
            root,
            self.safety,
            self.catalog,
            grants,
            persistent_approvals,
        )

    @property
    def approval_authority(self) -> PersistentApprovalService:
        authority = self.approvals
        if not isinstance(authority, PersistentApprovalService):
            raise RuntimeError("production runtime approval authority is not persistent")
        return authority

    async def stream_command(self, text: str, source: str = "voice") -> AsyncIterator[str]:
        """Stream informational Gemini replies; deterministic actions still execute once.

        Streaming does not change tool authority. Non-informational commands use the normal
        audited command path and yield one verified result. Informational responses preserve
        the same sanitizer, model budget, circuit breaker, audit record and episodic memory.
        """
        if not self.started:
            raise RuntimeError("runtime not started")
        intent = self.language.normalize(text)
        if intent.intent_name != "informational":
            result = await asyncio.to_thread(self.command, text, source)
            yield result.message
            return

        command = CommandEnvelope(text, source)
        grounding = self._grounding(text)
        context = self.context.assemble(text, grounding)
        route = self.model_router.route(text, deterministic_available=False)
        role = route.role or ModelRole.FAST
        prompt = (
            "You are PANGU, a concise personal Windows assistant. Answer the owner's question naturally. "
            "Use supplied local context only when relevant. Never claim a computer action happened unless a "
            "tool result verified it. Do not expose secrets.\n\n"
            f"Owner: {text}\n"
            f"Local context: {json.dumps(context, ensure_ascii=False, default=str)}"
        )
        provider = self.model_router.gemini
        if not isinstance(provider, StreamingGeminiProvider):
            result = await asyncio.to_thread(self._informational, command, text)
            self.db.record(command, result)
            self._remember_turn(command, intent.intent_name, result)
            yield result.message
            return

        chunks: list[str] = []
        try:
            async for chunk in provider.stream_async(
                ModelRequest(
                    prompt, role=role, trace_id=command.trace_id, mission_id="conversation"
                )
            ):
                chunks.append(chunk)
                yield chunk
        except (RuntimeError, PermissionError, ValueError):
            if chunks:
                message = "".join(chunks).strip()
                result = ToolResult(
                    command.command_id,
                    Status.UNVERIFIED,
                    message or "The streamed response ended unexpectedly.",
                    {"provider": "gemini", "streamed": True},
                    {"error": "STREAM_INTERRUPTED"},
                )
            else:
                result = ToolResult(
                    command.command_id,
                    Status.UNVERIFIED,
                    "Gemini reasoning is unavailable right now.",
                    {"provider": "gemini", "streamed": True},
                    {"error": "STREAM_UNAVAILABLE"},
                )
                yield result.message
        else:
            message = "".join(chunks).strip()
            result = ToolResult(
                command.command_id,
                Status.EXECUTED,
                message,
                {"provider": "gemini", "streamed": True},
                {"health": provider.health().value},
            )
        self.db.record(command, result)
        self._remember_turn(command, intent.intent_name, result)
