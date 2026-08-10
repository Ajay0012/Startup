from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module

from .speaker_identity import IdentityTrustEngine, SpeakerRole, TrustContext, TrustDecision


class StrongAuthState(StrEnum):
    VERIFIED = "VERIFIED"
    CANCELLED = "CANCELLED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class StrongAuthResult:
    state: StrongAuthState
    verified_at: float | None = None
    normalized_error: str | None = None


class WindowsHelloVerifier:
    """Optional Windows Hello / PIN consent boundary through WinRT.

    The provider is loaded lazily. No biometric template or credential is read by PANGU;
    Windows owns the authentication UI and returns only a verification result.
    """

    async def verify(self, message: str = "Verify your identity for PANGU") -> StrongAuthResult:
        try:
            module = import_module("winrt.windows.security.credentials.ui")
            verifier = getattr(module, "UserConsentVerifier")
            availability_enum = getattr(module, "UserConsentVerifierAvailability")
            result_enum = getattr(module, "UserConsentVerificationResult")
        except (ImportError, ModuleNotFoundError, AttributeError):
            return StrongAuthResult(
                StrongAuthState.UNAVAILABLE,
                normalized_error="WINDOWS_HELLO_BACKEND_UNAVAILABLE",
            )
        try:
            availability = await verifier.check_availability_async()
            available_values = {getattr(availability_enum, "AVAILABLE", object())}
            if availability not in available_values:
                return StrongAuthResult(
                    StrongAuthState.UNAVAILABLE,
                    normalized_error=f"WINDOWS_HELLO_{str(availability).upper()}",
                )
            result = await verifier.request_verification_async(message[:240])
            if result == getattr(result_enum, "VERIFIED", object()):
                return StrongAuthResult(StrongAuthState.VERIFIED, time.monotonic())
            cancelled = {
                getattr(result_enum, "CANCELED", object()),
                getattr(result_enum, "DEVICE_BUSY", object()),
            }
            if result in cancelled:
                return StrongAuthResult(
                    StrongAuthState.CANCELLED,
                    normalized_error="WINDOWS_HELLO_CANCELLED",
                )
            return StrongAuthResult(
                StrongAuthState.FAILED,
                normalized_error=f"WINDOWS_HELLO_{str(result).upper()}",
            )
        except (RuntimeError, OSError, ValueError):
            return StrongAuthResult(
                StrongAuthState.FAILED,
                normalized_error="WINDOWS_HELLO_VERIFICATION_FAILED",
            )


@dataclass(frozen=True)
class IdentitySecuritySnapshot:
    speaker: SpeakerRole
    windows_session_unlocked: bool
    trusted_device: bool
    local_presence: bool
    strong_auth_fresh: bool
    trust: TrustDecision


class ContextualIdentitySecurity:
    """One contextual trust decision with expiring strong-auth evidence."""

    def __init__(
        self,
        trust_engine: IdentityTrustEngine | None = None,
        hello: WindowsHelloVerifier | None = None,
        *,
        strong_auth_ttl_seconds: float = 180.0,
    ) -> None:
        if not 30 <= strong_auth_ttl_seconds <= 900:
            raise ValueError("strong auth TTL must be between 30 and 900 seconds")
        self.trust_engine = trust_engine or IdentityTrustEngine()
        self.hello = hello or WindowsHelloVerifier()
        self.strong_auth_ttl_seconds = strong_auth_ttl_seconds
        self._verified_at: float | None = None
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return (
            self._verified_at is not None
            and time.monotonic() - self._verified_at <= self.strong_auth_ttl_seconds
        )

    async def require_strong_auth(self, reason: str) -> StrongAuthResult:
        async with self._lock:
            if self._fresh():
                return StrongAuthResult(StrongAuthState.VERIFIED, self._verified_at)
            result = await self.hello.verify(reason)
            if result.state == StrongAuthState.VERIFIED:
                self._verified_at = result.verified_at or time.monotonic()
            return result

    def revoke_strong_auth(self) -> None:
        self._verified_at = None

    def assess(
        self,
        *,
        speaker: SpeakerRole,
        windows_session_unlocked: bool,
        trusted_device: bool,
        local_presence: bool,
        consequential: bool = False,
    ) -> IdentitySecuritySnapshot:
        fresh = self._fresh()
        decision = self.trust_engine.assess(
            TrustContext(
                speaker,
                windows_session_unlocked,
                trusted_device,
                local_presence,
                recent_strong_auth=fresh,
            ),
            consequential=consequential,
        )
        return IdentitySecuritySnapshot(
            speaker,
            windows_session_unlocked,
            trusted_device,
            local_presence,
            fresh,
            decision,
        )
