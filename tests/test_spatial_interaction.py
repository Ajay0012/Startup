from pangu.gestures import GestureDetection, GestureKind
from pangu.spatial_interaction import (
    SemanticTarget,
    SpatialAction,
    SpatialInteractionController,
    TrashZone,
)


def detection(
    kind: GestureKind,
    metadata: dict[str, float | str] | None = None,
    *,
    timestamp: float = 1.0,
) -> GestureDetection:
    return GestureDetection(kind, 0.9, ("right-0",), timestamp, metadata or {})


def target(**overrides: object) -> SemanticTarget:
    values: dict[str, object] = {
        "target_id": "tab-1",
        "kind": "browser_tab",
        "x": 0.20,
        "y": 0.20,
        "width": 0.20,
        "height": 0.20,
        "closable": True,
    }
    values.update(overrides)
    return SemanticTarget(**values)  # type: ignore[arg-type]


def test_point_only_proposes_pointer_motion() -> None:
    controller = SpatialInteractionController()
    proposal = controller.propose(detection(GestureKind.POINT, {"x": 0.25, "y": 0.75}))

    assert proposal is not None
    assert proposal.action == SpatialAction.POINTER_MOVE
    assert proposal.requires_target_resolution is False
    assert controller.state.pointer_x == 0.25
    assert controller.state.pointer_y == 0.75


def test_point_over_semantic_target_proposes_hover_not_os_input() -> None:
    controller = SpatialInteractionController()
    proposal = controller.propose(
        detection(GestureKind.POINT, {"x": 0.25, "y": 0.25}),
        (target(),),
    )

    assert proposal is not None
    assert proposal.action == SpatialAction.HOVER_TARGET
    assert proposal.parameters["target_id"] == "tab-1"
    assert proposal.requires_target_resolution is True


def test_pinch_is_a_selection_proposal_not_direct_execution() -> None:
    controller = SpatialInteractionController()
    controller.propose(detection(GestureKind.POINT, {"x": 0.4, "y": 0.6}))
    proposal = controller.propose(detection(GestureKind.PINCH, {"pinch_distance": 0.02}))

    assert proposal is not None
    assert proposal.action == SpatialAction.SELECT
    assert proposal.requires_target_resolution is True
    assert proposal.parameters["x"] == 0.4
    assert proposal.parameters["y"] == 0.6


def test_grab_then_point_proposes_drag_for_same_semantic_target() -> None:
    controller = SpatialInteractionController()
    item = target()
    controller.propose(
        detection(GestureKind.POINT, {"x": 0.25, "y": 0.25}, timestamp=1.0),
        (item,),
    )
    grab = controller.propose(detection(GestureKind.GRAB, timestamp=1.05), (item,))
    drag = controller.propose(
        detection(GestureKind.POINT, {"x": 0.60, "y": 0.55}, timestamp=1.20),
        (item,),
    )

    assert grab is not None and grab.action == SpatialAction.GRAB_BEGIN
    assert drag is not None and drag.action == SpatialAction.DRAG
    assert drag.parameters["target_id"] == "tab-1"
    assert controller.state.grabbed is True


def test_fist_can_air_grab_first_preferred_target_without_hover() -> None:
    controller = SpatialInteractionController()
    active = target(target_id="active-tab")
    other = target(target_id="other-tab", x=0.65)

    grab = controller.propose(detection(GestureKind.GRAB, timestamp=1.0), (active, other))

    assert grab is not None
    assert grab.action == SpatialAction.GRAB_BEGIN
    assert grab.parameters["target_id"] == "active-tab"
    assert grab.parameters["air_grab"] is True
    assert controller.state.grabbed_target_id == "active-tab"


def test_fast_throw_into_trash_proposes_close_but_does_not_execute() -> None:
    controller = SpatialInteractionController(throw_velocity_threshold=0.5)
    item = target()
    zone = TrashZone(x=0.75, y=0.70, width=0.25, height=0.30)

    controller.propose(
        detection(GestureKind.POINT, {"x": 0.25, "y": 0.25}, timestamp=1.0),
        (item,),
        zone,
    )
    controller.propose(detection(GestureKind.GRAB, timestamp=1.05), (item,), zone)
    controller.propose(
        detection(GestureKind.POINT, {"x": 0.70, "y": 0.65}, timestamp=1.20),
        (item,),
        zone,
    )
    controller.propose(
        detection(GestureKind.POINT, {"x": 0.80, "y": 0.76}, timestamp=1.28),
        (item,),
        zone,
    )
    proposal = controller.propose(
        detection(GestureKind.OPEN_PALM, timestamp=1.30),
        (item,),
        zone,
    )

    assert proposal is not None
    assert proposal.action == SpatialAction.THROW_TO_TRASH
    assert proposal.parameters["target_id"] == "tab-1"
    assert proposal.requires_target_resolution is True
    assert proposal.requires_approval is False
    assert controller.state.grabbed is False


def test_fast_throw_anywhere_proposes_close_without_trash_zone_hit() -> None:
    controller = SpatialInteractionController(throw_velocity_threshold=0.5)
    item = target(x=0.1, y=0.1)

    controller.propose(detection(GestureKind.GRAB, timestamp=1.0), (item,))
    controller.propose(
        detection(GestureKind.POINT, {"x": 0.35, "y": 0.25}, timestamp=1.12),
        (item,),
    )
    proposal = controller.propose(
        detection(GestureKind.OPEN_PALM, timestamp=1.16),
        (item,),
    )

    assert proposal is not None
    assert proposal.action == SpatialAction.THROW_TO_TRASH
    assert proposal.parameters["throw_anywhere"] is True


def test_unsaved_or_multi_target_throw_requires_approval() -> None:
    controller = SpatialInteractionController(throw_velocity_threshold=0.5)
    item = target(unsaved=True, selection_count=2, destructive=True)
    zone = TrashZone(x=0.75, y=0.70, width=0.25, height=0.30)

    controller.propose(
        detection(GestureKind.POINT, {"x": 0.25, "y": 0.25}, timestamp=2.0),
        (item,),
        zone,
    )
    controller.propose(detection(GestureKind.GRAB, timestamp=2.05), (item,), zone)
    controller.propose(
        detection(GestureKind.POINT, {"x": 0.82, "y": 0.80}, timestamp=2.25),
        (item,),
        zone,
    )
    proposal = controller.propose(
        detection(GestureKind.OPEN_PALM, timestamp=2.28),
        (item,),
        zone,
    )

    assert proposal is not None
    assert proposal.action == SpatialAction.THROW_TO_TRASH
    assert proposal.requires_approval is True
    assert proposal.parameters["selection_count"] == 2
    assert proposal.parameters["unsaved"] is True


def test_slow_release_never_becomes_throw() -> None:
    controller = SpatialInteractionController(throw_velocity_threshold=0.5)
    item = target()
    zone = TrashZone(x=0.75, y=0.70, width=0.25, height=0.30)

    controller.propose(
        detection(GestureKind.POINT, {"x": 0.25, "y": 0.25}, timestamp=3.0),
        (item,),
        zone,
    )
    controller.propose(detection(GestureKind.GRAB, timestamp=3.05), (item,), zone)
    controller.propose(
        detection(GestureKind.POINT, {"x": 0.80, "y": 0.80}, timestamp=4.50),
        (item,),
        zone,
    )
    proposal = controller.propose(
        detection(GestureKind.OPEN_PALM, timestamp=4.55),
        (item,),
        zone,
    )

    assert proposal is not None
    assert proposal.action == SpatialAction.RELEASE


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
