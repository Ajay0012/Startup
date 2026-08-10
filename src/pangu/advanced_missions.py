from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .agents import AgentPlanner
from .missions import MissionSnapshot, MissionState, MissionTaskResult, PersistentMissionRuntime


@dataclass(frozen=True)
class MissionRecoveryDecision:
    should_replan: bool
    reason: str
    grounding: tuple[str, ...] = ()


RecoveryPlanner = Callable[[MissionSnapshot], Awaitable[MissionRecoveryDecision]]
OperationExecutor = Callable[[str, dict[str, object]], Awaitable[MissionTaskResult]]


class AdaptiveMissionOrchestrator:
    """Long-running mission wrapper with bounded replanning and recovery.

    PersistentMissionRuntime remains the authoritative mission store/executor. This
    orchestrator only decides whether a failed/resumable mission should be replanned
    from its observed evidence. It never mutates completed mission history.
    """

    def __init__(
        self,
        missions: PersistentMissionRuntime,
        planner: AgentPlanner,
        executor: OperationExecutor,
        recovery_planner: RecoveryPlanner,
        *,
        max_replans: int = 3,
    ) -> None:
        if not 0 <= max_replans <= 8:
            raise ValueError("max_replans must be between 0 and 8")
        self.missions = missions
        self.planner = planner
        self.executor = executor
        self.recovery_planner = recovery_planner
        self.max_replans = max_replans

    async def _run(self, mission_id: str) -> MissionSnapshot:
        async def execute(task):  # type: ignore[no-untyped-def]
            return await self.executor(task.operation, dict(task.arguments))

        return await self.missions.run(mission_id, execute)

    async def execute_goal(
        self,
        goal: str,
        *,
        grounding: tuple[str, ...] = (),
        priority: int = 50,
    ) -> tuple[MissionSnapshot, ...]:
        plan = await self.planner.plan(goal, grounding)
        mission = self.missions.create(
            plan.goal,
            AgentPlanner.to_specs(plan),
            priority=priority,
            resumable=True,
        )
        history: list[MissionSnapshot] = []
        current = await self._run(mission.mission_id)
        history.append(current)
        replans = 0
        while current.state in {MissionState.FAILED, MissionState.PAUSED} and replans < self.max_replans:
            recovery = await self.recovery_planner(current)
            if not recovery.should_replan:
                break
            replans += 1
            failure_context = [
                f"task={task.title}; state={task.state.value}; error={task.error}; result={task.result}"
                for task in current.tasks
                if task.error or task.state.value in {"FAILED", "RUNNING"}
            ]
            revised = await self.planner.plan(
                goal,
                (*grounding, *recovery.grounding, *failure_context, f"replan_reason={recovery.reason}"),
            )
            replacement = self.missions.create(
                revised.goal,
                AgentPlanner.to_specs(revised),
                priority=priority,
                resumable=True,
            )
            current = await self._run(replacement.mission_id)
            history.append(current)
        return tuple(history)
