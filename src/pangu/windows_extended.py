from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import psutil


class WindowsExtendedState(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class WindowsExtendedResult:
    operation: str
    state: WindowsExtendedState
    data: object = None
    message: str = ""
    normalized_error: str | None = None


class PowerShellJsonRunner:
    """Fixed-script PowerShell boundary. User text is passed only through $args."""

    def __init__(self) -> None:
        self.executable = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")

    def run(self, script: str, args: tuple[str, ...] = (), timeout: int = 15) -> WindowsExtendedResult:
        if os.name != "nt" or not self.executable:
            return WindowsExtendedResult("powershell", WindowsExtendedState.UNAVAILABLE, normalized_error="POWERSHELL_UNAVAILABLE")
        completed = subprocess.run(
            [self.executable, "-NoProfile", "-NonInteractive", "-Command", script, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
        if completed.returncode != 0:
            return WindowsExtendedResult(
                "powershell",
                WindowsExtendedState.FAILED,
                message=completed.stderr[-2000:],
                normalized_error="POWERSHELL_COMMAND_FAILED",
            )
        text = completed.stdout.strip()
        if not text:
            return WindowsExtendedResult("powershell", WindowsExtendedState.VERIFIED, data=None)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = text
        return WindowsExtendedResult("powershell", WindowsExtendedState.VERIFIED, data=data)


class ExtendedWindowsRuntime:
    """Typed Windows management surface for status and bounded low-level control."""

    def __init__(self, runner: PowerShellJsonRunner | None = None) -> None:
        self.runner = runner or PowerShellJsonRunner()

    def network_adapters(self) -> WindowsExtendedResult:
        script = "Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,LinkSpeed,MacAddress | ConvertTo-Json -Depth 3 -Compress"
        result = self.runner.run(script)
        return WindowsExtendedResult("network_adapters", result.state, result.data, result.message, result.normalized_error)

    def wifi_profiles(self) -> WindowsExtendedResult:
        # Reads saved profile names only; credentials are never requested.
        script = "$out = netsh wlan show profiles; $names = @(); foreach($line in $out){ if($line -match 'All User Profile\\s*:\\s*(.+)$'){ $names += $Matches[1].Trim() } }; $names | ConvertTo-Json -Compress"
        result = self.runner.run(script)
        return WindowsExtendedResult("wifi_profiles", result.state, result.data, result.message, result.normalized_error)

    def bluetooth_devices(self) -> WindowsExtendedResult:
        script = "Get-PnpDevice -Class Bluetooth | Select-Object FriendlyName,Status,InstanceId | ConvertTo-Json -Depth 3 -Compress"
        result = self.runner.run(script)
        return WindowsExtendedResult("bluetooth_devices", result.state, result.data, result.message, result.normalized_error)

    def printers(self) -> WindowsExtendedResult:
        script = "Get-Printer | Select-Object Name,DriverName,PortName,PrinterStatus,Shared | ConvertTo-Json -Depth 3 -Compress"
        result = self.runner.run(script)
        return WindowsExtendedResult("printers", result.state, result.data, result.message, result.normalized_error)

    def services(self, name_filter: str = "") -> WindowsExtendedResult:
        script = "$q=$args[0]; Get-Service | Where-Object { -not $q -or $_.Name -like ('*'+$q+'*') -or $_.DisplayName -like ('*'+$q+'*') } | Select-Object Name,DisplayName,Status,StartType | ConvertTo-Json -Depth 3 -Compress"
        result = self.runner.run(script, (name_filter,), timeout=20)
        return WindowsExtendedResult("services", result.state, result.data, result.message, result.normalized_error)

    def startup_applications(self) -> WindowsExtendedResult:
        script = "Get-CimInstance Win32_StartupCommand | Select-Object Name,Command,Location,User | ConvertTo-Json -Depth 3 -Compress"
        result = self.runner.run(script)
        return WindowsExtendedResult("startup_applications", result.state, result.data, result.message, result.normalized_error)

    def clipboard_read(self) -> WindowsExtendedResult:
        script = "Get-Clipboard -Raw | ConvertTo-Json -Compress"
        result = self.runner.run(script)
        return WindowsExtendedResult("clipboard_read", result.state, result.data, result.message, result.normalized_error)

    def clipboard_write(self, text: str) -> WindowsExtendedResult:
        if len(text) > 100_000:
            return WindowsExtendedResult("clipboard_write", WindowsExtendedState.DENIED, normalized_error="CLIPBOARD_TEXT_TOO_LARGE")
        script = "Set-Clipboard -Value $args[0]; Get-Clipboard -Raw | ConvertTo-Json -Compress"
        result = self.runner.run(script, (text,))
        state = result.state
        if state == WindowsExtendedState.VERIFIED and result.data != text:
            state = WindowsExtendedState.UNVERIFIED
        return WindowsExtendedResult("clipboard_write", state, result.data, result.message, result.normalized_error)

    def process_diagnostics(self, limit: int = 25) -> WindowsExtendedResult:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        values: list[dict[str, object]] = []
        for process in sorted(
            psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]),
            key=lambda item: float(item.info.get("memory_percent") or 0.0),
            reverse=True,
        )[:limit]:
            values.append(
                {
                    "pid": process.info.get("pid"),
                    "name": process.info.get("name"),
                    "cpu_percent": process.info.get("cpu_percent"),
                    "memory_percent": process.info.get("memory_percent"),
                    "status": process.info.get("status"),
                }
            )
        return WindowsExtendedResult("process_diagnostics", WindowsExtendedState.VERIFIED, values)

    def device_health(self) -> WindowsExtendedResult:
        memory = psutil.virtual_memory()
        disk_root = Path(os.environ.get("SystemDrive", "C:")) / "\\"
        try:
            disk = psutil.disk_usage(str(disk_root))
            disk_data: dict[str, object] = {"percent": disk.percent, "free": disk.free}
        except OSError:
            disk_data = {"available": False}
        battery = psutil.sensors_battery()
        return WindowsExtendedResult(
            "device_health",
            WindowsExtendedState.VERIFIED,
            {
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory_percent": memory.percent,
                "memory_available": memory.available,
                "disk": disk_data,
                "battery_percent": None if battery is None else battery.percent,
                "plugged": None if battery is None else battery.power_plugged,
            },
        )

    def set_service_state(self, service_name: str, running: bool) -> WindowsExtendedResult:
        if not service_name.strip() or len(service_name) > 128:
            return WindowsExtendedResult("service_state", WindowsExtendedState.DENIED, normalized_error="INVALID_SERVICE_NAME")
        verb = "Start-Service" if running else "Stop-Service"
        script = f"$n=$args[0]; {verb} -Name $n -ErrorAction Stop; (Get-Service -Name $n | Select-Object Name,Status) | ConvertTo-Json -Compress"
        result = self.runner.run(script, (service_name,), timeout=20)
        return WindowsExtendedResult("service_state", result.state, result.data, result.message, result.normalized_error)

    def set_network_adapter_enabled(self, adapter_name: str, enabled: bool) -> WindowsExtendedResult:
        if not adapter_name.strip() or len(adapter_name) > 128:
            return WindowsExtendedResult("network_adapter", WindowsExtendedState.DENIED, normalized_error="INVALID_ADAPTER_NAME")
        verb = "Enable-NetAdapter" if enabled else "Disable-NetAdapter"
        script = f"$n=$args[0]; {verb} -Name $n -Confirm:$false -ErrorAction Stop; Get-NetAdapter -Name $n | Select-Object Name,Status | ConvertTo-Json -Compress"
        result = self.runner.run(script, (adapter_name,), timeout=20)
        return WindowsExtendedResult("network_adapter", result.state, result.data, result.message, result.normalized_error)

    def open_settings(self, page: str) -> WindowsExtendedResult:
        allowed = {
            "bluetooth": "ms-settings:bluetooth",
            "wifi": "ms-settings:network-wifi",
            "display": "ms-settings:display",
            "sound": "ms-settings:sound",
            "printers": "ms-settings:printers",
            "startup": "ms-settings:startupapps",
            "notifications": "ms-settings:notifications",
        }
        target = allowed.get(page.casefold())
        if target is None:
            return WindowsExtendedResult("open_settings", WindowsExtendedState.DENIED, normalized_error="SETTINGS_PAGE_NOT_ALLOWED")
        if os.name != "nt":
            return WindowsExtendedResult("open_settings", WindowsExtendedState.UNAVAILABLE, normalized_error="WINDOWS_REQUIRED")
        try:
            os.startfile(target)  # type: ignore[attr-defined]
        except OSError as error:
            return WindowsExtendedResult("open_settings", WindowsExtendedState.FAILED, message=str(error), normalized_error="SETTINGS_OPEN_FAILED")
        return WindowsExtendedResult("open_settings", WindowsExtendedState.UNVERIFIED, data={"uri": target}, message="Settings page launch requested")
