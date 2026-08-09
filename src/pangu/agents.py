from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from .missions import MissionSnapshot, MissionTaskResult, MissionTaskSpec, PersistentMissionRuntime
from .model_runtime import GeminiProvider, ModelRequest, ModelRole, StructuredOutputValidator


class AgentMode(StrEnum):
    CONTROLLED = "controlled"
    AGENT = "agent"


class PlannedTask(BaseModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=256)
    operation: str = Field(min_length=1, max_length=128)
    arguments: dict[str, object] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list, max_length=16)


class MissionPlan(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    tasks: list[PlannedTask] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_graph(self) -> MissionPlan:
        keys = [item.key for item in self.tasks]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate task key")
        known = set(keys)
        for task in self.tasks:
            if task.key in task.dependencies or not set(task.dependencies) <= known:
                raise ValueError("invalid task dependency")
        resolved: set[str] = set()
        dependency_map = {task.key: set(task.dependencies) for task in self.tasks}
        while len(resolved) < len(keys):
            ready = {
                key
                for key, deps in dependency_map.items()
                if key not in resolved and deps <= resolved
            }
            if not ready:
                raise ValueError("task dependency cycle")
            resolved.update(ready)
        return self


class AgentPlanner:
    """Gemini may propose plans; deterministic validation decides what can exist."""

    ALLOWED_OPERATIONS = frozenset(
        {
            "create_folder",
            "open_application",
            "focus_application",
            "minimize_application",
            "maximize_application",
            "restore_application",
            "get_volume",
            "set_volume",
            "increase_volume",
            "decrease_volume",
            "mute",
            "unmute",
            "get_brightness",
            "set_brightness",
            "increase_brightness",
            "decrease_brightness",
            "screen_snapshot",
            "browser_navigate",
            "browser_read",
        }
    )

    def __init__(self, provider: GeminiProvider) -> None:
        self.provider = provider
        self.validator = StructuredOutputValidator()

    @classmethod
    def validate_operations(cls, plan: MissionPlan) -> MissionPlan:
        forbidden = [task.operation for task in plan.tasks if task.operation not in cls.ALLOWED_OPERATIONS]
        if forbidden:
            raise ValueError(
                f"mission contains unsupported operations: {', '.join(sorted(set(forbidden)))}"
            )
        return plan

    async def plan(self, goal: str, grounding: tuple[str, ...] = ()) -> MissionPlan:
        allowed = ", ".join(sorted(self.ALLOWED_OPERATIONS))
        prompt = (
            "Create a small executable PANGU mission plan. Return JSON only with keys goal and tasks. "
            "Each task must contain key, title, operation, arguments, dependencies. "
            "Use ONLY the allowed operations below; do not invent tools, shell commands, coordinates, "
            "credentials, purchases, messages, destructive actions, or permission changes. Keep the plan "
            "minimal and dependency-correct. If the goal cannot be completed using allowed operations, "
            "create one screen_snapshot or browser_read observation task only when it genuinely helps; "
            "otherwise return a plan that gathers safe information rather than pretending completion.\n"
            f"Allowed operations: {allowed}\n"
            f"Goal: {goal}\n"
            f"Grounding: {list(grounding)[:12]}"
        )
        request = ModelRequest(
            prompt, ModelRole.PRIMARY, mission_id="mission-planning", timeout_seconds=30
        )
        result = await self.provider.generate_async(request, structured=True)
        if not result.text:
            raise RuntimeError(f"mission planning unavailable: {result.error}")
        try:
            plan = self.validator.validate(result.text, MissionPlan)
        except ValueError as error:
            raise RuntimeError("mission plan failed structured validation") from error
        assert isinstance(plan, MissionPlan)
        return self.validate_operations(plan)

    @classmethod
    def to_specs(cls, plan: MissionPlan) -> tuple[MissionTaskSpec, ...]:
        cls.validate_operations(plan)
        return tuple(
            MissionTaskSpec(
                item.key,
                item.title,
                item.operation,
                dict(item.arguments),
                tuple(item.dependencies),
            )
            for item in plan.tasks
        )


AgentOperationExecutor = Callable[[str, dict[str, object]], Awaitable[MissionTaskResult]]


@dataclass
class PanguAgentRuntime:
    planner: AgentPlanner
    missions: PersistentMissionRuntime
    executor: AgentOperationExecutor

    async def start_mission(
        self,
        goal: str,
        *,
        grounding: tuple[str, ...] = (),
        priority: int = 50,
    ) -> MissionSnapshot:
        plan = await self.planner.plan(goal, grounding)
        mission = self.missions.create(
            plan.goal,
            AgentPlanner.to_specs(plan),
            priority=priority,
            resumable=True,
        )

        async def execute_task(task):  # type: ignore[no-untyped-def]
            return await self.executor(task.operation, dict(task.arguments))

        return await self.missions.run(mission.mission_id, execute_task)

    async def resume_mission(self, mission_id: str) -> MissionSnapshot:
        async def execute_task(task):  # type: ignore[no-untyped-def]
            return await self.executor(task.operation, dict(task.arguments))

        return await self.missions.run(mission_id, execute_task)
