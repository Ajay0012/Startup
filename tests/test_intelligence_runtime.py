from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pangu.awareness import ProactiveAwarenessRuntime, ProactivePolicy
from pangu.database import DatabaseService
from pangu.events import EventBus, EventEnvelope
from pangu.memory import MemoryKind, PersistentMemoryRuntime
from pangu.missions import (
    MissionState,
    MissionTaskResult,
    MissionTaskSpec,
    PersistentMissionRuntime,
)
from pangu.world_model import PersonalWorldModel


def test_layered_memory_persists_and_recalls(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "memory.db")
    database.start()
    try:
        memory = PersistentMemoryRuntime(database)
        stored = memory.remember(
            MemoryKind.SEMANTIC,
            "preferred editor",
            {"text": "VS Code"},
            importance=0.9,
            source="owner",
        )
        recalled = memory.recall("editor")
        assert recalled and recalled[0].memory_id == stored.memory_id
        assert recalled[0].content["text"] == "VS Code"

        memory.remember(
            MemoryKind.SEMANTIC,
            "preferred editor",
            {"text": "Visual Studio Code"},
            importance=0.9,
            source="owner",
        )
        assert memory.recall("editor")[0].content["text"] == "Visual Studio Code"
    finally:
        database.stop()


def test_world_model_detects_real_changes(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "world.db")
    database.start()
    try:
        world = PersonalWorldModel(database)
        first = world.observe("system", "power", "battery", source="system")
        same = world.observe("system", "power", "battery", source="system")
        changed = world.observe("system", "power", "ac", source="system")
        assert first.changed is True
        assert same.changed is False
        assert changed.changed is True
        assert changed.previous == "battery"
        assert world.get("system", "power").value == "ac"  # type: ignore[union-attr]
    finally:
        database.stop()


@pytest.mark.asyncio
async def test_mission_runtime_runs_dependency_dag_and_checkpoints(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "missions.db")
    database.start()
    events = EventBus()
    await events.start()
    try:
        missions = PersistentMissionRuntime(database, events)
        mission = missions.create(
            "prepare report",
            (
                MissionTaskSpec("collect", "Collect", "collect", {}),
                MissionTaskSpec("write", "Write", "write", {}, ("collect",)),
            ),
        )
        executed: list[str] = []

        async def executor(task):  # type: ignore[no-untyped-def]
            executed.append(task.operation)
            return MissionTaskResult(True, {"ok": True})

        final = await missions.run(mission.mission_id, executor)
        assert final.state == MissionState.COMPLETED
        assert executed == ["collect", "write"]
        assert all(task.result == {"ok": True} for task in final.tasks)
    finally:
        await events.stop()
        database.stop()


def test_mission_rejects_cycles(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "cycle.db")
    database.start()
    events = EventBus()
    try:
        missions = PersistentMissionRuntime(database, events)
        with pytest.raises(ValueError, match="cycle"):
            missions.create(
                "cycle",
                (
                    MissionTaskSpec("a", "A", "a", {}, ("b",)),
                    MissionTaskSpec("b", "B", "b", {}, ("a",)),
                ),
            )
    finally:
        database.stop()


@pytest.mark.asyncio
async def test_proactive_awareness_rate_limits_and_remembers_dismissal(tmp_path: Path) -> None:
    database = DatabaseService(tmp_path / "awareness.db")
    database.start()
    events = EventBus()
    await events.start()
    memory = PersistentMemoryRuntime(database)
    awareness = ProactiveAwarenessRuntime(
        events,
        memory,
        ProactivePolicy(minimum_importance=0.7, cooldown_seconds=60, maximum_notices_per_hour=3),
    )
    notices: list[EventEnvelope] = []

    async def collect(event: EventEnvelope) -> None:
        notices.append(event)

    events.subscribe("awareness.notice", collect)
    await awareness.start()
    try:
        await events.publish(
            EventEnvelope(
                "world.delta",
                {
                    "entity": "battery",
                    "attribute": "level",
                    "changed": True,
                    "importance": 0.9,
                    "source": "system",
                    "message": "Battery is critically low.",
                },
            )
        )
        await asyncio.sleep(0.05)
        assert len(notices) == 1
        key = str(notices[0].payload["notice_key"])

        await events.publish(EventEnvelope("awareness.dismissed", {"notice_key": key}))
        await asyncio.sleep(0.05)
        await events.publish(
            EventEnvelope(
                "world.delta",
                {
                    "entity": "battery",
                    "attribute": "level",
                    "changed": True,
                    "importance": 0.95,
                    "source": "system",
                },
            )
        )
        await asyncio.sleep(0.05)
        assert len(notices) == 1
    finally:
        await awareness.stop()
        await events.stop()
        database.stop()
