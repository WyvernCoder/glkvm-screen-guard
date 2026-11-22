"""Tiny JSON translation layer.

Language files live in ``./langs`` and use the ``"#KEY": "text"`` shape.
Missing keys fall back to English, then to the key itself.
"""

from __future__ import annotations

import json
from pathlib import Path


class I18n:
    def __init__(self, lang_dir: Path | str, default: str = "en_US") -> None:
        self.lang_dir = Path(lang_dir)
        self.code = default
        self.data: dict[str, str] = {}
        self.fallback: dict[str, str] = {}
        self._load_fallback()
        self.load(default)

    def _read(self, code: str) -> dict[str, str]:
        path = self.lang_dir / f"{code}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in raw.items()}
        except Exception:
            return {}

    def _load_fallback(self) -> None:
        for code in ("en_US", "zh_CN"):
            data = self._read(code)
            if data:
                self.fallback = data
                return

    def available(self) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for path in sorted(self.lang_dir.glob("*.json")):
            data = self._read(path.stem)
            if not data:
                continue
            result.append((path.stem, data.get("#LANG_NAME", path.stem)))
        return result or [("en_US", "English")]

    def load(self, code: str) -> None:
        data = self._read(code)
        if not data:
            data = self._read("en_US")
        self.code = code
        self.data = data

    def tr(self, key: str, **kwargs) -> str:
        text = self.data.get(key) or self.fallback.get(key) or key
        if kwargs:
            try:
                text = text.format(**kwargs)
            except Exception:
                pass
        return text
