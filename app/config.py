"""Persistent settings stored in ``<Documents>/GLKVM Screen Guard/config.ini``."""

from __future__ import annotations

import configparser
import logging
from pathlib import Path
from typing import Any

from . import dpapi
from .paths import CONFIG_PATH, ensure_data_dirs

log = logging.getLogger("glkvm.config")

DEFAULTS: dict[str, dict[str, str]] = {
    "connection": {
        "ip": "",
        "user": "admin",
        "password_dpapi": "",
        "cert_sha256": "",
        "poll_seconds": "5",
        "auto_detect_on_launch": "true",
    },
    "actions": {
        "on_connect": "kvm-only",
        "on_disconnect": "restore",
        "screen_number": "1",
        "hdr_restore": "true",
        "kvm_screen": "2",
        "kvm_target": "",
        "kvm_slot": "0",
        "kvm_slot_key": "",
        "kvm_bind_mode": "",
        "min_primary_hz": "100",
        "auto_apply": "true",
        "min_dwell_seconds": "10",
    },
    "ui": {
        "language": "en_US",
        "theme": "light",
        "minimize_to_tray": "true",
        "start_at_login": "false",
    },
    "window": {
        "geometry": "",
        "last_tab": "0",
    },
}


class Config:
    def __init__(self, path: Path | str = CONFIG_PATH) -> None:
        self.path = Path(path)
        self.cp = configparser.ConfigParser(interpolation=None)
        self.load()

    # ---------------------------------------------------------------- load
    def load(self) -> None:
        self.cp.clear()
        if self.path.exists():
            try:
                self.cp.read(self.path, encoding="utf-8")
            except Exception as exc:  # corrupted INI: keep defaults, do not crash
                log.warning("Could not read config %s: %s", self.path, exc)
        for section, values in DEFAULTS.items():
            if not self.cp.has_section(section):
                self.cp.add_section(section)
            for key, value in values.items():
                if not self.cp.has_option(section, key):
                    self.cp.set(section, key, value)

    # ---------------------------------------------------------------- save
    def save(self) -> None:
        try:
            ensure_data_dirs()
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                self.cp.write(fh)
            tmp.replace(self.path)
        except Exception as exc:
            log.error("Could not save config %s: %s", self.path, exc)

    # ---------------------------------------------------------------- get
    def get(self, section: str, key: str, default: str = "") -> str:
        try:
            return self.cp.get(section, key)
        except Exception:
            return default

    def get_int(self, section: str, key: str, default: int = 0) -> int:
        try:
            return int(float(self.get(section, key, str(default))))
        except Exception:
            return default

    def get_bool(self, section: str, key: str, default: bool = False) -> bool:
        value = self.get(section, key, "true" if default else "false")
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    # ---------------------------------------------------------------- set
    def set(self, section: str, key: str, value: Any) -> None:
        if not self.cp.has_section(section):
            self.cp.add_section(section)
        if isinstance(value, bool):
            value = "true" if value else "false"
        self.cp.set(section, key, str(value))

    def set_bool(self, section: str, key: str, value: bool) -> None:
        self.set(section, key, bool(value))

    # ------------------------------------------------------------ password
    def password(self) -> str:
        return dpapi.unprotect(self.get("connection", "password_dpapi", ""))

    def set_password(self, password: str) -> None:
        self.set("connection", "password_dpapi", dpapi.protect(password))

    # ------------------------------------------------------------ snapshots
    def connection_snapshot(self) -> dict[str, Any]:
        return {
            "ip": self.get("connection", "ip").strip(),
            "user": self.get("connection", "user").strip(),
            "password": self.password(),
            "pin": self.get("connection", "cert_sha256").strip().upper(),
            "poll_seconds": max(1, self.get_int("connection", "poll_seconds", 5)),
        }
