"""Background workers for the connection test and the polling loop.

The PyQt UI never performs network I/O on its own thread.  Both workers are
plain ``QObject`` instances moved to a ``QThread``.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from .kvm import CertMismatch, Detection, KvmClient, KvmError, TestResult

log = logging.getLogger("glkvm.poller")


def _client(settings: dict) -> KvmClient:
    return KvmClient(
        ip=settings.get("ip", ""),
        user=settings.get("user", ""),
        password=settings.get("password", ""),
        pin=settings.get("pin", ""),
        timeout=settings.get("timeout", 6.0),
        wake_seconds=settings.get("wake_seconds", 60),
    )


class TestWorker(QObject):
    done = pyqtSignal(object)

    def __init__(self, settings: dict) -> None:
        super().__init__()
        self.settings = dict(settings)
        self._client: KvmClient | None = None

    @pyqtSlot()
    def run(self) -> None:
        try:
            self._client = _client(self.settings)
            if self.settings.get("probe_only") or not self.settings.get("password"):
                result: TestResult = self._client.probe()
            else:
                result = self._client.test_connection()
            self.done.emit({"ok": True, "result": result})
        except KvmError as exc:
            payload = {
                "ok": False,
                "code": exc.code,
                "message": str(exc),
                "fingerprint": self._client.last_fingerprint if self._client else "",
            }
            if isinstance(exc, CertMismatch):
                payload["expected"] = exc.expected
                payload["actual"] = exc.actual
            log.warning("Connection test failed: %s (%s)", exc.code, exc)
            self.done.emit(payload)
        except Exception as exc:  # defensive: never kill the UI
            log.exception("Unexpected connection test error")
            self.done.emit(
                {
                    "ok": False,
                    "code": "unknown",
                    "message": str(exc),
                    "fingerprint": self._client.last_fingerprint if self._client else "",
                }
            )


class PollWorker(QObject):
    result = pyqtSignal(object)

    def __init__(self, settings: dict) -> None:
        super().__init__()
        self.settings = dict(settings)
        self._running = True
        self._kick = False

    def stop(self) -> None:
        self._running = False

    def kick(self) -> None:
        """Ask a sleeping loop to poll immediately."""
        self._kick = True

    def _sleep(self, seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        while self._running and not self._kick and remaining > 0:
            chunk = min(0.2, remaining)
            QThread.msleep(int(chunk * 1000))
            remaining -= chunk
        self._kick = False

    @pyqtSlot()
    def run(self) -> None:
        try:
            client = _client(self.settings)
        except Exception as exc:
            self.result.emit(
                Detection(
                    state="unknown",
                    reason="network",
                    fatal=True,
                    detail={"message": str(exc)},
                )
            )
            return

        interval = max(1, int(self.settings.get("poll_seconds", 5)))
        log.info("Detection loop started (interval=%ss)", interval)

        while self._running:
            try:
                detection = client.detect()
            except KvmError as exc:
                detection = Detection(
                    state="unknown",
                    reason=exc.code,
                    fingerprint=client.last_fingerprint,
                    fatal=exc.fatal,
                    detail={"message": str(exc)},
                )
            except Exception as exc:
                log.exception("Unexpected detection error")
                detection = Detection(
                    state="unknown",
                    reason="unknown",
                    fingerprint=client.last_fingerprint,
                    detail={"message": str(exc)},
                )

            self.result.emit(detection)
            if detection.fatal:
                break
            self._sleep(interval)

        log.info("Detection loop stopped")
