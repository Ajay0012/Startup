from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pangu.approvals import (
    ApprovalBinding,
    ApprovalDenial,
    PersistentApprovalService,
    canonical_json,
)
from pangu.database import DatabaseService
from pangu.repositories import ApprovalRepository


def binding(**changes: object) -> ApprovalBinding:
    values: dict[str, object] = {
        "actor": "ajay",
        "tool_id": "filesystem",
        "tool_version": "1",
        "operation": "delete",
        "arguments": {"paths": ["a", "b"]},
        "target": "E:/Work/Report.txt",
        "risk_level": "HIGH_RISK",
        "permission_scopes": frozenset({"filesystem.delete:*", "filesystem.read:*"}),
        "mission_id": "m1",
        "session_id": "s1",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "approval_mode": "one_time",
    }
    values.update(changes)
    return ApprovalBinding(**values)  # type: ignore[arg-type]


def test_canonical_json_orders_keys_and_sets_but_not_lists() -> None:
    assert canonical_json({"b": {"z", "a"}, "a": [2, 1]}) == '{"a":[2,1],"b":["a","z"]}'


def test_persistent_approval_is_exact_and_records_consumption(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "pangu.db")
    database.start()
    try:
        service = PersistentApprovalService(database)
        request = binding()
        approval_id = service.issue(request)
        assert service.consume(approval_id, request) is None
        assert service.consume(approval_id, request) == ApprovalDenial.CONSUMED
        with database.transaction() as session:
            assert len(ApprovalRepository(session).consumptions(approval_id)) == 1
    finally:
        database.stop()


def test_persistent_approval_rejects_changed_binding_and_session(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "pangu.db")
    database.start()
    try:
        service = PersistentApprovalService(database)
        approval_id = service.issue(binding())
        assert (
            service.consume(approval_id, binding(target="E:/Other.txt"))
            == ApprovalDenial.BINDING_MISMATCH
        )
        assert (
            service.consume(approval_id, binding(session_id="other"))
            == ApprovalDenial.SESSION_MISMATCH
        )
    finally:
        database.stop()


def test_persistent_approval_revocation_is_restart_safe(tmp_path: Path) -> None:
    path = tmp_path / "pangu.db"
    database = DatabaseService(path)
    database.start()
    approval_id = PersistentApprovalService(database).issue(binding())
    assert PersistentApprovalService(database).revoke(approval_id)
    database.stop()
    database = DatabaseService(path)
    database.start()
    try:
        service = PersistentApprovalService(database)
        assert service.consume(approval_id, binding()) == ApprovalDenial.REVOKED
        with database.transaction() as session:
            assert len(ApprovalRepository(session).revocations(approval_id)) == 1
    finally:
        database.stop()


def test_expired_and_reusable_approvals(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "pangu.db")
    database.start()
    try:
        service = PersistentApprovalService(database)
        expired = binding(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        assert service.consume(service.issue(expired), expired) == ApprovalDenial.EXPIRED
        reusable = binding(approval_mode="reusable")
        approval_id = service.issue(reusable)
        assert service.consume(approval_id, reusable) is None
        assert service.consume(approval_id, reusable) is None
    finally:
        database.stop()
