"""Filesystem locations and version information.

The program lives next to this repository; user data lives in
``<Documents>/GLKVM Screen Guard`` so it survives updates and never mixes
with the code tree.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

APP_NAME = "GLKVM Screen Guard"
APP_DIR = Path(__file__).resolve().parent.parent

CONFIG_NAME = "config.ini"


def _documents_dir() -> Path:
    """Return the real Documents folder (handles OneDrive redirection)."""
    try:
        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_PERSONAL == 5
        rc = ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)
        if rc == 0 and buf.value:
            return Path(buf.value)
    except Exception:
        pass
    return Path.home() / "Documents"


DATA_DIR = _documents_dir() / APP_NAME
CONFIG_PATH = DATA_DIR / CONFIG_NAME
LOGS_DIR = DATA_DIR / "logs"

LANG_DIR = APP_DIR / "langs"
THEME_DIR = APP_DIR / "themes"
VERSION_FILE = APP_DIR / "version.txt"


def read_version() -> str:
    try:
        value = VERSION_FILE.read_text(encoding="utf-8").strip()
        return value or "v1.0.0"
    except OSError:
        return "v1.0.0"


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
