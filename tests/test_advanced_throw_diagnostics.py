from pathlib import Path

import pytest

from pangu.spatial_interaction import SpatialAction, SpatialActionProposal
from pangu.spatial_live_advanced import AdvancedLiveSpatialDryRunRuntime


@pytest.mark.asyncio
async def test_terminal_throw_diagnostics_are_not_overwritten_by_pointer_motion() -> None:
    runtime = AdvancedLiveSpatialDryRunRuntime(
        model_path=Path("models/vision/hand_landmarker.task"),
        hud_state_path=Path("runtime-data/overlay/state.json"),
        throw_velocity_threshold=0.22,
    )

    async def no_publish(_: SpatialActionProposal) -> None:
        return None

    runtime._publish_proposal = no_publish  # type: ignore[method-assign]

    throw = SpatialActionProposal(
        SpatialAction.THROW_TO_TRASH,
        ("right-0",),
        0.95,
        {"target_id": "tab-1", "speed": 0.41},
    )
    await runtime._handle_proposal(throw)

    runtime._last_action = SpatialAction.POINTER_MOVE.value
    diagnostics = runtime.throw_diagnostics()

    assert diagnostics["throws"] == 1
    assert diagnostics["releases"] == 0
    assert diagnostics["last_terminal_action"] == SpatialAction.THROW_TO_TRASH.value
    assert diagnostics["last_release_speed"] == pytest.approx(0.41)
    assert diagnostics["throw_threshold"] == pytest.approx(0.22)


def test_robust_throw_speed_uses_recent_displacement() -> None:
    runtime = AdvancedLiveSpatialDryRunRuntime(
        model_path=Path("models/vision/hand_landmarker.task"),
        hud_state_path=Path("runtime-data/overlay/state.json"),
        throw_velocity_threshold=0.22,
    )
    runtime._drag_velocity_history.extend(
        [
            (1.00, 0.20, 0.30),
            (1.10, 0.28, 0.30),
            (1.20, 0.38, 0.31),
        ]
    )
    assert runtime._robust_throw_speed() > 0.22


def test_advanced_throw_threshold_is_bounded() -> None:
    for invalid in (0.01, 2.1):
        with pytest.raises(ValueError):
            AdvancedLiveSpatialDryRunRuntime(
                model_path=Path("models/vision/hand_landmarker.task"),
                hud_state_path=Path("runtime-data/overlay/state.json"),
                throw_velocity_threshold=invalid,
            )
