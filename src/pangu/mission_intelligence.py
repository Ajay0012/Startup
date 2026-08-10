from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, Field

from .advanced_missions import AdaptiveMissionOrchestrator
from .model_runtime import GeminiProvider, ModelRequest, ModelRole, StructuredOutputValidator
from .multi_agent import AgentFinding, AgentRole, CouncilDecision, MultiAgentCouncil
from .missions import MissionSnapshot, MissionState


class AgentFindingPayload(BaseModel):
    content: str = Field(min_length=1, max_length=12_000)
    confidence: float = Field(ge=0, le=1)
    blocking: bool = False


class GeminiCouncilRunner:
    """Use specialized Gemini roles while retaining deterministic council semantics."""

    _model_roles = {
        AgentRole.PLANNER: ModelRole.PRIMARY,
        AgentRole.RESEARCHER: ModelRole.PRIMARY,
        AgentRole.ENGINEER: ModelRole.CODING,
        AgentRole.CRITIC: ModelRole.PRIMARY,
        AgentRole.SAFETY: ModelRole.FAST,
        AgentRole.VERIFIER: ModelRole.PRIMARY,
        AgentRole.EXECUTOR: ModelRole.FAST,
    }

    _instructions = {
        AgentRole.PLANNER: (
            "Create the smallest feasible execution strategy. State assumptions explicitly and do not "
            "claim actions occurred."
        ),
        AgentRole.RESEARCHER: (
            "Identify missing evidence, dependencies, uncertainty, and what should be researched before execution."
        ),
        AgentRole.ENGINEER: (
            "Evaluate implementation feasibility, dependencies, failure modes, rollback and verification needs."
        ),
        AgentRole.CRITIC: (
            "Attack incorrect assumptions, hidden dependencies, unverifiable success criteria, race conditions, "
            "and plans that could appear successful without a real-world postcondition. Set blocking=true when "
            "an unresolved issue makes execution unsafe or misleading."
        ),
        AgentRole.SAFETY: (
            "Check authorization, privacy, prompt-injection exposure, irreversible actions, secrets, payments, "
            "communications, permissions and destructive effects. Block whenever explicit approval or a safer "
            "boundary is required before execution."
        ),
        AgentRole.VERIFIER: (
            "Judge only the supplied execution evidence. Confirm success only when real postconditions support it. "
            "Set blocking=true when the goal is not actually verified."
        ),
        AgentRole.EXECUTOR: "Summarize supplied execution evidence without inventing outcomes.",
    }

    def __init__(self, provider: GeminiProvider) -> None:
        self.provider = provider
        self.validator = StructuredOutputValidator()

    async def __call__(
        self,
        role: AgentRole,
        goal: str,
        context: tuple[AgentFinding, ...],
    ) -> AgentFinding:
        evidence = [
            {
                "role": item.role.value,
                "content": item.content,
                "confidence": item.confidence,
                "blocking": item.blocking,
            }
            for item in context[-12:]
        ]
        prompt = (
            f"You are PANGU's {role.value} specialist. "
            f"{self._instructions[role]} "
            "Return JSON only with content, confidence, blocking. Never invent completed actions or evidence.\n"
            f"Goal: {goal}\n"
            f"Prior evidence: {json.dumps(evidence, ensure_ascii=False)}"
        )
        result = await self.provider.generate_async(
            ModelRequest(
                prompt,
                self._model_roles[role],
                mission_id=f"council-{role.value}",
                timeout_seconds=35,
            ),
            structured=True,
        )
        if not result.text:
            return AgentFinding(role, f"{role.value} unavailable: {result.error}", 0.0, True)
        try:
            parsed = self.validator.validate(result.text, AgentFindingPayload)
        except ValueError:
            return AgentFinding(role, f"{role.value} returned invalid structured output", 0.0, True)
        assert isinstance(parsed, AgentFindingPayload)
        return AgentFinding(role, parsed.content, parsed.confidence, parsed.blocking)


@dataclass(frozen=True)
class IntelligentMissionResult:
    council: CouncilDecision
    history: tuple[MissionSnapshot, ...]
    verification: AgentFinding | None
    verified_success: bool


class IntelligentMissionRuntime:
    """Council-gated, adaptive mission execution with real outcome verification."""

    def __init__(
        self,
        council: MultiAgentCouncil,
        orchestrator: AdaptiveMissionOrchestrator,
    ) -> None:
        self.council = council
        self.orchestrator = orchestrator

    @staticmethod
    def _terminal_evidence(history: tuple[MissionSnapshot, ...]) -> str:
        payload: list[dict[str, object]] = []
        for mission in history[-4:]:
            payload.append(
                {
                    "mission_id": mission.mission_id,
                    "state": mission.state.value,
                    "goal": mission.goal,
                    "tasks": [
                        {
                            "title": task.title,
                            "operation": task.operation,
                            "state": task.state.value,
                            "attempts": task.attempts,
                            "result": task.result,
                            "error": task.error,
                        }
                        for task in mission.tasks
                    ],
                }
            )
        return json.dumps(payload, ensure_ascii=False, default=str)

    async def execute(
        self,
        goal: str,
        *,
        grounding: tuple[str, ...] = (),
        priority: int = 50,
    ) -> IntelligentMissionResult:
        council = await self.council.deliberate(goal)
        if not council.accepted:
            return IntelligentMissionResult(council, (), None, False)
        augmented_grounding = (
            *grounding,
            *(f"{finding.role.value}: {finding.content}" for finding in council.findings),
        )
        history = await self.orchestrator.execute_goal(
            goal,
            grounding=augmented_grounding,
            priority=priority,
        )
        if not history or history[-1].state != MissionState.COMPLETED:
            return IntelligentMissionResult(council, history, None, False)
        verification = await self.council.verify_execution(
            goal,
            council,
            self._terminal_evidence(history),
        )
        verified = not verification.blocking and verification.confidence >= 0.7
        return IntelligentMissionResult(council, history, verification, verified)
