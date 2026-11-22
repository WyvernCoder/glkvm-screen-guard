"""Load QSS themes from ``./themes``.

``themes.json`` maps a theme id to a QSS file and its localized display
names.  Applying a theme is just ``app.setStyleSheet(...)`` on top of the
Fusion style; no custom-painted widgets are involved.
"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtGui import QColor, QPalette


class ThemeManager:
    def __init__(self, theme_dir: Path | str) -> None:
        self.dir = Path(theme_dir)
        self.index: dict[str, dict] = {}
        try:
            raw = json.loads((self.dir / "themes.json").read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.index = raw
        except Exception:
            pass
        if not self.index:
            self.index = {
                "light": {"file": "light.qss", "zh_CN": "浅色", "en_US": "Light"},
                "dark": {"file": "dark.qss", "zh_CN": "深色", "en_US": "Dark"},
            }

    def ids(self) -> list[str]:
        return list(self.index.keys())

    def label(self, theme_id: str, lang: str) -> str:
        item = self.index.get(theme_id, {})
        return item.get(lang) or item.get("en_US") or theme_id

    def default(self) -> str:
        return self.ids()[0] if self.ids() else "light"

    @staticmethod
    def _apply_palette(app, dark: bool) -> None:
        palette = QPalette()
        if dark:
            window = QColor("#1e1e1e")
            base = QColor("#252526")
            text = QColor("#e6e6e6")
            button = QColor("#5a5a5a")
            indicator = QColor("#6a6a6a")
            disabled = QColor("#7a7a7a")
            tooltip_base = QColor("#2d2d30")
            tooltip_text = text
            placeholder = QColor("#8a8a8a")
        else:
            window = QColor("#f3f3f3")
            base = QColor("#ffffff")
            text = QColor("#1b1b1b")
            button = QColor("#d0d0d0")
            indicator = QColor("#b8b8b8")
            disabled = QColor("#a0a0a0")
            tooltip_base = QColor("#ffffe0")
            tooltip_text = QColor("#1b1b1b")
            placeholder = QColor("#8a8a8a")

        palette.setColor(QPalette.ColorRole.Window, window)
        palette.setColor(QPalette.ColorRole.WindowText, text)
        palette.setColor(QPalette.ColorRole.Base, base)
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#2a2a2c") if dark else QColor("#fafafa"))
        palette.setColor(QPalette.ColorRole.Text, text)
        palette.setColor(QPalette.ColorRole.Button, button)
        palette.setColor(QPalette.ColorRole.ButtonText, text)
        palette.setColor(QPalette.ColorRole.Mid, indicator)
        palette.setColor(QPalette.ColorRole.Dark, indicator)
        palette.setColor(QPalette.ColorRole.Shadow, QColor("#000000"))
        palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#0067c0"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.ToolTipBase, tooltip_base)
        palette.setColor(QPalette.ColorRole.ToolTipText, tooltip_text)
        palette.setColor(QPalette.ColorRole.PlaceholderText, placeholder)
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
        ):
            palette.setColor(QPalette.ColorGroup.Disabled, role, disabled)
        app.setPalette(palette)

    def apply(self, app, theme_id: str) -> str:
        item = self.index.get(theme_id) or self.index.get(self.default()) or {}
        self._apply_palette(app, theme_id == "dark")
        file_name = item.get("file")
        if not file_name:
            app.setStyleSheet("")
            return ""
        try:
            qss = (self.dir / file_name).read_text(encoding="utf-8")
        except Exception:
            qss = ""
        app.setStyleSheet(qss)
        return qss

