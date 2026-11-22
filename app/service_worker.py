"""Elevated Windows service control for the GUI.

Starting/stopping a service and uninstalling it require Administrator.  The
GUI runs as the interactive user, so the worker launches one elevated
PowerShell process via UAC and reports its exit status.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

CREATE_NO_WINDOW = 0x08000000
SERVICE_NAME = "GLKVMGuard"


class ServiceWorker(QThread):
    done = pyqtSignal(str, bool, str)

    def __init__(self, action: str, root: Path | str, parent=None) -> None:
        super().__init__(parent)
        self.action = action
        self.root = Path(root)

    def _inner_command(self) -> str:
        if self.action == "start":
            return (
                f"$ErrorActionPreference='Stop'; "
                f"Start-Service -Name '{SERVICE_NAME}'; "
                f"(Get-Service -Name '{SERVICE_NAME}').Status"
            )
        if self.action == "stop":
            return (
                f"$ErrorActionPreference='Stop'; "
                f"Stop-Service -Name '{SERVICE_NAME}' -Force; "
                f"(Get-Service -Name '{SERVICE_NAME}').Status"
            )
        if self.action == "uninstall":
            script = str(self.root / "uninstall_service.ps1").replace("'", "''")
            return f"& '{script}'"
        raise ValueError(f"unknown service action: {self.action}")

    def run(self) -> None:
        try:
            inner = self._inner_command()
            encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
            outer = (
                "$ErrorActionPreference='Stop'; "
                "$p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait "
                "-PassThru -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass',"
                f"'-EncodedCommand','{encoded}'); exit $p.ExitCode"
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    outer,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=CREATE_NO_WINDOW,
            )
            message = (result.stdout or "").strip() or (result.stderr or "").strip()
            self.done.emit(self.action, result.returncode == 0, message)
        except Exception as exc:
            self.done.emit(self.action, False, str(exc))
