"""Qt client for the service named pipe (GUI is a config/status client)."""

from __future__ import annotations

import logging
import queue
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal

from .pipe import PipeClient
from .service_paths import PIPE_NAME

log = logging.getLogger("glkvm.service_client")


class ServiceClient(QObject):
    connected = pyqtSignal(bool)
    state = pyqtSignal(object)
    response = pyqtSignal(object, str)
    error = pyqtSignal(str)

    def __init__(self, pipe: str = PIPE_NAME) -> None:
        super().__init__()
        self.pipe = pipe
        self._outbox: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._channel: PipeClient | None = None
        self._next_id = 0
        self._id_cmd: dict[int, str] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="service-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        channel = self._channel
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def call(self, cmd: str, **fields) -> int:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._id_cmd[request_id] = cmd
        payload = {"id": request_id, "cmd": cmd}
        payload.update(fields)
        self._outbox.put(payload)
        return request_id

    def _run(self) -> None:
        try:
            channel = PipeClient(self.pipe, timeout=3.0)
            self._channel = channel
            channel.send({"role": "gui", "proto": 1})
            hello = channel.recv()
            if not hello or not hello.get("ok"):
                self.error.emit("service handshake rejected")
                self.connected.emit(False)
                channel.close()
                return
            self.connected.emit(True)
            if hello.get("state"):
                self.state.emit(hello["state"])
            while not self._stop.is_set():
                while True:
                    try:
                        message = self._outbox.get_nowait()
                    except queue.Empty:
                        break
                    channel.send(message)
                message = channel.recv_available()
                if message is None:
                    if channel.closed:
                        break
                    time.sleep(0.02)
                    continue
                if message.get("event") == "state" and isinstance(message.get("state"), dict):
                    self.state.emit(message["state"])
                    continue
                request_id = message.get("id")
                if request_id is not None:
                    with self._lock:
                        cmd = self._id_cmd.pop(int(request_id), "")
                    self.response.emit(message, cmd)
        except Exception as exc:
            log.warning("Service client stopped: %s", exc)
            self.error.emit(str(exc))
        finally:
            self.connected.emit(False)
            if self._channel is not None:
                self._channel.close()
                self._channel = None
