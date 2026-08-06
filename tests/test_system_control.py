from pangu.capabilities import CapabilityCatalog, ToolSpecification
from pangu.contracts import Risk
from pangu.database import DatabaseService
from pangu.permissions import PermissionGrant, PermissionStore
from pangu.security import SafetyGateway
from pangu.system_control import (
    Display,
    FakeSystemControlAdapter,
    SystemControlRuntime,
    SystemErrorCode,
    SystemVerification,
)


def runtime(tmp_path, adapter):
    database = DatabaseService(tmp_path / "pangu.db")
    database.start()
    catalog = CapabilityCatalog()
    catalog.register(
        ToolSpecification(
            "system.control",
            "1",
            frozenset(
                {
                    "get_volume",
                    "set_volume",
                    "increase_volume",
                    "decrease_volume",
                    "get_mute_state",
                    "mute",
                    "unmute",
                    "toggle_mute",
                    "get_brightness",
                    "set_brightness",
                    "increase_brightness",
                    "decrease_brightness",
                }
            ),
            Risk.LOW,
            frozenset(),
        )
    )
    permissions = PermissionStore(
        tuple(
            PermissionGrant(scope, "default")
            for scope in (
                "system.audio.read",
                "system.audio.write",
                "system.brightness.read",
                "system.brightness.write",
            )
        )
    )
    return SystemControlRuntime(adapter, catalog, permissions, SafetyGateway(), database), database


def test_audio_clamps_and_verifies(tmp_path):
    service, database = runtime(tmp_path, FakeSystemControlAdapter(volume=50))
    assert service.audio("set_volume", 120).observed_value == 100
    assert service.audio("decrease_volume", 200).observed_value == 0
    assert service.audio("increase_volume").observed_value == 5
    assert database.audit_count() == 3
    database.stop()


def test_mute_and_stale_postcondition(tmp_path):
    adapter = FakeSystemControlAdapter(muted=False)
    service, database = runtime(tmp_path, adapter)
    assert service.audio("mute").verification_state == SystemVerification.VERIFIED
    adapter.apply_changes = False
    assert service.audio("unmute").verification_state == SystemVerification.UNVERIFIED
    database.stop()


def test_brightness_ambiguous_and_selector(tmp_path):
    adapter = FakeSystemControlAdapter(
        displays=[Display("display-1", "One", 20), Display("display-2", "Two", 30)]
    )
    service, database = runtime(tmp_path, adapter)
    assert (
        service.brightness("set_brightness", 60).verification_state == SystemVerification.AMBIGUOUS
    )
    assert service.brightness("set_brightness", 60, "display-2").observed_value == 60
    database.stop()


def test_audio_reads_do_not_mutate(tmp_path):
    adapter = FakeSystemControlAdapter(volume=31, muted=True)
    service, database = runtime(tmp_path, adapter)
    assert service.audio("get_volume").observed_value == 31
    assert service.audio("get_mute_state").observed_value is True
    assert (adapter.volume, adapter.muted) == (31, True)
    database.stop()


def test_toggle_stale_state_is_unverified_and_retryable(tmp_path):
    adapter = FakeSystemControlAdapter(muted=False)
    service, database = runtime(tmp_path, adapter)
    adapter.apply_changes = False
    result = service.audio("toggle_mute")
    assert result.verification_state == SystemVerification.UNVERIFIED
    assert result.retryable is True
    assert result.normalized_error == SystemErrorCode.POSTCONDITION_TIMEOUT
    database.stop()


def test_audio_unavailable_does_not_disable_brightness(tmp_path):
    adapter = FakeSystemControlAdapter()
    adapter.audio_fail = SystemErrorCode.NO_ACTIVE_AUDIO_ENDPOINT
    service, database = runtime(tmp_path, adapter)
    assert service.audio("get_volume").normalized_error == SystemErrorCode.NO_ACTIVE_AUDIO_ENDPOINT
    assert service.brightness("get_brightness").verification_state == SystemVerification.VERIFIED
    database.stop()


def test_brightness_unavailable_does_not_disable_audio(tmp_path):
    adapter = FakeSystemControlAdapter()
    adapter.brightness_fail = SystemErrorCode.NO_COMPATIBLE_DISPLAY
    service, database = runtime(tmp_path, adapter)
    assert service.audio("get_volume").verification_state == SystemVerification.VERIFIED
    assert (
        service.brightness("get_brightness").normalized_error
        == SystemErrorCode.NO_COMPATIBLE_DISPLAY
    )
    database.stop()


def test_brightness_stale_selector_and_postcondition(tmp_path):
    adapter = FakeSystemControlAdapter()
    service, database = runtime(tmp_path, adapter)
    assert (
        service.brightness("set_brightness", 40, "foreign").normalized_error
        == SystemErrorCode.NOT_FOUND
    )
    adapter.apply_changes = False
    result = service.brightness("set_brightness", 40, "display-1")
    assert result.verification_state == SystemVerification.UNVERIFIED
    assert result.normalized_error == SystemErrorCode.POSTCONDITION_TIMEOUT
    database.stop()
