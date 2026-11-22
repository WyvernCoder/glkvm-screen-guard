# Changelog

## v1.2.1 — 2026-11-22

### Fixed

- HDR state now uses the Windows 11 24H2+ split HDR/WCG API
  (`DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2` and `SET_HDR_STATE`) on build 26100+.
  The legacy `DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO` reports generic advanced color,
  so an HDR-off/WCG-on display was incorrectly shown and captured as “HDR on”.
  The active HDR state is now `activeColorMode == HDR`; WCG is not treated as HDR.
- HDR restore/off uses `SET_HDR_STATE` on modern Windows. Older Windows builds keep
  the legacy API; targets where the modern API is unavailable fall back with a warning.
- Snapshots now record `hdr_api`. On modern Windows, HDR restore from a pre-v1.2.1
  snapshot without `hdr_api: "v2"` is skipped until the user recaptures, preventing
  stale WCG data from turning HDR on.
- The recorded-snapshot label now distinguishes the captured HDR state from the
  restore-HDR option and updates immediately when that option changes.

### Upgrade

- Restart the service, then capture the local display snapshot once more.

## v1.2.0 — 2026-11-22

First public release.

### Added

- Windows service `GLKVMGuard` (LocalSystem, automatic start) with RM10 polling,
  viewer state machine, configuration, daily logs, and a named-pipe server.
- Session-1 SYSTEM display agent: duplicated `winlogon` token,
  `TokenSessionId=1`, `CreateProcessAsUser`; CCD/HDR run only in a dedicated
  windowless thread that rebinds the input desktop for each command.
- Named pipe `\\.\pipe\GLKVMGuard-v1` for GUI ↔ service and service ↔ agent,
  with queued output and single-thread peek/read to avoid pipe deadlocks.
- Display engine: topology reading, local snapshot capture/validation, connector
  slot lock or target-ID follow, KVM-only / duplicate / extend / keep-current,
  10-second verification, rollback, and failure cooldown.
- Automatic switching from `/api/streamer` + `/api/log`, leave debounce, minimum
  dwell, and a master switch for detection-only operation.
- GUI service client: service/agent status, start/stop, capture, snapshot restore,
  and display queries through the service.
- Session change, power resume, and pre-shutdown handlers; service stop performs
  a best-effort local restore.
- `install_service.ps1` and `uninstall_service.ps1`; service install writes
  `C:\ProgramData\GLKVM Screen Guard\service.json`, sets the service Python
  environment, and syncs the machine-scope DPAPI password.
- `RunGUI.bat` as the user entry point; no packaged executable is required.
- English is the default UI language; Chinese remains available in Settings.
  `README.md` is English, with a Chinese `README.zh-CN.md`.

### Verified

- Service install/start/restart/uninstall, agent running as SYSTEM in Session 1.
- Pipe `query`, `capture`, and `apply_snapshot`.
- Real switch through the service/agent: KVM in **1085 ms**, local
  27U8A 4K164.99 + HDR restored in **859 ms**.
- Service stop in **0.5 s**.
- `check_i18n`, detection, read-only display, and UI smoke checks passed.

### Fixed

- The recorded-snapshot label now distinguishes the captured HDR state from the
  “turn system HDR on after restore” option. It displays, for example,
  `HDR restore: off (captured on)` and updates immediately when the option changes,
  so turning off the restore option no longer looks like a stale HDR state.

### Known limitations

- No-user logon screen and end-to-end lock/sleep/shutdown tests are still pending.
- The GUI uses a read-only `QueryDisplayConfig` to fill the display table;
  switching/capture/restore go through the service and agent.
- Two-step authentication is not supported.

## v1.1.0

- Added the display engine: local snapshots with validation, connector slot lock
  and target-ID follow, connect/disconnect actions, verification, and rollback.
- Added the four-tab GUI, display table, HDR restore, and automatic switching
  guardrails.
- Added `README.md`, `INSTALL.md`, and `AGENTS.md`.

## v1.0.0

- Initial RM10 client: manual IP/username/password, pinned TLS fingerprint,
  `/api/streamer` + `/api/log` detection, leave debounce, tray, themes, i18n,
  config, and logs.
