"""Display actions: capture, connect/disconnect, verify, rollback."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import ccd

log = logging.getLogger("glkvm.display")


@dataclass
class ActionResult:
    kind: str
    ok: bool
    code: str
    duration_ms: int = 0
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "ok": self.ok,
            "code": self.code,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }


class DisplayManager:
    def __init__(self, settings: dict, logger: logging.Logger | None = None) -> None:
        self.settings = dict(settings)
        self.log = logger or log
        self.kvm_key = str(settings.get("kvm_target") or "")
        self.snapshot_path = Path(settings.get("snapshot_path") or "")
        self.min_hz = float(settings.get("min_primary_hz") or 100)
        self.hdr_restore = bool(settings.get("hdr_restore", True))
        self.on_connect = str(settings.get("on_connect") or "kvm-only")
        self.on_disconnect = str(settings.get("on_disconnect") or "restore")
        self.screen_number = int(settings.get("screen_number") or 1)
        self._stopped = False

    # ------------------------------------------------------------- control
    def stop(self) -> None:
        self._stopped = True

    def _wait(self, predicate, timeout: float = 12.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stopped:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        if self._stopped:
            return False
        try:
            return bool(predicate())
        except Exception:
            return False

    # ------------------------------------------------------------- topology
    def infer_kvm_key(self) -> str:
        top = ccd.query_topology()
        active = {d.key for d in top.active}
        candidates = [
            d for d in top.connected
            if not d.active and d.available and d.name and d.key not in active
        ]
        if len(candidates) == 1:
            return candidates[0].key
        return ""

    def active_keys(self) -> set[str]:
        return {d.key for d in ccd.query_topology().active}

    def topology_mode(self) -> str:
        return ccd.query_topology().mode

    # ------------------------------------------------------------- snapshot
    def load_snapshot(self) -> dict | None:
        return ccd.load_snapshot(self.snapshot_path)

    def snapshot_info(self) -> dict:
        snapshot = self.load_snapshot()
        if snapshot is None:
            return {"ok": False, "code": "no_snapshot", "errors": []}
        errors = ccd.snapshot_errors(snapshot, self.kvm_key, self.min_hz)
        displays = snapshot.get("displays") or []
        first = displays[0] if displays else {}
        return {
            "ok": not errors,
            "code": "ok" if not errors else "invalid",
            "errors": errors,
            "width": first.get("width", 0),
            "height": first.get("height", 0),
            "hz": first.get("hz", 0),
            "hdr_supported": first.get("hdr_supported", False),
            "hdr_enabled": first.get("hdr_enabled", False),
            "name": first.get("name", ""),
            "number": first.get("display_number"),
            "topology": snapshot.get("topology", ""),
            "captured_at": snapshot.get("captured_at", ""),
            "count": len(displays),
        }

    def capture_local(self) -> ActionResult:
        started = time.perf_counter()
        kvm = self.kvm_key or self.infer_kvm_key()
        try:
            top = ccd.query_topology(kvm)
        except ccd.CcdError as exc:
            return self._error("capture", "ccd_error", started, {"rc": exc.rc})
        if kvm and any(d.key == kvm for d in top.active):
            return self._error("capture", "remote_layout", started)
        try:
            snapshot = ccd.capture_snapshot(kvm)
        except ccd.CcdError as exc:
            return self._error("capture", "capture_failed", started, {"rc": exc.rc})
        errors = ccd.snapshot_errors(snapshot, kvm, self.min_hz)
        if errors:
            return self._error("capture", "invalid_snapshot", started, {"errors": errors})
        rc = ccd.validate_snapshot(snapshot)
        if rc != 0:
            return self._error("capture", "validate_failed", started, {"rc": rc})
        ccd.save_snapshot(snapshot, self.snapshot_path)
        self.log.info("Captured local snapshot: %s paths, topology=%s", len(snapshot["paths"]), snapshot["topology"])
        return ActionResult(
            kind="capture", ok=True, code="captured",
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail={"snapshot": snapshot},
        )

    # ------------------------------------------------------------- connect
    def connect(self) -> ActionResult:
        started = time.perf_counter()
        if self.on_connect == "hold":
            return self._done("connect", "noop", started)
        if not self.kvm_key:
            return self._error("connect", "no_kvm", started)
        # Entering a remote layout is only safe when the disconnect action can
        # bring us back.  With the default "restore" that means a valid local
        # snapshot must exist first.
        if self.on_disconnect == "restore":
            snapshot = self.load_snapshot()
            if snapshot is None:
                return self._error("connect", "no_snapshot", started)
            errors = ccd.snapshot_errors(snapshot, self.kvm_key, self.min_hz)
            if errors:
                return self._error("connect", "no_snapshot", started, {"errors": errors})
        try:
            top = ccd.query_topology(self.kvm_key)
        except ccd.CcdError as exc:
            return self._error("connect", "ccd_error", started, {"rc": exc.rc})

        if self.on_connect == "kvm-only" and top.mode == "single" and {d.key for d in top.active} == {self.kvm_key}:
            return self._done("connect", "noop", started)
        if self.on_connect == "clone" and top.duplicate:
            return self._done("connect", "noop", started)
        if self.on_connect == "extend" and top.mode == "extend":
            return self._done("connect", "noop", started)

        try:
            previous = ccd.capture_snapshot(self.kvm_key)
        except ccd.CcdError as exc:
            return self._error("connect", "capture_failed", started, {"rc": exc.rc})

        if self.on_connect == "kvm-only":
            return self._enter_kvm_only(previous, started)
        if self.on_connect == "clone":
            return self._enter_clone(previous, started)
        if self.on_connect == "extend":
            return self._enter_extend(previous, started)
        return self._error("connect", "unknown_action", started, {"action": self.on_connect})

    def _enter_kvm_only(self, previous: dict, started: float) -> ActionResult:
        ok = False
        rc = ccd.apply_topology(ccd.SDC_TOPOLOGY_EXTERNAL)
        if rc == 0:
            ok = self._wait(lambda: {d.key for d in ccd.query_topology().active} == {self.kvm_key}, 8)
        if not ok:
            ccd.display_switch("/external")
            ok = self._wait(lambda: {d.key for d in ccd.query_topology().active} == {self.kvm_key}, 12)
        if ok:
            self.log.info("Switched to KVM-only in %.1fs", (time.perf_counter() - started))
            return self._done("connect", "switched", started, {"mode": "kvm-only"})
        self._rollback(previous)
        return self._error("connect", "verify_failed", started, {"rc": rc})

    def _enter_clone(self, previous: dict, started: float) -> ActionResult:
        ok = False
        rc = ccd.apply_topology(ccd.SDC_TOPOLOGY_CLONE)
        if rc == 0:
            ok = self._wait(lambda: ccd.query_topology().duplicate, 8)
        if not ok:
            ccd.display_switch("/clone")
            ok = self._wait(lambda: ccd.query_topology().duplicate, 12)
        if ok:
            self._hdr_off_active()
            return self._done("connect", "switched", started, {"mode": "clone"})
        self._rollback(previous)
        return self._error("connect", "verify_failed", started, {"rc": rc})

    def _enter_extend(self, previous: dict, started: float) -> ActionResult:
        ok = False
        rc = ccd.apply_topology(ccd.SDC_TOPOLOGY_EXTEND)
        if rc == 0:
            ok = self._wait(lambda: ccd.query_topology().mode == "extend", 8)
        if not ok:
            ccd.display_switch("/extend")
            ok = self._wait(lambda: ccd.query_topology().mode == "extend", 12)
        if ok:
            return self._done("connect", "switched", started, {"mode": "extend"})
        self._rollback(previous)
        return self._error("connect", "verify_failed", started, {"rc": rc})

    # ------------------------------------------------------------ disconnect
    def disconnect(self) -> ActionResult:
        started = time.perf_counter()
        if self.on_disconnect == "hold":
            return self._done("disconnect", "noop", started)
        if self.on_disconnect == "restore":
            return self.restore_local(started=started)
        if self.on_disconnect == "screen":
            return self._screen_only(self.screen_number, started)
        if self.on_disconnect == "clone":
            try:
                previous = ccd.capture_snapshot(self.kvm_key)
            except ccd.CcdError as exc:
                return self._error("disconnect", "capture_failed", started, {"rc": exc.rc})
            return self._enter_clone(previous, started)
        return self._error("disconnect", "unknown_action", started, {"action": self.on_disconnect})

    def restore_local(self, started: float | None = None) -> ActionResult:
        started = started or time.perf_counter()
        snapshot = self.load_snapshot()
        if snapshot is None:
            return self._error("disconnect", "no_snapshot", started)
        errors = ccd.snapshot_errors(snapshot, self.kvm_key, self.min_hz)
        if errors:
            return self._error("disconnect", "invalid_snapshot", started, {"errors": errors})

        expected = {d["key"] for d in snapshot.get("displays", [])}
        if expected and self.active_keys() == expected:
            self._restore_hdr(snapshot)
            return self._done("disconnect", "noop", started, {"mode": snapshot.get("topology")})

        rc = ccd.apply_snapshot(snapshot)
        ok = rc == 0 and self._wait(lambda: self._snapshot_matches(snapshot), 10)
        if not ok:
            self.log.warning("Snapshot restore failed (rc=%s); falling back to /internal", rc)
            ccd.display_switch("/internal")
            ok = self._wait(lambda: self.active_keys() == expected, 10)
        if ok:
            self._restore_hdr(snapshot)
            return self._done(
                "disconnect", "restored", started,
                {"mode": snapshot.get("topology"), "rc": rc},
            )
        return self._error("disconnect", "verify_failed", started, {"rc": rc})

    def _screen_only(self, number: int, started: float) -> ActionResult:
        if number == 1:
            rc = ccd.apply_topology(ccd.SDC_TOPOLOGY_INTERNAL)
            ok = rc == 0 and self._wait(lambda: self._is_single_non_kvm(), 8)
            if not ok:
                ccd.display_switch("/internal")
                ok = self._wait(lambda: self._is_single_non_kvm(), 10)
            if ok:
                self._hdr_on_active()
                return self._done("disconnect", "switched", started, {"mode": "screen1"})
            return self._error("disconnect", "verify_failed", started, {"rc": rc})
        rc = ccd.apply_topology(ccd.SDC_TOPOLOGY_EXTERNAL)
        ok = rc == 0 and self._wait(lambda: len(ccd.query_topology().active) == 1, 8)
        if not ok:
            ccd.display_switch("/external")
            ok = self._wait(lambda: len(ccd.query_topology().active) == 1, 10)
        if ok:
            return self._done("disconnect", "switched", started, {"mode": "screen2"})
        return self._error("disconnect", "verify_failed", started, {"rc": rc})

    # ------------------------------------------------------------- helpers
    def _snapshot_matches(self, snapshot: dict) -> bool:
        top = ccd.query_topology(self.kvm_key)
        expected = {d["key"] for d in snapshot.get("displays", [])}
        if {d.key for d in top.active} != expected:
            return False
        for item in snapshot.get("displays", []):
            active = next((d for d in top.active if d.key == item["key"]), None)
            if active is None:
                return False
            want = float(item.get("hz") or 0)
            if want and abs(active.hz - want) > max(2.0, want * 0.03):
                return False
        return True

    def _is_single_non_kvm(self) -> bool:
        top = ccd.query_topology(self.kvm_key)
        return len(top.active) == 1 and (not self.kvm_key or top.active[0].key != self.kvm_key)

    def _rollback(self, previous: dict) -> bool:
        self.log.warning("Rolling back display configuration")
        try:
            rc = ccd.apply_snapshot(previous)
            if rc == 0 and self._wait(lambda: self._snapshot_matches(previous), 8):
                return True
        except Exception as exc:
            self.log.error("Rollback failed: %s", exc)
        ccd.display_switch("/internal")
        return self._wait(lambda: self._is_single_non_kvm(), 8)

    def _restore_hdr(self, snapshot: dict) -> None:
        if not self.hdr_restore:
            return
        # A snapshot captured before the Windows 11 24H2 HDR API split may
        # contain advanced-color state (WCG) mislabelled as HDR.  Do not turn
        # HDR on from it; require a fresh capture with the modern API.
        if ccd.HDR_API_V2 and snapshot.get("hdr_api") != "v2":
            self.log.warning(
                "Skipping HDR restore: snapshot HDR state predates the Windows 11 24H2 API; recapture first"
            )
            return
        for item in snapshot.get("displays", []):
            if item.get("hdr_supported"):
                rc = ccd.set_hdr_by_key(item["key"], True)
                self.log.info("HDR on %s: rc=%s", item.get("name"), rc)

    def _hdr_off_active(self) -> None:
        for display in ccd.query_topology(self.kvm_key).active:
            if display.hdr_supported and display.hdr_enabled:
                rc = ccd.set_hdr_by_key(display.key, False)
                self.log.info("HDR off %s: rc=%s", display.name, rc)

    def _hdr_on_active(self) -> None:
        if not self.hdr_restore:
            return
        for display in ccd.query_topology(self.kvm_key).active:
            if display.hdr_supported and not display.hdr_enabled:
                rc = ccd.set_hdr_by_key(display.key, True)
                self.log.info("HDR on %s: rc=%s", display.name, rc)

    def _done(self, kind: str, code: str, started: float, detail: dict | None = None) -> ActionResult:
        return ActionResult(
            kind=kind, ok=True, code=code,
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail=detail or {},
        )

    def _error(self, kind: str, code: str, started: float, detail: dict | None = None) -> ActionResult:
        self.log.error("Display action %s failed: %s %s", kind, code, detail or {})
        return ActionResult(
            kind=kind, ok=False, code=code,
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail=detail or {},
        )
