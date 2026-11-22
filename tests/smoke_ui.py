"""UI smoke test (standalone mode).

Runs the real MainWindow against the real RM10, using a temporary config
and log directory.  It exercises:

  * window construction, i18n, theme, tray
  * the empty-password "test connection" path (TLS probe)
  * the first-use certificate trust dialog
  * saving the pinned fingerprint

Run with:  $env:GLKVM_TEST_IP='192.168.1.100'; py\\Scripts\\python.exe tests\\smoke_ui.py

Without GLKVM_TEST_IP the test is skipped."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["GLKVM_STANDALONE"] = "1"

IP = os.environ.get("GLKVM_TEST_IP", "").strip()
if not IP:
    print("SMOKE result: SKIP (set GLKVM_TEST_IP to the RM10 IP)")
    raise SystemExit(0)

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from app.config import Config
from app.i18n import I18n
from app.logs import LogBus, setup_logging
from app.paths import LANG_DIR, THEME_DIR
from app.theme import ThemeManager
from app.ui.main_window import MainWindow

TMP = ROOT / "tests" / "tmp"
TMP.mkdir(parents=True, exist_ok=True)
LOG_DIR = TMP / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG = TMP / "smoke-config.ini"
if CONFIG.exists():
    CONFIG.unlink()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    config = Config(CONFIG)
    config.set("connection", "ip", IP)
    config.set("connection", "user", "admin")
    config.set("connection", "cert_sha256", "")   # force the trust dialog
    config.set_bool("connection", "auto_detect_on_launch", False)
    config.set_bool("ui", "start_at_login", False)
    config.save()

    i18n = I18n(LANG_DIR, "zh_CN")
    themes = ThemeManager(THEME_DIR)
    themes.apply(app, "light")
    bus = LogBus()
    logger = setup_logging(LOG_DIR, bus)
    window = MainWindow(app, config, i18n, themes, logger, bus)
    window.show()

    state = {"done": False, "pin_before": config.get("connection", "cert_sha256")}

    def accept_trust() -> None:
        modal = QApplication.activeModalWidget()
        if isinstance(modal, QMessageBox):
            for button in modal.buttons():
                if modal.buttonRole(button) == QMessageBox.ButtonRole.YesRole:
                    button.click()
                    logger.info("smoke: accepted certificate trust dialog")
                    return
        if not state["done"] and time.time() < state["deadline"]:
            QTimer.singleShot(100, accept_trust)

    def start_test() -> None:
        # Radio exclusivity must survive the screen-radio being inside a row.
        window.on_disconnect_radios["screen"].click()
        assert window.screen_combo.isEnabled(), "screen combo should be enabled"
        assert sum(1 for r in window.on_disconnect_radios.values() if r.isChecked()) == 1
        window.on_disconnect_radios["restore"].click()
        assert not window.screen_combo.isEnabled(), "screen combo should be disabled"
        assert window.on_disconnect_radios["restore"].isChecked()
        print("SMOKE actions: PASS")

        window.ip_edit.setText(IP)
        window.user_edit.setText("admin")
        window.pw_edit.setText("")
        window.test_btn.click()
        logger.info("smoke: test button clicked")

    def finish() -> None:
        pin = config.get("connection", "cert_sha256")
        status = window.conn_status.text()
        safe_status = status.encode("ascii", "replace").decode("ascii")
        print("SMOKE status:", safe_status)
        print("SMOKE pin:", pin)
        print("SMOKE config:", CONFIG)
        ok = bool(pin) and ("TLS" in status or "需要密码" in status or "已连接" in status)
        print("SMOKE result:", "PASS" if ok else "FAIL")
        state["done"] = True
        window._quitting = True
        window.close()
        app.quit()
        state["exit"] = 0 if ok else 1

    state["deadline"] = time.time() + 15
    QTimer.singleShot(300, accept_trust)
    QTimer.singleShot(500, start_test)

    def poll() -> None:
        if config.get("connection", "cert_sha256"):
            QTimer.singleShot(500, finish)
            return
        if time.time() >= state["deadline"]:
            finish()
            return
        QTimer.singleShot(200, poll)

    QTimer.singleShot(1200, poll)
    app.exec()
    return int(state.get("exit", 1))


if __name__ == "__main__":
    raise SystemExit(main())
