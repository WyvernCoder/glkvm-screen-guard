"""Small JSON-lines named-pipe transport for the service.

Security: the pipe DACL allows LocalSystem, Administrators and Authenticated
Users.  The pipe is local-only; payloads never contain the KVM password.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import pywintypes
import win32con
import win32file
import win32pipe
import win32security

from .service_paths import PIPE_NAME

log = logging.getLogger("glkvm.pipe")

_BUFFER = 65536


def _make_security_attributes() -> win32security.SECURITY_ATTRIBUTES:
    sd = win32security.SECURITY_DESCRIPTOR()
    acl = win32security.ACL()
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)
    admins = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid)
    users = win32security.CreateWellKnownSid(win32security.WinAuthenticatedUserSid)
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, system)
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, admins)
    acl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        users,
    )
    sd.SetSecurityDescriptorDacl(True, acl, False)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.bInheritHandle = False
    sa.SECURITY_DESCRIPTOR = sd
    return sa


class PipeChannel:
    def __init__(self, handle) -> None:
        self.handle = handle
        self._write_lock = threading.Lock()
        self._buffer = b""
        self.closed = False

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                win32file.WriteFile(self.handle, data)
            except pywintypes.error as exc:
                self.closed = True
                raise OSError(exc.winerror, exc.strerror) from exc

    def recv(self) -> dict | None:
        while b"\n" not in self._buffer:
            try:
                _hr, data = win32file.ReadFile(self.handle, _BUFFER)
            except pywintypes.error as exc:
                if exc.winerror in (109, 232, 233):
                    self.closed = True
                    return None
                self.closed = True
                raise OSError(exc.winerror, exc.strerror) from exc
            if not data:
                self.closed = True
                return None
            self._buffer += data
        return self._take_line()

    def recv_available(self) -> dict | None:
        """Non-blocking read used by single-threaded pipe handlers.

        A synchronous named-pipe handle must not have a blocking ReadFile
        outstanding while another thread writes on the same handle; peeking
        first keeps read and write in one thread.
        """
        if b"\n" not in self._buffer:
            try:
                peek = win32pipe.PeekNamedPipe(self.handle, 0)
                if isinstance(peek, tuple):
                    # pywin32 returns (data, bytes_available, ...) for this call.
                    available = int(peek[1]) if len(peek) > 1 else 0
                    if not available and isinstance(peek[0], int):
                        available = int(peek[0])
                else:
                    available = int(peek)
            except pywintypes.error:
                self.closed = True
                return None
            if available <= 0:
                return None
            try:
                _hr, data = win32file.ReadFile(self.handle, min(available, _BUFFER))
            except pywintypes.error as exc:
                if exc.winerror in (109, 232, 233):
                    self.closed = True
                    return None
                self.closed = True
                raise OSError(exc.winerror, exc.strerror) from exc
            if not data:
                self.closed = True
                return None
            self._buffer += data
        if b"\n" not in self._buffer:
            return None
        return self._take_line()

    def _take_line(self) -> dict | None:
        line, self._buffer = self._buffer.split(b"\n", 1)
        if not line.strip():
            if b"\n" in self._buffer:
                return self._take_line()
            return None
        try:
            value = json.loads(line.decode("utf-8"))
        except Exception:
            log.warning("Ignoring malformed pipe line (%d bytes)", len(line))
            if b"\n" in self._buffer:
                return self._take_line()
            return None
        return value if isinstance(value, dict) else None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            win32file.CloseHandle(self.handle)
        except Exception:
            pass


class PipeClient(PipeChannel):
    def __init__(self, pipe: str = PIPE_NAME, timeout: float = 5.0) -> None:
        deadline = time.time() + max(0.1, timeout)
        last: Exception | None = None
        handle = None
        while time.time() < deadline:
            try:
                handle = win32file.CreateFile(
                    pipe,
                    win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                    0,
                    None,
                    win32con.OPEN_EXISTING,
                    0,
                    None,
                )
                break
            except pywintypes.error as exc:
                last = exc
                if exc.winerror not in (2, 231):
                    raise OSError(exc.winerror, exc.strerror) from exc
                time.sleep(0.1)
        if handle is None:
            raise OSError(2, f"named pipe not available: {pipe} ({last})")
        win32pipe.SetNamedPipeHandleState(
            handle,
            win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            None,
            None,
        )
        super().__init__(handle)


class PipeServer:
    """One listening instance per connection; a thread per client."""

    def __init__(self, on_client, pipe: str = PIPE_NAME) -> None:
        self.on_client = on_client
        self.pipe = pipe
        self._stop = threading.Event()
        self._sa = _make_security_attributes()
        self._thread: threading.Thread | None = None
        self._handles: list = []
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._accept_loop, name="pipe-accept", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for handle in list(self._handles):
                try:
                    win32file.CloseHandle(handle)
                except Exception:
                    pass
            self._handles.clear()
        # Unblock a pending ConnectNamedPipe.
        try:
            dummy = PipeClient(self.pipe, timeout=0.3)
            dummy.close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            handle = None
            try:
                handle = win32pipe.CreateNamedPipe(
                    self.pipe,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_BYTE
                    | win32pipe.PIPE_READMODE_BYTE
                    | win32pipe.PIPE_WAIT,
                    win32pipe.PIPE_UNLIMITED_INSTANCES,
                    _BUFFER,
                    _BUFFER,
                    0,
                    self._sa,
                )
                with self._lock:
                    self._handles.append(handle)
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as exc:
                if exc.winerror == 535:  # ERROR_PIPE_CONNECTED
                    pass
                elif self._stop.is_set():
                    break
                else:
                    log.warning("Named pipe accept failed: %s", exc)
                    if handle is not None:
                        try:
                            win32file.CloseHandle(handle)
                        except Exception:
                            pass
                    time.sleep(0.2)
                    continue
            if self._stop.is_set():
                try:
                    win32file.CloseHandle(handle)
                except Exception:
                    pass
                break
            with self._lock:
                try:
                    self._handles.remove(handle)
                except ValueError:
                    pass
            channel = PipeChannel(handle)
            threading.Thread(
                target=self._run_client, args=(channel,), name="pipe-client", daemon=True
            ).start()

    def _run_client(self, channel: PipeChannel) -> None:
        try:
            self.on_client(channel)
        except Exception:
            log.exception("Pipe client handler failed")
        finally:
            channel.close()
