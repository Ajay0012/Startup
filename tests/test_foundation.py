from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from pangu.capabilities import CapabilityCatalog, ToolSpecification
from pangu.contracts import Risk, Status, ToolRequest
from pangu.events import EventBus, EventEnvelope, EventPriority
from pangu.filesystem import FilesystemAdapter
from pangu.lifecycle import LifecycleKernel, LifecycleService, LifecycleState
from pangu.permissions import PermissionGrant, PermissionStore
from pangu.runtime import build_runtime
from pangu.security import ApprovalStore


@pytest.mark.asyncio
async def test_event_bus_delivers_and_stops() -> None:
    received: list[str] = []
    bus = EventBus()

    async def handler(event: EventEnvelope) -> None:
        received.append(event.payload["value"])

    bus.subscribe("test", handler)
    await bus.start()
    await bus.publish(EventEnvelope("test", {"value": "ok"}))
    await bus.stop()
    assert received == ["ok"]


@pytest.mark.asyncio
async def test_event_bus_rejects_publish_outside_lifecycle() -> None:
    bus = EventBus()
    with pytest.raises(RuntimeError):
        await bus.publish(EventEnvelope("test", {}))
    await bus.start()
    await bus.stop()
    with pytest.raises(RuntimeError):
        await bus.publish(EventEnvelope("test", {}))


@pytest.mark.asyncio
async def test_event_bus_priority_ordering() -> None:
    seen: list[str] = []
    bus = EventBus()

    async def handler(event: EventEnvelope) -> None:
        seen.append(event.payload["id"])

    bus.subscribe("test", handler)
    await bus.start()
    await bus.publish(EventEnvelope("test", {"id": "low"}, EventPriority.LOW))
    await bus.publish(EventEnvelope("test", {"id": "high"}, EventPriority.HIGH))
    await bus.stop()
    assert seen == ["high", "low"]


@pytest.mark.asyncio
async def test_event_bus_isolates_handler_failure() -> None:
    bus = EventBus()

    async def bad(_: EventEnvelope) -> None:
        raise RuntimeError("expected")

    bus.subscribe("test", bad)
    await bus.start()
    await bus.publish(EventEnvelope("test", {}))
    await bus.stop()
    assert len(bus.dead_letters) == 1


@pytest.mark.asyncio
async def test_event_bus_handler_timeout_is_dead_letter() -> None:
    bus = EventBus(handler_timeout=0.01)

    async def slow(_: EventEnvelope) -> None:
        await asyncio.sleep(0.1)

    bus.subscribe("test", slow)
    await bus.start()
    await bus.publish(EventEnvelope("test", {}))
    await bus.stop()
    assert len(bus.dead_letters) == 1


@pytest.mark.asyncio
async def test_lifecycle_orders_dependencies_and_reverse_shutdown() -> None:
    trace: list[str] = []

    def service(name: str, deps: tuple[str, ...] = ()) -> LifecycleService:
        async def start() -> None:
            trace.append(f"start:{name}")

        async def stop() -> None:
            trace.append(f"stop:{name}")

        return LifecycleService(name, start, stop, deps)

    kernel = LifecycleKernel()
    kernel.register(service("database"))
    kernel.register(service("runtime", ("database",)))
    await kernel.start()
    await kernel.stop()
    assert trace == ["start:database", "start:runtime", "stop:runtime", "stop:database"]


@pytest.mark.asyncio
async def test_lifecycle_cycle_is_rejected() -> None:
    async def noop() -> None:
        pass

    kernel = LifecycleKernel()
    kernel.register(LifecycleService("a", noop, noop, ("b",)))
    kernel.register(LifecycleService("b", noop, noop, ("a",)))
    with pytest.raises(RuntimeError):
        await kernel.start()
    assert kernel.state == LifecycleState.STOPPED


