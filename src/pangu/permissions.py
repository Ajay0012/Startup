from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class PermissionGrant:
    scope: str
    subject: str
    expires_at: datetime | None = None


class PermissionStore:
    def __init__(self, grants: tuple[PermissionGrant, ...] = ()) -> None:
        self._grants = list(grants)

    def allows(self, subject: str, required: str) -> bool:
        now = datetime.now(UTC)
        for grant in self._grants:
            if grant.subject != subject or (grant.expires_at and grant.expires_at < now):
                continue
            if grant.scope == required or (grant.scope.endswith(":*") and required.startswith(grant.scope[:-1])):
                return True
        return False
