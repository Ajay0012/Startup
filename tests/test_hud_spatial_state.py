import json

import pytest

from pangu.events import EventBus, EventEnvelope
from pangu.hud_bridge import HudStateBridge


@pytest.mark.asyncio
async def test_hud_bridge_serializes_live_spatial_pointer_and_trash_state(tmp_path) -> None:
    path = tmp_path / "state.json"
    bridge = HudStateBridge(EventBus(), path, minimum_write_interval=0.0)
    await bridge.start()

    await bridge._on_event(
        EventEnvelope(
            "gesture.detected",
            {
                "gesture": "POINT",
                "confidence": 0.98,
                "metadata": {"x": 0.25, "y": 0.40},
            },
        )
    )
    await bridge._on_event(
        EventEnvelope(
            "spatial.target",
            {
                "label": "PANGU CI Finish",
                "x": 0.02,
                "y": 0.01,
                "width": 0.17,
                "height": 0.04,
                "confidence": 0.99,
            },
        )
    )
    await bridge._on_event(
        EventEnvelope(
            "spatial.proposal",
            {
                "action": "GRAB_BEGIN",
                "grabbed": True,
                "target_id": "chrome:tab:1",
                "requires_approval": False,
            },
        )
    )

    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["Spatial"]["Pointer"] == {"X": 0.25, "Y": 0.4}
    assert state["Spatial"]["Gesture"] == "POINT"
    assert state["Spatial"]["Grabbed"] is True
    assert state["Spatial"]["GrabbedTargetId"] == "chrome:tab:1"
    assert state["Spatial"]["TrashZone"]["Active"] is True
    assert state["Target"]["Label"] == "PANGU CI Finish"

    await bridge.stop()


@pytest.mark.asyncio
async def test_hud_bridge_marks_throw_confirmation_without_executing_action(tmp_path) -> None:
    path = tmp_path / "state.json"
    bridge = HudStateBridge(EventBus(), path, minimum_write_interval=0.0)
    await bridge.start()

    await bridge._on_event(
        EventEnvelope(
            "spatial.proposal",
            {
                "action": "THROW_TO_TRASH",
                "target_id": "chrome:tab:1",
                "speed": 1.25,
                "requires_approval": True,
            },
        )
    )

    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["Spatial"]["Interaction"] == "THROW_TO_TRASH"
    assert state["Spatial"]["ConfirmationRequired"] is True
    assert state["Spatial"]["ThrowSpeed"] == 1.25
    assert state["Spatial"]["Grabbed"] is False
    assert state["Spatial"]["TrashZone"]["Active"] is True

    await bridge.stop()
