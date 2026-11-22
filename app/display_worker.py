"""QThread worker for display actions in standalone mode.

Keeps SetDisplayConfig + the 10 s verification loop off the GUI thread.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from .display_manager import ActionResult, DisplayManager


class DisplayWorker(QObject):
    finished = pyqtSignal(object)

    def __init__(self, kind: str, settings: dict, logger: logging.Logger) -> None:
        super().__init__()
        self.kind = kind
        self.settings = dict(settings)
        self.logger = logger
        self._manager: DisplayManager | None = None
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True
        if self._manager is not None:
            self._manager.stop()

    @pyqtSlot()
    def run(self) -> None:
        if self._stopped:
            self.finished.emit(
                ActionResult(kind=self.kind, ok=False, code="stopped").as_dict()
            )
            return
        manager = DisplayManager(self.settings, self.logger)
        self._manager = manager
        try:
            if self.kind == "connect":
                result = manager.connect()
            elif self.kind == "disconnect":
                result = manager.disconnect()
            elif self.kind == "restore":
                result = manager.restore_local()
            elif self.kind == "capture":
                result = manager.capture_local()
            else:
                result = ActionResult(kind=self.kind, ok=False, code="unknown_action")
        except Exception as exc:
            self.logger.exception("Display worker failed")
            result = ActionResult(
                kind=self.kind, ok=False, code="exception",
                detail={"message": str(exc)},
            )
        self.finished.emit(result.as_dict())
