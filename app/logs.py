"""Daily rolling log files plus a Qt bridge for the in-window log view."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


class DailyFileHandler(logging.Handler):
    """Write to ``logs/YYYY-MM-DD.log`` and reopen when the date changes."""

    def __init__(self, log_dir: Path | str) -> None:
        super().__init__()
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._date = ""
        self._stream = None

    def _open_for_today(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self._stream is not None and today == self._date:
            return
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
        self._date = today
        self._stream = open(self.log_dir / f"{today}.log", "a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._open_for_today()
            assert self._stream is not None
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
        finally:
            self._stream = None
            super().close()


class LogBus(QObject):
    message = pyqtSignal(str)


class QtLogHandler(logging.Handler):
    def __init__(self, bus: LogBus) -> None:
        super().__init__()
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bus.message.emit(self.format(record))
        except Exception:
            self.handleError(record)


def setup_logging(log_dir: Path | str, bus: LogBus | None = None) -> logging.Logger:
    logger = logging.getLogger("glkvm")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    fmt = logging.Formatter(LOG_FORMAT, "%Y-%m-%d %H:%M:%S")

    daily = DailyFileHandler(log_dir)
    daily.setLevel(logging.DEBUG)
    daily.setFormatter(fmt)
    logger.addHandler(daily)

    # pythonw.exe has no stderr; only add a console handler when it exists.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(fmt)
        logger.addHandler(console)

    if bus is not None:
        qt = QtLogHandler(bus)
        qt.setLevel(logging.INFO)
        qt.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        logger.addHandler(qt)

    return logger


def log_action(logger: logging.Logger, action: str, **details) -> None:
    suffix = ""
    if details:
        suffix = " " + " ".join(f"{k}={v}" for k, v in details.items())
    logger.info("User action: %s%s", action, suffix)
