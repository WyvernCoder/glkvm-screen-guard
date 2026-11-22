"""Read-only checks: CCD structs, topology, snapshot capture/validate, safety.

This test never calls SetDisplayConfig(APPLY) and never changes the display
layout.  It is safe to run while the user is at the machine.
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import ccd
from app.display_manager import DisplayManager

logger = logging.getLogger("test_displays")
logger.addHandler(logging.NullHandler())


def main() -> int:
    sizes = ccd.struct_sizes()
    expected = {
        "path": 72,
        "mode": 64,
        "targetName": 420,
        "sourceName": 84,
        "adapterName": 276,
        "colorInfo": 32,
        "colorInfo2": 36,
        "setColor": 24,
        "setHdrState": 24,
    }
    for key, value in expected.items():
        if sizes.get(key) != value:
            print(f"FAIL struct {key}: {sizes.get(key)} != {value}")
            return 1
    print("ok structs:", sizes)

    # Pure decoder checks: WCG is not HDR, and bit 1 alone is not HDR state.
    for value, mode, want_supported, want_enabled in (
        (0xD7, 1, True, False),
        (0xD7, 2, True, True),
        (0x40, 1, False, False),
        (0x02, 1, False, False),
    ):
        info = ccd.decode_color_info_v2(value, mode, 10)
        if info["supported"] != want_supported or info["enabled"] != want_enabled:
            print(
                "FAIL HDR decoder",
                hex(value),
                mode,
                info["supported"],
                info["enabled"],
            )
            return 1
    print("ok HDR decoder (WCG != HDR)")

    top = ccd.query_topology()
    if not top.active:
        print("FAIL no active display")
        return 1
    print("ok active:", [(d.display_number, d.name, d.width, d.height, round(d.hz, 2), d.hdr_enabled) for d in top.active])
    print("ok connected:", [(d.name, d.active, d.output_name, d.target_id) for d in top.connected])
    hdr_info = ccd._color_info(top.active[0].path)
    print(
        "ok HDR source:",
        f"build={ccd.WINDOWS_BUILD}",
        f"api={hdr_info.get('api')}",
        f"activeColorMode={hdr_info.get('active_color_mode')}",
        f"enabled={hdr_info.get('enabled')}",
    )
    if top.active[0].hdr_enabled != hdr_info.get("enabled"):
        print("FAIL HDR display state mismatch")
        return 1

    kvm_key = ""
    if len(top.connected) >= 2:
        # Mirror the UI inference: exactly one inactive/available target.
        inactive = [d for d in top.connected if not d.active and d.available]
        if len(inactive) == 1:
            kvm_key = inactive[0].key
    print("ok kvm key:", kvm_key or "(none)")

    tmpdir = Path(tempfile.mkdtemp(prefix="glkvm-display-test-"))
    snap_path = tmpdir / "local.json"
    settings = {
        "kvm_target": kvm_key,
        "snapshot_path": str(snap_path),
        "min_primary_hz": 100,
        "hdr_restore": True,
        "on_connect": "kvm-only",
        "on_disconnect": "restore",
        "screen_number": 1,
    }

    # Safety: without a snapshot, connect/restore must refuse without touching CCD.
    manager = DisplayManager(settings, logger)
    missing = manager.connect()
    if missing.ok or missing.code != "no_snapshot":
        print(f"FAIL connect without snapshot: ok={missing.ok} code={missing.code}")
        return 1
    print("ok connect blocked without snapshot")

    captured = manager.capture_local()
    if not captured.ok:
        print(f"FAIL capture: {captured.code} {captured.detail}")
        return 1
    print("ok capture:", captured.code)

    info = manager.snapshot_info()
    if not info["ok"]:
        print(f"FAIL snapshot info: {info}")
        return 1
    print("ok snapshot:", info["width"], info["height"], round(info["hz"], 2), info["name"])

    snapshot = manager.load_snapshot()
    if snapshot is None:
        print("FAIL load snapshot")
        return 1
    if ccd.HDR_API_V2 and snapshot.get("hdr_api") != "v2":
        print(f"FAIL snapshot HDR API: {snapshot.get('hdr_api')!r} != 'v2'")
        return 1
    print("ok snapshot HDR API:", snapshot.get("hdr_api"))
    rc = ccd.validate_snapshot(snapshot)
    if rc != 0:
        print(f"FAIL SDC_VALIDATE rc={rc}")
        return 1
    print("ok SDC_VALIDATE rc=0")

    if ccd.HDR_API_V2:
        legacy = dict(snapshot)
        legacy.pop("hdr_api", None)
        calls: list[tuple[str, bool]] = []
        original_set_hdr = ccd.set_hdr_by_key
        ccd.set_hdr_by_key = lambda key, enabled: calls.append((key, enabled)) or 0
        try:
            manager._restore_hdr(legacy)
        finally:
            ccd.set_hdr_by_key = original_set_hdr
        if calls:
            print("FAIL legacy HDR restore was not skipped")
            return 1
        print("ok legacy HDR restore guard")

    paths, modes = ccd.snapshot_to_arrays(snapshot)
    print("ok arrays:", len(paths), "paths,", len(modes), "modes")

    restore = manager.restore_local()
    if not restore.ok or restore.code not in ("noop", "restored"):
        print(f"FAIL restore: ok={restore.ok} code={restore.code}")
        return 1
    print("ok restore:", restore.code)

    shutil.rmtree(tmpdir, ignore_errors=True)
    print("DISPLAY CHECK: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
