from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class AgentRole(StrEnum):
    PLANNER = "planner"
    RESEARCHER = "researcher"
    ENGINEER = "engineer"
    CRITIC = "critic"
    SAFETY = "safety"
    EXECUTOR = "executor"
    VERIFIER = "verifier"


@dataclass(frozen=True)
class AgentFinding:
    role: AgentRole
    content: str
    confidence: float
    blocking: bool = False

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("finding content is required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class CouncilDecision:
    accepted: bool
    plan: str
    findings: tuple[AgentFinding, ...]
    blockers: tuple[AgentFinding, ...]
    verification_required: bool


AgentRunner = Callable[[AgentRole, str, tuple[AgentFinding, ...]], Awaitable[AgentFinding]]


class MultiAgentCouncil:
    """Structured deliberation with distinct responsibilities, not majority voting.

    Planner/researcher/engineer produce proposals in parallel. Critic receives those
    proposals and specifically hunts assumptions/failure modes. Safety receives the
    same evidence and can block execution independently. Verifier is required for any
    accepted consequential plan and must inspect actual postconditions after execution.
    """

    def __init__(self, runner: AgentRunner, *, timeout_seconds: float = 25.0) -> None:
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    async def _run(
        self,
        role: AgentRole,
        goal: str,
        context: tuple[AgentFinding, ...],
    ) -> AgentFinding:
        finding = await asyncio.wait_for(
            self.runner(role, goal, context), timeout=self.timeout_seconds
        )
        if finding.role != role:
            raise ValueError(f"agent returned wrong role: expected {role}, got {finding.role}")
        return finding

    async def deliberate(self, goal: str) -> CouncilDecision:
        goal = " ".join(goal.strip().split())
        if not goal:
            raise ValueError("goal is required")
        initial = await asyncio.gather(
            self._run(AgentRole.PLANNER, goal, ()),
            self._run(AgentRole.RESEARCHER, goal, ()),
            self._run(AgentRole.ENGINEER, goal, ()),
        )
        evidence = tuple(initial)
        critic, safety = await asyncio.gather(
            self._run(AgentRole.CRITIC, goal, evidence),
            self._run(AgentRole.SAFETY, goal, evidence),
        )
        all_findings = (*evidence, critic, safety)
        blockers = tuple(item for item in all_findings if item.blocking)
        if blockers:
            return CouncilDecision(False, initial[0].content, tuple(all_findings), blockers, False)
        # Critic confidence below 0.45 indicates unresolved assumptions even if it did
        # not hard-block. We fail closed and request replanning rather than vote it away.
        if critic.confidence < 0.45:
            return CouncilDecision(
                False,
                initial[0].content,
                tuple(all_findings),
                (critic,),
                False,
            )
        return CouncilDecision(True, initial[0].content, tuple(all_findings), (), True)

    async def verify_execution(
        self,
        goal: str,
        prior: CouncilDecision,
        execution_evidence: str,
    ) -> AgentFinding:
        if not prior.accepted or not prior.verification_required:
            raise RuntimeError("cannot verify a plan that was not accepted for execution")
        context = (
            *prior.findings,
            AgentFinding(AgentRole.EXECUTOR, execution_evidence, 1.0, False),
        )
        verifier = await self._run(AgentRole.VERIFIER, goal, context)
        return verifier
