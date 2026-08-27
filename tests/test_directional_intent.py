from pangu.directional_intent import DirectionalIntentAssist
from pangu.spatial_interaction import SemanticTarget


def target() -> SemanticTarget:
    return SemanticTarget(
        target_id="button-1",
        kind="ui_action",
        x=0.72,
        y=0.42,
        width=0.10,
        height=0.10,
    )


def test_motion_toward_target_gets_intent_assist() -> None:
    assist = DirectionalIntentAssist(horizon_seconds=0.2, corridor_radius=0.10)
    assist.apply(0.30, 0.47, 1.0, (target(),))
    result = assist.apply(0.40, 0.47, 1.1, (target(),))
    assert result.target_id == "button-1"
    assert result.x > 0.40
    assert result.speed > 0.0


def test_motion_away_from_target_is_not_magnetized() -> None:
    assist = DirectionalIntentAssist(horizon_seconds=0.2, corridor_radius=0.08)
    assist.apply(0.60, 0.47, 1.0, (target(),))
    result = assist.apply(0.50, 0.47, 1.1, (target(),))
    assert result.target_id is None
    assert result.x == 0.50
