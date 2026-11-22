"""Paths and secrets shared by the service, session-1 agent and GUI.

The service owns ``C:\\ProgramData\\GLKVM Screen Guard\\service.json`` (written
by the installer) and a machine-scope DPAPI password blob.  The user-facing
config/logs/snapshot stay under the user's Documents folder.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import dpapi

SERVICE_NAME = "GLKVMGuard"
SERVICE_DISPLAY_NAME = "GLKVM Screen Guard"
PIPE_NAME = r"\\.\pipe\GLKVMGuard-v1"

PROGRAM_DATA_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "GLKVM Screen Guard"
SERVICE_JSON = PROGRAM_DATA_DIR / "service.json"
PASSWORD_BIN = PROGRAM_DATA_DIR / "password.bin"

DEFAULT_SERVICE = {
    "root": "",
    "pythonw": "",
    "agent": "",
    "data_dir": "",
    "pipe": PIPE_NAME,
}

log = logging.getLogger("glkvm.service_paths")


def load_service_config() -> dict:
    data = dict(DEFAULT_SERVICE)
    try:
        raw = json.loads(SERVICE_JSON.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            data.update(raw)
    except Exception:
        pass
    return data


def write_service_config(root: Path, pythonw: Path, agent: Path, data_dir: Path) -> dict:
    PROGRAM_DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "root": str(root),
        "pythonw": str(pythonw),
        "agent": str(agent),
        "data_dir": str(data_dir),
        "pipe": PIPE_NAME,
    }
    SERVICE_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def resolve_data_dir() -> Path:
    """The service/GUI data dir; installer-provided, then user Documents."""
    data = load_service_config().get("data_dir", "")
    if data:
        path = Path(data)
    else:
        path = Path.home() / "Documents" / "GLKVM Screen Guard"
    path.mkdir(parents=True, exist_ok=True)
    (path / "logs").mkdir(parents=True, exist_ok=True)
    (path / "snapshots").mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return resolve_data_dir() / "config.ini"


def snapshot_path() -> Path:
    return resolve_data_dir() / "snapshots" / "local.json"


def logs_dir() -> Path:
    return resolve_data_dir() / "logs"


def save_password_machine(password: str) -> None:
    PROGRAM_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not password:
        PASSWORD_BIN.unlink(missing_ok=True)
        return
    blob = dpapi.protect(password, machine=True)
    PASSWORD_BIN.write_text(blob, encoding="ascii")


def load_password_machine() -> str:
    try:
        return dpapi.unprotect(PASSWORD_BIN.read_text(encoding="ascii")).strip()
    except Exception:
        return ""


def password_saved() -> bool:
    return PASSWORD_BIN.exists() and bool(load_password_machine())
