"""Entry point for GLKVM Screen Guard."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from app.config import Config
from app.i18n import I18n
from app.logs import LogBus, setup_logging
from app.paths import LANG_DIR, LOGS_DIR, THEME_DIR, ensure_data_dirs
from app.theme import ThemeManager
from app.ui.main_window import MainWindow


def _has_flag(flag: str) -> bool:
    return flag in sys.argv


def _flag_value(flag: str, default: str = "") -> str:
    try:
        index = sys.argv.index(flag)
        return sys.argv[index + 1]
    except (ValueError, IndexError):
        return default


def main() -> int:
    ensure_data_dirs()

    app = QApplication(sys.argv)
    app.setApplicationName("GLKVM Screen Guard")
    app.setOrganizationName("GLKVM Screen Guard")
    app.setStyle("Fusion")

    config = Config()
    theme_override = _flag_value("--theme")
    if theme_override:
        config.set("ui", "theme", theme_override)
    lang_override = _flag_value("--lang")
    i18n = I18n(LANG_DIR, config.get("ui", "language", "en_US"))
    if lang_override:
        i18n.load(lang_override)
    themes = ThemeManager(THEME_DIR)
    themes.apply(app, config.get("ui", "theme", "light"))

    bus = LogBus()
    logger = setup_logging(LOGS_DIR, bus)
    window = MainWindow(app, config, i18n, themes, logger, bus)
    window.show()
    tab_override = _flag_value("--tab")
    if tab_override.isdigit():
        window.tabs.setCurrentIndex(int(tab_override))
    logger.info("Application started (version %s)", window.version)

    if _has_flag("--minimized") and window.tray is not None:
        window.hide()

    if _has_flag("--screenshot"):
        target = Path(_flag_value("--screenshot", "screenshot.png"))
        target.parent.mkdir(parents=True, exist_ok=True)

        def snap() -> None:
            window.showNormal()
            window.raise_()
            window._quitting = True
            QTimer.singleShot(700, lambda: (window.grab().save(str(target)), app.quit()))

        QTimer.singleShot(1200, snap)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
