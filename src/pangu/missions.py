from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import select

from .database import DatabaseService, MissionCheckpointRow, MissionRow, MissionTaskRow
from .events import EventBus, EventEnvelope, EventPriority


class MissionState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class MissionTaskState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class MissionTaskSpec:
    key: str
    title: str
    operation: str
    arguments: dict[str, object]
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class MissionTask:
    task_id: str
    mission_id: str
    state: MissionTaskState
    title: str
    operation: str
    arguments: dict[str, object]
    dependencies: tuple[str, ...]
    attempts: int
    ordinal: int
    result: dict[str, object] | None = None
    error: str | None = None


@dataclass(frozen=True)
class MissionSnapshot:
    mission_id: str
    goal: str
    state: MissionState
    priority: int
    resumable: bool
    tasks: tuple[MissionTask, ...]


@dataclass(frozen=True)
class MissionTaskResult:
    success: bool
    result: dict[str, object]
    error: str | None = None
    retryable: bool = False


MissionExecutor = Callable[[MissionTask], Awaitable[MissionTaskResult]]


class PersistentMissionRuntime:
    """Bounded, resumable DAG mission engine backed by PANGU's existing mission tables."""

    def __init__(self, database: DatabaseService, events: EventBus, max_steps: int = 32) -> None:
        if not 1 <= max_steps <= 128:
            raise ValueError("max_steps must be between 1 and 128")
        self.database = database
        self.events = events
        self.max_steps = max_steps

    @staticmethod
    def _task(row: MissionTaskRow) -> MissionTask:
        return MissionTask(
            row.id,
            row.mission_id,
            MissionTaskState(row.state),
            row.title or row.operation or "task",
            row.operation or "unknown",
            dict(row.arguments or {}),
            tuple(row.dependencies or []),
            int(row.attempts),
            int(row.ordinal),
            dict(row.result) if row.result is not None else None,
            row.error,
        )

    def create(
        self,
        goal: str,
        tasks: tuple[MissionTaskSpec, ...],
        *,
        priority: int = 50,
        resumable: bool = True,
    ) -> MissionSnapshot:
        if not goal.strip() or not tasks:
            raise ValueError("mission requires a goal and at least one task")
        if not 0 <= priority <= 100:
            raise ValueError("priority must be between 0 and 100")
        keys = [item.key for item in tasks]
        if len(set(keys)) != len(keys) or any(not key.strip() for key in keys):
            raise ValueError("mission task keys must be unique and non-empty")
        known = set(keys)
        for item in tasks:
            if item.key in item.dependencies or not set(item.dependencies) <= known:
                raise ValueError("invalid mission dependency")
        # Simple DAG validation over symbolic task keys.
        dependencies = {item.key: set(item.dependencies) for item in tasks}
        resolved: set[str] = set()
        while len(resolved) < len(tasks):
            ready = {
                key
                for key, deps in dependencies.items()
                if key not in resolved and deps <= resolved
            }
            if not ready:
                raise ValueError("mission dependency graph contains a cycle")
            resolved.update(ready)

        mission_id = str(uuid4())
        ids = {item.key: str(uuid4()) for item in tasks}
        now = datetime.now(UTC)
        with self.database.transaction() as session:
            session.add(
                MissionRow(
                    id=mission_id,
                    state=MissionState.PENDING.value,
                    goal=goal.strip(),
                    priority=priority,
                    resumable=resumable,
                    created_at=now,
                    updated_at=now,
                )
            )
            # SQLite foreign-key enforcement is enabled. Flush the parent row before
            # adding task/checkpoint rows so SQLAlchemy cannot emit dependents first.
            session.flush()
            for ordinal, item in enumerate(tasks):
                session.add(
                    MissionTaskRow(
                        id=ids[item.key],
                        mission_id=mission_id,
                        state=MissionTaskState.PENDING.value,
                        title=item.title,
                        operation=item.operation,
                        arguments=dict(item.arguments),
                        dependencies=[ids[key] for key in item.dependencies],
                        result=None,
                        error=None,
                        attempts=0,
                        ordinal=ordinal,
                    )
                )
            session.add(
                MissionCheckpointRow(
                    id=str(uuid4()),
                    mission_id=mission_id,
                    payload={"kind": "created", "goal": goal.strip(), "task_count": len(tasks)},
                )
            )
        return self.snapshot(mission_id)

    def snapshot(self, mission_id: str) -> MissionSnapshot:
        with self.database.transaction() as session:
            mission = session.get(MissionRow, mission_id)
            if mission is None:
                raise LookupError("mission not found")
            rows = list(
                session.scalars(
                    select(MissionTaskRow)
                    .where(MissionTaskRow.mission_id == mission_id)
                    .order_by(MissionTaskRow.ordinal)
                ).all()
            )
            return MissionSnapshot(
                mission.id,
                mission.goal or "",
                MissionState(mission.state),
                int(mission.priority),
                bool(mission.resumable),
                tuple(self._task(row) for row in rows),
            )

    def _set_state(self, mission_id: str, state: MissionState, reason: str) -> None:
        now = datetime.now(UTC)
        with self.database.transaction() as session:
            mission = session.get(MissionRow, mission_id)
            if mission is None:
                raise LookupError("mission not found")
            mission.state = state.value
            mission.updated_at = now
            session.add(
                MissionCheckpointRow(
                    id=str(uuid4()),
                    mission_id=mission_id,
                    payload={
                        "kind": "state",
                        "state": state.value,
                        "reason": reason,
                        "at": now.isoformat(),
                    },
                )
            )

    def pause(self, mission_id: str, reason: str = "owner") -> None:
        self._set_state(mission_id, MissionState.PAUSED, reason)

    def cancel(self, mission_id: str, reason: str = "owner") -> None:
        self._set_state(mission_id, MissionState.CANCELLED, reason)

    async def run(self, mission_id: str, executor: MissionExecutor) -> MissionSnapshot:
        snapshot = self.snapshot(mission_id)
        if snapshot.state in {MissionState.COMPLETED, MissionState.CANCELLED}:
            return snapshot
        if snapshot.state == MissionState.PAUSED and not snapshot.resumable:
            raise RuntimeError("mission is not resumable")
        self._set_state(mission_id, MissionState.RUNNING, "execution_started")
        await self.events.publish(
            EventEnvelope("mission.started", {"mission_id": mission_id, "goal": snapshot.goal})
        )

        steps = 0
        while steps < self.max_steps:
            current = self.snapshot(mission_id)
            if current.state in {MissionState.PAUSED, MissionState.CANCELLED}:
                return current
            completed = {
                task.task_id for task in current.tasks if task.state == MissionTaskState.COMPLETED
            }
            pending = [task for task in current.tasks if task.state == MissionTaskState.PENDING]
            if not pending:
                final = (
                    MissionState.FAILED
                    if any(task.state == MissionTaskState.FAILED for task in current.tasks)
                    else MissionState.COMPLETED
                )
                self._set_state(mission_id, final, "terminal")
                await self.events.publish(
                    EventEnvelope(
                        "mission.completed"
                        if final == MissionState.COMPLETED
                        else "mission.failed",
                        {"mission_id": mission_id, "state": final.value},
                    )
                )
                return self.snapshot(mission_id)
            ready = [task for task in pending if set(task.dependencies) <= completed]
            if not ready:
                self._set_state(mission_id, MissionState.FAILED, "dependency_blocked")
                return self.snapshot(mission_id)

            task = ready[0]
            with self.database.transaction() as session:
                row = session.get(MissionTaskRow, task.task_id)
                assert row is not None
                row.state = MissionTaskState.RUNNING.value
                row.attempts += 1
            await self.events.publish(
                EventEnvelope(
                    "mission.task.started",
                    {
                        "mission_id": mission_id,
                        "task_id": task.task_id,
                        "operation": task.operation,
                    },
                    EventPriority.LOW,
                )
            )
            result = await executor(self.snapshot(mission_id).tasks[task.ordinal])
            with self.database.transaction() as session:
                row = session.get(MissionTaskRow, task.task_id)
                assert row is not None
                row.result = dict(result.result)
                row.error = result.error
                row.state = (
                    MissionTaskState.PENDING.value
                    if not result.success and result.retryable and row.attempts < 3
                    else MissionTaskState.COMPLETED.value
                    if result.success
                    else MissionTaskState.FAILED.value
                )
                session.add(
                    MissionCheckpointRow(
                        id=str(uuid4()),
                        mission_id=mission_id,
                        payload={
                            "kind": "task_result",
                            "task_id": task.task_id,
                            "success": result.success,
                            "retryable": result.retryable,
                            "attempt": row.attempts,
                        },
                    )
                )
            if not result.success and not result.retryable:
                self._set_state(mission_id, MissionState.FAILED, "task_failed")
                return self.snapshot(mission_id)
            steps += 1

        self._set_state(mission_id, MissionState.PAUSED, "iteration_budget_reached")
        return self.snapshot(mission_id)