@pytest.mark.asyncio
async def test_lifecycle_start_failure_rolls_back() -> None:
    trace: list[str] = []

    async def start_ok() -> None:
        trace.append("start")

    async def stop_ok() -> None:
        trace.append("stop")

    async def fail() -> None:
        raise RuntimeError("fail")

    kernel = LifecycleKernel()
    kernel.register(LifecycleService("one", start_ok, stop_ok))
    kernel.register(LifecycleService("two", fail, stop_ok, ("one",)))
    with pytest.raises(RuntimeError):
        await kernel.start()
    assert trace == ["start", "stop"]


def test_catalog_registration_and_lookup() -> None:
    catalog = CapabilityCatalog()
    spec = ToolSpecification(
        "file", "1", frozenset({"read"}), Risk.READ_ONLY, frozenset({"filesystem.read:*"})
    )
    catalog.register(spec)
    assert catalog.resolve("file", "read") == spec


def test_catalog_duplicate_and_unknown_are_rejected() -> None:
    catalog = CapabilityCatalog()
    spec = ToolSpecification("file", "1", frozenset({"read"}), Risk.READ_ONLY, frozenset())
    catalog.register(spec)
    with pytest.raises(ValueError):
        catalog.register(spec)
    with pytest.raises(LookupError):
        catalog.resolve("file", "write")


@pytest.mark.parametrize(
    ("grant", "required", "allowed"),
    [
        ("filesystem.read:E:/work", "filesystem.read:E:/work", True),
        ("filesystem.write:*", "filesystem.write:E:/work/report.txt", True),
        ("filesystem.read:E:/work", "filesystem.write:E:/work", False),
        ("application.control:Chrome", "application.control:Chrome", True),
        ("browser.access:example.com", "browser.access:other.com", False),
    ],
)
def test_permission_matching(grant: str, required: str, allowed: bool) -> None:
    store = PermissionStore((PermissionGrant(grant, "u"),))
    assert store.allows("u", required) is allowed


def test_filesystem_create_write_list_and_recycle(tmp_path: Path) -> None:
    fs = FilesystemAdapter(tmp_path)
    folder, created = fs.create_folder("reports")
    path, digest = fs.write_text("reports/result.txt", "hello")
    assert created and path.exists() and len(digest) == 64
    assert fs.list_directory("reports") == ["result.txt"]
    recycled = fs.recycle("reports/result.txt")
    assert recycled.exists() and not path.exists() and folder.exists()


@pytest.mark.parametrize("path", ["../outside", "../../outside", str(Path.cwd().anchor)])
def test_filesystem_rejects_escapes(tmp_path: Path, path: str) -> None:
    with pytest.raises(PermissionError):
        FilesystemAdapter(tmp_path).resolve(path)


def test_filesystem_refuses_overwrite(tmp_path: Path) -> None:
    fs = FilesystemAdapter(tmp_path)
    fs.write_text("a.txt", "one")
    with pytest.raises(FileExistsError):
        fs.write_text("a.txt", "two")


def test_approval_is_bound_and_one_time() -> None:
    store = ApprovalStore()
    request = ToolRequest("file", "delete", {"path": "a"})
    token = store.issue(request)
    assert store.consume(request, token)
    assert not store.consume(request, token)


def test_approval_argument_mutation_is_rejected() -> None:
    store = ApprovalStore()
    token = store.issue(ToolRequest("file", "delete", {"path": "a"}))
    assert not store.consume(ToolRequest("file", "delete", {"path": "b"}), token)


def test_runtime_unknown_intent_is_unverified(tmp_path: Path) -> None:
    os.environ["PANGU_RUNTIME_ROOT"] = str(tmp_path / "runtime")
    runtime = build_runtime(tmp_path)
    runtime.start()
    try:
        assert runtime.command("compose a poem").status == Status.UNVERIFIED
    finally:
        runtime.stop()


def test_runtime_audits_folder_command(tmp_path: Path) -> None:
    os.environ["PANGU_RUNTIME_ROOT"] = str(tmp_path / "runtime")
    runtime = build_runtime(tmp_path)
    runtime.start()
    try:
        assert runtime.command("create folder output").status == Status.VERIFIED
        assert runtime.db.audit_count() == 1
    finally:
        runtime.stop()
