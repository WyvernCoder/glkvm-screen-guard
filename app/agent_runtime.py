"""Session-1 SYSTEM agent: the only process that calls CCD/HDR."""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pythoncom

from . import binding, ccd
from .display_manager import DisplayManager
from .logs import setup_logging
from .pipe import PipeClient
from .service_paths import PIPE_NAME, logs_dir, resolve_data_dir

log = logging.getLogger("glkvm.agent")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

DESKTOP_ALL_ACCESS = 0x10000000

user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
user32.SetThreadDesktop.restype = wintypes.BOOL
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.GetThreadDesktop.restype = wintypes.HANDLE
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _bind_input_desktop() -> tuple[int, int] | None:
    """Bind this (windowless) thread to the current input desktop.

    The Default/Winlogon input desktop changes at lock/logon; this must be
    re-done per operation.  Return (input, original) for restoring.
    """
    current = kernel32.GetCurrentThreadId()
    original = user32.GetThreadDesktop(current)
    input_desktop = user32.OpenInputDesktop(0, False, DESKTOP_ALL_ACCESS)
    if not input_desktop:
        log.warning("OpenInputDesktop failed: %s", ctypes.get_last_error())
        return None
    if not user32.SetThreadDesktop(input_desktop):
        log.warning("SetThreadDesktop(input) failed: %s", ctypes.get_last_error())
        user32.CloseDesktop(input_desktop)
        return None
    return int(input_desktop), int(original)


def _restore_desktop(bound: tuple[int, int] | None) -> None:
    if not bound:
        return
    input_desktop, original = bound
    try:
        if original:
            user32.SetThreadDesktop(wintypes.HANDLE(original))
    finally:
        user32.CloseDesktop(wintypes.HANDLE(input_desktop))


class CcdThread:
    def __init__(self, logger: logging.Logger) -> None:
        self.log = logger
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="ccd-agent", daemon=True)
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)

    def execute(self, command: dict, timeout: float = 60.0) -> dict:
        event = threading.Event()
        box: dict = {}
        self._queue.put((command, box, event))
        if not event.wait(timeout):
            return {"ok": False, "code": "ccd_timeout"}
        return box.get("result", {"ok": False, "code": "ccd_empty"})

    def _run(self) -> None:
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass
        self._ready.set()
        while True:
            command, box, event = self._queue.get()
            cmd = str(command.get("cmd", ""))
            self.log.info("CCD command start: %s", cmd)
            try:
                bound = _bind_input_desktop()
                self.log.info("CCD input desktop bound=%s cmd=%s", bool(bound), cmd)
                try:
                    result = self._dispatch(command)
                finally:
                    _restore_desktop(bound)
            except Exception as exc:
                self.log.exception("CCD command failed")
                result = {"ok": False, "code": "exception", "error": str(exc)}
            self.log.info("CCD command done: %s ok=%s code=%s", cmd, result.get("ok"), result.get("code"))
            box["result"] = result
            event.set()

    def _dispatch(self, command: dict) -> dict:
        cmd = str(command.get("cmd", ""))
        settings = dict(command.get("settings") or {})
        snapshot_path = settings.get("snapshot_path") or str(
            resolve_data_dir() / "snapshots" / "local.json"
        )
        settings["snapshot_path"] = snapshot_path
        top = ccd.query_topology()
        mode = str(settings.get("kvm_bind_mode") or "slot")
        slot = int(settings.get("kvm_slot") or 0)
        slot_key = str(settings.get("kvm_slot_key") or "")
        target = str(settings.get("kvm_target") or "")
        kvm_key = binding.configured_kvm_key(top, mode, slot, slot_key, target)
        base = {
            "kvm_target": kvm_key,
            "snapshot_path": snapshot_path,
            "min_primary_hz": float(settings.get("min_primary_hz") or 100),
            "hdr_restore": bool(settings.get("hdr_restore", True)),
            "on_connect": str(settings.get("on_connect") or "kvm-only"),
            "on_disconnect": str(settings.get("on_disconnect") or "restore"),
            "screen_number": int(settings.get("screen_number") or 1),
        }
        manager = DisplayManager(base, self.log)

        if cmd == "query":
            info = manager.snapshot_info()
            payload = binding.topology_payload(top, kvm_key, info)
            payload["snapshot_info"] = info
            return {"ok": True, "topology": payload}
        if cmd == "capture":
            result = manager.capture_local()
            return {
                "ok": result.ok,
                "code": result.code,
                "result": result.as_dict(),
                "topology": binding.topology_payload(top, kvm_key, manager.snapshot_info()),
            }
        if cmd == "connect":
            result = manager.connect()
            return {"ok": result.ok, "code": result.code, "result": result.as_dict()}
        if cmd == "disconnect":
            result = manager.disconnect()
            return {"ok": result.ok, "code": result.code, "result": result.as_dict()}
        if cmd in ("restore", "apply_snapshot"):
            result = manager.restore_local()
            return {"ok": result.ok, "code": result.code, "result": result.as_dict()}
        if cmd == "snapshot_info":
            return {"ok": True, "snapshot": manager.snapshot_info()}
        if cmd == "ping":
            return {"ok": True, "pid": __import__("os").getpid()}
        return {"ok": False, "code": "unknown_command", "cmd": cmd}


class AgentRuntime:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or resolve_data_dir()
        self.log = setup_logging(self.data_dir / "logs")
        self.ccd = CcdThread(self.log)
        self._stop = threading.Event()

    def run(self) -> int:
        self.log.info("Display agent starting (data=%s)", self.data_dir)
        self.ccd.start()
        channel = None
        try:
            channel = PipeClient(PIPE_NAME, timeout=30)
            channel.send(
                {
                    "role": "agent",
                    "proto": 1,
                    "pid": __import__("os").getpid(),
                    "session": __import__("win32ts").ProcessIdToSessionId(
                        __import__("os").getpid()
                    ),
                }
            )
            hello = channel.recv()
            if not hello or not hello.get("ok"):
                self.log.error("agent handshake rejected: %s", hello)
                return 2
            channel.send({"event": "ready", "pid": __import__("os").getpid()})
            self.log.info("Agent connected to service pipe")
            while not self._stop.is_set():
                command = channel.recv()
                if command is None:
                    self.log.warning("Service pipe closed")
                    break
                if command.get("event"):
                    continue
                if command.get("cmd") == "shutdown":
                    self.log.info("Agent shutdown requested")
                    break
                request_id = command.get("id")
                self.log.info("Agent received cmd=%s id=%s", command.get("cmd"), request_id)
                result = self.ccd.execute(command)
                result["id"] = request_id
                channel.send(result)
                self.log.info("Agent replied cmd=%s id=%s ok=%s", command.get("cmd"), request_id, result.get("ok"))
        except Exception:
            self.log.exception("Agent runtime failed")
            return 1
        finally:
            self._stop.set()
            if channel is not None:
                channel.close()
            self.log.info("Agent stopped")
        return 0


def main() -> int:
    data_dir = resolve_data_dir()
    for index, arg in enumerate(sys.argv):
        if arg == "--data-dir" and index + 1 < len(sys.argv):
            data_dir = Path(sys.argv[index + 1])
    return AgentRuntime(data_dir).run()
