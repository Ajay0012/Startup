from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse


class BrowserActionKind(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    READ = "read"
    BACK = "back"
    FORWARD = "forward"


class BrowserState(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class BrowserElement:
    element_id: str
    role: str
    name: str
    input_type: str = ""
    href: str | None = None
    visible: bool = True
    enabled: bool = True


@dataclass(frozen=True)
class BrowserSnapshot:
    url: str
    title: str
    text: str
    elements: tuple[BrowserElement, ...]
    verification_state: BrowserState
    untrusted_content: bool = True
    truncated: bool = False
    normalized_error: str | None = None


@dataclass(frozen=True)
class BrowserActionRequest:
    action: BrowserActionKind
    url: str | None = None
    target_name: str | None = None
    target_role: str | None = None
    text: str | None = None


@dataclass(frozen=True)
class BrowserActionResult:
    action: BrowserActionKind
    state: BrowserState
    message: str
    snapshot: BrowserSnapshot | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    normalized_error: str | None = None


class BrowserAdapter(Protocol):
    async def start(self) -> bool: ...
    async def stop(self) -> None: ...
    async def snapshot(self) -> BrowserSnapshot: ...
    async def navigate(self, url: str) -> bool: ...
    async def click(self, role: str, name: str) -> bool: ...
    async def fill(self, role: str, name: str, text: str) -> bool: ...
    async def back(self) -> bool: ...
    async def forward(self) -> bool: ...


class PlaywrightBrowserAdapter:
    """Persistent isolated Chromium context; no connection to the user's normal profile."""

    def __init__(self, profile_dir: Path, headless: bool = False, max_text_chars: int = 30_000) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.max_text_chars = max_text_chars
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None

    async def start(self) -> bool:
        if self._context is not None:
            return True
        try:
            async_api = import_module("playwright.async_api")
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = await async_api.async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                java_script_enabled=True,
                accept_downloads=False,
            )
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
            return True
        except (ImportError, ModuleNotFoundError, OSError, RuntimeError):
            await self.stop()
            return False

    async def stop(self) -> None:
        context, playwright = self._context, self._playwright
        self._page = None
        self._context = None
        self._playwright = None
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:  # noqa: BLE001
                pass

    async def snapshot(self) -> BrowserSnapshot:
        page = self._page
        if page is None:
            return BrowserSnapshot(
                "",
                "",
                "",
                (),
                BrowserState.UNAVAILABLE,
                normalized_error="BROWSER_NOT_STARTED",
            )
        try:
            title = await page.title()
            url = page.url
            body = await page.locator("body").inner_text(timeout=3000)
            truncated = len(body) > self.max_text_chars
            text = body[: self.max_text_chars]
            raw = await page.locator(
                "a,button,input,textarea,select,[role='button'],[role='link'],[role='textbox']"
            ).evaluate_all(
                """els => els.slice(0, 500).map((e,i) => ({
                    id: String(i), role: e.getAttribute('role') || e.tagName.toLowerCase(),
                    name: (e.getAttribute('aria-label') || e.innerText || e.value || e.name || '').trim(),
                    type: e.getAttribute('type') || '', href: e.href || null,
                    visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
                    enabled: !e.disabled
                }))"""
            )
            elements = tuple(
                BrowserElement(
                    str(item.get("id", "")),
                    str(item.get("role", ""))[:64],
                    str(item.get("name", ""))[:512],
                    str(item.get("type", ""))[:64],
                    str(item["href"]) if item.get("href") else None,
                    bool(item.get("visible", False)),
                    bool(item.get("enabled", False)),
                )
                for item in raw
                if isinstance(item, dict)
            )
            return BrowserSnapshot(url, title, text, elements, BrowserState.VERIFIED, True, truncated)
        except Exception:  # noqa: BLE001
            return BrowserSnapshot(
                str(getattr(page, "url", "")),
                "",
                "",
                (),
                BrowserState.UNVERIFIED,
                normalized_error="BROWSER_SNAPSHOT_FAILED",
            )

    async def navigate(self, url: str) -> bool:
        if self._page is None:
            return False
        try:
            response = await self._page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            return response is None or int(response.status) < 500
        except Exception:  # noqa: BLE001
            return False

    async def click(self, role: str, name: str) -> bool:
        if self._page is None:
            return False
        try:
            locator = self._page.get_by_role(role, name=name, exact=True)
            if await locator.count() != 1:
                return False
            await locator.click(timeout=5000)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def fill(self, role: str, name: str, text: str) -> bool:
        if self._page is None:
            return False
        try:
            locator = self._page.get_by_role(role, name=name, exact=True)
            if await locator.count() != 1:
                return False
            await locator.fill(text, timeout=5000)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def back(self) -> bool:
        if self._page is None:
            return False
        try:
            await self._page.go_back(wait_until="domcontentloaded", timeout=10_000)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def forward(self) -> bool:
        if self._page is None:
            return False
        try:
            await self._page.go_forward(wait_until="domcontentloaded", timeout=10_000)
            return True
        except Exception:  # noqa: BLE001
            return False


class BrowserRuntime:
    """Structured browser control with navigation and consequential-action guards."""

    _commit_terms = frozenset(
        {
            "buy",
            "purchase",
            "pay",
            "place order",
            "submit",
            "send",
            "confirm",
            "delete",
            "remove",
            "transfer",
            "withdraw",
            "apply",
            "book",
            "reserve",
            "subscribe",
            "publish",
            "post",
        }
    )
    _secret_types = frozenset({"password"})

    def __init__(self, adapter: BrowserAdapter) -> None:
        self.adapter = adapter
        self.started = False

    async def start(self) -> None:
        self.started = await self.adapter.start()

    async def stop(self) -> None:
        await self.adapter.stop()
        self.started = False

    @staticmethod
    def _host_is_private(host: str) -> bool:
        if host.casefold() in {"localhost", "localhost.localdomain"}:
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)

    @classmethod
    def allowed_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not cls._host_is_private(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )

    @classmethod
    def _consequential(cls, value: str) -> bool:
        lower = value.casefold()
        return any(term in lower for term in cls._commit_terms)

    async def snapshot(self) -> BrowserSnapshot:
        if not self.started:
            return BrowserSnapshot(
                "",
                "",
                "",
                (),
                BrowserState.UNAVAILABLE,
                normalized_error="BROWSER_NOT_STARTED",
            )
        return await self.adapter.snapshot()

    async def execute(self, request: BrowserActionRequest) -> BrowserActionResult:
        if not self.started:
            return BrowserActionResult(
                request.action,
                BrowserState.UNAVAILABLE,
                "Browser runtime is unavailable.",
                normalized_error="BROWSER_NOT_STARTED",
            )
        if request.action == BrowserActionKind.NAVIGATE:
            if not request.url or not self.allowed_url(request.url):
                return BrowserActionResult(
                    request.action,
                    BrowserState.DENIED,
                    "Navigation target is blocked by browser safety policy.",
                    normalized_error="NAVIGATION_BLOCKED",
                )
            ok = await self.adapter.navigate(request.url)
        elif request.action in {BrowserActionKind.CLICK, BrowserActionKind.FILL}:
            if not request.target_name or not request.target_role:
                return BrowserActionResult(
                    request.action,
                    BrowserState.DENIED,
                    "A unique structured browser target is required.",
                    normalized_error="TARGET_REQUIRED",
                )
            snapshot = await self.adapter.snapshot()
            matches = [
                item
                for item in snapshot.elements
                if item.visible
                and item.enabled
                and item.role.casefold() == request.target_role.casefold()
                and item.name.casefold() == request.target_name.casefold()
            ]
            if len(matches) != 1:
                return BrowserActionResult(
                    request.action,
                    BrowserState.DENIED,
                    "Browser target is missing or ambiguous.",
                    snapshot,
                    normalized_error="TARGET_AMBIGUOUS" if matches else "TARGET_NOT_FOUND",
                )
            target = matches[0]
            if target.input_type.casefold() in self._secret_types:
                return BrowserActionResult(
                    request.action,
                    BrowserState.DENIED,
                    "PANGU does not fill password fields through browser automation.",
                    snapshot,
                    normalized_error="PASSWORD_FIELD_BLOCKED",
                )
            if self._consequential(target.name):
                return BrowserActionResult(
                    request.action,
                    BrowserState.DENIED,
                    "This browser control may commit a consequential action and requires approval.",
                    snapshot,
                    normalized_error="CONSEQUENTIAL_CONTROL_REQUIRES_APPROVAL",
                )
            if request.action == BrowserActionKind.CLICK:
                ok = await self.adapter.click(target.role, target.name)
            else:
                if request.text is None or len(request.text) > 20_000:
                    return BrowserActionResult(
                        request.action,
                        BrowserState.DENIED,
                        "Browser text input is missing or too large.",
                        snapshot,
                        normalized_error="INVALID_TEXT_INPUT",
                    )
                ok = await self.adapter.fill(target.role, target.name, request.text)
        elif request.action == BrowserActionKind.BACK:
            ok = await self.adapter.back()
        elif request.action == BrowserActionKind.FORWARD:
            ok = await self.adapter.forward()
        elif request.action == BrowserActionKind.READ:
            current = await self.adapter.snapshot()
            return BrowserActionResult(
                request.action,
                current.verification_state,
                "Browser page captured as untrusted content.",
                current,
                {"untrusted_content": True},
                current.normalized_error,
            )
        else:
            return BrowserActionResult(
                request.action,
                BrowserState.DENIED,
                "Unsupported browser action.",
            )
        if not ok:
            return BrowserActionResult(
                request.action,
                BrowserState.FAILED,
                "Browser action failed.",
                normalized_error="BROWSER_ACTION_FAILED",
            )
        await asyncio.sleep(0.05)
        after = await self.adapter.snapshot()
        return BrowserActionResult(
            request.action,
            BrowserState.UNVERIFIED,
            "Browser action executed; semantic postcondition requires a task-specific verifier.",
            after,
            {"untrusted_content": True, "url": after.url},
            None if after.verification_state == BrowserState.VERIFIED else after.normalized_error,
        )
