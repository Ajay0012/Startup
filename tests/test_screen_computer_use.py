from __future__ import annotations

from dataclasses import dataclass

from pangu.computer_use import (
    ComputerActionKind,
    ComputerActionRequest,
    ComputerTarget,
    ComputerUseRuntime,
    ComputerUseState,
)
from pangu.screen_perception import (
    ScreenPerceptionRuntime,
    ScreenRect,
    ScreenSnapshot,
    UIElement,
)


def element(
    name: str,
    *,
    automation_id: str = "",
    control_type: str = "Button",
    password: bool = False,
    handle: int = 10,
) -> UIElement:
    return UIElement(
        f"{handle}:{automation_id}:{name}",
        name,
        control_type,
        automation_id,
        "TestClass",
        ScreenRect(0, 0, 100, 40),
        True,
        True,
        True,
        password,
        handle,
    )


@dataclass
class FakeScreenAdapter:
    snapshot_value: ScreenSnapshot

    def snapshot(self) -> ScreenSnapshot:
        return self.snapshot_value


class FakeActionAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def focus(self, target: UIElement) -> bool:
        self.calls.append(("focus", target.name))
        return True

    def invoke(self, target: UIElement) -> bool:
        self.calls.append(("invoke", target.name))
        return True

    def set_text(self, target: UIElement, text: str) -> bool:
        self.calls.append(("set_text", f"{target.name}:{text}"))
        return True

    def scroll(self, target: UIElement, amount: int) -> bool:
        self.calls.append(("scroll", f"{target.name}:{amount}"))
        return True


def test_screen_find_is_structured_and_bounded() -> None:
    snapshot = ScreenSnapshot(
        "fake",
        "VERIFIED",
        "Editor",
        10,
        (element("Save", automation_id="save"), element("Search", control_type="Edit")),
    )
    runtime = ScreenPerceptionRuntime(FakeScreenAdapter(snapshot))
    assert runtime.find("save")[0].automation_id == "save"
    assert runtime.find("search", control_type="Edit")[0].name == "Search"


def test_computer_use_rejects_ambiguous_and_password_targets() -> None:
    ambiguous = ScreenSnapshot(
        "fake",
        "VERIFIED",
        "App",
        10,
        (element("Open"), element("Open")),
    )
    runtime = ComputerUseRuntime(
        ScreenPerceptionRuntime(FakeScreenAdapter(ambiguous)), FakeActionAdapter()
    )
    result = runtime.execute(
        ComputerActionRequest(ComputerActionKind.INVOKE, ComputerTarget(name="Open"))
    )
    assert result.state == ComputerUseState.DENIED
    assert result.normalized_error == "TARGET_AMBIGUOUS"

    password_snapshot = ScreenSnapshot(
        "fake", "VERIFIED", "Login", 10, (element("Password", password=True),)
    )
    runtime = ComputerUseRuntime(
        ScreenPerceptionRuntime(FakeScreenAdapter(password_snapshot)), FakeActionAdapter()
    )
    result = runtime.execute(
        ComputerActionRequest(
            ComputerActionKind.SET_TEXT, ComputerTarget(name="Password"), text="secret"
        )
    )
    assert result.state == ComputerUseState.DENIED
    assert result.normalized_error == "PASSWORD_FIELD_BLOCKED"


def test_computer_use_blocks_consequential_controls_without_approval_path() -> None:
    snapshot = ScreenSnapshot("fake", "VERIFIED", "Checkout", 10, (element("Pay now"),))
    actions = FakeActionAdapter()
    runtime = ComputerUseRuntime(ScreenPerceptionRuntime(FakeScreenAdapter(snapshot)), actions)
    result = runtime.execute(
        ComputerActionRequest(ComputerActionKind.INVOKE, ComputerTarget(name="Pay now"))
    )
    assert result.state == ComputerUseState.DENIED
    assert result.normalized_error == "CONSEQUENTIAL_CONTROL_REQUIRES_APPROVAL"
    assert not actions.calls


def test_computer_use_executes_non_sensitive_typed_action() -> None:
    snapshot = ScreenSnapshot(
        "fake", "VERIFIED", "Editor", 10, (element("Search", control_type="Edit"),)
    )
    actions = FakeActionAdapter()
    runtime = ComputerUseRuntime(ScreenPerceptionRuntime(FakeScreenAdapter(snapshot)), actions)
    result = runtime.execute(
        ComputerActionRequest(
            ComputerActionKind.SET_TEXT,
            ComputerTarget(name="Search", control_type="Edit"),
            text="PANGU",
        )
    )
    assert result.state == ComputerUseState.UNVERIFIED
    assert actions.calls == [("set_text", "Search:PANGU")]
