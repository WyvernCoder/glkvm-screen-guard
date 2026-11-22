"""Shared display-slot binding logic for the agent, service and GUI."""

from __future__ import annotations

from . import ccd


def port_rank(display: ccd.Display) -> tuple[int, int, int, int]:
    if display.output_tech in (10, 11):
        rank = 1
    elif display.output_tech in (5, 6):
        rank = 2
    else:
        rank = 3
    return (rank, display.output_tech, display.connector_instance, display.target_id)


def slot_map(top: ccd.Topology) -> dict[str, int]:
    ordered = sorted(top.connected, key=lambda d: (d.adapter, port_rank(d)))
    return {display.key: index for index, display in enumerate(ordered, 1)}


def resolve_slot_display(
    top: ccd.Topology, slot: int, slot_key: str, target: str
) -> ccd.Display | None:
    slots = slot_map(top)
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


def configured_kvm_key(
    top: ccd.Topology, mode: str, slot: int, slot_key: str, target: str
) -> str:
    if mode in ("none", "unset"):
        return ""
    if mode == "target":
        if not target or target == "none":
            return ""
        for display in top.connected:
            if display.key == target:
                return display.key
        return ""
    display = resolve_slot_display(top, slot, slot_key, target)
    return display.key if display else ""


def display_payload(display: ccd.Display, slot: int) -> dict:
    return {
        "key": display.key,
        "slot_key": display.slot_key,
        "slot": slot,
        "name": display.name,
        "adapter": display.adapter,
        "target_id": display.target_id,
        "source_id": display.source_id,
        "source_name": display.source_name,
        "monitor_path": display.monitor_path,
        "output_tech": display.output_tech,
        "output_name": display.output_name,
        "active": display.active,
        "available": display.available,
        "width": display.width,
        "height": display.height,
        "hz": display.hz,
        "hdr_supported": display.hdr_supported,
        "hdr_enabled": display.hdr_enabled,
        "hdr_bits": display.hdr_bits,
        "display_number": display.display_number,
        "connector_instance": display.connector_instance,
    }


def topology_payload(
    top: ccd.Topology, kvm_key: str = "", snapshot: dict | None = None
) -> dict:
    slots = slot_map(top)
    displays = [
        display_payload(display, slots[display.key])
        for display in sorted(top.connected, key=lambda d: slots[d.key])
    ]
    return {
        "mode": top.mode,
        "duplicate": top.duplicate,
        "kvm_key": kvm_key,
        "active_keys": [d.key for d in top.active],
        "displays": displays,
        "snapshot": snapshot or {},
    }
