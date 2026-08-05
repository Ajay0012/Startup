from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pangu.approvals import ApprovalBinding, ApprovalDenial, PersistentApprovalService
from pangu.database import DatabaseService
from pangu.repositories import ApprovalRepository


def _binding() -> ApprovalBinding:
    return ApprovalBinding(
        actor="test",
        tool_id="filesystem",
        tool_version="1",
        operation="delete",
        arguments={"paths": ["a"]},
        target="E:/safe/a",
        risk_level="HIGH_RISK",
        permission_scopes=frozenset({"filesystem.delete:*"}),
        mission_id=None,
        session_id="s",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        approval_mode="one_time",
    )


def test_one_time_approval_has_exactly_one_concurrent_consumer(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    database = DatabaseService(path, timeout_seconds=10)
    database.start()
    try:
        service = PersistentApprovalService(database)
        binding = _binding()
        approval_id = service.issue(binding)
        with ThreadPoolExecutor(max_workers=5) as pool:
            outcomes = list(
                pool.map(
                    lambda _: PersistentApprovalService(database).consume(approval_id, binding),
                    range(5),
                )
            )
        assert outcomes.count(None) == 1
        assert outcomes.count(ApprovalDenial.CONSUMED) == 4
        with database.transaction() as session:
            assert len(ApprovalRepository(session).consumptions(approval_id)) == 1
    finally:
        database.stop()
    database = DatabaseService(path)
    database.start()
    try:
        assert (
            PersistentApprovalService(database).consume(approval_id, binding)
            == ApprovalDenial.CONSUMED
        )
    finally:
        database.stop()
