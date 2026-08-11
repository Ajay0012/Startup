from __future__ import annotations

from pathlib import Path

from .approvals import PersistentApprovalService
from .permissions import PermissionGrant, PermissionStore
from .runtime import Runtime
from .tools import ToolRuntime


class HardenedRuntime(Runtime):
    """Production Runtime with one DB-backed exact approval authority.

    The historical Runtime constructor still creates an in-memory compatibility ApprovalStore.
    Production composition immediately replaces that object and its ToolRuntime before startup,
    so no production tool execution or approval issuance can flow through the legacy store.
    This keeps import/test compatibility while the old constructor surface is retired safely.
    """

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
