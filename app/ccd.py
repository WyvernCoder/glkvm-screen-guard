"""Windows CCD (DisplayConfig) layer.

Reads the display topology with ``QueryDisplayConfig`` and applies explicit
path/mode sets with ``SetDisplayConfig``.  In service mode the display calls
run in the Session-1 SYSTEM agent; the standalone GUI uses a worker thread in
the interactive session, where the CCD calls are allowed.

No UI imports here: this module is testable without Qt.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)

# ---------------------------------------------------------------- constants
QDC_ALL_PATHS = 0x00000001
QDC_ONLY_ACTIVE_PATHS = 0x00000002

SDC_TOPOLOGY_INTERNAL = 0x00000001
SDC_TOPOLOGY_CLONE = 0x00000002
SDC_TOPOLOGY_EXTEND = 0x00000004
SDC_TOPOLOGY_EXTERNAL = 0x00000008
SDC_TOPOLOGY_SUPPLIED = 0x00000010
SDC_USE_SUPPLIED_DISPLAY_CONFIG = 0x00000020
SDC_VALIDATE = 0x00000040
SDC_APPLY = 0x00000080
SDC_SAVE_TO_DATABASE = 0x00000200
SDC_ALLOW_CHANGES = 0x00000400

PATH_ACTIVE = 0x00000001
MODE_IDX_INVALID = 0xFFFFFFFF

DEVICE_INFO_GET_SOURCE_NAME = 1
DEVICE_INFO_GET_TARGET_NAME = 2
DEVICE_INFO_GET_ADAPTER_NAME = 4
DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9
DEVICE_INFO_SET_ADVANCED_COLOR_STATE = 10
DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2 = 15
DEVICE_INFO_SET_HDR_STATE = 16

# Windows 11 24H2 split advanced color into HDR and WCG.  The legacy API
# (types 9/10) cannot distinguish them; on 26100+ use the new HDR API.
HDR_API_BUILD = 26100
try:
    WINDOWS_BUILD = int(sys.getwindowsversion().build)
except Exception:
    WINDOWS_BUILD = 0
HDR_API_V2 = WINDOWS_BUILD >= HDR_API_BUILD

log = logging.getLogger("glkvm.ccd")
_V2_WARNING_SHOWN = False

# DISPLAYCONFIG_OUTPUT_TECHNOLOGY_*
TECH_NAMES = {
    0: "Other",
    1: "VGA",
    2: "S-Video",
    3: "Composite",
    4: "Component",
    5: "DVI",
    6: "HDMI",
    7: "LVDS",
    8: "D-JPN",
    9: "SDI",
    10: "DisplayPort",
    11: "eDP",
    12: "UDI",
    13: "UDI",
    14: "SDTV",
    15: "Miracast",
    16: "Internal",
    17: "DP-USB",
}

DISPLAY_NUMBER_RE = re.compile(r"DISPLAY(\d+)$", re.IGNORECASE)


class CcdError(Exception):
    def __init__(self, rc: int, operation: str) -> None:
        self.rc = int(rc)
        self.operation = operation
        super().__init__(f"{operation} failed: rc={self.rc}")


# ------------------------------------------------------------------ structs
class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class PATH_SOURCE(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("statusFlags", wintypes.UINT),
    ]


class PATH_TARGET(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("outputTechnology", wintypes.UINT),
        ("rotation", wintypes.UINT),
        ("scaling", wintypes.UINT),
        ("refreshRateNum", wintypes.UINT),
        ("refreshRateDen", wintypes.UINT),
        ("scanLineOrdering", wintypes.UINT),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", wintypes.UINT),
    ]


class PATH_INFO(ctypes.Structure):
    _fields_ = [("source", PATH_SOURCE), ("target", PATH_TARGET), ("flags", wintypes.UINT)]


class REGION2D(ctypes.Structure):
    _fields_ = [("cx", wintypes.UINT), ("cy", wintypes.UINT)]


class VIDEO_SIGNAL_INFO(ctypes.Structure):
    _fields_ = [
        ("pixelRate", ctypes.c_ulonglong),
        ("hSyncFreqNum", wintypes.UINT),
        ("hSyncFreqDen", wintypes.UINT),
        ("vSyncFreqNum", wintypes.UINT),
        ("vSyncFreqDen", wintypes.UINT),
        ("activeSize", REGION2D),
        ("totalSize", REGION2D),
        ("videoStandard", wintypes.UINT),
        ("scanLineOrdering", wintypes.UINT),
    ]


class TARGET_MODE(ctypes.Structure):
    _fields_ = [("targetVideoSignalInfo", VIDEO_SIGNAL_INFO)]


class POINTL(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SOURCE_MODE(ctypes.Structure):
    _fields_ = [
        ("width", wintypes.UINT),
        ("height", wintypes.UINT),
        ("pixelFormat", wintypes.UINT),
        ("position", POINTL),
    ]


class MODE_UNION(ctypes.Union):
    _fields_ = [("targetMode", TARGET_MODE), ("sourceMode", SOURCE_MODE)]


class MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("infoType", wintypes.UINT),
        ("id", wintypes.UINT),
        ("adapterId", LUID),
        ("mode", MODE_UNION),
    ]


class DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.UINT),
        ("size", wintypes.UINT),
        ("adapterId", LUID),
        ("id", wintypes.UINT),
    ]


class TARGET_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DEVICE_INFO_HEADER),
        ("flags", wintypes.UINT),
        ("outputTechnology", wintypes.UINT),
        ("edidManufactureId", wintypes.USHORT),
        ("edidProductCodeId", wintypes.USHORT),
        ("connectorInstance", wintypes.UINT),
        ("monitorFriendlyDeviceName", ctypes.c_wchar * 64),
        ("monitorDevicePath", ctypes.c_wchar * 128),
    ]


class SOURCE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DEVICE_INFO_HEADER),
        ("viewGdiDeviceName", ctypes.c_wchar * 32),
    ]


class ADAPTER_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DEVICE_INFO_HEADER),
        ("adapterDevicePath", ctypes.c_wchar * 128),
    ]


class COLOR_INFO(ctypes.Structure):
    _fields_ = [
        ("header", DEVICE_INFO_HEADER),
        ("value", wintypes.UINT),
        ("colorEncoding", wintypes.UINT),
        ("bitsPerColorChannel", wintypes.UINT),
    ]


class SET_COLOR(ctypes.Structure):
    _fields_ = [("header", DEVICE_INFO_HEADER), ("value", wintypes.UINT)]


class COLOR_INFO_2(ctypes.Structure):
    _fields_ = [
        ("header", DEVICE_INFO_HEADER),
        ("value", wintypes.UINT),
        ("colorEncoding", wintypes.UINT),
        ("bitsPerColorChannel", wintypes.UINT),
        ("activeColorMode", wintypes.UINT),
    ]


class SET_HDR_STATE(ctypes.Structure):
    _fields_ = [("header", DEVICE_INFO_HEADER), ("value", wintypes.UINT)]


# ---------------------------------------------------------------- signatures
user32.GetDisplayConfigBufferSizes.argtypes = [
    wintypes.UINT,
    ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(wintypes.UINT),
]
user32.GetDisplayConfigBufferSizes.restype = wintypes.LONG

user32.QueryDisplayConfig.argtypes = [
    wintypes.UINT,
    ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(PATH_INFO),
    ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(MODE_INFO),
    ctypes.c_void_p,
]
user32.QueryDisplayConfig.restype = wintypes.LONG

user32.SetDisplayConfig.argtypes = [
    wintypes.UINT,
    ctypes.POINTER(PATH_INFO),
    wintypes.UINT,
    ctypes.POINTER(MODE_INFO),
    wintypes.UINT,
]
user32.SetDisplayConfig.restype = wintypes.LONG

user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.c_void_p]
user32.DisplayConfigGetDeviceInfo.restype = wintypes.LONG
user32.DisplayConfigSetDeviceInfo.argtypes = [ctypes.c_void_p]
user32.DisplayConfigSetDeviceInfo.restype = wintypes.LONG


# ------------------------------------------------------------------ helpers
def struct_sizes() -> dict[str, int]:
    return {
        "path": ctypes.sizeof(PATH_INFO),
        "mode": ctypes.sizeof(MODE_INFO),
        "targetName": ctypes.sizeof(TARGET_NAME),
        "sourceName": ctypes.sizeof(SOURCE_NAME),
        "adapterName": ctypes.sizeof(ADAPTER_NAME),
        "colorInfo": ctypes.sizeof(COLOR_INFO),
        "colorInfo2": ctypes.sizeof(COLOR_INFO_2),
        "setColor": ctypes.sizeof(SET_COLOR),
        "setHdrState": ctypes.sizeof(SET_HDR_STATE),
    }


def _query(flags: int) -> tuple[list[PATH_INFO], list[MODE_INFO]]:
    np = wintypes.UINT(0)
    nm = wintypes.UINT(0)
    rc = user32.GetDisplayConfigBufferSizes(flags, ctypes.byref(np), ctypes.byref(nm))
    if rc != 0:
        raise CcdError(rc, "GetDisplayConfigBufferSizes")

    path_array = (PATH_INFO * (np.value + 16))()
    mode_array = (MODE_INFO * (nm.value + 32))()
    pcount = wintypes.UINT(len(path_array))
    mcount = wintypes.UINT(len(mode_array))
    rc = user32.QueryDisplayConfig(
        flags,
        ctypes.byref(pcount),
        path_array,
        ctypes.byref(mcount),
        mode_array,
        None,
    )
    if rc != 0:
        raise CcdError(rc, "QueryDisplayConfig")
    return list(path_array[: pcount.value]), list(mode_array[: mcount.value])


def query_active() -> tuple[list[PATH_INFO], list[MODE_INFO]]:
    return _query(QDC_ONLY_ACTIVE_PATHS)


def query_all() -> tuple[list[PATH_INFO], list[MODE_INFO]]:
    return _query(QDC_ALL_PATHS)


def _get_device_info(packet) -> int:
    return int(user32.DisplayConfigGetDeviceInfo(ctypes.cast(ctypes.byref(packet), ctypes.c_void_p)))


def _set_device_info(packet) -> int:
    return int(user32.DisplayConfigSetDeviceInfo(ctypes.cast(ctypes.byref(packet), ctypes.c_void_p)))


def luid_str(luid: LUID) -> str:
    return f"{luid.LowPart:08x}:{luid.HighPart:08x}"


def _adapter_name(path: PATH_INFO) -> str:
    packet = ADAPTER_NAME()
    packet.header.type = DEVICE_INFO_GET_ADAPTER_NAME
    packet.header.size = ctypes.sizeof(ADAPTER_NAME)
    packet.header.adapterId = path.target.adapterId
    packet.header.id = 0
    if _get_device_info(packet) != 0:
        return ""
    return str(packet.adapterDevicePath).rstrip("\x00")


def _source_name(path: PATH_INFO) -> str:
    packet = SOURCE_NAME()
    packet.header.type = DEVICE_INFO_GET_SOURCE_NAME
    packet.header.size = ctypes.sizeof(SOURCE_NAME)
    packet.header.adapterId = path.source.adapterId
    packet.header.id = path.source.id
    if _get_device_info(packet) != 0:
        return ""
    return str(packet.viewGdiDeviceName).rstrip("\x00")


def _target_name(path: PATH_INFO) -> TARGET_NAME | None:
    packet = TARGET_NAME()
    packet.header.type = DEVICE_INFO_GET_TARGET_NAME
    packet.header.size = ctypes.sizeof(TARGET_NAME)
    packet.header.adapterId = path.target.adapterId
    packet.header.id = path.target.id
    if _get_device_info(packet) != 0:
        return None
    return packet


def decode_color_info_v2(value: int, active_mode: int, bits: int = 0) -> dict:
    """Decode DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2.

    Windows 11 24H2+ separates HDR from WCG.  Bit 1 is *advanced color active*
    and is also set for WCG-only targets, so it must not be used as HDR state.
    """
    value = int(value)
    active_mode = int(active_mode)
    hdr_supported = bool(value & (1 << 4))
    wcg_user_enabled = bool(value & (1 << 7))
    return {
        "rc": 0,
        "api": "v2",
        "supported": hdr_supported,
        "enabled": hdr_supported and active_mode == 2,
        "wide": wcg_user_enabled,
        "forceDisabled": False,
        "bits": int(bits),
        "raw": value,
        "hdr_user_enabled": bool(value & (1 << 5)),
        "wcg_supported": bool(value & (1 << 6)),
        "wcg_user_enabled": wcg_user_enabled,
        "active_color_mode": active_mode,
    }


def _color_info_legacy(path: PATH_INFO) -> dict:
    packet = COLOR_INFO()
    packet.header.type = DEVICE_INFO_GET_ADVANCED_COLOR_INFO
    packet.header.size = ctypes.sizeof(COLOR_INFO)
    packet.header.adapterId = path.target.adapterId
    packet.header.id = path.target.id
    rc = _get_device_info(packet)
    return {
        "rc": rc,
        "api": "v1",
        "supported": bool(packet.value & 1),
        "enabled": bool(packet.value & 2),
        "wide": bool(packet.value & 4),
        "forceDisabled": bool(packet.value & 8),
        "bits": int(packet.bitsPerColorChannel),
        "raw": int(packet.value),
        "hdr_user_enabled": False,
        "wcg_supported": False,
        "wcg_user_enabled": False,
        "active_color_mode": -1,
    }


def _color_info(path: PATH_INFO) -> dict:
    global _V2_WARNING_SHOWN
    if HDR_API_V2:
        packet = COLOR_INFO_2()
        packet.header.type = DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2
        packet.header.size = ctypes.sizeof(COLOR_INFO_2)
        packet.header.adapterId = path.target.adapterId
        packet.header.id = path.target.id
        rc = _get_device_info(packet)
        if rc == 0:
            return decode_color_info_v2(
                int(packet.value),
                int(packet.activeColorMode),
                int(packet.bitsPerColorChannel),
            )
        if rc == 31:
            log.debug("HDR API v2 unavailable for an inactive/unsupported target (rc=31)")
        elif not _V2_WARNING_SHOWN:
            log.warning(
                "HDR API v2 query failed (rc=%s); falling back to legacy advanced-color API",
                rc,
            )
            _V2_WARNING_SHOWN = True
    return _color_info_legacy(path)


def set_hdr(path: PATH_INFO, enabled: bool) -> int:
    info = _color_info(path)
    if info["rc"] != 0:
        return info["rc"]
    if not info["supported"]:
        return 50  # ERROR_NOT_SUPPORTED
    if info.get("api") == "v2":
        packet = SET_HDR_STATE()
        packet.header.type = DEVICE_INFO_SET_HDR_STATE
        packet.header.size = ctypes.sizeof(SET_HDR_STATE)
    else:
        packet = SET_COLOR()
        packet.header.type = DEVICE_INFO_SET_ADVANCED_COLOR_STATE
        packet.header.size = ctypes.sizeof(SET_COLOR)
    packet.header.adapterId = path.target.adapterId
    packet.header.id = path.target.id
    packet.value = 1 if enabled else 0
    return _set_device_info(packet)


def _source_mode(modes: list[MODE_INFO], idx: int) -> SOURCE_MODE | None:
    if idx == MODE_IDX_INVALID or idx >= len(modes):
        return None
    mode = modes[idx]
    if mode.infoType != 1:
        return None
    return mode.mode.sourceMode


def _target_mode(modes: list[MODE_INFO], idx: int) -> TARGET_MODE | None:
    if idx == MODE_IDX_INVALID or idx >= len(modes):
        return None
    mode = modes[idx]
    if mode.infoType != 2:
        return None
    return mode.mode.targetMode


def _display_number(source_name: str) -> int | None:
    match = DISPLAY_NUMBER_RE.search(source_name or "")
    return int(match.group(1)) if match else None


@dataclass
class Display:
    key: str
    name: str
    adapter: str
    target_id: int
    source_id: int
    source_name: str
    monitor_path: str
    output_tech: int
    output_name: str
    active: bool
    available: bool
    width: int = 0
    height: int = 0
    hz: float = 0.0
    hdr_supported: bool = False
    hdr_enabled: bool = False
    hdr_bits: int = 0
    display_number: int | None = None
    connector_instance: int = 0
    path: PATH_INFO | None = field(default=None, repr=False)
    source_mode_idx: int = MODE_IDX_INVALID
    target_mode_idx: int = MODE_IDX_INVALID

    @property
    def slot_key(self) -> str:
        """Stable physical-slot key, independent of monitor name/EDID/target id.

        ``targetId`` can be reassigned when a monitor's EDID changes; the
        adapter's connector instance and output technology belong to the port,
        so this key survives a monitor swap on the same connector.
        """
        return f"{self.adapter}|ci{self.connector_instance}|tech{self.output_tech}"

    def summary(self) -> str:
        if self.width and self.height:
            text = f"{self.width} × {self.height}"
            if self.hz:
                text += f" @ {self.hz:.2f} Hz"
            if self.hdr_supported:
                text += " · HDR" + (" 开" if self.hdr_enabled else " 关")
            return text
        return "未激活"

    def role(self, kvm_key: str) -> str:
        if self.key == kvm_key:
            return "kvm" if self.active else "kvm_inactive"
        return "local" if self.active else "unused"


@dataclass
class Topology:
    active: list[Display]
    connected: list[Display]
    duplicate: bool
    mode: str  # single | duplicate | extend
    kvm_key: str = ""

    def by_key(self, key: str) -> Display | None:
        for display in self.connected:
            if display.key == key:
                return display
        return None

    def local_displays(self, kvm_key: str) -> list[Display]:
        return [d for d in self.active if d.key != kvm_key]


def _make_display(path: PATH_INFO, modes: list[MODE_INFO], active: bool) -> Display | None:
    target_name = _target_name(path)
    if target_name is None:
        return None
    friendly = str(target_name.monitorFriendlyDeviceName).rstrip("\x00")
    monitor_path = str(target_name.monitorDevicePath).rstrip("\x00")
    if not friendly and not active:
        return None
    adapter = _adapter_name(path)
    key = f"{adapter}|{path.target.id}"
    source_name = _source_name(path)
    source_mode = _source_mode(modes, path.source.modeInfoIdx)
    target_mode = _target_mode(modes, path.target.modeInfoIdx)
    hz = 0.0
    width = 0
    height = 0
    if target_mode is not None:
        info = target_mode.targetVideoSignalInfo
        if info.vSyncFreqDen:
            hz = info.vSyncFreqNum / info.vSyncFreqDen
        width = int(info.activeSize.cx)
        height = int(info.activeSize.cy)
    if source_mode is not None:
        width = int(source_mode.width) or width
        height = int(source_mode.height) or height
    color = _color_info(path)
    return Display(
        key=key,
        name=friendly or f"Target {path.target.id}",
        adapter=adapter,
        target_id=int(path.target.id),
        source_id=int(path.source.id),
        source_name=source_name,
        monitor_path=monitor_path,
        output_tech=int(path.target.outputTechnology),
        output_name=TECH_NAMES.get(int(path.target.outputTechnology), f"Tech {path.target.outputTechnology}"),
        active=active,
        available=bool(path.target.targetAvailable),
        width=width,
        height=height,
        hz=hz,
        hdr_supported=color["supported"],
        hdr_enabled=color["enabled"],
        hdr_bits=color["bits"],
        display_number=_display_number(source_name) if active else None,
        connector_instance=int(target_name.connectorInstance),
        path=path,
        source_mode_idx=int(path.source.modeInfoIdx),
        target_mode_idx=int(path.target.modeInfoIdx),
    )


def query_topology(kvm_key: str = "") -> Topology:
    active_paths, active_modes = query_active()
    active: list[Display] = []
    for path in active_paths:
        if not (path.flags & PATH_ACTIVE):
            continue
        display = _make_display(path, active_modes, True)
        if display is not None:
            active.append(display)

    all_paths, all_modes = query_all()
    seen = {d.key for d in active}
    connected: list[Display] = list(active)
    for path in all_paths:
        if not path.target.targetAvailable:
            continue
        display = _make_display(path, all_modes, False)
        if display is None:
            continue
        if display.key in seen:
            continue
        seen.add(display.key)
        connected.append(display)

    duplicate = len(active) >= 2 and all(
        p.source.id == active_paths[0].source.id
        and p.source.adapterId.LowPart == active_paths[0].source.adapterId.LowPart
        for p in active_paths
    )
    mode = "single"
    if len(active) >= 2:
        mode = "duplicate" if duplicate else "extend"
    return Topology(active=active, connected=connected, duplicate=duplicate, mode=mode, kvm_key=kvm_key)


# ---------------------------------------------------------------- snapshots
def _path_to_dict(path: PATH_INFO, adapter: str) -> dict:
    return {
        "adapter": adapter,
        "source": {
            "luid": [path.source.adapterId.LowPart, path.source.adapterId.HighPart],
            "id": int(path.source.id),
            "mode_info_idx": int(path.source.modeInfoIdx),
            "status_flags": int(path.source.statusFlags),
        },
        "target": {
            "luid": [path.target.adapterId.LowPart, path.target.adapterId.HighPart],
            "id": int(path.target.id),
            "mode_info_idx": int(path.target.modeInfoIdx),
            "output_technology": int(path.target.outputTechnology),
            "rotation": int(path.target.rotation),
            "scaling": int(path.target.scaling),
            "refresh_num": int(path.target.refreshRateNum),
            "refresh_den": int(path.target.refreshRateDen),
            "scan_line_ordering": int(path.target.scanLineOrdering),
            "target_available": bool(path.target.targetAvailable),
            "status_flags": int(path.target.statusFlags),
        },
        "flags": int(path.flags),
    }


def _mode_to_dict(mode: MODE_INFO, adapter: str) -> dict:
    item: dict = {
        "adapter": adapter,
        "info_type": int(mode.infoType),
        "id": int(mode.id),
        "luid": [mode.adapterId.LowPart, mode.adapterId.HighPart],
    }
    if mode.infoType == 1:
        source = mode.mode.sourceMode
        item["source"] = {
            "width": int(source.width),
            "height": int(source.height),
            "pixel_format": int(source.pixelFormat),
            "x": int(source.position.x),
            "y": int(source.position.y),
        }
    elif mode.infoType == 2:
        info = mode.mode.targetMode.targetVideoSignalInfo
        item["target"] = {
            "pixel_rate": int(info.pixelRate),
            "h_num": int(info.hSyncFreqNum),
            "h_den": int(info.hSyncFreqDen),
            "v_num": int(info.vSyncFreqNum),
            "v_den": int(info.vSyncFreqDen),
            "active_cx": int(info.activeSize.cx),
            "active_cy": int(info.activeSize.cy),
            "total_cx": int(info.totalSize.cx),
            "total_cy": int(info.totalSize.cy),
            "video_standard": int(info.videoStandard),
            "scan_line_ordering": int(info.scanLineOrdering),
        }
    return item


def capture_snapshot(kvm_key: str = "") -> dict:
    paths, modes = query_active()
    topology = query_topology(kvm_key)
    path_dicts: list[dict] = []
    for path in paths:
        adapter = _adapter_name(path)
        path_dicts.append(_path_to_dict(path, adapter))
    mode_dicts = [
        _mode_to_dict(mode, _adapter_name(paths[0]) if paths else "")
        for mode in modes
    ]
    # Each mode's adapter is the adapter of the path that owns it.  Resolve
    # it per mode via the path indices; fall back to the first path adapter.
    for index, mode in enumerate(modes):
        owner = ""
        for path in paths:
            if path.source.modeInfoIdx == index or path.target.modeInfoIdx == index:
                owner = _adapter_name(path)
                break
        if owner:
            mode_dicts[index]["adapter"] = owner
    # Per-display HDR state is only trustworthy if every active display used
    # the modern backend; otherwise mark the snapshot as legacy.
    hdr_api = "v2" if HDR_API_V2 else "v1"
    if HDR_API_V2 and not all(
        _color_info(display.path).get("api") == "v2" for display in topology.active
    ):
        hdr_api = "v1"
    return {
        "version": 1,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "topology": topology.mode,
        "kvm_key": kvm_key,
        "hdr_api": hdr_api,
        "paths": path_dicts,
        "modes": mode_dicts,
        "displays": [
            {
                "key": d.key,
                "name": d.name,
                "width": d.width,
                "height": d.height,
                "hz": d.hz,
                "hdr_supported": d.hdr_supported,
                "hdr_enabled": d.hdr_enabled,
                "hdr_bits": d.hdr_bits,
                "display_number": d.display_number,
            }
            for d in topology.active
        ],
    }


def snapshot_errors(snapshot: dict, kvm_key: str, min_hz: float = 100.0) -> list[str]:
    errors: list[str] = []
    paths = snapshot.get("paths") or []
    displays = snapshot.get("displays") or []
    if not paths:
        errors.append("快照没有活动路径")
    if not displays:
        errors.append("快照没有显示器信息")
    if displays:
        first = displays[0]
        if float(first.get("hz") or 0) < min_hz:
            errors.append(f"刷新率 {first.get('hz')} Hz 低于阈值 {min_hz} Hz")
        if kvm_key and first.get("key") == kvm_key:
            errors.append("快照的第一块屏是 KVM，不是本地布局")
    if any(d.get("key") == kvm_key for d in displays) and len(displays) == 1:
        errors.append("快照只有 KVM 屏")
    return errors


def save_snapshot(snapshot: dict, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def load_snapshot(path: Path | str) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _adapter_map() -> dict[str, LUID]:
    mapping: dict[str, LUID] = {}
    paths, _modes = query_all()
    for path in paths:
        name = _adapter_name(path)
        if name:
            mapping[name] = path.target.adapterId
    return mapping


def snapshot_to_arrays(snapshot: dict) -> tuple[list[PATH_INFO], list[MODE_INFO]]:
    adapters = _adapter_map()
    paths: list[PATH_INFO] = []
    for item in snapshot.get("paths", []):
        adapter = adapters.get(item.get("adapter", ""))
        if adapter is None:
            raise CcdError(87, "adapter not found")
        source = item["source"]
        target = item["target"]
        path = PATH_INFO()
        path.source.adapterId = adapter
        path.source.id = source["id"]
        path.source.modeInfoIdx = source["mode_info_idx"]
        path.source.statusFlags = source["status_flags"]
        path.target.adapterId = adapter
        path.target.id = target["id"]
        path.target.modeInfoIdx = target["mode_info_idx"]
        path.target.outputTechnology = target["output_technology"]
        path.target.rotation = target["rotation"]
        path.target.scaling = target["scaling"]
        path.target.refreshRateNum = target["refresh_num"]
        path.target.refreshRateDen = target["refresh_den"]
        path.target.scanLineOrdering = target["scan_line_ordering"]
        path.target.targetAvailable = target["target_available"]
        path.target.statusFlags = target["status_flags"]
        path.flags = item["flags"]
        paths.append(path)

    modes: list[MODE_INFO] = []
    for item in snapshot.get("modes", []):
        adapter = adapters.get(item.get("adapter", ""))
        if adapter is None:
            raise CcdError(87, "mode adapter not found")
        mode = MODE_INFO()
        mode.infoType = item["info_type"]
        mode.id = item["id"]
        mode.adapterId = adapter
        if mode.infoType == 1:
            source = item["source"]
            mode.mode.sourceMode.width = source["width"]
            mode.mode.sourceMode.height = source["height"]
            mode.mode.sourceMode.pixelFormat = source["pixel_format"]
            mode.mode.sourceMode.position.x = source["x"]
            mode.mode.sourceMode.position.y = source["y"]
        elif mode.infoType == 2:
            target = item["target"]
            info = mode.mode.targetMode.targetVideoSignalInfo
            info.pixelRate = target["pixel_rate"]
            info.hSyncFreqNum = target["h_num"]
            info.hSyncFreqDen = target["h_den"]
            info.vSyncFreqNum = target["v_num"]
            info.vSyncFreqDen = target["v_den"]
            info.activeSize.cx = target["active_cx"]
            info.activeSize.cy = target["active_cy"]
            info.totalSize.cx = target["total_cx"]
            info.totalSize.cy = target["total_cy"]
            info.videoStandard = target["video_standard"]
            info.scanLineOrdering = target["scan_line_ordering"]
        modes.append(mode)
    return paths, modes


def validate_snapshot(snapshot: dict) -> int:
    paths, modes = snapshot_to_arrays(snapshot)
    return _set(paths, modes, SDC_VALIDATE | SDC_USE_SUPPLIED_DISPLAY_CONFIG)


def apply_snapshot(snapshot: dict) -> int:
    paths, modes = snapshot_to_arrays(snapshot)
    return _set(
        paths,
        modes,
        SDC_USE_SUPPLIED_DISPLAY_CONFIG
        | SDC_APPLY
        | SDC_SAVE_TO_DATABASE
        | SDC_ALLOW_CHANGES,
    )


def _set(paths: list[PATH_INFO], modes: list[MODE_INFO], flags: int) -> int:
    path_array = (PATH_INFO * len(paths))(*paths) if paths else None
    mode_array = (MODE_INFO * len(modes))(*modes) if modes else None
    return int(user32.SetDisplayConfig(len(paths), path_array, len(modes), mode_array, flags))


def apply_topology(flag: int) -> int:
    # SDC_ALLOW_CHANGES and SDC_SAVE_TO_DATABASE are only valid together with
    # SDC_USE_SUPPLIED_DISPLAY_CONFIG.  Combining either of them with a
    # topology flag makes SetDisplayConfig fail with ERROR_INVALID_PARAMETER
    # (87) on current drivers, so a plain SDC_APPLY is the only form that works.
    return int(user32.SetDisplayConfig(0, None, 0, None, SDC_APPLY | flag))


def display_switch(argument: str) -> bool:
    """Call DisplaySwitch.exe directly (not via Start-Process)."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    exe = Path(system_root) / "System32" / "DisplaySwitch.exe"
    # The agent binds its CCD thread to the input desktop, which is Winlogon's
    # secure desktop while the workstation is locked.  A plain CreateProcess
    # would inherit that desktop and the projection switch would do nothing, so
    # force the child onto the interactive desktop explicitly.
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.lpDesktop = "winsta0\\default"
    try:
        subprocess.run(
            [str(exe), argument],
            check=False,
            timeout=10,
            creationflags=0x08000000,
            startupinfo=startupinfo,
        )
        return True
    except Exception:
        return False


def wait_for(predicate, timeout: float = 12.0, interval: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    try:
        return bool(predicate())
    except Exception:
        return False


def set_hdr_by_key(key: str, enabled: bool) -> int:
    paths, _modes = query_all()
    for path in paths:
        adapter = _adapter_name(path)
        if f"{adapter}|{path.target.id}" == key:
            return set_hdr(path, enabled)
    return 87
