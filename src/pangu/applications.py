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
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Protocol, cast

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


def normalise_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def default_aliases(name: str) -> tuple[str, ...]:
    key = normalise_name(name)
    if "chrome" in key:
        return ("Chrome", "Google Chrome", "chrome browser")
    if key in {"code", "visual studio code"} or "visual studio code" in key:
        return ("VS Code", "Visual Studio Code", "Code")
    if key in {"notepad", "windows notepad"}:
        return ("Notepad", "Windows Notepad")
    return ()


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
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    confidence: float = 0.5
    stale: bool = False
    source_evidence: tuple[dict[str, str], ...] = ()
    health_state: str = "HEALTHY"

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
        )

    def public(self) -> dict[str, object]:
        return {
            "application_id": self.application_id,
            "display_name": self.display_name,
            "normalized_name": self.normalized_name,
            "aliases": self.aliases,
            "executable_name": self.executable_name,
            "package_identity": self.package_identity,
            "install_source": self.install_source,
            "version": self.version,
            "process_names": self.process_names,
            "confidence": self.confidence,
            "stale": self.stale,
            "health_state": self.health_state,
        }


@dataclass(frozen=True)
class ProcessObservation:
    pid: int
    name: str
    executable_path: str | None = None


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

    _blocked: ClassVar[set[str]] = {
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "wscript.exe",
        "cscript.exe",
        "python.exe",
        "pythonw.exe",
        "bash.exe",
    }

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
        for process in self.processes():
            if process.name.casefold() not in self._blocked:
                records.append(
                    ApplicationRecord.create(
                        Path(process.name).stem,
                        executable_path=process.executable_path,
                        executable_name=process.name,
                        process_names=(process.name,),
                        install_source="running_process",
                        confidence=0.65,
                        source_evidence=({"source": "running_process"},),
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
                names.update(
                    path.name
                    for path in Path(item).glob("*.exe")
                    if path.name.casefold() not in self._blocked
                )
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
        # tasklist has no untrusted input and keeps this dependency-free.
        try:
            text = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            ).stdout
            return [
                ProcessObservation(int(parts[-2].replace(",", "")), parts[0].strip('"'))
                for line in text.splitlines()
                if (parts := line.split('","'))
                and len(parts) >= 2
                and parts[-2].replace('"', "").replace(",", "").isdigit()
            ]
        except (OSError, subprocess.SubprocessError):
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
        return AdapterResult(self._supported(), False, "package activation unavailable")

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
        name = app.executable_name or app.display_name + ".exe"
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

    def refresh(self) -> list[ApplicationRecord]:
        discovered = self.adapter.discover()
        grouped: dict[str, list[ApplicationRecord]] = {}
        for record in discovered:
            grouped.setdefault(
                record.executable_name.casefold()
                if record.executable_name
                else record.normalized_name,
                [],
            ).append(record)
        now = datetime.now(UTC)
        self._records = {}
        for group in grouped.values():
            first = group[0]
            aliases = tuple(
                sorted(
                    {x.display_name for x in group} | set().union(*(set(x.aliases) for x in group))
                )
            )
            merged = replace(
                first,
                aliases=aliases,
                source_evidence=tuple(e for x in group for e in x.source_evidence),
                last_seen_at=now,
                confidence=max(x.confidence for x in group),
            )
            self._records[merged.application_id] = merged
        with self.db.transaction() as session:
            repo = ApplicationCatalogRepository(session)
            for record in self._records.values():
                repo.upsert(ApplicationCatalogRecord.from_application(record))
        return self.list()

    def list(self) -> list[ApplicationRecord]:
        return sorted(self._records.values(), key=lambda item: item.display_name)

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


class ApplicationResolver:
    def __init__(self, catalog: ApplicationCatalog) -> None:
        self.catalog = catalog

    def resolve(self, query: str) -> ResolutionResult:
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

    def operate(
        self, operation: str, name: str, approval_token: str | None = None, actor: str = "default"
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
            processes = [
                p
                for p in self.adapter.processes()
                if p.name.casefold()
                in {x.casefold() for x in app.process_names or (app.executable_name or "",)}
            ]
            return ApplicationOperationResult(
                operation,
                name,
                app.application_id,
                "running state",
                "running" if processes else "not running",
                VerificationState.VERIFIED,
                resolution.confidence,
                {"process_ids": [p.pid for p in processes]},
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
        windows = [
            item
            for item in self.adapter.windows()
            if item.pid
            in {
                p.pid
                for p in self.adapter.processes()
                if p.name.casefold()
                in {x.casefold() for x in app.process_names or (app.executable_name or "",)}
            }
        ]
        if operation == "open":
            result = (
                self.adapter.activate(app) if app.app_user_model_id else self.adapter.launch(app)
            )
            observed = any(p.pid == result.pid for p in self.adapter.processes())
            state = (
                VerificationState.VERIFIED
                if result.succeeded and observed
                else VerificationState.FAILED
            )
        elif operation in {"focus", "minimize", "maximize", "restore"}:
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
            result = self.adapter.window_action(windows[0].handle, operation)
            observed_window = next(
                (x for x in self.adapter.windows() if x.handle == windows[0].handle), None
            )
            ok = bool(
                observed_window
                and (
                    (operation == "focus" and observed_window.foreground)
                    or (operation == "minimize" and observed_window.minimized)
                    or (operation == "maximize" and observed_window.maximized)
                    or (
                        operation == "restore"
                        and not observed_window.minimized
                        and not observed_window.maximized
                    )
                )
            )
            state = (
                VerificationState.VERIFIED
                if result.succeeded and ok
                else VerificationState.PARTIALLY_VERIFIED
                if result.succeeded
                else VerificationState.FAILED
            )
            observed = ok
        elif operation == "close":
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
            result = self.adapter.graceful_close(windows[0].handle)
            observed = not any(x.handle == windows[0].handle for x in self.adapter.windows())
            state = (
                VerificationState.VERIFIED
                if result.succeeded and observed
                else VerificationState.FAILED
            )
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
        return ApplicationOperationResult(
            operation,
            name,
            app.application_id,
            operation,
            "observed" if observed else "not observed",
            state,
            resolution.confidence,
            {"adapter_succeeded": result.succeeded, "pid": result.pid},
            normalized_error=result.error,
        )
