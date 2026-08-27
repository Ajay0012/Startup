from __future__ import annotations

import pytest

from pangu.agents import AgentPlanner, MissionPlan


def test_agent_plan_accepts_only_supported_dependency_correct_operations() -> None:
    plan = MissionPlan.model_validate(
        {
            "goal": "Open VS Code and inspect the screen",
            "tasks": [
                {
                    "key": "open",
                    "title": "Open VS Code",
                    "operation": "open_application",
                    "arguments": {"application": "Visual Studio Code"},
                    "dependencies": [],
                },
                {
                    "key": "screen",
                    "title": "Read the screen",
                    "operation": "screen_snapshot",
                    "arguments": {},
                    "dependencies": ["open"],
                },
            ],
        }
    )
    assert AgentPlanner.validate_operations(plan) is plan
    specs = AgentPlanner.to_specs(plan)
    assert specs[1].dependencies == ("open",)


def test_agent_plan_rejects_shell_and_consequential_operations() -> None:
    plan = MissionPlan.model_validate(
        {
            "goal": "Run arbitrary command",
            "tasks": [
                {
                    "key": "shell",
                    "title": "Shell",
                    "operation": "shell",
                    "arguments": {"command": "whoami"},
                    "dependencies": [],
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="unsupported operations"):
        AgentPlanner.validate_operations(plan)


def test_agent_plan_rejects_cycles_before_persistence() -> None:
    with pytest.raises(ValueError, match="cycle"):
        MissionPlan.model_validate(
            {
                "goal": "cycle",
                "tasks": [
                    {
                        "key": "a",
                        "title": "A",
                        "operation": "screen_snapshot",
                        "arguments": {},
                        "dependencies": ["b"],
                    },
                    {
                        "key": "b",
                        "title": "B",
                        "operation": "browser_read",
                        "arguments": {},
                        "dependencies": ["a"],
                    },
                ],
            }
        )
