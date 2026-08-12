from __future__ import annotations

from .device_ecosystem import DeviceActionResult
from .phone_link import PhoneCapability, PhoneCommand, PhoneLinkRuntime


class PhoneLinkCallTransport:
    """DelegatedCallSession transport backed by the single PhoneLinkRuntime.

    Call placement and answer/end operations require a fresh device-auth lease. Speech is
    available only if the companion negotiated CALL_MEDIA (for example an app-owned VoIP
    media path). Normal carrier-call media is never assumed.
    """

    def __init__(self, link: PhoneLinkRuntime) -> None:
        self.link = link

    def start(self, target: str) -> DeviceActionResult:
        if not target.strip():
            return DeviceActionResult(
                False, "Invalid call target.", normalized_error="INVALID_CALL_TARGET"
            )
        try:
            lease = self.link.queue_command(
                PhoneCommand.PLACE_CALL,
                {"target": target.strip()},
                capability=PhoneCapability.PLACE_CALL,
                requires_device_auth=True,
            )
        except (RuntimeError, PermissionError) as error:
            return DeviceActionResult(
                False, "Phone call could not be started.", normalized_error=str(error)
            )
        return DeviceActionResult(
            True,
            "Phone call queued on the paired companion.",
            {"command_id": lease.command_id},
        )

    def say(self, text: str) -> DeviceActionResult:
        clean = " ".join(text.split())
        if not clean:
            return DeviceActionResult(False, "Nothing to say.", normalized_error="EMPTY_SPEECH")
        try:
            lease = self.link.queue_command(
                PhoneCommand.SPEAK,
                {"text": clean[:4000]},
                capability=PhoneCapability.CALL_MEDIA,
            )
        except (RuntimeError, PermissionError) as error:
            return DeviceActionResult(
                False,
                "Assistant call media is unavailable on this phone transport.",
                normalized_error=str(error),
            )
        return DeviceActionResult(
            True, "Assistant speech queued.", {"command_id": lease.command_id}
        )

    def pause(self) -> DeviceActionResult:
        try:
            lease = self.link.queue_command(
                PhoneCommand.PAUSE_SPEECH,
                {},
                capability=PhoneCapability.CALL_MEDIA,
            )
        except (RuntimeError, PermissionError) as error:
            return DeviceActionResult(
                False, "Call media pause unavailable.", normalized_error=str(error)
            )
        return DeviceActionResult(
            True, "Assistant call media paused.", {"command_id": lease.command_id}
        )

    def resume(self) -> DeviceActionResult:
        try:
            lease = self.link.queue_command(
                PhoneCommand.RESUME_SPEECH,
                {},
                capability=PhoneCapability.CALL_MEDIA,
            )
        except (RuntimeError, PermissionError) as error:
            return DeviceActionResult(
                False, "Call media resume unavailable.", normalized_error=str(error)
            )
        return DeviceActionResult(
            True, "Assistant call media resumed.", {"command_id": lease.command_id}
        )

    def end(self) -> DeviceActionResult:
        try:
            lease = self.link.queue_command(
                PhoneCommand.END_CALL,
                {},
                capability=PhoneCapability.END_CALL,
                requires_device_auth=True,
            )
        except (RuntimeError, PermissionError) as error:
            return DeviceActionResult(
                False, "Phone call could not be ended.", normalized_error=str(error)
            )
        return DeviceActionResult(
            True, "End-call command queued.", {"command_id": lease.command_id}
        )
