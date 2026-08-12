from pangu.gestures import GestureDetection, GestureKind
from pangu.spatial_interaction import SpatialAction, SpatialInteractionController


def detection(
    kind: GestureKind, metadata: dict[str, float | str] | None = None
) -> GestureDetection:
    return GestureDetection(kind, 0.9, ("right-0",), 1.0, metadata or {})


def test_point_only_proposes_pointer_motion() -> None:
    controller = SpatialInteractionController()
    proposal = controller.propose(detection(GestureKind.POINT, {"x": 0.25, "y": 0.75}))

    assert proposal is not None
    assert proposal.action == SpatialAction.POINTER_MOVE
    assert proposal.requires_target_resolution is False
    assert controller.state.pointer_x == 0.25
    assert controller.state.pointer_y == 0.75


def test_pinch_is_a_selection_proposal_not_direct_execution() -> None:
    controller = SpatialInteractionController()
    controller.propose(detection(GestureKind.POINT, {"x": 0.4, "y": 0.6}))
    proposal = controller.propose(detection(GestureKind.PINCH, {"pinch_distance": 0.02}))

    assert proposal is not None
    assert proposal.action == SpatialAction.SELECT
    assert proposal.requires_target_resolution is True
    assert proposal.parameters == {"x": 0.4, "y": 0.6}


def test_two_hand_scale_stays_a_proposal() -> None:
    controller = SpatialInteractionController()
    scale = GestureDetection(
        GestureKind.TWO_HAND_SCALE_OUT,
        0.9,
        ("left-0", "right-0"),
        2.0,
        {"scale_delta": 0.22},
    )
    proposal = controller.propose(scale)

    assert proposal is not None
    assert proposal.action == SpatialAction.SCALE
    assert proposal.requires_target_resolution is True
    assert proposal.parameters["scale_delta"] == 0.22
