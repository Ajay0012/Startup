from __future__ import annotations

from pathlib import Path

from .approvals import PersistentApprovalService
from .capabilities import CapabilityCatalog, ToolSpecification
from .contracts import Risk, Status, ToolRequest, ToolResult
from .filesystem import FilesystemAdapter
from .permissions import PermissionStore
from .security import SafetyGateway


class ToolRuntime:
    def __init__(
        self,
        allowed_root: Path,
        safety: SafetyGateway,
        catalog: CapabilityCatalog,
        permissions: PermissionStore,
        approvals: PersistentApprovalService,
    ) -> None:
        self.root = allowed_root.resolve()
        self.safety = safety
        self.catalog = catalog
        self.permissions = permissions
        self.approvals = approvals
        self.filesystem = FilesystemAdapter(self.root)

    def _approval_target(self, request: ToolRequest) -> str:
        candidate = request.arguments.get("path") or request.arguments.get("name")
        if candidate is not None:
            try:
                return str(self.filesystem.resolve(str(candidate)))
            except PermissionError:
                return str(self.root / "runtime-data" / "approval-targets" / "rejected-path")
        safe_tool = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in request.tool_id)
        safe_operation = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in request.operation
        )
        return str(
            self.root
            / "runtime-data"
            / "approval-targets"
            / f"{safe_tool}-{safe_operation}"
        )

    def issue_approval(self, request: ToolRequest, seconds: int = 300) -> str:
        specification = self.catalog.resolve(request.tool_id, request.operation)
        if specification.risk not in {Risk.HIGH, Risk.PRIVILEGED}:
            raise ValueError("explicit approval is only issued for high or privileged operations")
        return self.approvals.issue_tool_request(
            request,
            specification,
            self._approval_target(request),
            seconds=seconds,
        )

    def _approved(
        self,
        request: ToolRequest,
        specification: ToolSpecification,
        approval: str | None,
    ) -> bool:
        if specification.risk not in {Risk.HIGH, Risk.PRIVILEGED}:
            return True
        if not approval:
            return False
        denial = self.approvals.consume_tool_request(
            approval,
            request,
            specification,
            self._approval_target(request),
        )
        return denial is None

    def execute(self, request: ToolRequest, approval: str | None = None) -> ToolResult:
        try:
            specification = self.catalog.resolve(request.tool_id, request.operation)
        except LookupError:
            return ToolResult(request.request_id, Status.DENIED, "Unknown capability.")
        if specification.risk == Risk.PROHIBITED:
            return ToolResult(request.request_id, Status.DENIED, "Operation is prohibited.")
        if any(
            not self.permissions.allows(request.actor, scope)
            for scope in specification.permission_scopes
        ):
            return ToolResult(
                request.request_id, Status.DENIED, "Required permission scope is not granted."
            )
        if not self._approved(request, specification, approval):
            return ToolResult(request.request_id, Status.DENIED, "Exact approval is required.")
        try:
            if request.operation == "create_folder":
                target, verified = self.filesystem.create_folder(str(request.arguments["name"]))
                return ToolResult(
                    request.request_id,
                    Status.VERIFIED if verified else Status.FAILED,
                    "Folder created and verified." if verified else "Folder verification failed.",
                    {"path": str(target)},
                    {"exists": verified},
                )
            if request.operation == "write_text":
                target, digest = self.filesystem.write_text(
                    str(request.arguments["path"]),
                    str(request.arguments["content"]),
                    bool(request.arguments.get("overwrite", False)),
                )
                return ToolResult(
                    request.request_id,
                    Status.VERIFIED,
                    "File written and content verified.",
                    {"path": str(target)},
                    {"sha256": digest},
                )
            if request.operation == "battery_status":
                return ToolResult(
                    request.request_id,
                    Status.UNVERIFIED,
                    "Battery adapter is unavailable in this process.",
                )
        except (PermissionError, OSError) as error:
            return ToolResult(request.request_id, Status.FAILED, f"Tool failed safely: {error}")
        return ToolResult(
            request.request_id,
            Status.DENIED,
            "No registered deterministic tool supports this operation.",
        )
