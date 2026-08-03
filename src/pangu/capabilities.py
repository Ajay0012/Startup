from __future__ import annotations

from dataclasses import dataclass

from .contracts import Risk


@dataclass(frozen=True)
class ToolSpecification:
    tool_id: str
    version: str
    operations: frozenset[str]
    risk: Risk
    permission_scopes: frozenset[str]
    timeout_seconds: float = 10.0


class CapabilityCatalog:
    def __init__(self) -> None:
        self._specifications: dict[str, ToolSpecification] = {}

    def register(self, specification: ToolSpecification) -> None:
        if specification.tool_id in self._specifications:
            raise ValueError(f"duplicate tool: {specification.tool_id}")
        self._specifications[specification.tool_id] = specification

    def resolve(self, tool_id: str, operation: str) -> ToolSpecification:
        specification = self._specifications.get(tool_id)
        if specification is None or operation not in specification.operations:
            raise LookupError("unknown tool operation")
        return specification
