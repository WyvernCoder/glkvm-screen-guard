"""Windows service entry point (pywin32)."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import servicemanager
import win32event
import win32service
import win32serviceutil

from app.service_runtime import ServiceRuntime


class GLKVMGuardService(win32serviceutil.ServiceFramework):
    _svc_name_ = "GLKVMGuard"
    _svc_display_name_ = "GLKVM Screen Guard"
    _svc_description_ = (
        "GLKVM RM10 viewer detection and display switching "
        "(Session 0 service + Session 1 SYSTEM agent)."
    )

    def __init__(self, args):
        super().__init__(args)
        self._stop = threading.Event()
        self.runtime: ServiceRuntime | None = None

    def GetAcceptedControls(self):
        accepted = super().GetAcceptedControls()
        accepted |= win32service.SERVICE_ACCEPT_SESSIONCHANGE
        accepted |= win32service.SERVICE_ACCEPT_POWEREVENT
        accepted |= win32service.SERVICE_ACCEPT_PRESHUTDOWN
        return accepted

    def SvcDoRun(self):
        servicemanager.LogInfoMsg("GLKVM Screen Guard service starting")
        self.runtime = ServiceRuntime()
        self.runtime.start()
        self._stop.wait()
        servicemanager.LogInfoMsg("GLKVM Screen Guard service stopped")

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, 15000)
        if self.runtime is not None:
            self.runtime.stop()
        self._stop.set()

    def SvcShutdown(self):
        self.SvcStop()

    def SvcOtherEx(self, control, event_type, data):
        if self.runtime is None:
            return
        if control == win32service.SERVICE_CONTROL_SESSIONCHANGE:
            self.runtime.on_session_change(int(event_type))
        elif control == win32service.SERVICE_CONTROL_POWEREVENT:
            self.runtime.on_power_event(int(event_type))
        elif control == win32service.SERVICE_CONTROL_PRESHUTDOWN:
            self.runtime.on_preshutdown()


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(GLKVMGuardService)
