"""Verified Windows application discovery and control.

This module intentionally exposes typed observations instead of Win32/registry
exceptions.  The simulated adapter is opt-in and is never selected by the
production composition root.
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol, cast

from .approvals import ApprovalBinding, PersistentApprovalService
from .database import DatabaseService
from .permissions import PermissionStore
from .repositories import ApplicationCatalogRecord, ApplicationCatalogRepository


class ResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    STALE = "STALE"
    UNSUPPORTED = "UNSUPPORTED"


class VerificationState(StrEnum):
    REQUESTED = "REQUESTED"
    EXECUTED = "EXECUTED"
    OBSERVED = "OBSERVED"
    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    UNSUPPORTED = "UNSUPPORTED"


class ApplicationKind(StrEnum):
    USER_APPLICATION = "USER_APPLICATION"
    SYSTEM_COMPONENT = "SYSTEM_COMPONENT"
    ADMINISTRATIVE_TOOL = "ADMINISTRATIVE_TOOL"
    COMMAND_LINE_TOOL = "COMMAND_LINE_TOOL"
    BACKGROUND_PROCESS = "BACKGROUND_PROCESS"
    URI_HANDLER = "URI_HANDLER"
    UNKNOWN = "UNKNOWN"


_ADMINISTRATIVE = {
    "takeown.exe",
    "vssadmin.exe",
    "wbadmin.exe",
    "wusa.exe",
    "winrs.exe",
    "wsl.exe",
    "wslconfig.exe",
    "tpmvscmgr.exe",
}
_COMMAND_LINE = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "wscript.exe",
    "cscript.exe",
    "python.exe",
    "pythonw.exe",
    "node.exe",
    "taskkill.exe",
    "whoami.exe",
    "systeminfo.exe",
    "bash.exe",
    "msbuild.exe",
    "cl.exe",
    "csc.exe",
    "npm.cmd",
}
_SYSTEM_COMPONENTS = {"winlogon.exe", "wininit.exe", "userinit.exe"}
_BACKGROUND = {"msmpeng.exe", "securityhealthservice.exe", "pangu.exe", "pangu-session-agent.exe"}
_SERVICE_PROCESSES = {"wslservice.exe", "wlanext.exe", "svchost.exe", "services.exe"}


def classify_application(
    executable_name: str | None, install_source: str, uri_scheme: str | None = None
) -> tuple[ApplicationKind, bool, bool, bool, bool, str | None]:
    """Return a fail-closed classification for ordinary application control."""
    name = (executable_name or "").casefold()
    source = install_source.casefold()
    if uri_scheme:
        return ApplicationKind.URI_HANDLER, False, False, False, False, "URI handlers are not apps"
    if name in _SYSTEM_COMPONENTS:
        return ApplicationKind.SYSTEM_COMPONENT, False, False, False, True, "system component"
    if name in _BACKGROUND:
        return ApplicationKind.BACKGROUND_PROCESS, False, False, False, True, "background process"
    if name in _SERVICE_PROCESSES or source in {"running_process", "visible_window"}:
        return (
            ApplicationKind.BACKGROUND_PROCESS,
            False,
            False,
            False,
            True,
            "process-only evidence",
        )
    if name in _ADMINISTRATIVE:
        return ApplicationKind.ADMINISTRATIVE_TOOL, False, False, True, True, "administrative tool"
    if name in _COMMAND_LINE:
        return ApplicationKind.COMMAND_LINE_TOOL, False, False, False, True, "command-line tool"
    # A PATH-only record has no installation or shell identity proof.  Treat it
    # as unknown until a stronger source corroborates it.
    if install_source == "path":
        return ApplicationKind.UNKNOWN, False, False, False, False, "PATH-only executable"
    return ApplicationKind.USER_APPLICATION, True, True, False, False, None


def normalise_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def default_aliases(name: str) -> tuple[str, ...]:
    key = normalise_name(name)
    if "chrome" in key:
        return ("Chrome", "Google Chrome", "chrome browser")
    if key in {"code", "visual studio code"} or "visual studio code" in key:
        return ("VS Code", "Visual Studio Code", "Code")
    if key in {"notepad", "windows notepad"}:
        return ("Notepad", "Windows Notepad", "notepad.exe")
    return ()


def _window_public(window: WindowObservation) -> dict[str, object]:
    return {
        "handle": window.handle,
        "pid": window.pid,
        "title": window.title,
        "class_name": window.class_name,
        "minimized": window.minimized,
        "maximized": window.maximized,
        "foreground": window.foreground,
    }


@dataclass(frozen=True)
class ApplicationRecord:
    application_id: str
    display_name: str
    normalized_name: str
    aliases: tuple[str, ...] = ()
    executable_path: str | None = None
    executable_name: str | None = None
    launch_arguments: tuple[str, ...] = ()
    package_identity: str | None = None
    app_user_model_id: str | None = None
    uri_scheme: str | None = None
    install_source: str = "unknown"
    version: str | None = None
    process_names: tuple[str, ...] = ()
    window_classes: tuple[str, ...] = ()
    discovered_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Refresh evidence is intentionally volatile and must not alter record identity/equality.
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(UTC), compare=False)
    confidence: float = 0.5
    stale: bool = False
    source_evidence: tuple[dict[str, str], ...] = ()
    health_state: str = "HEALTHY"
    application_kind: ApplicationKind = ApplicationKind.UNKNOWN
    launch_eligible: bool = False
    control_eligible: bool = False
    requires_elevation: bool = False
    protected: bool = False
    exclusion_reason: str | None = None

    @staticmethod
    def create(name: str, **values: object) -> ApplicationRecord:
        normalized = normalise_name(name)
        identity = str(
            values.get("package_identity")
            or values.get("app_user_model_id")
            or values.get("executable_path")
            or values.get("executable_name")
            or normalized
        )
        identifier = hashlib.sha256(identity.casefold().encode()).hexdigest()[:32]
        payload = dict(values)
        executable_path = payload.get("executable_path")
        executable_name = cast(str | None, payload.pop("executable_name", None)) or (
            Path(str(executable_path)).name if executable_path else None
        )
        kind, launchable, controllable, elevation, protected, exclusion = classify_application(
            executable_name,
            cast(str, payload.get("install_source", "unknown")),
            cast(str | None, payload.get("uri_scheme")),
        )
        if re.search(
            r"visual studio.*(?:native tools|cross tools).*command prompt", name, re.IGNORECASE
        ):
            kind, launchable, controllable, elevation, protected, exclusion = (
                ApplicationKind.COMMAND_LINE_TOOL,
                False,
                False,
                False,
                True,
                "developer command prompt",
            )
        return ApplicationRecord(
            identifier,
            name,
            normalized,
            aliases=tuple(
                sorted(
                    set(default_aliases(name))
                    | set(cast(tuple[str, ...], payload.get("aliases", ())))
                )
            ),
            executable_path=cast(str | None, payload.get("executable_path")),
            executable_name=executable_name,
            launch_arguments=tuple(cast(tuple[str, ...], payload.get("launch_arguments", ()))),
            package_identity=cast(str | None, payload.get("package_identity")),
            app_user_model_id=cast(str | None, payload.get("app_user_model_id")),
            uri_scheme=cast(str | None, payload.get("uri_scheme")),
            install_source=cast(str, payload.get("install_source", "unknown")),
            version=cast(str | None, payload.get("version")),
            process_names=tuple(cast(tuple[str, ...], payload.get("process_names", ()))),
            window_classes=tuple(cast(tuple[str, ...], payload.get("window_classes", ()))),
            confidence=cast(float, payload.get("confidence", 0.5)),
            stale=cast(bool, payload.get("stale", False)),
            source_evidence=tuple(
                cast(tuple[dict[str, str], ...], payload.get("source_evidence", ()))
            ),
            application_kind=cast(ApplicationKind, payload.get("application_kind", kind)),
            launch_eligible=cast(bool, payload.get("launch_eligible", launchable)),
            control_eligible=cast(bool, payload.get("control_eligible", controllable)),
            requires_elevation=cast(bool, payload.get("requires_elevation", elevation)),
            protected=cast(bool, payload.get("protected", protected)),
            exclusion_reason=cast(str | None, payload.get("exclusion_reason", exclusion)),
        )

    def public(self) -> dict[str, object]:
        return {
            "application_id": self.application_id,
            "display_name": self.display_name,
            "normalized_name": self.normalized_name,
            "aliases": self.aliases,
            "executable_name": self.executable_name,
            "package_identity": self.package_identity,
            "app_user_model_id": self.app_user_model_id,
            "install_source": self.install_source,
            "version": self.version,
            "process_names": self.process_names,
            "confidence": self.confidence,
            "stale": self.stale,
            "health_state": self.health_state,
            "application_kind": self.application_kind,
            "launch_eligible": self.launch_eligible,
            "control_eligible": self.control_eligible,
            "requires_elevation": self.requires_elevation,
            "protected": self.protected,
            "exclusion_reason": self.exclusion_reason,
        }


@dataclass(frozen=True)
class ProcessObservation:
    pid: int
    name: str
    executable_path: str | None = None
    created_at: float | None = None


@dataclass(frozen=True)
class WindowObservation:
    handle: int
    pid: int
    title: str = ""
    class_name: str = ""
    minimized: bool = False
    maximized: bool = False
    foreground: bool = False


@dataclass(frozen=True)
class AdapterResult:
    supported: bool
    succeeded: bool
    error: str | None = None
    pid: int | None = None


class WindowsApplicationAdapter(Protocol):
    def discover(self) -> list[ApplicationRecord]: ...
    def processes(self) -> list[ProcessObservation]: ...
    def windows(self) -> list[WindowObservation]: ...
    def launch(self, app: ApplicationRecord) -> AdapterResult: ...
    def activate(self, app: ApplicationRecord) -> AdapterResult: ...
    def window_action(self, handle: int, action: str) -> AdapterResult: ...
    def graceful_close(self, handle: int) -> AdapterResult: ...
    def terminate(self, pid: int) -> AdapterResult: ...


class RealWindowsApplicationAdapter:
    """Windows-only boundary. Registry is read directly; shell invocation is absent."""

    _blocked: ClassVar[set[str]] = (
        _COMMAND_LINE | _ADMINISTRATIVE | _SYSTEM_COMPONENTS | _BACKGROUND
    )

    def _supported(self) -> bool:
        return platform.system() == "Windows"

    def discover(self) -> list[ApplicationRecord]:
        if not self._supported():
            return []
        records: list[ApplicationRecord] = []
        records.extend(self._start_menu())
        records.extend(self._app_paths())
        records.extend(self._uninstall_entries())
        records.extend(self._path_apps())
        records.extend(self._appx_packages())
        records.extend(self._uri_schemes())
        visible = {window.pid: window for window in self.windows()}
        for process in self.processes():
            window = visible.get(process.pid)
            records.append(
                ApplicationRecord.create(
                    window.title or Path(process.name).stem if window else Path(process.name).stem,
                    executable_path=process.executable_path,
                    executable_name=process.name,
                    process_names=(process.name,),
                    window_classes=(window.class_name,) if window else (),
                    install_source="visible_window" if window else "running_process",
                    confidence=0.65,
                    source_evidence=(
                        {"source": "visible_window" if window else "running_process"},
                    ),
                )
            )
        return records

    def _uninstall_entries(self) -> list[ApplicationRecord]:
        import winreg

        results: list[ApplicationRecord] = []
        bases = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
        views = (0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY)
        key_name = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        for base in bases:
            for view in views:
                try:
                    root = winreg.OpenKey(base, key_name, 0, winreg.KEY_READ | view)
                    for index in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            child = winreg.OpenKey(root, winreg.EnumKey(root, index))
                            title, _ = winreg.QueryValueEx(child, "DisplayName")
                            version = self._registry_value(child, "DisplayVersion")
                            location = self._registry_value(child, "InstallLocation")
                            executable = self._first_executable(str(location)) if location else None
                            results.append(
                                ApplicationRecord.create(
                                    str(title),
                                    executable_path=executable,
                                    install_source="registry_uninstall",
                                    version=str(version) if version else None,
                                    confidence=0.55,
                                    source_evidence=({"source": "registry_uninstall"},),
                                )
                            )
                        except OSError:
                            continue
                except OSError:
                    continue
        return results

    @staticmethod
    def _registry_value(key: object, name: str) -> object | None:
        import winreg

        try:
            return cast(object, winreg.QueryValueEx(key, name)[0])  # type: ignore[arg-type]
        except OSError:
            return None

    @staticmethod
    def _first_executable(location: str) -> str | None:
        path = Path(location)
        if not path.is_dir():
            return None
        try:
            return str(next(path.glob("*.exe")))
        except StopIteration:
            return None

    def _start_menu(self) -> list[ApplicationRecord]:
        # .lnk target resolution needs COM; do not infer targets when unavailable.
        roots = [
            Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        ]
        return [
            ApplicationRecord.create(
                link.stem,
                executable_path=self._resolve_shortcut(link),
                executable_name=Path(self._resolve_shortcut(link) or link.name).name,
                install_source="start_menu",
                confidence=0.6,
                source_evidence=({"source": "start_menu", "title": link.stem},),
            )
            for root in roots
            if root.exists()
            for link in root.rglob("*.lnk")
        ]

    def _resolve_shortcut(self, link: Path) -> str | None:
        """Use the fixed Shell.Application COM script; the path is an argument, never source text."""
        if not self._supported():
            return None
        script = "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($args[0]);[Console]::Write($s.TargetPath)"
        try:
            value = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script, str(link)],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            ).stdout.strip()
            return value if value.lower().endswith(".exe") and Path(value).is_file() else None
        except (OSError, subprocess.SubprocessError):
            return None

    def _app_paths(self) -> list[ApplicationRecord]:
        import winreg

        found: list[ApplicationRecord] = []
        for view in (0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                root = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
                    0,
                    winreg.KEY_READ | view,
                )
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(root, index)
                        index += 1
                    except OSError:
                        break
                    try:
                        child = winreg.OpenKey(root, name)
                        path, _ = winreg.QueryValueEx(child, "")
                        if str(path).lower().endswith(".exe"):
                            found.append(
                                ApplicationRecord.create(
                                    Path(name).stem,
                                    executable_path=str(path),
                                    executable_name=name,
                                    install_source="registry_app_paths",
                                    confidence=0.9,
                                    source_evidence=({"source": "registry_app_paths"},),
                                )
                            )
                    except OSError:
                        continue
            except OSError:
                continue
        return found

    def _path_apps(self) -> list[ApplicationRecord]:
        names = {"notepad.exe", "chrome.exe", "code.exe"}
        for item in os.environ.get("PATH", "").split(os.pathsep):
            try:
                names.update(path.name for path in Path(item).glob("*.exe"))
            except OSError:
                continue
        return [
            ApplicationRecord.create(
                Path(name).stem,
                executable_path=path,
                executable_name=name,
                install_source="path",
                confidence=0.55,
                source_evidence=({"source": "path"},),
            )
            for name in names
            if (path := shutil.which(name))
        ]

    def _appx_packages(self) -> list[ApplicationRecord]:
        # Get-StartApps is a bounded Windows facility without a usable stdlib API.
        if not self._supported():
            return []
        try:
            output = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Get-StartApps | Select-Object -Property Name,AppID | ConvertTo-Csv -NoTypeInformation",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            ).stdout
            return [
                ApplicationRecord.create(
                    row["Name"],
                    app_user_model_id=row["AppID"],
                    package_identity=row["AppID"].split("!")[0],
                    install_source="appx",
                    confidence=0.8,
                    source_evidence=({"source": "appx"},),
                )
                for row in csv.DictReader(output.splitlines())
                if row.get("Name") and row.get("AppID")
            ]
        except (OSError, subprocess.SubprocessError):
            return []

    def _uri_schemes(self) -> list[ApplicationRecord]:
        import winreg

        if not self._supported():
            return []
        result: list[ApplicationRecord] = []
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_CLASSES_ROOT):
            try:
                root = winreg.OpenKey(hive, "")
                for index in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        scheme = winreg.EnumKey(root, index)
                        child = winreg.OpenKey(root, scheme)
                        if self._registry_value(child, "URL Protocol") is not None and re.fullmatch(
                            r"[a-z][a-z0-9+.-]{1,31}", scheme, re.IGNORECASE
                        ):
                            result.append(
                                ApplicationRecord.create(
                                    scheme,
                                    uri_scheme=scheme,
                                    install_source="uri_scheme",
                                    confidence=0.4,
                                    source_evidence=({"source": "uri_scheme"},),
                                )
                            )
                    except OSError:
                        continue
            except OSError:
                continue
        return result

    def processes(self) -> list[ProcessObservation]:
        if not self._supported():
            return []
        # psutil exposes the actual Process.pid and creation time; do not infer a
        # PID from tasklist CSV field positions (the session id is not a PID).
        try:
            import psutil  # type: ignore[import-untyped]

            return [
                ProcessObservation(
                    process.pid,
                    process.info["name"] or "",
                    process.info["exe"],
                    process.info["create_time"],
                )
                for process in psutil.process_iter(["name", "exe", "create_time"])
                if isinstance(process.pid, int) and process.pid > 0
            ]
        except (OSError, ImportError):
            return []

    def windows(self) -> list[WindowObservation]:
        if not self._supported():
            return []
        user32 = ctypes.windll.user32
        found: list[WindowObservation] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        foreground = user32.GetForegroundWindow()

        def visit(handle: int, _: int) -> bool:
            if not user32.IsWindowVisible(handle):
                return True
            length = user32.GetWindowTextLengthW(handle)
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(handle, title, length + 1)
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(handle, class_name, 256)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            # Ignore invisible helper/tool windows, retaining titled or classed top-level windows only.
            if title.value or class_name.value:
                found.append(
                    WindowObservation(
                        int(handle),
                        int(pid.value),
                        title.value[:512],
                        class_name.value[:256],
                        bool(user32.IsIconic(handle)),
                        bool(user32.IsZoomed(handle)),
                        int(handle) == int(foreground),
                    )
                )
            return True

        try:
            user32.EnumWindows(callback_type(visit), 0)
            return found
        except (OSError, AttributeError):
            return []

    def launch(self, app: ApplicationRecord) -> AdapterResult:
        if not self._supported():
            return AdapterResult(False, False, "unsupported platform")
        if not app.executable_path or Path(app.executable_path).name.casefold() in self._blocked:
            return AdapterResult(True, False, "unsafe or missing executable")
        try:
            return AdapterResult(
                True, True, pid=subprocess.Popen([app.executable_path, *app.launch_arguments]).pid
            )
        except OSError:
            return AdapterResult(True, False, "launch failed")

    def activate(self, app: ApplicationRecord) -> AdapterResult:
        if not self._supported():
            return AdapterResult(False, False, "APPX_ACTIVATION_UNAVAILABLE")
        app_id = app.app_user_model_id or ""
        if not re.fullmatch(r"[A-Za-z0-9._-]+![A-Za-z0-9._-]+", app_id):
            return AdapterResult(True, False, "APPX_ACTIVATION_UNAVAILABLE")
        activated = self._activate_application_manager(app_id)
        if activated is not None:
            return activated
        try:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
            return AdapterResult(True, True)
        except OSError:
            return self._trusted_executable_fallback(app)

    @staticmethod
    def _guid(value: str) -> Any:
        raw = uuid.UUID(value).bytes_le
        return (ctypes.c_byte * 16).from_buffer_copy(raw)

    def _activate_application_manager(self, app_id: str) -> AdapterResult | None:
        """Use IApplicationActivationManager when the Windows COM server is available."""
        try:
            ole32 = ctypes.OleDLL("ole32")
            initialized = ole32.CoInitializeEx(None, 2) in (0, 1)
            manager = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                ctypes.byref(self._guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")),
                None,
                4,
                ctypes.byref(self._guid("2E941141-7F97-4756-BA1D-9DECDE894A3D")),
                ctypes.byref(manager),
            )
            if hr < 0 or not manager.value:
                return None
            try:
                vtable = ctypes.cast(
                    manager, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
                ).contents
                activate = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.c_wchar_p,
                    ctypes.c_wchar_p,
                    ctypes.c_uint,
                    ctypes.POINTER(ctypes.c_uint),
                )(vtable[3])
                pid = ctypes.c_uint()
                result = activate(manager, app_id, None, 0, ctypes.byref(pid))
                return AdapterResult(
                    True,
                    result >= 0,
                    "APPX_ACTIVATION_FAILED" if result < 0 else None,
                    pid.value or None,
                )
            finally:
                release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
                release(manager)
                if initialized:
                    ole32.CoUninitialize()
        except (OSError, AttributeError):
            return None

    def _trusted_executable_fallback(self, app: ApplicationRecord) -> AdapterResult:
        if app.executable_path and app.install_source in {"start_menu", "registry_app_paths"}:
            return self.launch(app)
        if app.package_identity and "microsoft.windowsnotepad" in app.package_identity.casefold():
            path = Path(os.environ.get("WINDIR", r"C:\\Windows")) / "System32" / "notepad.exe"
            if path.is_file():
                try:
                    return AdapterResult(True, True, pid=subprocess.Popen([str(path)]).pid)
                except OSError:
                    pass
        return AdapterResult(True, False, "EXECUTABLE_FALLBACK_FAILED")

    def window_action(self, handle: int, action: str) -> AdapterResult:
        if not self._supported():
            return AdapterResult(False, False, "unsupported platform")
        commands = {"minimize": 6, "maximize": 3, "restore": 9}
        try:
            user32 = ctypes.windll.user32
            if action == "focus":
                user32.ShowWindow(handle, 9)
                return AdapterResult(
                    True,
                    bool(user32.SetForegroundWindow(handle)),
                    "foreground restricted" if user32.GetForegroundWindow() != handle else None,
                )
            if action not in commands:
                return AdapterResult(True, False, "unknown window action")
            return AdapterResult(True, bool(user32.ShowWindow(handle, commands[action])))
        except (OSError, AttributeError):
            return AdapterResult(True, False, "window action failed")

    def graceful_close(self, handle: int) -> AdapterResult:
        if not self._supported():
            return AdapterResult(False, False, "unsupported platform")
        try:
            return AdapterResult(
                True, bool(ctypes.windll.user32.PostMessageW(handle, 0x0010, 0, 0))
            )
        except (OSError, AttributeError):
            return AdapterResult(True, False, "graceful close failed")

    def terminate(self, pid: int) -> AdapterResult:
        if not self._supported():
            return AdapterResult(False, False, "unsupported platform")
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
            if not handle:
                return AdapterResult(True, False, "process access denied")
            try:
                return AdapterResult(True, bool(ctypes.windll.kernel32.TerminateProcess(handle, 1)))
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except (OSError, AttributeError):
            return AdapterResult(True, False, "termination failed")


class SimulatedWindowsApplicationAdapter:
    def __init__(self, applications: list[ApplicationRecord] | None = None) -> None:
        self.records = applications or []
        self._next_pid = 4000
        self._processes: list[ProcessObservation] = []
        self._windows: list[WindowObservation] = []

    def discover(self) -> list[ApplicationRecord]:
        return self.records.copy()

    def processes(self) -> list[ProcessObservation]:
        return self._processes.copy()

    def windows(self) -> list[WindowObservation]:
        return self._windows.copy()

    def launch(self, app: ApplicationRecord) -> AdapterResult:
        self._next_pid += 1
        pid = self._next_pid
        name = app.executable_name or (
            "notepad.exe"
            if app.package_identity
            and "microsoft.windowsnotepad" in app.package_identity.casefold()
            else app.display_name + ".exe"
        )
        self._processes.append(ProcessObservation(pid, name, app.executable_path))
        self._windows.append(WindowObservation(pid, pid, app.display_name))
        return AdapterResult(True, True, pid=pid)

    def activate(self, app: ApplicationRecord) -> AdapterResult:
        return self.launch(app)

    def window_action(self, handle: int, action: str) -> AdapterResult:
        for index, window in enumerate(self._windows):
            if window.handle == handle:
                self._windows[index] = (
                    replace(
                        window,
                        minimized=action == "minimize",
                        maximized=action == "maximize",
                        foreground=action == "focus",
                    )
                    if action != "restore"
                    else replace(window, minimized=False, maximized=False)
                )
                return AdapterResult(True, True)
        return AdapterResult(True, False, "window not found")

    def graceful_close(self, handle: int) -> AdapterResult:
        self._windows = [window for window in self._windows if window.handle != handle]
        return AdapterResult(True, True)

    def terminate(self, pid: int) -> AdapterResult:
        self._processes = [item for item in self._processes if item.pid != pid]
        self._windows = [item for item in self._windows if item.pid != pid]
        return AdapterResult(True, True)


class ApplicationCatalog:
    def __init__(self, database: DatabaseService, adapter: WindowsApplicationAdapter) -> None:
        self.db = database
        self.adapter = adapter
        self._records: dict[str, ApplicationRecord] = {}
        self._hydrated = False
        self._empty_refresh_attempted = False

    def load(self) -> list[ApplicationRecord]:
        """Hydrate durable catalog state for a newly-created runtime container."""
        with self.db.transaction() as session:
            persisted = ApplicationCatalogRepository(session).list()
        self._records = {item.application_id: self._from_persisted(item) for item in persisted}
        self._hydrated = True
        return self.list(include_non_user=True)

    def prepare_for_resolution(self) -> None:
        if not self._hydrated:
            self.load()
        if not self._records and not self._empty_refresh_attempted:
            self._empty_refresh_attempted = True
            self.refresh()

    @staticmethod
    def _from_persisted(item: ApplicationCatalogRecord) -> ApplicationRecord:
        body = item.body
        return ApplicationRecord.create(
            item.display_name,
            executable_path=body.get("executable_path"),
            executable_name=body.get("executable_name"),
            launch_arguments=tuple(cast(list[str], body.get("launch_arguments", []))),
            package_identity=body.get("package_identity"),
            app_user_model_id=body.get("app_user_model_id"),
            uri_scheme=body.get("uri_scheme"),
            install_source=body.get("install_source", "unknown"),
            version=body.get("version"),
            process_names=tuple(cast(list[str], body.get("process_names", []))),
            window_classes=tuple(cast(list[str], body.get("window_classes", []))),
            aliases=tuple(cast(list[str], body.get("aliases", []))),
            source_evidence=tuple(cast(list[dict[str, str]], body.get("source_evidence", []))),
            health_state=body.get("health_state", "HEALTHY"),
            application_kind=ApplicationKind(cast(str, body.get("application_kind", "UNKNOWN"))),
            launch_eligible=body.get("launch_eligible", False),
            control_eligible=body.get("control_eligible", False),
            requires_elevation=body.get("requires_elevation", False),
            protected=body.get("protected", False),
            exclusion_reason=body.get("exclusion_reason"),
            stale=item.stale,
            confidence=0.5,
        )

    def refresh(self) -> list[ApplicationRecord]:
        discovered = self.adapter.discover()
        self._hydrated = True
        grouped: list[list[ApplicationRecord]] = []
        for record in discovered:
            matches = [group for group in grouped if self._same_application(record, group[0])]
            if matches:
                matches[0].append(record)
            else:
                grouped.append([record])
        now = datetime.now(UTC)
        self._records = {}
        precedence = {
            "start_menu": 0,
            "appx": 1,
            "registry_app_paths": 2,
            "registry_uninstall": 3,
            "visible_window": 4,
            "running_process": 5,
            "uri_scheme": 6,
            "path": 7,
        }
        for group in grouped:
            first = min(group, key=lambda item: precedence.get(item.install_source, 99))
            aliases = tuple(
                sorted(
                    {x.display_name for x in group} | set().union(*(set(x.aliases) for x in group))
                )
            )
            merged = replace(
                first,
                executable_path=next((x.executable_path for x in group if x.executable_path), None),
                executable_name=next((x.executable_name for x in group if x.executable_name), None),
                process_names=tuple(sorted(set().union(*(set(x.process_names) for x in group)))),
                aliases=aliases,
                source_evidence=tuple(e for x in group for e in x.source_evidence),
                last_seen_at=now,
                confidence=max(x.confidence for x in group),
                launch_eligible=any(x.launch_eligible for x in group),
                control_eligible=any(x.control_eligible for x in group),
            )
            self._records[merged.application_id] = merged
        with self.db.transaction() as session:
            repo = ApplicationCatalogRepository(session)
            for record in self._records.values():
                repo.upsert(ApplicationCatalogRecord.from_application(record))
        return self.list(include_non_user=True)

    @staticmethod
    def _same_application(left: ApplicationRecord, right: ApplicationRecord) -> bool:
        """Merge only on concrete identity, never on a name by itself."""
        left_ids = {
            x.casefold()
            for x in (left.executable_path, left.app_user_model_id, left.package_identity)
            if x
        }
        right_ids = {
            x.casefold()
            for x in (right.executable_path, right.app_user_model_id, right.package_identity)
            if x
        }
        if left_ids & right_ids:
            return True
        if (
            left.normalized_name == right.normalized_name
            and left.normalized_name in {"notepad", "windows notepad"}
            and {x.casefold() for x in (left.executable_name, right.executable_name) if x}
            & {"notepad.exe"}
            and any(
                "microsoft.windowsnotepad" in x.casefold()
                for x in (
                    left.package_identity,
                    right.package_identity,
                    left.app_user_model_id,
                    right.app_user_model_id,
                )
                if x
            )
        ):
            return True
        # An executable filename is useful only with a matching display identity.
        return bool(
            left.executable_name
            and right.executable_name
            and left.executable_name.casefold() == right.executable_name.casefold()
            and left.normalized_name == right.normalized_name
        )

    def list(self, include_non_user: bool = False) -> list[ApplicationRecord]:
        records: list[ApplicationRecord] = list(self._records.values())
        if not include_non_user:
            records = [x for x in records if x.application_kind == ApplicationKind.USER_APPLICATION]
        return sorted(records, key=lambda item: item.display_name)

    def get(self, app_id: str) -> ApplicationRecord | None:
        return self._records.get(app_id)

    def add_alias(self, app_id: str, alias: str) -> bool:
        record = self.get(app_id)
        if not record:
            return False
        self._records[app_id] = replace(
            record, aliases=tuple(sorted(set(record.aliases) | {alias}))
        )
        return True


@dataclass(frozen=True)
class ResolutionResult:
    status: ResolutionStatus
    selected_application: ApplicationRecord | None = None
    candidates: tuple[ApplicationRecord, ...] = ()
    confidence: float = 0
    matched_evidence: tuple[str, ...] = ()
    ambiguity_reason: str | None = None
    clarification_options: tuple[str, ...] = ()

    def public(self) -> dict[str, object]:
        return {
            "status": self.status,
            "selected_application": (
                self.selected_application.public() if self.selected_application else None
            ),
            "candidates": [item.public() for item in self.candidates],
            "confidence": self.confidence,
            "matched_evidence": self.matched_evidence,
            "ambiguity_reason": self.ambiguity_reason,
            "clarification_options": self.clarification_options,
        }


class ApplicationResolver:
    def __init__(self, catalog: ApplicationCatalog) -> None:
        self.catalog = catalog

    def resolve(self, query: str) -> ResolutionResult:
        self.catalog.prepare_for_resolution()
        key = normalise_name(query)
        scored = []
        for app in self.catalog.list():
            signals = [
                app.normalized_name,
                normalise_name(app.executable_name or ""),
                *(normalise_name(x) for x in app.aliases),
                *(normalise_name(x) for x in app.process_names),
            ]
            score = (
                1.0
                if key in signals
                else (
                    0.75 if key and any(key in signal or signal in key for signal in signals) else 0
                )
            )
            if score:
                scored.append((score, app))
        if not scored:
            return ResolutionResult(ResolutionStatus.NOT_FOUND)
        scored.sort(key=lambda item: item[0], reverse=True)
        best = scored[0]
        tied = tuple(app for score, app in scored if score >= best[0] - 0.05)
        if len(tied) > 1:
            return ResolutionResult(
                ResolutionStatus.AMBIGUOUS,
                candidates=tied,
                confidence=best[0],
                ambiguity_reason="multiple plausible applications",
                clarification_options=tuple(x.display_name for x in tied),
            )
        if best[1].stale:
            return ResolutionResult(ResolutionStatus.STALE, best[1], confidence=best[0])
        return ResolutionResult(
            ResolutionStatus.RESOLVED,
            best[1],
            confidence=best[0],
            matched_evidence=("catalog name/alias",),
        )


@dataclass(frozen=True)
class ApplicationOperationResult:
    operation: str
    requested_target: str
    resolved_application_id: str | None
    requested_outcome: str
    observed_outcome: str
    verification_state: VerificationState
    confidence: float
    evidence: dict[str, object] = field(default_factory=dict)
    remaining_uncertainty: str | None = None
    retryable: bool = False
    rollback_information: str | None = None
    normalized_error: str | None = None


@dataclass(frozen=True)
class ApplicationWindowsResult:
    status: ResolutionStatus
    application_id: str | None = None
    windows: tuple[WindowObservation, ...] = ()
    detail: str = ""

    def public(self) -> dict[str, object]:
        return {
            "status": self.status,
            "application_id": self.application_id,
            "windows": [
                {
                    "handle": item.handle,
                    "pid": item.pid,
                    "title": item.title,
                    "class_name": item.class_name,
                    "minimized": item.minimized,
                    "maximized": item.maximized,
                    "foreground": item.foreground,
                }
                for item in self.windows
            ],
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ApplicationObservation:
    processes: tuple[ProcessObservation, ...] = ()
    windows: tuple[WindowObservation, ...] = ()


class ApplicationControlRuntime:
    def __init__(
        self,
        catalog: ApplicationCatalog,
        resolver: ApplicationResolver,
        adapter: WindowsApplicationAdapter,
        permissions: PermissionStore,
        approvals: PersistentApprovalService,
    ) -> None:
        self.catalog, self.resolver, self.adapter, self.permissions, self.approvals = (
            catalog,
            resolver,
            adapter,
            permissions,
            approvals,
        )

    def resolve(self, name: str) -> ResolutionResult:
        return self.resolver.resolve(name)

    def discover(self) -> list[ApplicationRecord]:
        return self.catalog.refresh()

    @staticmethod
    def _process_names(app: ApplicationRecord) -> set[str]:
        return {
            normalise_name(item)
            for item in (*app.process_names, app.executable_name or "", *app.aliases)
            if item
        }

    def _matching_processes(self, app: ApplicationRecord) -> list[ProcessObservation]:
        names = self._process_names(app)
        return [p for p in self.adapter.processes() if normalise_name(p.name) in names]

    def observe(self, app: ApplicationRecord) -> ApplicationObservation:
        processes = tuple(self._matching_processes(app))
        pids = {process.pid for process in processes if process.pid > 0}
        return ApplicationObservation(
            processes, tuple(x for x in self.adapter.windows() if x.pid in pids)
        )

    def operate(
        self,
        operation: str,
        name: str,
        approval_token: str | None = None,
        actor: str = "default",
        window_handle: int | None = None,
    ) -> ApplicationOperationResult:
        resolution = self.resolve(name)
        # A stale executable is never launched. One bounded rediscovery may repair
        # identity evidence (App Paths, shortcuts, AppX, PATH, process metadata).
        if resolution.status == ResolutionStatus.STALE:
            self.catalog.refresh()
            resolution = self.resolve(name)
        if (
            resolution.status != ResolutionStatus.RESOLVED
            or resolution.selected_application is None
        ):
            return ApplicationOperationResult(
                operation,
                name,
                None,
                operation,
                resolution.status,
                VerificationState.UNSUPPORTED
                if resolution.status == ResolutionStatus.UNSUPPORTED
                else VerificationState.DENIED,
                0,
                normalized_error=resolution.status,
            )
        app = resolution.selected_application
        if operation in {"open", "focus", "close", "restart"} and (
            not app.launch_eligible
            or app.protected
            or app.application_kind != ApplicationKind.USER_APPLICATION
        ):
            return ApplicationOperationResult(
                operation,
                name,
                app.application_id,
                operation,
                "prohibited",
                VerificationState.DENIED,
                0,
                normalized_error=app.exclusion_reason or "not launchable",
            )
        scope = f"application.control:{operation}"
        protected = {
            "msmpeng.exe",
            "securityhealthservice.exe",
            "pangu.exe",
            "pangu-session-agent.exe",
        }
        if (
            operation == "terminate"
            and (
                {x.casefold() for x in app.process_names}
                | {str(app.executable_name or "").casefold()}
            )
            & protected
        ):
            return ApplicationOperationResult(
                operation,
                name,
                app.application_id,
                operation,
                "prohibited",
                VerificationState.DENIED,
                0,
                normalized_error="protected application",
            )
        if operation == "status":
            snapshot = self.observe(app)
            processes = snapshot.processes
            return ApplicationOperationResult(
                operation,
                name,
                app.application_id,
                "running state",
                "running" if processes else "not running",
                VerificationState.VERIFIED,
                resolution.confidence,
                {
                    "process_ids": [p.pid for p in processes],
                    "visible_window_count": len(snapshot.windows),
                    "window_owner_process_ids": sorted({item.pid for item in snapshot.windows}),
                    "running_without_visible_windows": bool(processes and not snapshot.windows),
                },
            )
        if operation == "terminate":
            binding = ApprovalBinding(
                actor,
                "application",
                "1.0.0",
                operation,
                {"application_id": app.application_id},
                app.application_id,
                "HIGH_RISK",
                frozenset({scope}),
                None,
                "local",
                datetime.now(UTC),
            )
            if not approval_token or self.approvals.consume(approval_token, binding) is not None:
                return ApplicationOperationResult(
                    operation,
                    name,
                    app.application_id,
                    operation,
                    "approval required",
                    VerificationState.DENIED,
                    0,
                    normalized_error="exact approval required",
                )
        windows = list(self.observe(app).windows)
        if (
            operation in {"focus", "minimize", "maximize", "restore", "close"}
            and window_handle is not None
        ):
            selected = next((window for window in windows if window.handle == window_handle), None)
            if selected is None:
                return ApplicationOperationResult(
                    operation,
                    name,
                    app.application_id,
                    operation,
                    "window not found",
                    VerificationState.DENIED,
                    0,
                    normalized_error="window handle is stale or foreign",
                )
            windows = [selected]
        if operation == "open":
            error: str | None
            pre_launch = self.observe(app)
            before_pids = {p.pid for p in pre_launch.processes}
            before_handles = {window.handle for window in pre_launch.windows}
            result = (
                self.adapter.activate(app) if app.app_user_model_id else self.adapter.launch(app)
            )
            observed_process: ProcessObservation | None = None
            observed_window = None
            deadline = time.monotonic() + 3.0
            while result.succeeded and time.monotonic() < deadline:
                current = self.observe(app)
                new = [p for p in current.processes if p.pid not in before_pids]
                if result.pid:
                    new = [p for p in new if p.pid == result.pid] or new
                new_windows = [w for w in current.windows if w.handle not in before_handles]
                visible = [w for w in current.windows if w.pid in {p.pid for p in new}]
                if new and visible:
                    observed_process, observed_window = new[0], visible[0]
                    break
                # Single-instance AppX handoff: a broker/activation PID may
                # delegate a new window to an existing trusted app process.
                if new_windows:
                    observed_window = new_windows[0]
                    observed_process = next(
                        (p for p in current.processes if p.pid == observed_window.pid), None
                    )
                if observed_process and observed_window:
                    break
                time.sleep(0.05)
            observed = observed_process is not None and observed_window is not None
            handoff = bool(observed and observed_process and observed_process.pid in before_pids)
            state = VerificationState.VERIFIED if observed else VerificationState.UNVERIFIED
            if not result.succeeded:
                error = result.error or "APPX_ACTIVATION_FAILED"
            else:
                error = None if observed else "LAUNCH_POSTCONDITION_TIMEOUT"
        elif operation in {"focus", "minimize", "maximize", "restore"}:
            if len(windows) > 1:
                return ApplicationOperationResult(
                    operation,
                    name,
                    app.application_id,
                    operation,
                    "ambiguous windows",
                    VerificationState.DENIED,
                    0,
                    {"clarification_options": [_window_public(w) for w in windows]},
                    normalized_error=ResolutionStatus.AMBIGUOUS,
                )
            if not windows:
                return ApplicationOperationResult(
                    operation,
                    name,
                    app.application_id,
                    operation,
                    "no window",
                    VerificationState.UNVERIFIED,
                    0.4,
                    retryable=True,
                )
            target = windows[0]
            result = self.adapter.window_action(target.handle, operation)
            started = time.monotonic()
            observed_window = None
            while result.succeeded and time.monotonic() - started < 1.5:
                observed_window = next(
                    (x for x in self.observe(app).windows if x.handle == target.handle), None
                )
                ok = bool(
                    observed_window
                    and (
                        (operation == "focus" and observed_window.foreground)
                        or (operation == "minimize" and observed_window.minimized)
                        or (operation == "maximize" and observed_window.maximized)
                        or (operation == "restore" and not observed_window.minimized)
                    )
                )
                if ok:
                    break
                time.sleep(0.05)
            ok = bool(
                observed_window
                and (
                    (operation == "focus" and observed_window.foreground)
                    or (operation == "minimize" and observed_window.minimized)
                    or (operation == "maximize" and observed_window.maximized)
                    or (operation == "restore" and not observed_window.minimized)
                )
            )
            state = (
                VerificationState.VERIFIED
                if result.succeeded and ok
                else (
                    VerificationState.UNVERIFIED if result.succeeded else VerificationState.FAILED
                )
            )
            observed = ok
            error = (
                None
                if ok
                else (
                    "FOREGROUND_RESTRICTED"
                    if operation == "focus"
                    else "WINDOW_STATE_POSTCONDITION_TIMEOUT"
                )
            )
        elif operation == "close":
            if len(windows) > 1:
                return ApplicationOperationResult(
                    operation,
                    name,
                    app.application_id,
                    operation,
                    "ambiguous windows",
                    VerificationState.DENIED,
                    0,
                    {"clarification_options": [_window_public(w) for w in windows]},
                    normalized_error=ResolutionStatus.AMBIGUOUS,
                )
            if not windows:
                return ApplicationOperationResult(
                    operation,
                    name,
                    app.application_id,
                    operation,
                    "no window",
                    VerificationState.UNVERIFIED,
                    0.5,
                )
            target = windows[0]
            result = self.adapter.graceful_close(target.handle)
            started = time.monotonic()
            observed = False
            while result.succeeded and time.monotonic() - started < 1.5:
                observed = not any(x.handle == target.handle for x in self.observe(app).windows)
                if observed:
                    break
                time.sleep(0.05)
            state = (
                VerificationState.VERIFIED
                if result.succeeded and observed
                else (
                    VerificationState.UNVERIFIED if result.succeeded else VerificationState.FAILED
                )
            )
            error = None if observed else "CLOSE_POSTCONDITION_TIMEOUT"
        elif operation == "restart":
            close = self.operate("close", name, approval_token, actor)
            if close.verification_state != VerificationState.VERIFIED:
                return replace(close, operation="restart")
            return self.operate("open", name, approval_token, actor)
        else:
            return ApplicationOperationResult(
                operation,
                name,
                app.application_id,
                operation,
                "denied",
                VerificationState.DENIED,
                0,
                normalized_error="unknown operation",
            )
        observed_outcomes = {
            "focus": "selected window focused",
            "minimize": "selected window minimized",
            "restore": "selected window restored",
            "maximize": "selected window maximized",
            "close": "selected window closed",
            "open": "running with visible window",
        }
        evidence: dict[str, object] = {
            "adapter_succeeded": result.succeeded,
            "pid": observed_process.pid if operation == "open" and observed_process else result.pid,
            "process_creation_time": observed_process.created_at
            if operation == "open" and observed_process
            else None,
            "window_handle": observed_window.handle
            if operation == "open" and observed_window
            else None,
            "window_title": observed_window.title
            if operation == "open" and observed_window
            else None,
            "window_class": observed_window.class_name
            if operation == "open" and observed_window
            else None,
            "observation_source": "process_and_visible_window"
            if operation == "open" and observed
            else None,
            "activation_method": "app_user_model_id" if app.app_user_model_id else "executable",
            "activation_pid": result.pid if operation == "open" else None,
            "preexisting_process": observed_process.pid in before_pids
            if operation == "open" and observed_process
            else False,
            "new_window_handle": observed_window.handle
            if operation == "open" and observed_window
            else None,
            "handoff_detected": handoff if operation == "open" else False,
            "selected_window_handle": target.handle
            if operation in {"focus", "minimize", "restore", "maximize", "close"}
            else None,
            "owner_pid": target.pid
            if operation in {"focus", "minimize", "restore", "maximize", "close"}
            else None,
            "title": target.title
            if operation in {"focus", "minimize", "restore", "maximize", "close"}
            else None,
            "class_name": target.class_name
            if operation in {"focus", "minimize", "restore", "maximize", "close"}
            else None,
            "previous_state": _window_public(target)
            if operation in {"focus", "minimize", "restore", "maximize", "close"}
            else None,
            "observed_state": _window_public(observed_window)
            if operation in {"focus", "minimize", "restore", "maximize"} and observed_window
            else {"exists": False}
            if operation == "close" and observed
            else None,
            "verification_source": "shared_application_observation"
            if operation != "open"
            else None,
        }
        if operation != "open":
            for key in (
                "activation_method",
                "activation_pid",
                "new_window_handle",
                "handoff_detected",
                "preexisting_process",
                "pid",
                "process_creation_time",
                "window_handle",
                "window_title",
                "window_class",
                "observation_source",
            ):
                evidence.pop(key)
        return ApplicationOperationResult(
            operation,
            name,
            app.application_id,
            operation,
            observed_outcomes[operation] if observed else "not observed",
            state,
            resolution.confidence,
            evidence,
            retryable=(operation in {"open", "focus", "minimize", "restore", "maximize"})
            and result.succeeded
            and not observed,
            normalized_error=error
            if operation in {"open", "focus", "minimize", "restore", "maximize", "close"}
            and not observed
            else result.error,
        )
