"""Unit tests for the three-state detection logic (no network).

Run with:  py\\Scripts\\python.exe tests\\test_detect.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.kvm import KvmClient


def _line(session_id: str, day: datetime, stream: bool = True) -> str:
    flags = "{'stream': True}" if stream else "{'stream': False}"
    return (
        f"[{day:%Y-%m-%d %H:%M:%S}] --- kvmd] --- Registered new client session: "
        f"WsSession(id={session_id}, {flags}); clients now: 1"
    )


def make_client(logs: list[str]) -> KvmClient:
    client = KvmClient("127.0.0.1", "u", "p", timeout=1)
    client._token = "dummy"
    client.get_streamer = lambda: None          # KVM output is off
    client.get_log = lambda: logs.pop(0)
    return client


def main() -> int:
    now = datetime.now()
    old = _line("100", now - timedelta(days=1))
    new = _line("200", now - timedelta(days=1))  # deliberately old timestamp

    # 1) old forgotten tab at startup: seed, stay idle
    c = make_client([old])
    first = c.detect()
    assert first.state == "idle", first
    print("seed old ->", first.state, first.reason)

    # 2) a new id while running wakes even if its timestamp is wrong (1970 clock)
    c = make_client([old, new])
    assert c.detect().state == "idle"
    second = c.detect()
    assert second.state == "watching" and second.reason == "log_new", second
    print("new id with old clock ->", second.state, second.reason)

    # 3) a recent session at startup is treated as a viewer
    recent = _line("300", now - timedelta(seconds=10))
    c = make_client([recent])
    first = c.detect()
    assert first.state == "watching", first
    print("seed recent ->", first.state, first.reason)

    # 4) stream=False / hid_only must be ignored
    ignored = _line("400", now, stream=False)
    c = make_client([ignored])
    first = c.detect()
    assert first.state == "idle", first
    print("stream=False ->", first.state, first.reason)

    print("ALL DETECTION TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
