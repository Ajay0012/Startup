from __future__ import annotations

from dataclasses import dataclass

import pytest

from pangu.browser import (
    BrowserActionKind,
    BrowserActionRequest,
    BrowserElement,
    BrowserRuntime,
    BrowserSnapshot,
    BrowserState,
)


@dataclass
class FakeBrowserAdapter:
    current: BrowserSnapshot
    started: bool = False

    async def start(self) -> bool:
        self.started = True
        return True

    async def stop(self) -> None:
        self.started = False

    async def snapshot(self) -> BrowserSnapshot:
        return self.current

    async def navigate(self, url: str) -> bool:
        self.current = BrowserSnapshot(url, "Page", "content", (), BrowserState.VERIFIED)
        return True

    async def click(self, role: str, name: str) -> bool:
        return True

    async def fill(self, role: str, name: str, text: str) -> bool:
        return True

    async def back(self) -> bool:
        return True

    async def forward(self) -> bool:
        return True


def snapshot(*elements: BrowserElement) -> BrowserSnapshot:
    return BrowserSnapshot(
        "https://example.com", "Example", "Untrusted page text", tuple(elements), BrowserState.VERIFIED
    )


def test_navigation_policy_blocks_local_and_script_urls() -> None:
    assert BrowserRuntime.allowed_url("https://example.com/path") is True
    assert BrowserRuntime.allowed_url("http://127.0.0.1:8000") is False
    assert BrowserRuntime.allowed_url("http://localhost:8000") is False
    assert BrowserRuntime.allowed_url("file:///c:/secret.txt") is False
    assert BrowserRuntime.allowed_url("javascript:alert(1)") is False
    assert BrowserRuntime.allowed_url("data:text/plain,secret") is False
    assert BrowserRuntime.allowed_url("https://user:pass@example.com") is False


@pytest.mark.asyncio
async def test_browser_blocks_password_and_commit_controls() -> None:
    adapter = FakeBrowserAdapter(
        snapshot(
            BrowserElement("1", "textbox", "Password", "password"),
            BrowserElement("2", "button", "Pay now"),
        )
    )
    runtime = BrowserRuntime(adapter)
    await runtime.start()
    try:
        password = await runtime.execute(
            BrowserActionRequest(
                BrowserActionKind.FILL,
                target_name="Password",
                target_role="textbox",
                text="secret",
            )
        )
        assert password.state == BrowserState.DENIED
        assert password.normalized_error == "PASSWORD_FIELD_BLOCKED"

        payment = await runtime.execute(
            BrowserActionRequest(
                BrowserActionKind.CLICK, target_name="Pay now", target_role="button"
            )
        )
        assert payment.state == BrowserState.DENIED
        assert payment.normalized_error == "CONSEQUENTIAL_CONTROL_REQUIRES_APPROVAL"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_browser_read_marks_web_content_untrusted() -> None:
    adapter = FakeBrowserAdapter(snapshot(BrowserElement("1", "button", "Search")))
    runtime = BrowserRuntime(adapter)
    await runtime.start()
    try:
        result = await runtime.execute(BrowserActionRequest(BrowserActionKind.READ))
        assert result.state == BrowserState.VERIFIED
        assert result.snapshot is not None and result.snapshot.untrusted_content is True
        assert result.evidence["untrusted_content"] is True
    finally:
        await runtime.stop()
