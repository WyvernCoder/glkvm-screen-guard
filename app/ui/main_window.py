"""Main window for GLKVM Screen Guard.

The GUI is a configuration/status client when the Windows service is
installed.  Without the service it can run in standalone mode with the
display engine in a worker thread.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import time
import winreg
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QByteArray, QSignalBlocker, QThread, QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QCloseEvent, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStatusBar,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from .. import ccd
from ..display_manager import DisplayManager
from ..display_worker import DisplayWorker
from ..i18n import I18n
from ..kvm import Detection, TestResult
from ..logs import LogBus, log_action
from ..poller import PollWorker, TestWorker
from ..paths import APP_DIR, DATA_DIR, LOGS_DIR, VERSION_FILE, read_version
from ..service_client import ServiceClient
from ..service_paths import SERVICE_JSON
from ..service_worker import ServiceWorker
from ..theme import ThemeManager

log = logging.getLogger("glkvm.ui")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "GLKVM Screen Guard"


def _format_fp(fingerprint: str) -> str:
    value = (fingerprint or "").strip().upper()
    return ":".join(value[i : i + 2] for i in range(0, len(value), 2))


def _short(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class MainWindow(QMainWindow):
    def __init__(
        self,
        app: QApplication,
        config: Config,
        i18n: I18n,
        themes: ThemeManager,
        logger: logging.Logger,
        bus: LogBus,
    ) -> None:
        super().__init__()
        self.app = app
        self.config = config
        self.i18n = i18n
        self.themes = themes
        self.logger = logger
        self.bus = bus
        self.version = read_version()

        self._texts: list[tuple[object, str, callable]] = []
        self._tips: list[tuple[object, str]] = []
        self._disconnect_timer = QTimer(self)
        self._disconnect_timer.setSingleShot(True)
        self._disconnect_timer.timeout.connect(self._on_disconnect_timer)

        self._poll_thread: QThread | None = None
        self._poll_worker: PollWorker | None = None
        self._test_thread: QThread | None = None
        self._test_worker: TestWorker | None = None
        self._display_thread: QThread | None = None
        self._display_worker: DisplayWorker | None = None
        self._display_busy = False
        self._poll_running = False
        self._test_busy = False
        self._retest_after_trust = False
        self._pending_first_result = False
        self._quitting = False
        self._tray_notice_shown = False
        self._det_state = "unknown"
        self._idle_misses = 0
        self._last_detection: Detection | None = None
        self._last_check_text = "-"
        self._authenticated = False
        self._topology: ccd.Topology | None = None
        self._snapshot_ok = False
        self._watching_since = 0.0
        self._last_action_ts = 0.0
        self._action_fail_until = 0.0
        self._pending_action: str | None = None
        self.service_client: ServiceClient | None = None
        self._service_mode = False
        self._service_configured = (
            SERVICE_JSON.exists()
            and os.environ.get("GLKVM_STANDALONE", "").lower() not in ("1", "true", "yes")
        )
        self._service_connected = False
        self._service_agent_connected = False
        self._last_service_action: dict = {}
        self._service_busy = False
        self._service_worker: ServiceWorker | None = None

        self.tray: QSystemTrayIcon | None = None
        self.tray_menu: QMenu | None = None

        self._build_ui()
        self._build_actions()
        self._build_menu()
        self._build_tray()
        self._bind_events()
        self._load_config()
        self._apply_theme()
        self._retranslate()
        self._restore_window()
        self.bus.message.connect(self.append_log)
        self._update_ui_state()
        self.refresh_displays()
        self._init_service_client()
        QTimer.singleShot(300, self._auto_start)

    # ================================================================= UI
    def _bind(self, widget, key: str, setter=None):
        self._texts.append((widget, key, setter or (lambda w, t: w.setText(t))))
        return widget

    def _bind_tip(self, widget, key: str):
        self._tips.append((widget, key))
        return widget

    def _group(self, key: str) -> QGroupBox:
        box = QGroupBox()
        self._bind(box, key, lambda w, t: w.setTitle(t))
        return box

    @staticmethod
    def _scrollable(content: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(content)
        return area

    def _build_ui(self) -> None:
        self.setMinimumSize(880, 620)
        self.resize(1040, 720)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 8)
        outer.setSpacing(8)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self.tabs.addTab(self._build_connection_tab(), "")
        self.tabs.addTab(self._build_actions_tab(), "")
        self.tabs.addTab(self._build_displays_tab(), "")
        self.tabs.addTab(self._build_settings_tab(), "")

        self.setStatusBar(QStatusBar(self))
        self.version_label = QLabel()
        self.statusBar().addPermanentWidget(self.version_label)

    # --------------------------------------------------------- connection
    def _build_connection_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        device = self._group("#GROUP_DEVICE")
        form = QFormLayout(device)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.ip_edit = QLineEdit()
        self.ip_edit.setClearButtonEnabled(True)
        self.ip_edit.setMaximumWidth(360)
        self._bind(self.ip_edit, "#PLACEHOLDER_IP", lambda w, t: w.setPlaceholderText(t))
        self.user_edit = QLineEdit()
        self.user_edit.setMaximumWidth(220)
        self.pw_edit = QLineEdit()
        self.pw_edit.setMaximumWidth(320)
        self.pw_edit.setEchoMode(QLineEdit.EchoMode.Password)

        pw_row = QWidget()
        pw_layout = QHBoxLayout(pw_row)
        pw_layout.setContentsMargins(0, 0, 0, 0)
        pw_layout.setSpacing(6)
        pw_layout.addWidget(self.pw_edit, 1)
        self.show_pw_btn = QPushButton()
        self.show_pw_btn.setFixedWidth(64)
        self._bind(self.show_pw_btn, "#BTN_SHOW")
        pw_layout.addWidget(self.show_pw_btn)
        pw_layout.addStretch(1)

        form.addRow(self._bind(QLabel(), "#LABEL_IP"), self.ip_edit)
        form.addRow(self._bind(QLabel(), "#LABEL_USER"), self.user_edit)
        form.addRow(self._bind(QLabel(), "#LABEL_PASSWORD"), pw_row)

        test_row = QWidget()
        test_layout = QHBoxLayout(test_row)
        test_layout.setContentsMargins(0, 0, 0, 0)
        test_layout.setSpacing(10)
        self.test_btn = QPushButton()
        self._bind(self.test_btn, "#BTN_TEST")
        self.conn_status = QLabel()
        self.conn_status.setWordWrap(True)
        test_layout.addWidget(self.test_btn)
        test_layout.addWidget(self.conn_status, 1)
        form.addRow("", test_row)

        self.cert_value = QLabel()
        self.cert_value.setWordWrap(True)
        self.cert_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cert_value.setStyleSheet("color: #8a94a6; font-family: Consolas, monospace;")
        form.addRow(self._bind(QLabel(), "#LABEL_CERT"), self.cert_value)

        self.note_ip = QLabel()
        self.note_ip.setWordWrap(True)
        self._bind(self.note_ip, "#NOTE_IP")
        form.addRow(self.note_ip)

        # ------------------------------------------------------- detection
        detect = self._group("#GROUP_DETECTION")
        dform = QFormLayout(detect)
        dform.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        dform.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        poll_row = QWidget()
        poll_layout = QHBoxLayout(poll_row)
        poll_layout.setContentsMargins(0, 0, 0, 0)
        poll_layout.setSpacing(6)
        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(1, 60)
        self.poll_spin.setValue(5)
        self.poll_spin.setFixedWidth(80)
        poll_layout.addWidget(self.poll_spin)
        self.poll_unit = QLabel()
        self._bind(self.poll_unit, "#UNIT_SECONDS", lambda w, t: w.setText(t))
        poll_layout.addWidget(self.poll_unit)
        poll_layout.addStretch(1)
        dform.addRow(self._bind(QLabel(), "#LABEL_POLL"), poll_row)

        self.state_value = QLabel()
        self.state_value.setStyleSheet("font-weight: 600;")
        dform.addRow(self._bind(QLabel(), "#LABEL_STATE"), self.state_value)
        self.clients_value = QLabel("-")
        dform.addRow(self._bind(QLabel(), "#LABEL_CLIENTS"), self.clients_value)
        self.last_check_value = QLabel("-")
        dform.addRow(self._bind(QLabel(), "#LABEL_LAST_CHECK"), self.last_check_value)
        self.reason_value = QLabel("-")
        self.reason_value.setWordWrap(True)
        self.reason_value.setStyleSheet("color: #8a94a6;")
        dform.addRow(self._bind(QLabel(), "#LABEL_REASON"), self.reason_value)
        self.service_value = QLabel("-")
        self.service_value.setWordWrap(True)
        dform.addRow(self._bind(QLabel(), "#LABEL_SERVICE"), self.service_value)

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(8)
        self.start_btn = QPushButton()
        self._bind(self.start_btn, "#BTN_START")
        self.stop_btn = QPushButton()
        self._bind(self.stop_btn, "#BTN_STOP")
        self.check_now_btn = QPushButton()
        self._bind(self.check_now_btn, "#BTN_CHECK_NOW")
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addWidget(self.check_now_btn)
        btn_layout.addStretch(1)
        dform.addRow("", btn_row)

        self.auto_detect_check = QCheckBox()
        self._bind(self.auto_detect_check, "#CHECK_AUTO_DETECT")
        dform.addRow("", self.auto_detect_check)

        self.note_detection = QLabel()
        self.note_detection.setWordWrap(True)
        self._bind(self.note_detection, "#NOTE_DETECTION")
        dform.addRow(self.note_detection)

        # ------------------------------------------------------------- log
        log_group = self._group("#GROUP_LOG")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        self.log_view.setMinimumHeight(120)
        self.log_view.setMaximumHeight(180)
        font = self.log_view.font()
        font.setFamily("Consolas")
        font.setPointSize(9)
        self.log_view.setFont(font)
        log_layout.addWidget(self.log_view)
        log_buttons = QHBoxLayout()
        self.clear_log_btn = QPushButton()
        self._bind(self.clear_log_btn, "#BTN_CLEAR_LOG")
        self.open_log_btn = QPushButton()
        self._bind(self.open_log_btn, "#BTN_OPEN_LOG")
        log_buttons.addWidget(self.clear_log_btn)
        log_buttons.addWidget(self.open_log_btn)
        log_buttons.addStretch(1)
        log_layout.addLayout(log_buttons)

        layout.addWidget(device)
        layout.addWidget(detect)
        layout.addWidget(log_group)
        layout.addStretch(1)
        return self._scrollable(page)

    # ------------------------------------------------------------ actions
    def _build_actions_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        master = self._group("#GROUP_GUARD")
        mv = QVBoxLayout(master)
        self.auto_apply_check = QCheckBox()
        self._bind(self.auto_apply_check, "#CHECK_GUARD_ENABLED")
        font = self.auto_apply_check.font()
        font.setPointSize(11)
        font.setBold(True)
        self.auto_apply_check.setFont(font)
        self.guard_note = QLabel()
        self.guard_note.setWordWrap(True)
        self.guard_note.setStyleSheet("color: #8a94a6;")
        self._bind(self.guard_note, "#NOTE_GUARD_MASTER")
        mv.addWidget(self.auto_apply_check)
        mv.addWidget(self.guard_note)

        on_connect = self._group("#GROUP_ON_CONNECT")
        v1 = QVBoxLayout(on_connect)
        self.on_connect_radios: dict[str, QRadioButton] = {}
        self.on_connect_group = QButtonGroup(self)
        self.on_connect_group.setExclusive(True)
        for value, key in (
            ("kvm-only", "#ON_CONNECT_KVM_ONLY"),
            ("clone", "#ON_CONNECT_CLONE"),
            ("extend", "#ON_CONNECT_EXTEND"),
            ("hold", "#ON_CONNECT_HOLD"),
        ):
            radio = QRadioButton()
            self._bind(radio, key)
            self.on_connect_radios[value] = radio
            self.on_connect_group.addButton(radio)
            v1.addWidget(radio)

        on_disconnect = self._group("#GROUP_ON_DISCONNECT")
        v2 = QVBoxLayout(on_disconnect)
        self.on_disconnect_radios: dict[str, QRadioButton] = {}
        self.on_disconnect_group = QButtonGroup(self)
        self.on_disconnect_group.setExclusive(True)
        for value, key in (
            ("restore", "#ON_DISCONNECT_RESTORE"),
            ("screen", "#ON_DISCONNECT_SCREEN"),
            ("clone", "#ON_DISCONNECT_CLONE"),
            ("hold", "#ON_DISCONNECT_HOLD"),
        ):
            radio = QRadioButton()
            self._bind(radio, key)
            self.on_disconnect_radios[value] = radio
            self.on_disconnect_group.addButton(radio)
            if value == "screen":
                row = QWidget()
                h = QHBoxLayout(row)
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(8)
                h.addWidget(radio)
                self.screen_combo = QComboBox()
                self.screen_combo.addItem("1", 1)
                self.screen_combo.addItem("2", 2)
                self.screen_combo.setFixedWidth(64)
                h.addWidget(self.screen_combo)
                h.addStretch(1)
                v2.addWidget(row)
            else:
                v2.addWidget(radio)

        self.snapshot_label = QLabel()
        self.snapshot_label.setWordWrap(True)
        self.snapshot_label.setStyleSheet("color: #8a94a6;")
        self._bind(self.snapshot_label, "#SNAPSHOT_PENDING")
        v2.addSpacing(4)
        v2.addWidget(self._bind(QLabel(), "#LABEL_SNAPSHOT", lambda w, t: w.setText(t + "：")))
        v2.addWidget(self.snapshot_label)

        action_row = QHBoxLayout()
        self.apply_btn = QPushButton()
        self._bind(self.apply_btn, "#BTN_APPLY")
        self.restore_btn = QPushButton()
        self._bind(self.restore_btn, "#BTN_RESTORE")
        action_row.addWidget(self.apply_btn)
        action_row.addWidget(self.restore_btn)
        action_row.addStretch(1)
        v2.addLayout(action_row)

        self.hdr_check = QCheckBox()
        self._bind(self.hdr_check, "#LABEL_HDR")
        v2.addWidget(self.hdr_check)

        self.action_status = QLabel()
        self.action_status.setWordWrap(True)
        self.action_status.setStyleSheet("color: #8a94a6;")
        v2.addWidget(self.action_status)

        self.note_actions = QLabel()
        self.note_actions.setWordWrap(True)
        self._bind(self.note_actions, "#NOTE_ACTIONS")
        v2.addWidget(self.note_actions)

        layout.addWidget(master)
        layout.addWidget(on_connect)
        layout.addWidget(on_disconnect)
        layout.addStretch(1)
        return self._scrollable(page)

    # ----------------------------------------------------------- displays
    def _build_displays_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        displays = self._group("#GROUP_DISPLAYS")
        dv = QVBoxLayout(displays)
        top_row = QHBoxLayout()
        self.refresh_displays_btn = QPushButton()
        self._bind(self.refresh_displays_btn, "#BTN_REFRESH")
        self.display_status = QLabel()
        self.display_status.setWordWrap(True)
        self.display_status.setStyleSheet("color: #8a94a6;")
        top_row.addWidget(self.refresh_displays_btn)
        top_row.addWidget(self.display_status, 1)
        dv.addLayout(top_row)
        self.display_table = QTableWidget(0, 7)
        self.display_table.verticalHeader().setVisible(False)
        self.display_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.display_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.display_table.setMinimumHeight(150)
        self.display_table.horizontalHeader().setStretchLastSection(True)
        dv.addWidget(self.display_table)
        self.note_displays = QLabel()
        self.note_displays.setWordWrap(True)
        self._bind(self.note_displays, "#NOTE_DISPLAYS")
        dv.addWidget(self.note_displays)

        lock = self._group("#GROUP_LOCK")
        lv = QVBoxLayout(lock)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addWidget(self._bind(QLabel(), "#LABEL_LOCK_PREFIX"))
        self.lock_combo = QComboBox()
        self.lock_combo.setMinimumWidth(440)
        self.lock_combo.setMaximumWidth(720)
        h.addWidget(self.lock_combo)
        h.addWidget(self._bind(QLabel(), "#LABEL_LOCK_SUFFIX"))
        h.addStretch(1)
        lv.addWidget(row)
        self.note_lock = QLabel()
        self.note_lock.setWordWrap(True)
        self._bind(self.note_lock, "#NOTE_LOCK")
        lv.addWidget(self.note_lock)

        layout.addWidget(displays)
        layout.addWidget(lock)
        layout.addStretch(1)
        return self._scrollable(page)

    # ----------------------------------------------------------- settings
    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        service_group = self._group("#GROUP_SERVICE")
        sv = QVBoxLayout(service_group)
        self.settings_service_value = QLabel("—")
        self.settings_service_value.setWordWrap(True)
        self.settings_service_value.setStyleSheet("color: #8a94a6;")
        sv.addWidget(self.settings_service_value)
        service_buttons = QHBoxLayout()
        self.service_start_btn = QPushButton()
        self._bind(self.service_start_btn, "#BTN_SERVICE_START")
        self.service_stop_btn = QPushButton()
        self._bind(self.service_stop_btn, "#BTN_SERVICE_STOP")
        self.service_uninstall_btn = QPushButton()
        self._bind(self.service_uninstall_btn, "#BTN_SERVICE_UNINSTALL")
        service_buttons.addWidget(self.service_start_btn)
        service_buttons.addWidget(self.service_stop_btn)
        service_buttons.addWidget(self.service_uninstall_btn)
        service_buttons.addStretch(1)
        sv.addLayout(service_buttons)
        self.service_action_status = QLabel()
        self.service_action_status.setWordWrap(True)
        self.service_action_status.setStyleSheet("color: #8a94a6;")
        sv.addWidget(self.service_action_status)
        self.note_service_control = QLabel()
        self.note_service_control.setWordWrap(True)
        self._bind(self.note_service_control, "#NOTE_SERVICE_CONTROL")
        sv.addWidget(self.note_service_control)

        appearance = self._group("#GROUP_APPEARANCE")
        aform = QFormLayout(appearance)
        aform.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.lang_combo = QComboBox()
        self.lang_combo.setMinimumWidth(180)
        self.lang_combo.setMaximumWidth(260)
        self.theme_combo = QComboBox()
        self.theme_combo.setMinimumWidth(180)
        self.theme_combo.setMaximumWidth(260)
        aform.addRow(self._bind(QLabel(), "#LABEL_LANGUAGE"), self.lang_combo)
        aform.addRow(self._bind(QLabel(), "#LABEL_THEME"), self.theme_combo)

        tray_group = self._group("#GROUP_TRAY")
        tv = QVBoxLayout(tray_group)
        self.minimize_check = QCheckBox()
        self._bind(self.minimize_check, "#CHECK_MINIMIZE")
        self.autostart_check = QCheckBox()
        self._bind(self.autostart_check, "#CHECK_AUTOSTART")
        tv.addWidget(self.minimize_check)
        tv.addWidget(self.autostart_check)

        data_group = self._group("#GROUP_DATA")
        dv = QVBoxLayout(data_group)
        self.config_path_label = QLabel(str(self.config.path))
        self.log_path_label = QLabel(str(LOGS_DIR))
        self.data_path_label = QLabel(str(DATA_DIR))
        for label in (self.config_path_label, self.log_path_label, self.data_path_label):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setStyleSheet("color: #8a94a6;")
        dv.addWidget(self._bind(QLabel(), "#LABEL_CONFIG_PATH"))
        dv.addWidget(self.config_path_label)
        dv.addWidget(self._bind(QLabel(), "#LABEL_LOG_PATH"))
        dv.addWidget(self.log_path_label)
        dv.addWidget(self._bind(QLabel(), "#LABEL_DATA_PATH"))
        dv.addWidget(self.data_path_label)
        data_buttons = QHBoxLayout()
        self.open_config_btn = QPushButton()
        self._bind(self.open_config_btn, "#BTN_OPEN_CONFIG_DIR")
        self.open_data_btn = QPushButton()
        self._bind(self.open_data_btn, "#BTN_OPEN_DATA_DIR")
        data_buttons.addWidget(self.open_config_btn)
        data_buttons.addWidget(self.open_data_btn)
        data_buttons.addStretch(1)
        dv.addLayout(data_buttons)

        about = self._group("#GROUP_ABOUT")
        av = QVBoxLayout(about)
        self.about_label = QLabel()
        self.about_label.setWordWrap(True)
        self._bind(self.about_label, "#ABOUT")
        av.addWidget(self.about_label)

        layout.addWidget(service_group)
        layout.addWidget(appearance)
        layout.addWidget(tray_group)
        layout.addWidget(data_group)
        layout.addWidget(about)
        layout.addStretch(1)
        return self._scrollable(page)

    # ------------------------------------------------------------- menus
    def _action(self, key: str, slot, shortcut: str | None = None, checkable: bool = False) -> QAction:
        action = QAction(self)
        self._bind(action, key, lambda w, t: w.setText(t))
        action.triggered.connect(slot)
        if shortcut:
            action.setShortcut(shortcut)
        if checkable:
            action.setCheckable(True)
        return action

    def _build_actions(self) -> None:
        self.quit_action = self._action("#ACTION_QUIT", self._quit, "Ctrl+Q")
        self.check_action = self._action("#ACTION_CHECK_NOW", self._on_check_now, "F5")
        self.pause_action = QAction(self)
        self.pause_action.setCheckable(True)
        self.pause_action.triggered.connect(self._on_pause_action)
        self.open_config_action = self._action("#ACTION_OPEN_CONFIG", self._open_config_dir)
        self.open_log_action = self._action("#ACTION_OPEN_LOG", self._open_log_dir)
        self.about_action = self._action("#ACTION_ABOUT", self._show_about)
        self.show_action = self._action("#ACTION_SHOW", self._show_from_tray)
        self.tray_pause_action = QAction(self)
        self.tray_pause_action.setCheckable(True)
        self.tray_pause_action.triggered.connect(self._on_pause_action)

    def _build_menu(self) -> None:
        bar = self.menuBar()
        self.menu_file = bar.addMenu("")
        self.menu_tools = bar.addMenu("")
        self.menu_help = bar.addMenu("")
        self._bind(self.menu_file, "#MENU_FILE", lambda w, t: w.setTitle(t))
        self._bind(self.menu_tools, "#MENU_TOOLS", lambda w, t: w.setTitle(t))
        self._bind(self.menu_help, "#MENU_HELP", lambda w, t: w.setTitle(t))
        self.menu_file.addAction(self.quit_action)
        self.menu_tools.addAction(self.check_action)
        self.menu_tools.addAction(self.pause_action)
        self.menu_tools.addSeparator()
        self.menu_tools.addAction(self.open_config_action)
        self.menu_tools.addAction(self.open_log_action)
        self.menu_help.addAction(self.about_action)

    def _build_tray(self) -> None:
        icon = self._make_icon()
        self.setWindowIcon(icon)
        self.app.setWindowIcon(icon)
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            self.minimize_check.setChecked(False)
            self._bind_tip(self.minimize_check, "#ERR_TRAY_UNAVAILABLE")
            return
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(self.i18n.tr("#APP_TITLE"))
        self.tray_menu = QMenu(self)
        self.tray_menu.addAction(self.show_action)
        self.tray_menu.addAction(self.check_action)
        self.tray_menu.addAction(self.tray_pause_action)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(self.quit_action)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    @staticmethod
    def _make_icon() -> QIcon:
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2d7ff9"))
        painter.drawRoundedRect(2, 4, 28, 20, 4, 4)
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(7, 9, 18, 10, 2, 2)
        painter.setPen(QPen(QColor("#2d7ff9"), 2))
        painter.drawLine(16, 24, 16, 27)
        painter.drawLine(11, 29, 21, 29)
        painter.end()
        return QIcon(pixmap)

    # ------------------------------------------------------------- events
    def _bind_events(self) -> None:
        self.test_btn.clicked.connect(self._on_test)
        self.show_pw_btn.clicked.connect(self._toggle_password)
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)
        self.check_now_btn.clicked.connect(self._on_check_now)
        self.clear_log_btn.clicked.connect(self.log_view.clear)
        self.open_log_btn.clicked.connect(self._open_log_dir)
        self.open_config_btn.clicked.connect(self._open_config_dir)
        self.open_data_btn.clicked.connect(self._open_data_dir)
        self.poll_spin.valueChanged.connect(self._on_poll_changed)
        self.auto_detect_check.toggled.connect(self._on_auto_detect_changed)
        self.minimize_check.toggled.connect(self._on_minimize_changed)
        self.autostart_check.toggled.connect(self._on_autostart_changed)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self.lock_combo.currentIndexChanged.connect(self._on_lock_changed)
        self.screen_combo.currentIndexChanged.connect(self._on_actions_changed)
        self.hdr_check.toggled.connect(self._on_actions_changed)
        for radio in self.on_connect_radios.values():
            radio.toggled.connect(self._on_actions_changed)
        for radio in self.on_disconnect_radios.values():
            radio.toggled.connect(self._on_actions_changed)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.ip_edit.textChanged.connect(lambda *_: self._update_ui_state())
        self.user_edit.textChanged.connect(lambda *_: self._update_ui_state())
        self.pw_edit.textChanged.connect(lambda *_: self._update_ui_state())
        self.ip_edit.editingFinished.connect(self._save_connection)
        self.user_edit.editingFinished.connect(self._save_connection)
        self.pw_edit.editingFinished.connect(self._save_connection)
        self.refresh_displays_btn.clicked.connect(self._on_refresh_displays)
        self.apply_btn.clicked.connect(self._on_capture)
        self.restore_btn.clicked.connect(self._on_manual_restore)
        self.auto_apply_check.toggled.connect(self._on_auto_apply_changed)
        self.service_start_btn.clicked.connect(self._on_service_start)
        self.service_stop_btn.clicked.connect(self._on_service_stop)
        self.service_uninstall_btn.clicked.connect(self._on_service_uninstall)

    # ============================================================ config
    def _load_config(self) -> None:
        cfg = self.config
        blocked = [self.poll_spin, self.auto_detect_check, self.minimize_check,
                   self.autostart_check, self.hdr_check, self.screen_combo,
                   self.lock_combo, self.auto_apply_check]
        blocked += list(self.on_connect_radios.values())
        blocked += list(self.on_disconnect_radios.values())
        blockers = [QSignalBlocker(w) for w in blocked]
        try:
            self.ip_edit.setText(cfg.get("connection", "ip"))
            self.user_edit.setText(cfg.get("connection", "user"))
            self.pw_edit.setText(cfg.password())
            self.poll_spin.setValue(cfg.get_int("connection", "poll_seconds", 5))
            self.auto_detect_check.setChecked(cfg.get_bool("connection", "auto_detect_on_launch", True))

            self._select_radio(self.on_connect_radios, cfg.get("actions", "on_connect", "kvm-only"))
            self._select_radio(self.on_disconnect_radios, cfg.get("actions", "on_disconnect", "restore"))
            self.screen_combo.setCurrentIndex(
                max(0, self.screen_combo.findData(cfg.get_int("actions", "screen_number", 1)))
            )
            self.hdr_check.setChecked(cfg.get_bool("actions", "hdr_restore", True))
            self.auto_apply_check.setChecked(cfg.get_bool("actions", "auto_apply", True))

            self._repopulate_locks()
            lock_value = self._lock_combo_value()
            idx = self.lock_combo.findData(lock_value)
            self.lock_combo.setCurrentIndex(idx if idx >= 0 else 0)

            self.minimize_check.setChecked(cfg.get_bool("ui", "minimize_to_tray", True))
            self.autostart_check.setChecked(cfg.get_bool("ui", "start_at_login", False))
            if self.tray is None:
                self.minimize_check.setChecked(False)
        finally:
            del blockers

        self._update_ui_state()
        self._sync_screen_enabled()
        self._update_cert_label()
        self.conn_status.setText(self.i18n.tr("#STATE_DISCONNECTED"))
        self.conn_status.setStyleSheet("color: #8a94a6;")
        if self.autostart_check.isChecked():
            self._apply_autostart(True)

    def _select_radio(self, radios: dict[str, QRadioButton], value: str) -> None:
        radio = radios.get(value)
        if radio is None:
            radio = next(iter(radios.values()))
        radio.setChecked(True)

    def _save_connection(self) -> None:
        self.config.set("connection", "ip", self.ip_edit.text().strip())
        self.config.set("connection", "user", self.user_edit.text().strip())
        try:
            self.config.set_password(self.pw_edit.text())
        except Exception as exc:
            self.logger.error("Could not protect password with DPAPI: %s", exc)
        self.config.set("connection", "poll_seconds", self.poll_spin.value())
        self.config.set_bool("connection", "auto_detect_on_launch", self.auto_detect_check.isChecked())
        self.config.save()
        if self._service_mode and self.service_client is not None and not self._quitting:
            self.service_client.call("sync_password", password=self.pw_edit.text())
            self.service_client.call("reload_config")

    def _on_poll_changed(self, value: int) -> None:
        self.config.set("connection", "poll_seconds", value)
        self.config.save()
        if self._poll_running:
            self._on_stop()
            self._on_start()

    def _on_auto_detect_changed(self, checked: bool) -> None:
        self.config.set_bool("connection", "auto_detect_on_launch", checked)
        self.config.save()

    def _on_actions_changed(self, *_args) -> None:
        if not self._texts:
            return
        for value, radio in self.on_connect_radios.items():
            if radio.isChecked():
                self.config.set("actions", "on_connect", value)
        for value, radio in self.on_disconnect_radios.items():
            if radio.isChecked():
                self.config.set("actions", "on_disconnect", value)
        self.config.set("actions", "screen_number", self.screen_combo.currentData() or 1)
        self.config.set_bool("actions", "hdr_restore", self.hdr_check.isChecked())
        self.config.save()
        self._sync_screen_enabled()
        self._update_snapshot_label()
        self._service_reload_config()
        log_action(self.logger, "save_actions", on_connect=self.config.get("actions", "on_connect"))

    def _sync_screen_enabled(self) -> None:
        self.screen_combo.setEnabled(self.on_disconnect_radios["screen"].isChecked())

    def _on_lock_changed(self, *_args) -> None:
        value = self.lock_combo.currentData() or "none"
        top = self._topology
        kvm = ""
        slot = 0
        mode = "none"
        if value == "none":
            self.config.set("actions", "kvm_bind_mode", "none")
            self.config.set("actions", "kvm_slot", "0")
            self.config.set("actions", "kvm_slot_key", "none")
            self.config.set("actions", "kvm_target", "none")
            self.config.set("actions", "kvm_screen", "none")
            self.config.save()
        elif value.startswith("slot:"):
            mode = "slot"
            try:
                slot = int(value[5:])
            except ValueError:
                slot = 0
            display = None
            if top is not None and slot > 0:
                slots = self._slot_map(top)
                display = next(
                    (d for d in top.connected if slots.get(d.key) == slot), None
                )
            if display is not None and top is not None:
                slot = self._save_slot_binding(display, top)
                kvm = display.key
            else:
                # Saved slot but monitor currently missing: keep the connector
                # fingerprint; never bind a different monitor automatically.
                self.config.set("actions", "kvm_bind_mode", "slot")
                self.config.set("actions", "kvm_slot", str(slot))
                self.config.set("actions", "kvm_screen", str(slot) if slot else "none")
                self.config.save()
        elif value.startswith("target:"):
            mode = "target"
            target_key = value[7:]
            display = None
            if top is not None:
                display = next(
                    (d for d in top.connected if d.key == target_key), None
                )
            if display is not None and top is not None:
                slot = self._save_target_binding(display, top)
                kvm = display.key
            else:
                self.config.set("actions", "kvm_bind_mode", "target")
                self.config.set("actions", "kvm_target", target_key)
                self.config.set("actions", "kvm_screen", "none")
                self.config.save()
        if top is not None:
            top.kvm_key = kvm
            self._populate_display_table(top)
            self._update_display_status(top)
        self._update_snapshot_label()
        self._update_ui_state()
        self._service_reload_config()
        log_action(self.logger, "set_kvm_binding", mode=mode, slot=slot, target=kvm)

    def _on_tab_changed(self, index: int) -> None:
        self.config.set("window", "last_tab", index)
        self.config.save()

    # ------------------------------------------------------------- language
    def _on_language_changed(self, *_args) -> None:
        code = self.lang_combo.currentData()
        if not code or code == self.i18n.code:
            return
        self.i18n.load(code)
        self.config.set("ui", "language", code)
        self.config.save()
        log_action(self.logger, "set_language", language=code)
        self._retranslate()

    def _on_theme_changed(self, *_args) -> None:
        theme_id = self.theme_combo.currentData()
        if not theme_id:
            return
        self.themes.apply(self.app, theme_id)
        self.config.set("ui", "theme", theme_id)
        self.config.save()
        log_action(self.logger, "set_theme", theme=theme_id)

    def _apply_theme(self) -> None:
        self.themes.apply(self.app, self.config.get("ui", "theme", "light"))

    # --------------------------------------------------------------- tray
    def _on_minimize_changed(self, checked: bool) -> None:
        self.config.set_bool("ui", "minimize_to_tray", checked)
        self.config.save()
        log_action(self.logger, "set_minimize_to_tray", enabled=checked)

    def _on_autostart_changed(self, checked: bool) -> None:
        ok = self._apply_autostart(checked)
        if not ok:
            blocker = QSignalBlocker(self.autostart_check)
            self.autostart_check.setChecked(not checked)
            del blocker
            self.statusBar().showMessage(self.i18n.tr("#ERR_AUTOSTART"), 5000)
            return
        self.config.set_bool("ui", "start_at_login", checked)
        self.config.save()
        log_action(self.logger, "set_autostart", enabled=checked)

    def _autostart_command(self) -> str:
        exe = Path(sys.executable)
        if not getattr(sys, "frozen", False):
            pythonw = exe.with_name("pythonw.exe")
            if pythonw.exists():
                exe = pythonw
        main = APP_DIR / "main.py"
        return f'"{exe}" "{main}" --minimized'

    def _apply_autostart(self, enabled: bool) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, self._autostart_command())
                else:
                    try:
                        winreg.DeleteValue(key, RUN_VALUE)
                    except FileNotFoundError:
                        pass
            return True
        except OSError as exc:
            self.logger.error("Could not update autostart: %s", exc)
            return False

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ============================================================ testing
    def _toggle_password(self) -> None:
        if self.pw_edit.echoMode() == QLineEdit.EchoMode.Password:
            self.pw_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.pw_edit.setEchoMode(QLineEdit.EchoMode.Password)

    def _on_test(self) -> None:
        if self._test_thread is not None:
            return
        ip = self.ip_edit.text().strip()
        if not ip:
            self._warn(self.i18n.tr("#ERR_IP_EMPTY"))
            self.ip_edit.setFocus()
            return
        self._save_connection()
        settings = self.config.connection_snapshot()
        settings["password"] = self.pw_edit.text()
        # Never send the password before the certificate is pinned: the first
        # run probes TLS only, the user trusts the fingerprint, then the
        # thread-finished handler automatically retries with the login.
        settings["probe_only"] = not bool(settings.get("pin"))
        log_action(self.logger, "test_connection", ip=ip, has_password=bool(settings["password"]))
        self._test_busy = True
        self._update_ui_state()
        self._set_conn_status("#STATUS_TESTING", "#8a94a6")
        self.test_btn.setEnabled(False)

        self._test_thread = QThread(self)
        self._test_worker = TestWorker(settings)
        self._test_worker.moveToThread(self._test_thread)
        self._test_thread.started.connect(self._test_worker.run)
        self._test_worker.done.connect(self._on_test_done)
        self._test_worker.done.connect(self._test_thread.quit)
        self._test_thread.finished.connect(self._test_worker.deleteLater)
        self._test_thread.finished.connect(self._test_thread.deleteLater)
        self._test_thread.finished.connect(self._on_test_thread_finished)
        self._test_thread.start()

    def _on_test_thread_finished(self) -> None:
        self._test_thread = None
        self._test_worker = None
        self._test_busy = False
        self._update_ui_state()
        if self._retest_after_trust:
            self._retest_after_trust = False
            QTimer.singleShot(0, self._on_test)

    def _on_test_done(self, payload: dict) -> None:
        if not payload.get("ok"):
            code = payload.get("code", "unknown")
            fingerprint = payload.get("fingerprint", "")
            if code == "cert":
                self._handle_cert_mismatch(payload.get("expected", ""), payload.get("actual", ""))
            else:
                self._set_conn_status(self._error_key(code), "#d9534f")
                self.statusBar().showMessage(self._short_error(code, payload.get("message", "")), 6000)
            self._authenticated = False
            self._update_ui_state()
            return

        result: TestResult = payload["result"]
        fingerprint = result.fingerprint
        trusted_now = False
        log.info(
            "Test result: authenticated=%s fingerprint=%s latency=%sms",
            result.authenticated,
            fingerprint,
            result.latency_ms,
        )

        if not self.config.get("connection", "cert_sha256", ""):
            if not self._confirm(
                self.i18n.tr("#TRUST_TITLE"),
                self.i18n.tr("#TRUST_TEXT").format(fp=_format_fp(fingerprint)),
            ):
                self._set_conn_status("#STATUS_TRUST_REQUIRED", "#d9902f")
                self._update_ui_state()
                return
            self.config.set("connection", "cert_sha256", fingerprint)
            self.config.save()
            self._service_reload_config()
            trusted_now = True
            log_action(self.logger, "trust_certificate", fingerprint=fingerprint)

        self._update_cert_label()

        if result.authenticated:
            self._authenticated = True
            self._set_conn_status("#STATUS_CONNECTED", "#2fbf71", ms=result.latency_ms)
            self.statusBar().showMessage(self.i18n.tr("#STATUS_CONNECTED", ms=result.latency_ms), 5000)
        else:
            self._authenticated = False
            self._set_conn_status("#STATUS_TLS_ONLY", "#d9902f")
            if trusted_now and self.pw_edit.text():
                self._retest_after_trust = True
        self._update_ui_state()

    def _handle_cert_mismatch(self, expected: str, actual: str) -> None:
        log.warning("Certificate fingerprint changed")
        if self._confirm(
            self.i18n.tr("#CERT_MISMATCH_TITLE"),
            self.i18n.tr("#CERT_MISMATCH_TEXT").format(
                expected=_format_fp(expected), actual=_format_fp(actual)
            ),
        ):
            self.config.set("connection", "cert_sha256", actual)
            self.config.save()
            self._service_reload_config()
            self._update_cert_label()
            self._set_conn_status("#STATUS_CERT_UPDATED", "#d9902f")
            log_action(self.logger, "update_certificate", fingerprint=actual)
        else:
            self._set_conn_status(self._error_key("cert"), "#d9534f")

    def _error_key(self, code: str) -> str:
        return {
            "auth": "#REASON_AUTH",
            "two_step": "#REASON_TWO_STEP",
            "rate": "#REASON_RATE",
            "network": "#REASON_NETWORK",
            "http": "#REASON_HTTP",
            "cert": "#REASON_CERT",
        }.get(code, "#REASON_UNKNOWN")

    def _short_error(self, code: str, message: str) -> str:
        return self.i18n.tr("#STATUS_ERROR", message=_short(message or self.i18n.tr(self._error_key(code))))

    # ============================================================ detection
    def _on_start(self) -> None:
        if self._poll_running:
            return
        if self._service_mode:
            if not self._service_connected or self.service_client is None:
                self._warn(self.i18n.tr("#SERVICE_DISCONNECTED"))
                return
            self._save_connection()
            self.service_client.call("start")
            self._set_conn_status("#STATE_STARTING", "#2f7ff0")
            log_action(self.logger, "start_detection_service")
            return
        ip = self.ip_edit.text().strip()
        if not ip:
            self._warn(self.i18n.tr("#ERR_IP_EMPTY"))
            return
        self._save_connection()
        settings = self.config.connection_snapshot()
        if not settings["password"]:
            self._warn(self.i18n.tr("#ERR_AUTH_EMPTY"))
            return
        if not settings["pin"]:
            self._warn(self.i18n.tr("#ERR_PIN_MISSING"))
            return

        log_action(self.logger, "start_detection", ip=settings["ip"], interval=settings["poll_seconds"])
        self._disconnect_timer.stop()
        self._watching_since = 0.0
        self._poll_thread = QThread(self)
        self._poll_worker = PollWorker(settings)
        self._poll_worker.moveToThread(self._poll_thread)
        self._poll_thread.started.connect(self._poll_worker.run)
        self._poll_worker.result.connect(self._on_poll_result)
        self._poll_thread.finished.connect(self._poll_worker.deleteLater)
        self._poll_thread.finished.connect(self._on_poll_finished)
        self._poll_running = True
        self._pending_first_result = True
        self._det_state = "unknown"
        self._idle_misses = 0
        self._last_detection = None
        self._last_check_text = "-"
        self._poll_thread.start()
        self._update_ui_state()
        self._set_conn_status("#STATUS_DETECTING", "#2f7ff0")

    def _on_stop(self) -> None:
        if self._service_mode:
            self._disconnect_timer.stop()
            self._watching_since = 0.0
            if self._service_connected and self.service_client is not None:
                self.service_client.call("stop")
            self._poll_running = False
            self._update_ui_state()
            self._set_conn_status("#STATE_STOPPED", "#8a94a6")
            log_action(self.logger, "stop_detection_service")
            return
        self._disconnect_timer.stop()
        self._watching_since = 0.0
        worker = self._poll_worker
        thread = self._poll_thread
        if worker is not None:
            worker.stop()
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        self._poll_worker = None
        self._poll_thread = None
        self._poll_running = False
        self._pending_first_result = False
        self._update_ui_state()
        self._set_conn_status("#STATE_STOPPED", "#8a94a6")
        log_action(self.logger, "stop_detection")

    def _on_poll_finished(self) -> None:
        self._poll_running = False
        self._poll_worker = None
        self._poll_thread = None
        self._update_ui_state()

    def _on_poll_result(self, detection: Detection) -> None:
        self._pending_first_result = False
        self._last_check_text = datetime.now().strftime("%H:%M:%S")
        old_state = self._det_state

        if detection.state == "watching":
            self._det_state = "watching"
            self._idle_misses = 0
        elif detection.state == "idle":
            if self._det_state == "watching":
                self._idle_misses += 1
                if self._idle_misses >= 2:
                    self._det_state = "idle"
            else:
                self._det_state = "idle"
        else:
            # Unknown: keep the last known state, never invent a transition.
            if self._det_state not in ("watching", "idle"):
                self._det_state = "unknown"

        self._last_detection = detection
        if old_state != self._det_state:
            log.info("Detection state: %s -> %s (%s)", old_state, self._det_state, detection.reason)
            self._on_detection_transition(old_state, self._det_state)

        if detection.fatal:
            self._poll_running = False
            self._authenticated = False
            if detection.reason == "cert":
                self._handle_cert_mismatch(
                    self.config.get("connection", "cert_sha256", ""),
                    detection.fingerprint,
                )
            else:
                self._set_conn_status(self._error_key(detection.reason), "#d9534f")
                message = (detection.detail or {}).get("message", "")
                self.statusBar().showMessage(self._short_error(detection.reason, message), 7000)

        self._update_ui_state()
        self._update_tray_tooltip()

    def _on_check_now(self) -> None:
        if self._service_mode:
            if self.service_client is not None:
                self.service_client.call("reload_config")
                self.service_client.call("get_state")
            self.statusBar().showMessage(self.i18n.tr("#STATUS_DETECTING"), 2500)
            return
        if self._poll_running and self._poll_worker is not None:
            self._poll_worker.kick()
            self.statusBar().showMessage(self.i18n.tr("#STATUS_DETECTING"), 2500)
        elif not self._poll_running:
            self._on_start()

    def _on_pause_action(self, _checked: bool) -> None:
        if self._poll_running:
            self._on_stop()
        else:
            self._on_start()
        self._update_pause_actions()

    # ============================================================== service
    def _init_service_client(self) -> None:
        if not self._service_configured:
            return
        self._service_mode = True
        client = ServiceClient()
        client.connected.connect(self._on_service_connected)
        client.state.connect(self._on_service_state)
        client.response.connect(self._on_service_response)
        client.error.connect(lambda message: self.logger.warning("Service client: %s", message))
        self.service_client = client
        client.start()
        self.logger.info("Service client started")

    def _service_reload_config(self) -> None:
        if self._service_mode and self.service_client is not None:
            self.service_client.call("reload_config")

    def _update_service_label(self) -> None:
        if not self._service_configured:
            text = self.i18n.tr("#SERVICE_VALUE_STANDALONE")
            color = "#8a94a6"
        elif self._service_connected and self._service_agent_connected:
            text = self.i18n.tr("#SERVICE_VALUE_OK")
            color = "#2fbf71"
        elif self._service_connected:
            text = self.i18n.tr("#SERVICE_VALUE_AGENT_DOWN")
            color = "#d9902f"
        else:
            text = self.i18n.tr("#SERVICE_VALUE_DOWN")
            color = "#d9534f"
        self.service_value.setText(text)
        self.service_value.setStyleSheet(f"color: {color};")
        settings_label = getattr(self, "settings_service_value", None)
        if settings_label is not None:
            settings_label.setText(text)
            settings_label.setStyleSheet(f"color: {color};")

    def _on_service_connected(self, ok: bool) -> None:
        self._service_mode = self._service_configured
        self._service_connected = bool(ok)
        self.logger.info("Service connection: %s", ok)
        if ok:
            self._set_conn_status("#SERVICE_CONNECTED", "#2fbf71")
            self._update_service_label()
            if self.service_client is not None:
                self.service_client.call("sync_password", password=self.pw_edit.text())
                self.service_client.call("reload_config")
                self.service_client.call("get_state")
                self.service_client.call("query_displays")
            log_action(self.logger, "service_connected")
            self._update_ui_state()
        else:
            self._poll_running = False
            self._service_agent_connected = False
            self._set_conn_status("#SERVICE_DISCONNECTED", "#d9534f")
            self._update_service_label()
            self._update_ui_state()

    def _on_service_state(self, state: dict) -> None:
        self._service_mode = True
        self._service_connected = True
        self._poll_running = not bool(state.get("paused", False))
        self._det_state = str(state.get("state") or "unknown")
        reason = str(state.get("reason") or "")
        self._last_check_text = str(state.get("last_check") or "-")
        self._last_detection = Detection(
            state=self._det_state,
            reason=reason,
            clients=int(state.get("clients") or 0),
        )
        self._pending_first_result = False
        snapshot = state.get("snapshot") or {}
        self._service_agent_connected = bool(state.get("agent_connected", False))
        if snapshot:
            self._snapshot_ok = bool(snapshot.get("ok"))
        key = {
            "watching": "#STATE_WATCHING",
            "idle": "#STATE_IDLE",
        }.get(self._det_state, "#STATE_UNKNOWN")
        color = {
            "watching": "#e08a2e",
            "idle": "#2fbf71",
        }.get(self._det_state, "#8a94a6")
        self._set_conn_status(key, color)
        self._update_service_label()
        if not bool(state.get("agent_connected", False)):
            self.statusBar().showMessage(self.i18n.tr("#SERVICE_AGENT_DOWN"), 5000)
        action = state.get("last_action") or {}
        if action and action != self._last_service_action:
            self._last_service_action = dict(action)
            self._show_service_action(action)
            self.refresh_displays()
        self._update_snapshot_label()
        self._update_ui_state()
        self._update_tray_tooltip()

    def _show_service_action(self, action: dict) -> None:
        if bool(action.get("ok")):
            code = str(action.get("code") or "")
            if code == "noop":
                self._set_action_status("", "#8a94a6")
                self.statusBar().clearMessage()
            elif code == "switched":
                self._set_action_status(self.i18n.tr("#STATUS_ACTION_SWITCHED"), "#2fbf71")
            elif code == "restored":
                self._set_action_status(self.i18n.tr("#STATUS_ACTION_RESTORED"), "#2fbf71")
            elif code == "captured":
                self._set_action_status(self.i18n.tr("#STATUS_ACTION_CAPTURED"), "#2fbf71")
        else:
            reason = self._display_error_text(str(action.get("code") or ""))
            self._set_action_status(
                self.i18n.tr("#STATUS_ACTION_FAILED").format(reason=reason), "#d9534f"
            )

    def _on_service_response(self, message: dict, cmd: str) -> None:
        if cmd == "get_state" and isinstance(message.get("state"), dict):
            self._on_service_state(message["state"])
            return
        if cmd == "query_displays":
            if message.get("ok"):
                self.refresh_displays()
            return
        if cmd == "capture":
            if message.get("ok"):
                self._snapshot_ok = True
                self._set_action_status(self.i18n.tr("#STATUS_ACTION_CAPTURED"), "#2fbf71")
                self.statusBar().showMessage(self.i18n.tr("#STATUS_ACTION_CAPTURED"), 5000)
                self._update_snapshot_label()
                self.refresh_displays()
            else:
                reason = self._display_error_text(str(message.get("code") or ""))
                self._set_action_status(
                    self.i18n.tr("#STATUS_ACTION_FAILED").format(reason=reason), "#d9534f"
                )
            return
        if cmd in ("apply_snapshot", "restore"):
            code = str(message.get("code") or "")
            if message.get("ok") and code == "noop":
                self._set_action_status("", "#8a94a6")
            elif message.get("ok"):
                self._set_action_status(self.i18n.tr("#STATUS_ACTION_RESTORED"), "#2fbf71")
                self.refresh_displays()
            else:
                reason = self._display_error_text(code)
                self._set_action_status(
                    self.i18n.tr("#STATUS_ACTION_FAILED").format(reason=reason), "#d9534f"
                )
            self._update_snapshot_label()
            return
        if cmd in ("start", "stop"):
            self._update_ui_state()
            return
        if cmd in ("ping", "reload_config", "sync_password"):
            return

    # ------------------------------------------------------- service control
    def _set_service_action_status(self, text: str, color: str = "#8a94a6") -> None:
        self.service_action_status.setText(text)
        self.service_action_status.setStyleSheet(f"color: {color};")

    def _update_service_buttons(self) -> None:
        if not hasattr(self, "service_start_btn"):
            return
        installed = bool(self._service_configured)
        running = bool(self._service_connected)
        busy = bool(self._service_busy)
        self.service_start_btn.setEnabled(installed and not running and not busy)
        self.service_stop_btn.setEnabled(installed and running and not busy)
        self.service_uninstall_btn.setEnabled(installed and not busy)

    def _on_service_start(self) -> None:
        if not self._service_configured:
            self._warn(self.i18n.tr("#ERR_SERVICE_NOT_INSTALLED"))
            return
        if self._service_busy:
            return
        self._run_service_action("start")

    def _on_service_stop(self) -> None:
        if not self._service_configured or self._service_busy:
            return
        if not self._confirm(
            self.i18n.tr("#CONFIRM_STOP_SERVICE_TITLE"),
            self.i18n.tr("#CONFIRM_STOP_SERVICE_TEXT"),
        ):
            return
        self._run_service_action("stop")

    def _on_service_uninstall(self) -> None:
        if not self._service_configured or self._service_busy:
            return
        if not self._confirm(
            self.i18n.tr("#CONFIRM_UNINSTALL_SERVICE_TITLE"),
            self.i18n.tr("#CONFIRM_UNINSTALL_SERVICE_TEXT"),
        ):
            return
        self._run_service_action("uninstall")

    def _run_service_action(self, action: str) -> None:
        if self._service_busy:
            return
        self._service_busy = True
        self._set_service_action_status(
            self.i18n.tr("#STATUS_SERVICE_ACTION_RUNNING"), "#2f7ff0"
        )
        self._update_service_buttons()
        worker = ServiceWorker(action, APP_DIR, self)
        worker.done.connect(self._on_service_action_done)
        worker.finished.connect(worker.deleteLater)
        self._service_worker = worker
        log_action(self.logger, "service_control", action=action)
        worker.start()

    def _on_service_action_done(self, action: str, ok: bool, message: str) -> None:
        self._service_busy = False
        self._service_worker = None
        detail = (message or "").strip()[-300:]
        if not ok:
            reason = detail or self.i18n.tr("#ERR_SERVICE_ACTION_UNKNOWN")
            self._set_service_action_status(
                self.i18n.tr("#STATUS_SERVICE_ACTION_FAILED").format(error=reason),
                "#d9534f",
            )
            self.logger.warning("Service action %s failed: %s", action, detail)
            self._update_service_buttons()
            self._update_ui_state()
            return

        if action == "start":
            self._service_configured = SERVICE_JSON.exists()
            self._service_mode = self._service_configured
            self._service_connected = False
            self._service_agent_connected = False
            self._poll_running = False
            self._set_service_action_status(
                self.i18n.tr("#STATUS_SERVICE_STARTED"), "#2fbf71"
            )
            self._set_conn_status("#STATE_STARTING", "#2f7ff0")
            self._restart_service_client()
        elif action == "stop":
            self._service_configured = SERVICE_JSON.exists()
            self._service_connected = False
            self._service_agent_connected = False
            self._poll_running = False
            self._drop_service_client()
            self._set_conn_status("#STATE_STOPPED", "#8a94a6")
            self._set_service_action_status(
                self.i18n.tr("#STATUS_SERVICE_STOPPED"), "#8a94a6"
            )
        elif action == "uninstall":
            self._service_configured = False
            self._service_mode = False
            self._service_connected = False
            self._service_agent_connected = False
            self._poll_running = False
            self._drop_service_client()
            self._set_conn_status("#SERVICE_VALUE_STANDALONE", "#8a94a6")
            self._set_service_action_status(
                self.i18n.tr("#STATUS_SERVICE_UNINSTALLED"), "#2fbf71"
            )
            self._warn(self.i18n.tr("#SERVICE_UNINSTALLED_TEXT"))

        self._update_service_label()
        self._update_ui_state()
        log_action(self.logger, "service_control_done", action=action, ok=ok)

    def _restart_service_client(self) -> None:
        self._drop_service_client()
        if not self._service_configured:
            return
        client = ServiceClient()
        client.connected.connect(self._on_service_connected)
        client.state.connect(self._on_service_state)
        client.response.connect(self._on_service_response)
        client.error.connect(lambda message: self.logger.warning("Service client: %s", message))
        self.service_client = client
        client.start()

    def _drop_service_client(self) -> None:
        client = self.service_client
        self.service_client = None
        if client is not None:
            client.stop()

    # ============================================================ displays
    def _slot_map(self, top: ccd.Topology) -> dict[str, int]:
        """Stable user-facing slot numbers, based on the physical port.

        DisplayPort/eDP sorts before HDMI/DVI, so on this machine the physical
        DP monitor is screen 1 and the DVI KVM is screen 2.  Target id is only
        the last tie-breaker; it is not the lock identity.
        """
        def port_rank(display: ccd.Display) -> tuple[int, int, int, int]:
            if display.output_tech in (10, 11):
                rank = 1
            elif display.output_tech in (5, 6):
                rank = 2
            else:
                rank = 3
            return (rank, display.output_tech, display.connector_instance, display.target_id)

        ordered = sorted(top.connected, key=lambda d: (d.adapter, port_rank(d)))
        return {display.key: index for index, display in enumerate(ordered, 1)}

    def _slot_config(self) -> tuple[int, str, str]:
        return (
            self.config.get_int("actions", "kvm_slot", 0),
            self.config.get("actions", "kvm_slot_key", "").strip(),
            self.config.get("actions", "kvm_target", "").strip(),
        )

    def _bind_mode(self) -> str:
        mode = self.config.get("actions", "kvm_bind_mode", "").strip()
        if mode in ("slot", "target", "none"):
            return mode
        _slot, slot_key, target = self._slot_config()
        if slot_key == "none" or target == "none":
            return "none"
        if slot_key:
            return "slot"
        if target:
            return "target"
        return "unset"

    def _lock_combo_value(self) -> str:
        mode = self._bind_mode()
        slot, _slot_key, target = self._slot_config()
        if mode == "slot":
            return f"slot:{slot}"
        if mode == "target":
            return f"target:{target}"
        return "none"

    def _resolve_slot_display(
        self, top: ccd.Topology, slot: int, slot_key: str, target: str
    ) -> ccd.Display | None:
        """Resolve the locked connector without ever using the monitor name.

        1. exact connector-slot fingerprint (adapter + connector instance +
           output technology) - survives EDID/name/target-id changes;
        2. legacy target key;
        3. nothing (never guess another monitor).
        """
        slots = self._slot_map(top)
        if slot_key and slot_key != "none":
            candidates = [d for d in top.connected if d.slot_key == slot_key]
            if slot > 0:
                for display in candidates:
                    if slots.get(display.key) == slot:
                        return display
            if len(candidates) == 1:
                return candidates[0]
        if target and target != "none":
            for display in top.connected:
                if display.key == target:
                    return display
        return None

    def _save_slot_binding(self, display: ccd.Display, top: ccd.Topology) -> int:
        slot = self._slot_map(top).get(display.key, 0)
        self.config.set("actions", "kvm_bind_mode", "slot")
        self.config.set("actions", "kvm_slot", str(slot))
        self.config.set("actions", "kvm_slot_key", display.slot_key)
        self.config.set("actions", "kvm_target", display.key)
        self.config.set("actions", "kvm_screen", str(slot) if slot else "none")
        self.config.save()
        return slot

    def _save_target_binding(self, display: ccd.Display, top: ccd.Topology) -> int:
        slot = self._slot_map(top).get(display.key, 0)
        self.config.set("actions", "kvm_bind_mode", "target")
        self.config.set("actions", "kvm_slot", str(slot))
        self.config.set("actions", "kvm_slot_key", display.slot_key)
        self.config.set("actions", "kvm_target", display.key)
        self.config.set("actions", "kvm_screen", str(slot) if slot else "none")
        self.config.save()
        return slot

    def _configured_kvm_key(self, top: ccd.Topology | None = None) -> str:
        top = top or self._topology
        if top is None:
            return ""
        mode = self._bind_mode()
        if mode in ("none", "unset"):
            return ""
        slot, slot_key, target = self._slot_config()
        if mode == "target":
            if not target or target == "none":
                return ""
            for display in top.connected:
                if display.key == target:
                    return display.key
            return ""
        display = self._resolve_slot_display(top, slot, slot_key, target)
        return display.key if display else ""

    def _kvm_lock_state(self) -> str:
        mode = self._bind_mode()
        if mode == "none":
            return "none"
        if mode == "unset":
            return "unset"
        return "locked"

    def _display_settings(self) -> dict:
        return {
            "kvm_target": self._configured_kvm_key(),
            "snapshot_path": str(DATA_DIR / "snapshots" / "local.json"),
            "min_primary_hz": self.config.get_int("actions", "min_primary_hz", 100),
            "hdr_restore": self.config.get_bool("actions", "hdr_restore", True),
            "on_connect": self.config.get("actions", "on_connect", "kvm-only"),
            "on_disconnect": self.config.get("actions", "on_disconnect", "restore"),
            "screen_number": self.config.get_int("actions", "screen_number", 1),
        }

    def _new_manager(self) -> DisplayManager:
        return DisplayManager(self._display_settings(), self.logger)

    def _display_error_text(self, code: str) -> str:
        key = {
            "no_kvm": "#DISP_ERR_NO_KVM",
            "no_snapshot": "#DISP_ERR_NO_SNAPSHOT",
            "invalid_snapshot": "#DISP_ERR_INVALID_SNAPSHOT",
            "remote_layout": "#DISP_ERR_REMOTE_LAYOUT",
            "verify_failed": "#DISP_ERR_VERIFY",
            "capture_failed": "#DISP_ERR_CAPTURE",
            "validate_failed": "#DISP_ERR_VALIDATE",
            "ccd_error": "#DISP_ERR_CCD",
            "unknown_action": "#DISP_ERR_UNKNOWN_ACTION",
            "exception": "#DISP_ERR_EXCEPTION",
            "stopped": "#DISP_ERR_STOPPED",
        }.get(code, "#DISP_ERR_UNKNOWN")
        return self.i18n.tr(key)

    def _topology_text(self, mode: str) -> str:
        return self.i18n.tr(
            {
                "single": "#TOPO_SINGLE",
                "duplicate": "#TOPO_CLONE",
                "extend": "#TOPO_EXTEND",
            }.get(mode, "#TOPO_UNKNOWN")
        )

    def _set_action_status(self, text: str, color: str = "#8a94a6") -> None:
        self.action_status.setText(text)
        self.action_status.setStyleSheet(f"color: {color};")

    def _on_refresh_displays(self) -> None:
        self.refresh_displays(manual=True)

    def refresh_displays(self, manual: bool = False) -> None:
        if self._display_busy:
            return
        try:
            top = ccd.query_topology()
        except Exception as exc:
            self.logger.error("Display query failed: %s", exc)
            self._topology = None
            self.display_status.setText(self.i18n.tr("#DISP_ERR_CCD"))
            self.display_status.setStyleSheet("color: #d9534f;")
            self.display_table.setRowCount(0)
            self._update_ui_state()
            return

        slot, slot_key, target = self._slot_config()
        mode = self._bind_mode()
        configured_mode = self.config.get("actions", "kvm_bind_mode", "").strip()
        if configured_mode not in ("slot", "target", "none") and mode in ("slot", "target", "none"):
            self.config.set("actions", "kvm_bind_mode", mode)
            self.config.save()

        kvm = ""
        if mode == "none":
            # Explicit "not locked": detection only, never auto-select again.
            if slot_key != "none" or target != "none" or slot != 0:
                self.config.set("actions", "kvm_slot", "0")
                self.config.set("actions", "kvm_slot_key", "none")
                self.config.set("actions", "kvm_target", "none")
                self.config.set("actions", "kvm_screen", "none")
                self.config.save()
        elif mode == "unset":
            # Very first run: one-time convenience if exactly one target is
            # inactive.  After this the user's choice is persisted.
            try:
                inferred = DisplayManager(self._display_settings(), self.logger).infer_kvm_key()
            except Exception:
                inferred = ""
            if inferred:
                found = next((d for d in top.connected if d.key == inferred), None)
                if found is not None:
                    self._save_slot_binding(found, top)
                    kvm = found.key
                    self.logger.info(
                        "Auto-detected KVM slot %s: %s",
                        self._slot_map(top).get(found.key),
                        found.name,
                    )
        elif mode == "target":
            if target and target != "none":
                display = next((d for d in top.connected if d.key == target), None)
                if display is not None:
                    kvm = display.key
                    current_slot = self._slot_map(top).get(display.key, 0)
                    if (current_slot, display.slot_key, display.key) != (slot, slot_key, target):
                        self.logger.info(
                            "Follow-display target resolved to %s (t%s), screen %s",
                            display.name,
                            display.target_id,
                            current_slot,
                        )
                        self._save_target_binding(display, top)
        else:
            display = self._resolve_slot_display(top, slot, slot_key, target)
            if display is not None:
                kvm = display.key
                current_slot = self._slot_map(top).get(display.key, 0)
                if (current_slot, display.slot_key, display.key) != (slot, slot_key, target):
                    self.logger.info(
                        "KVM slot %s bound to %s (connector slot key, not the name)",
                        current_slot,
                        display.name,
                    )
                    self._save_slot_binding(display, top)

        top.kvm_key = kvm
        self._topology = top
        self._populate_display_table(top)
        self._repopulate_locks()
        self._update_display_status(top)
        self._update_snapshot_label()
        self._update_ui_state()
        if manual:
            log_action(self.logger, "refresh_displays", count=len(top.connected))

    def _populate_display_table(self, top: ccd.Topology) -> None:
        kvm = top.kvm_key
        slots = self._slot_map(top)
        displays = sorted(top.connected, key=lambda d: slots[d.key])
        self.display_table.setRowCount(len(displays))
        for row, display in enumerate(displays):
            if display.active and display.width and display.height:
                resolution = f"{display.width} × {display.height}"
            else:
                resolution = "-"
            hz = f"{display.hz:.2f} Hz" if display.active and display.hz else "-"
            if display.hdr_supported:
                hdr = f"HDR{display.hdr_bits}" if display.hdr_enabled else "SDR"
            else:
                hdr = "-"
            if display.key == kvm:
                role = self.i18n.tr("#ROLE_KVM" if display.active else "#ROLE_KVM_INACTIVE")
            else:
                role = self.i18n.tr("#ROLE_LOCAL" if display.active else "#ROLE_UNUSED")
            values = [
                f"{slots[display.key]}.",
                display.name,
                f"{display.output_name} · t{display.target_id}",
                resolution,
                hz,
                hdr,
                role,
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(
                    f"Windows 显示号: {display.display_number or '-'}\n"
                    f"槽位: {slots[display.key]}\n"
                    f"{display.key}\n{display.monitor_path}\n{display.adapter}"
                )
                self.display_table.setItem(row, column, item)
        self.display_table.resizeColumnsToContents()
        self.display_table.horizontalHeader().setStretchLastSection(True)

    def _update_display_status(self, top: ccd.Topology) -> None:
        kvm = self._configured_kvm_key(top)
        state = self._kvm_lock_state()
        text = self.i18n.tr("#DISPLAY_SUMMARY").format(
            count=len(top.connected), topology=self._topology_text(top.mode)
        )
        if state in ("none", "unset"):
            text += " · " + self.i18n.tr("#DISPLAY_NO_LOCK")
        elif not kvm:
            text += " · " + self.i18n.tr("#DISPLAY_LOCK_MISSING")
        else:
            mode = self._bind_mode()
            display = next((d for d in top.connected if d.key == kvm), None)
            if mode == "target" and display is not None:
                text += " · " + self.i18n.tr("#DISPLAY_KVM_FOLLOW").format(
                    name=display.name
                )
            else:
                slot = self._slot_map(top).get(kvm, 0)
                text += " · " + self.i18n.tr("#DISPLAY_KVM_SLOT").format(slot=slot)
        remote = bool(kvm and any(d.key == kvm for d in top.active))
        if remote:
            text += " · " + self.i18n.tr("#DISPLAY_REMOTE")
        self.display_status.setText(text)
        self.display_status.setStyleSheet(
            "color: #d9902f;" if remote else "color: #8a94a6;"
        )

    def _update_snapshot_label(self) -> None:
        try:
            info = self._new_manager().snapshot_info()
        except Exception as exc:
            self.logger.error("Snapshot info failed: %s", exc)
            self._snapshot_ok = False
            self.snapshot_label.setText(self.i18n.tr("#DISP_ERR_CCD"))
            return
        self._snapshot_ok = bool(info.get("ok"))
        if info.get("code") == "no_snapshot":
            self.snapshot_label.setText(self.i18n.tr("#SNAPSHOT_PENDING"))
            self.snapshot_label.setStyleSheet("color: #8a94a6;")
        elif not self._snapshot_ok:
            errors = "；".join(info.get("errors") or []) or self.i18n.tr("#DISP_ERR_UNKNOWN")
            self.snapshot_label.setText(
                self.i18n.tr("#SNAPSHOT_INVALID").format(errors=_short(errors, 140))
            )
            self.snapshot_label.setStyleSheet("color: #d9534f;")
        else:
            hdr_on = bool(info.get("hdr_enabled"))
            hdr_restore = self.config.get_bool("actions", "hdr_restore", True)
            if hdr_on and not hdr_restore:
                hdr = self.i18n.tr("#HDR_RESTORE_OFF_CAPTURED_ON")
            elif not hdr_on and hdr_restore:
                hdr = self.i18n.tr("#HDR_RESTORE_ON_CAPTURED_OFF")
            else:
                hdr = self.i18n.tr("#HDR_ON" if hdr_on else "#HDR_OFF")
            line = self.i18n.tr("#SNAPSHOT_LINE").format(
                width=info.get("width", 0),
                height=info.get("height", 0),
                hz=f"{float(info.get('hz') or 0):.2f}",
                hdr=hdr,
                topology=self._topology_text(info.get("topology", "")),
                name=info.get("name", ""),
            )
            self.snapshot_label.setText(line)
            self.snapshot_label.setStyleSheet("color: #2fbf71;")
        self.snapshot_label.setToolTip(
            self.i18n.tr("#SNAPSHOT_CAPTURED").format(time=info.get("captured_at", "-"))
            + "\n"
            + self.i18n.tr("#TIP_SNAPSHOT")
        )

    def _on_capture(self) -> None:
        if self._display_busy:
            return
        if self._service_mode:
            if not self._service_connected or self.service_client is None:
                self._warn(self.i18n.tr("#SERVICE_DISCONNECTED"))
                return
            if self._det_state == "watching":
                self._warn(self.i18n.tr("#DISP_ERR_REMOTE_LAYOUT"))
                return
            self._set_action_status(self.i18n.tr("#STATUS_ACTION_RUNNING"), "#2f7ff0")
            self.service_client.call("capture")
            return
        if self._det_state == "watching":
            self._warn(self.i18n.tr("#DISP_ERR_REMOTE_LAYOUT"))
            return
        self.refresh_displays()
        if self._topology is None:
            return
        kvm = self._configured_kvm_key()
        if kvm and any(d.key == kvm for d in self._topology.active):
            self._warn(self.i18n.tr("#DISP_ERR_REMOTE_LAYOUT"))
            return
        result = self._new_manager().capture_local()
        if result.ok:
            self._set_action_status(self.i18n.tr("#STATUS_ACTION_CAPTURED"), "#2fbf71")
            self.statusBar().showMessage(self.i18n.tr("#STATUS_ACTION_CAPTURED"), 5000)
            log_action(self.logger, "capture_local_snapshot")
        else:
            reason = self._display_error_text(result.code)
            self._set_action_status(
                self.i18n.tr("#STATUS_ACTION_FAILED").format(reason=reason), "#d9534f"
            )
            self.statusBar().showMessage(reason, 7000)
            self.logger.warning("Capture failed: %s %s", result.code, result.detail)
        self._update_snapshot_label()
        self._update_ui_state()

    def _on_manual_restore(self) -> None:
        if not self._snapshot_ok:
            self._warn(self.i18n.tr("#DISP_ERR_NO_SNAPSHOT"))
            return
        if self._service_mode:
            if not self._service_connected or self.service_client is None:
                self._warn(self.i18n.tr("#SERVICE_DISCONNECTED"))
                return
            self._set_action_status(self.i18n.tr("#STATUS_ACTION_RUNNING"), "#2f7ff0")
            self.service_client.call("apply_snapshot")
            return
        self._start_display_action("restore")

    def _start_display_action(self, kind: str) -> None:
        if self._service_mode:
            if kind == "restore":
                self._on_manual_restore()
            return
        if self._display_busy or self._display_thread is not None:
            self._pending_action = kind
            return
        settings = self._display_settings()
        if kind == "connect" and not settings["kvm_target"]:
            self._set_action_status(self.i18n.tr("#DISP_ERR_NO_KVM"), "#d9534f")
            return
        if (
            kind == "connect"
            and self.config.get("actions", "on_disconnect", "restore") == "restore"
            and not self._snapshot_ok
        ):
            self._set_action_status(self.i18n.tr("#DISP_ERR_NO_SNAPSHOT"), "#d9534f")
            return
        if kind == "restore" and not self._snapshot_ok:
            self._set_action_status(self.i18n.tr("#DISP_ERR_NO_SNAPSHOT"), "#d9534f")
            return

        self._display_busy = True
        self._set_action_status(self.i18n.tr("#STATUS_ACTION_RUNNING"), "#2f7ff0")
        self._update_ui_state()
        log_action(self.logger, "display_action", kind=kind)

        self._display_thread = QThread(self)
        self._display_worker = DisplayWorker(kind, settings, self.logger)
        self._display_worker.moveToThread(self._display_thread)
        self._display_thread.started.connect(self._display_worker.run)
        self._display_worker.finished.connect(self._on_display_finished)
        self._display_worker.finished.connect(self._display_thread.quit)
        self._display_thread.finished.connect(self._on_display_thread_finished)
        self._display_thread.finished.connect(self._display_worker.deleteLater)
        self._display_thread.finished.connect(self._display_thread.deleteLater)
        self._display_thread.start()

    def _on_display_finished(self, result: dict) -> None:
        self._display_busy = False
        self._last_action_ts = time.time()
        code = str(result.get("code") or "")
        ok = bool(result.get("ok"))
        if ok:
            self._action_fail_until = 0.0
            if code == "noop":
                self._set_action_status("", "#8a94a6")
                self.statusBar().clearMessage()
            elif code == "switched":
                message = self.i18n.tr("#STATUS_ACTION_SWITCHED")
                self._set_action_status(message, "#2fbf71")
                self.statusBar().showMessage(message, 6000)
            elif code == "restored":
                message = self.i18n.tr("#STATUS_ACTION_RESTORED")
                self._set_action_status(message, "#2fbf71")
                self.statusBar().showMessage(message, 6000)
            elif code == "captured":
                message = self.i18n.tr("#STATUS_ACTION_CAPTURED")
                self._set_action_status(message, "#2fbf71")
                self.statusBar().showMessage(message, 6000)
            else:
                message = self.i18n.tr("#STATUS_ACTION_DONE")
                self._set_action_status(message, "#2fbf71")
                self.statusBar().showMessage(message, 6000)
            self.logger.info("Display action %s: %s (%s ms)", result.get("kind"), code, result.get("duration_ms"))
        else:
            self._action_fail_until = time.time() + 60
            reason = self._display_error_text(code)
            message = self.i18n.tr("#STATUS_ACTION_FAILED").format(reason=reason)
            self._set_action_status(message, "#d9534f")
            self.statusBar().showMessage(message, 8000)
            self.logger.warning("Display action %s failed: %s %s", result.get("kind"), code, result.get("detail"))
        self.refresh_displays()
        self._update_snapshot_label()
        self._update_ui_state()

    def _on_display_thread_finished(self) -> None:
        self._display_thread = None
        self._display_worker = None
        self._display_busy = False
        self._update_ui_state()
        if self._pending_action and not self._quitting:
            kind = self._pending_action
            self._pending_action = None
            QTimer.singleShot(0, lambda k=kind: self._start_display_action(k))

    def _on_auto_apply_changed(self, checked: bool) -> None:
        self.config.set_bool("actions", "auto_apply", checked)
        self.config.save()
        self._service_reload_config()
        log_action(self.logger, "set_auto_apply", enabled=checked)

    def _on_detection_transition(self, old_state: str, new_state: str) -> None:
        if self._service_mode:
            return
        if new_state == "watching" and old_state != "watching":
            self._disconnect_timer.stop()
            self._watching_since = time.time()
            self._auto_display_action("connect")
        elif new_state == "idle" and old_state == "watching":
            dwell = self.config.get_int("actions", "min_dwell_seconds", 10)
            remaining = dwell - (time.time() - self._watching_since)
            if remaining > 0:
                # Do not skip the restore just because the viewer left early:
                # come back after the minimum dwell, unless they reconnect.
                self._disconnect_timer.start(int(remaining * 1000))
            else:
                self._auto_display_action("disconnect")
                self._watching_since = 0.0

    def _on_disconnect_timer(self) -> None:
        if self._det_state == "idle":
            self._auto_display_action("disconnect")
        self._watching_since = 0.0

    def _auto_display_action(self, kind: str) -> None:
        if self._service_mode:
            return
        if not self.config.get_bool("actions", "auto_apply", True):
            return
        if self._display_busy or self._display_thread is not None:
            self._pending_action = kind
            return
        if time.time() < self._action_fail_until:
            return
        if time.time() - self._last_action_ts < 5:
            return
        if kind == "connect" and self.config.get("actions", "on_connect", "kvm-only") == "hold":
            return
        if kind == "disconnect" and self.config.get("actions", "on_disconnect", "restore") == "hold":
            return
        if kind == "connect" and not self._configured_kvm_key():
            self._set_action_status(self.i18n.tr("#DISP_ERR_NO_KVM"), "#d9534f")
            return
        if (
            kind == "connect"
            and self.config.get("actions", "on_disconnect", "restore") == "restore"
            and not self._snapshot_ok
        ):
            self._set_action_status(self.i18n.tr("#DISP_ERR_NO_SNAPSHOT"), "#d9534f")
            return
        if (
            kind == "disconnect"
            and self.config.get("actions", "on_disconnect", "restore") == "restore"
            and not self._snapshot_ok
        ):
            self._set_action_status(self.i18n.tr("#DISP_ERR_NO_SNAPSHOT"), "#d9534f")
            return
        self._start_display_action(kind)

    # ============================================================ helpers
    def _update_ui_state(self) -> None:
        ip = self.ip_edit.text().strip()
        has_creds = bool(ip and self.user_edit.text().strip() and self.pw_edit.text())
        has_pin = bool(self.config.get("connection", "cert_sha256", ""))
        if self._service_mode:
            service_ready = self._service_connected and self.service_client is not None
            self.start_btn.setEnabled(
                service_ready and not self._poll_running and not self._test_busy
            )
            self.stop_btn.setEnabled(service_ready and self._poll_running)
            self.check_now_btn.setEnabled(service_ready and self._poll_running)
            self.test_btn.setEnabled(not self._test_busy)
            self.poll_spin.setEnabled(True)
        else:
            can_start = has_creds and has_pin and not self._test_busy and not self._poll_running
            self.start_btn.setEnabled(can_start)
            self.stop_btn.setEnabled(self._poll_running)
            self.check_now_btn.setEnabled(self._poll_running)
            self.test_btn.setEnabled(not self._test_busy and not self._poll_running)
            self.poll_spin.setEnabled(not self._poll_running)

        kvm = self._configured_kvm_key()
        kvm_active = bool(self._topology and any(d.key == kvm for d in self._topology.active))
        can_capture = (
            not self._display_busy
            and self._topology is not None
            and self._det_state != "watching"
            and not kvm_active
        )
        self.apply_btn.setEnabled(can_capture)
        self.restore_btn.setEnabled(self._snapshot_ok and not self._display_busy)
        self.refresh_displays_btn.setEnabled(not self._display_busy)

        self._update_pause_actions()
        self._update_state_label()
        self._update_cert_label()
        self._update_reason()
        self._update_service_label()
        self._update_service_buttons()
        self._update_about_text()

    def _update_pause_actions(self) -> None:
        text = self.i18n.tr("#ACTION_RESUME") if not self._poll_running else self.i18n.tr("#ACTION_PAUSE")
        self.pause_action.blockSignals(True)
        self.pause_action.setChecked(self._poll_running)
        self.pause_action.setText(text)
        self.pause_action.blockSignals(False)
        self.tray_pause_action.blockSignals(True)
        self.tray_pause_action.setChecked(self._poll_running)
        self.tray_pause_action.setText(text)
        self.tray_pause_action.blockSignals(False)

    def _state_key(self) -> str:
        if self._pending_first_result or self._poll_running and self._last_detection is None:
            return "#STATE_STARTING"
        if self._poll_running:
            return {
                "watching": "#STATE_WATCHING",
                "idle": "#STATE_IDLE",
            }.get(self._det_state, "#STATE_UNKNOWN")
        if self._last_detection is not None:
            return "#STATE_STOPPED"
        return "#STATE_DISCONNECTED"

    def _state_color(self) -> str:
        if self._pending_first_result:
            return "#2f7ff0"
        if self._poll_running:
            return {
                "watching": "#e08a2e",
                "idle": "#2fbf71",
            }.get(self._det_state, "#8a94a6")
        return "#8a94a6"

    def _update_state_label(self) -> None:
        self.state_value.setText("● " + self.i18n.tr(self._state_key()))
        self.state_value.setStyleSheet(f"color: {self._state_color()}; font-weight: 600;")
        if self._last_detection is not None:
            self.clients_value.setText(str(self._last_detection.clients))
        else:
            self.clients_value.setText("-")
        self.last_check_value.setText(self._last_check_text)

    def _reason_text(self, detection: Detection | None) -> str:
        if detection is None:
            return "-"
        key = {
            "streamer": "#REASON_STREAMER",
            "log_new": "#REASON_LOG_NEW",
            "log_wake": "#REASON_LOG_WAKE",
            "log_only": "#REASON_LOG_ONLY",
            "no_clients": "#REASON_NO_CLIENTS",
            "streamer_null": "#REASON_STREAMER_NULL",
            "auth": "#REASON_AUTH",
            "cert": "#REASON_CERT",
            "network": "#REASON_NETWORK",
            "two_step": "#REASON_TWO_STEP",
            "rate": "#REASON_RATE",
            "http": "#REASON_HTTP",
        }.get(detection.reason, "#REASON_UNKNOWN")
        text = self.i18n.tr(key)
        detail = (detection.detail or {}).get("message", "")
        if detail:
            text += " · " + _short(detail, 100)
        return text

    def _update_reason(self) -> None:
        self.reason_value.setText(self._reason_text(self._last_detection))

    def _update_cert_label(self) -> None:
        pin = self.config.get("connection", "cert_sha256", "").strip()
        self.cert_value.setText(_format_fp(pin) if pin else self.i18n.tr("#CERT_NONE"))

    def _update_about_text(self) -> None:
        self.about_label.setText(
            self.i18n.tr("#ABOUT").format(
                version=self.version, data=str(DATA_DIR), config=str(self.config.path)
            )
        )

    def _update_tray_tooltip(self) -> None:
        if self.tray is None:
            return
        state = self.i18n.tr(self._state_key())
        self.tray.setToolTip(f"{self.i18n.tr('#APP_TITLE')} - {state}")

    def _set_conn_status(self, key: str, color: str, **kwargs) -> None:
        text = self.i18n.tr(key, **kwargs) if key.startswith("#") else key
        self.conn_status.setText("● " + text)
        self.conn_status.setStyleSheet(f"color: {color}; font-weight: 600;")

    def _repopulate_locks(self) -> None:
        blocker = QSignalBlocker(self.lock_combo)
        self.lock_combo.clear()
        self.lock_combo.addItem(self.i18n.tr("#LOCK_NONE"), "none")
        mode = self._bind_mode()
        slot, _slot_key, target = self._slot_config()
        top = self._topology
        if top is not None:
            slots = self._slot_map(top)
            ordered = sorted(top.connected, key=lambda d: slots[d.key])
            for display in ordered:
                label = self.i18n.tr("#LOCK_SLOT_ITEM").format(
                    slot=slots[display.key], name=display.name
                )
                self.lock_combo.addItem(label, f"slot:{slots[display.key]}")
            for display in ordered:
                label = self.i18n.tr("#LOCK_FOLLOW_ITEM").format(
                    name=display.name, target_id=display.target_id
                )
                self.lock_combo.addItem(label, f"target:{display.key}")
            if mode == "slot" and slot > 0 and slot not in slots.values():
                self.lock_combo.addItem(
                    self.i18n.tr("#LOCK_SLOT_MISSING").format(slot=slot), f"slot:{slot}"
                )
            if mode == "target" and target and target not in {d.key for d in top.connected}:
                self.lock_combo.addItem(
                    self.i18n.tr("#LOCK_FOLLOW_MISSING"), f"target:{target}"
                )
        else:
            if slot > 0:
                self.lock_combo.addItem(
                    self.i18n.tr("#LOCK_SLOT_MISSING").format(slot=slot), f"slot:{slot}"
                )
            if mode == "target" and target:
                self.lock_combo.addItem(
                    self.i18n.tr("#LOCK_FOLLOW_MISSING"), f"target:{target}"
                )
        selected = self._lock_combo_value()
        idx = self.lock_combo.findData(selected)
        if idx >= 0:
            self.lock_combo.setCurrentIndex(idx)
        elif selected != "none":
            self.lock_combo.setCurrentIndex(0)
        del blocker

    def _repopulate_languages(self) -> None:
        blocker = QSignalBlocker(self.lang_combo)
        self.lang_combo.clear()
        current = 0
        for index, (code, label) in enumerate(self.i18n.available()):
            self.lang_combo.addItem(label, code)
            if code == self.i18n.code:
                current = index
        self.lang_combo.setCurrentIndex(current)
        del blocker

    def _repopulate_themes(self) -> None:
        blocker = QSignalBlocker(self.theme_combo)
        self.theme_combo.clear()
        current = 0
        selected = self.config.get("ui", "theme", "light")
        for index, theme_id in enumerate(self.themes.ids()):
            self.theme_combo.addItem(self.themes.label(theme_id, self.i18n.code), theme_id)
            if theme_id == selected:
                current = index
        self.theme_combo.setCurrentIndex(current)
        del blocker

    # ------------------------------------------------------------- status
    def _confirm(self, title: str, text: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setIcon(QMessageBox.Icon.Question)
        yes = box.addButton(self.i18n.tr("#MSGBOX_YES"), QMessageBox.ButtonRole.YesRole)
        box.addButton(self.i18n.tr("#MSGBOX_NO"), QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(yes)
        box.exec()
        return box.clickedButton() is yes

    def _warn(self, text: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(self.i18n.tr("#APP_TITLE"))
        box.setText(text)
        box.setIcon(QMessageBox.Icon.Warning)
        box.addButton(self.i18n.tr("#BTN_OK"), QMessageBox.ButtonRole.AcceptRole)
        box.exec()

    def _show_about(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(self.i18n.tr("#ACTION_ABOUT"))
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            self.i18n.tr("#ABOUT_TEXT").format(
                title=self.i18n.tr("#APP_TITLE"),
                version=self.version,
                note=self.i18n.tr("#ABOUT"),
                data=str(DATA_DIR),
            )
        )
        box.addButton(self.i18n.tr("#BTN_OK"), QMessageBox.ButtonRole.AcceptRole)
        box.exec()

    # ------------------------------------------------------------- folders
    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(str(path))
            log_action(self.logger, "open_path", path=str(path))
        except OSError as exc:
            self.logger.error("Could not open %s: %s", path, exc)

    def _open_config_dir(self) -> None:
        self._open_path(DATA_DIR)

    def _open_log_dir(self) -> None:
        self._open_path(LOGS_DIR)

    def _open_data_dir(self) -> None:
        self._open_path(DATA_DIR)

    # ------------------------------------------------------------- logging
    def append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text.rstrip())

    # ------------------------------------------------------------- window
    def _restore_window(self) -> None:
        geometry = self.config.get("window", "geometry", "")
        if geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
            except Exception:
                pass
        last_tab = self.config.get_int("window", "last_tab", 0)
        if 0 <= last_tab < self.tabs.count():
            self.tabs.setCurrentIndex(last_tab)

    def _save_window(self) -> None:
        geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self.config.set("window", "geometry", geometry)
        self.config.set("window", "last_tab", self.tabs.currentIndex())
        self.config.save()

    def _auto_start(self) -> None:
        if self._service_configured:
            # The service owns polling; starting a local poll would create a
            # second controller for the same display.
            return
        snapshot = self.config.connection_snapshot()
        if (
            self.config.get_bool("connection", "auto_detect_on_launch", True)
            and snapshot["ip"]
            and snapshot["password"]
            and snapshot["pin"]
        ):
            self._on_start()

    def _quit(self) -> None:
        self._quitting = True
        self.close()
        self.app.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._quitting and self.tray is not None and self.minimize_check.isChecked():
            self._save_window()
            if not self._tray_notice_shown:
                self.tray.showMessage(
                    self.i18n.tr("#APP_TITLE"),
                    self.i18n.tr("#TRAY_MINIMIZED"),
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
                self._tray_notice_shown = True
            self.hide()
            event.ignore()
            log_action(self.logger, "minimize_to_tray")
            return

        self._quitting = True
        self._disconnect_timer.stop()
        if self._service_worker is not None:
            self._service_worker.wait(3000)
        if self.service_client is not None:
            self.service_client.stop()
        if self._poll_worker is not None:
            self._poll_worker.stop()
        if self._poll_thread is not None:
            self._poll_thread.quit()
            self._poll_thread.wait(3000)
        if self._display_worker is not None:
            self._display_worker.stop()
        if self._display_thread is not None:
            self._display_thread.quit()
            self._display_thread.wait(3000)
        if self._test_thread is not None:
            self._test_thread.quit()
            self._test_thread.wait(2000)
        self._save_window()
        self._save_connection()
        if self.tray is not None:
            self.tray.hide()
        log.info("Application closed")
        event.accept()

    # ------------------------------------------------------------- i18n
    def _retranslate(self) -> None:
        self.setWindowTitle(f"{self.i18n.tr('#APP_TITLE')} {self.version}")
        for widget, key, setter in list(self._texts):
            try:
                setter(widget, self.i18n.tr(key))
            except Exception:
                pass
        for widget, key in self._tips:
            try:
                widget.setToolTip(self.i18n.tr(key))
            except Exception:
                pass

        self.tabs.setTabText(0, self.i18n.tr("#TAB_CONNECTION"))
        self.tabs.setTabText(1, self.i18n.tr("#TAB_ACTIONS"))
        self.tabs.setTabText(2, self.i18n.tr("#TAB_DISPLAYS"))
        self.tabs.setTabText(3, self.i18n.tr("#TAB_SETTINGS"))
        self.tabs.setTabToolTip(0, self.i18n.tr("#TIP_TAB_CONNECTION"))
        self.tabs.setTabToolTip(1, self.i18n.tr("#TIP_TAB_ACTIONS"))
        self.tabs.setTabToolTip(2, self.i18n.tr("#TIP_TAB_DISPLAYS"))
        self.tabs.setTabToolTip(3, self.i18n.tr("#TIP_TAB_SETTINGS"))

        self.display_table.setHorizontalHeaderLabels([
            self.i18n.tr("#TABLE_DISPLAY_NUM"),
            self.i18n.tr("#TABLE_NAME"),
            self.i18n.tr("#TABLE_PORT"),
            self.i18n.tr("#TABLE_RES"),
            self.i18n.tr("#TABLE_HZ"),
            self.i18n.tr("#TABLE_HDR"),
            self.i18n.tr("#TABLE_ROLE"),
        ])

        self._repopulate_languages()
        self._repopulate_themes()
        lock_blocker = QSignalBlocker(self.lock_combo)
        try:
            self._repopulate_locks()
            lock_value = self._lock_combo_value()
            idx = self.lock_combo.findData(lock_value)
            self.lock_combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            del lock_blocker

        self.version_label.setText(self.i18n.tr("#LABEL_VERSION").format(version=self.version))
        self._bind_tips()
        self._update_pause_actions()
        self._update_cert_label()
        self._update_about_text()
        self._update_state_label()
        self._update_reason()
        if self._topology is not None:
            self._update_display_status(self._topology)
        self._update_snapshot_label()
        self._update_tray_tooltip()

    def _bind_tips(self) -> None:
        # Tooltips are bound once; retranslate only refreshes their text.
        tips = [
            (self.ip_edit, "#TIP_IP"),
            (self.user_edit, "#TIP_USER"),
            (self.pw_edit, "#TIP_PASSWORD"),
            (self.show_pw_btn, "#TIP_SHOW_PW"),
            (self.test_btn, "#TIP_TEST"),
            (self.poll_spin, "#TIP_POLL"),
            (self.start_btn, "#TIP_START"),
            (self.stop_btn, "#TIP_STOP"),
            (self.check_now_btn, "#TIP_CHECK"),
            (self.auto_detect_check, "#TIP_AUTO"),
            (self.lang_combo, "#TIP_LANGUAGE"),
            (self.theme_combo, "#TIP_THEME"),
            (self.minimize_check, "#TIP_MINIMIZE"),
            (self.autostart_check, "#TIP_AUTOSTART"),
            (self.open_config_btn, "#TIP_OPEN_CONFIG"),
            (self.open_log_btn, "#TIP_OPEN_LOG"),
            (self.open_data_btn, "#TIP_OPEN_DATA"),
            (self.clear_log_btn, "#TIP_CLEAR_LOG"),
            (self.apply_btn, "#TIP_APPLY"),
            (self.restore_btn, "#TIP_RESTORE"),
            (self.auto_apply_check, "#TIP_AUTO_APPLY"),
            (self.refresh_displays_btn, "#TIP_REFRESH"),
            (self.display_table, "#TIP_DISPLAY_TABLE"),
            (self.display_status, "#TIP_DISPLAY_STATUS"),
            (self.lock_combo, "#TIP_LOCK"),
            (self.screen_combo, "#TIP_SCREEN"),
            (self.hdr_check, "#TIP_HDR"),
            (self.snapshot_label, "#TIP_SNAPSHOT"),
            (self.service_start_btn, "#TIP_SERVICE_START"),
            (self.service_stop_btn, "#TIP_SERVICE_STOP"),
            (self.service_uninstall_btn, "#TIP_SERVICE_UNINSTALL"),
            (self.on_connect_radios["kvm-only"], "#TIP_ON_CONNECT_KVM_ONLY"),
            (self.on_connect_radios["clone"], "#TIP_ON_CONNECT_CLONE"),
            (self.on_connect_radios["extend"], "#TIP_ON_CONNECT_EXTEND"),
            (self.on_connect_radios["hold"], "#TIP_ON_CONNECT_HOLD"),
            (self.on_disconnect_radios["restore"], "#TIP_ON_DISCONNECT_RESTORE"),
            (self.on_disconnect_radios["screen"], "#TIP_ON_DISCONNECT_SCREEN"),
            (self.on_disconnect_radios["clone"], "#TIP_ON_DISCONNECT_CLONE"),
            (self.on_disconnect_radios["hold"], "#TIP_ON_DISCONNECT_HOLD"),
        ]
        for widget, key in tips:
            widget.setToolTip(self.i18n.tr(key))
            if (widget, key) not in self._tips:
                self._tips.append((widget, key))
