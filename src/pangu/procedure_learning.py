from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import Any

from .memory import MemoryKind, PersistentMemoryRuntime


class DemonstrationAction(StrEnum):
    OPEN_APP = "open_app"
    FOCUS_APP = "focus_app"
    CLICK_CONTROL = "click_control"
    SET_TEXT = "set_text"
    HOTKEY = "hotkey"
    BROWSER_NAVIGATE = "browser_navigate"
    BROWSER_CLICK = "browser_click"
    BROWSER_FILL = "browser_fill"
    WAIT_FOR = "wait_for"
    VERIFY = "verify"


@dataclass(frozen=True)
class DemonstrationStep:
    action: DemonstrationAction
    target: str
    arguments: dict[str, object] = field(default_factory=dict)
    observed_state: dict[str, object] = field(default_factory=dict)
    sensitive: bool = False


@dataclass(frozen=True)
class LearnedProcedure:
    name: str
    fingerprint: str
    steps: tuple[DemonstrationStep, ...]
    parameters: tuple[str, ...]
    verified: bool


class ProcedureLearningRuntime:
    """Compile owner demonstrations into bounded procedural memory.

    The recorder stores semantic actions, never raw passwords or unrestricted mouse
    coordinates. Learned procedures remain unverified until the owner explicitly
    confirms the replay description.
    """

    _forbidden_targets = (
        "password",
        "passcode",
        "otp",
        "one-time password",
        "cvv",
        "security code",
        "seed phrase",
        "private key",
    )

    def __init__(self, memory: PersistentMemoryRuntime, *, max_steps: int = 64) -> None:
        if not 1 <= max_steps <= 256:
            raise ValueError("max_steps must be between 1 and 256")
        self.memory = memory
        self.max_steps = max_steps
        self._name: str | None = None
        self._steps: list[DemonstrationStep] = []

    def begin(self, name: str) -> None:
        clean = " ".join(name.strip().split())
        if not clean:
            raise ValueError("procedure name is required")
        if self._name is not None:
            raise RuntimeError("a procedure demonstration is already active")
        self._name = clean
        self._steps.clear()

    def record(self, step: DemonstrationStep) -> None:
        if self._name is None:
            raise RuntimeError("no active demonstration")
        if len(self._steps) >= self.max_steps:
            raise RuntimeError("procedure demonstration exceeded maximum steps")
        target = step.target.casefold()
        if step.sensitive or any(term in target for term in self._forbidden_targets):
            raise ValueError("sensitive fields cannot be learned into procedures")
        if any(
            key.casefold() in {"password", "secret", "token", "otp", "cvv"}
            for key in step.arguments
        ):
            raise ValueError("sensitive arguments cannot be learned into procedures")
        if step.action == DemonstrationAction.CLICK_CONTROL and (
            "x" in step.arguments or "y" in step.arguments
        ):
            raise ValueError("raw coordinates cannot be stored in learned procedures")
        self._steps.append(step)

    @staticmethod
    def _discover_parameters(steps: tuple[DemonstrationStep, ...]) -> tuple[str, ...]:
        parameters: set[str] = set()
        for step in steps:
            for value in step.arguments.values():
                if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
                    name = value[2:-2].strip()
                    if name:
                        parameters.add(name)
        return tuple(sorted(parameters))

    @staticmethod
    def _fingerprint(name: str, steps: tuple[DemonstrationStep, ...]) -> str:
        normalized = [name]
        for step in steps:
            normalized.append(f"{step.action.value}|{step.target}|{sorted(step.arguments.items())}")
        return sha256("\n".join(normalized).encode("utf-8")).hexdigest()

    def finish(self) -> LearnedProcedure:
        if self._name is None:
            raise RuntimeError("no active demonstration")
        if not self._steps:
            raise ValueError("cannot learn an empty procedure")
        name = self._name
        steps = tuple(self._steps)
        self._name = None
        self._steps = []
        fingerprint = self._fingerprint(name, steps)
        parameters = self._discover_parameters(steps)
        procedure = LearnedProcedure(name, fingerprint, steps, parameters, False)
        self.memory.remember(
            MemoryKind.PROCEDURAL,
            f"procedure:{name}",
            {
                "name": name,
                "fingerprint": fingerprint,
                "parameters": list(parameters),
                "verified": False,
                "steps": [
                    {
                        "action": step.action.value,
                        "target": step.target,
                        "arguments": dict(step.arguments),
                        "observed_state": dict(step.observed_state),
                    }
                    for step in steps
                ],
            },
            importance=0.9,
            confidence=0.8,
            source="owner-demonstration",
        )
        return procedure

    def verify(self, name: str) -> LearnedProcedure:
        records = self.memory.recall(f"procedure:{name}", kinds=(MemoryKind.PROCEDURAL,), limit=8)
        record = next((item for item in records if item.subject == f"procedure:{name}"), None)
        if record is None:
            raise KeyError(name)
        raw_steps = record.content.get("steps")
        if not isinstance(raw_steps, list):
            raise TypeError("stored procedure is invalid")
        steps: list[DemonstrationStep] = []
        for raw in raw_steps:
            if not isinstance(raw, dict):
                raise TypeError("stored procedure step is invalid")
            action = DemonstrationAction(str(raw.get("action", "")))
            target = str(raw.get("target", ""))
            arguments = raw.get("arguments", {})
            observed = raw.get("observed_state", {})
            if not isinstance(arguments, dict) or not isinstance(observed, dict):
                raise TypeError("stored procedure arguments are invalid")
            steps.append(DemonstrationStep(action, target, dict(arguments), dict(observed)))
        fingerprint = str(record.content.get("fingerprint", ""))
        raw_parameters = record.content.get("parameters", [])
        if not isinstance(raw_parameters, list):
            raise TypeError("stored procedure parameters are invalid")
        parameters = tuple(str(item) for item in raw_parameters)
        self.memory.remember(
            MemoryKind.PROCEDURAL,
            record.subject,
            {**record.content, "verified": True},
            importance=record.importance,
            confidence=1.0,
            source="owner-verified-procedure",
        )
        return LearnedProcedure(name, fingerprint, tuple(steps), parameters, True)

    def instantiate(self, name: str, parameters: dict[str, str]) -> tuple[DemonstrationStep, ...]:
        records = self.memory.recall(f"procedure:{name}", kinds=(MemoryKind.PROCEDURAL,), limit=8)
        record = next((item for item in records if item.subject == f"procedure:{name}"), None)
        if record is None or record.content.get("verified") is not True:
            raise RuntimeError("procedure is missing or not owner-verified")
        raw_parameters = record.content.get("parameters", [])
        if not isinstance(raw_parameters, list):
            raise TypeError("stored procedure parameters are invalid")
        required = {str(item) for item in raw_parameters}
        if not required <= parameters.keys():
            missing = ", ".join(sorted(required - parameters.keys()))
            raise ValueError(f"missing procedure parameters: {missing}")
        raw_steps = record.content.get("steps", [])
        if not isinstance(raw_steps, list):
            raise TypeError("stored procedure steps are invalid")
        result: list[DemonstrationStep] = []
        for raw in raw_steps:
            if not isinstance(raw, dict):
                continue
            raw_arguments = raw.get("arguments", {})
            raw_observed = raw.get("observed_state", {})
            if not isinstance(raw_arguments, dict) or not isinstance(raw_observed, dict):
                continue
            args: dict[str, Any] = dict(raw_arguments)
            for key, value in tuple(args.items()):
                if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
                    args[key] = parameters[value[2:-2].strip()]
            result.append(
                DemonstrationStep(
                    DemonstrationAction(str(raw["action"])),
                    str(raw["target"]),
                    args,
                    dict(raw_observed),
                )
            )
        return tuple(result)
