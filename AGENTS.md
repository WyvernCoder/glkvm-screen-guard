# AGENTS.md — Project Handoff

> Read [README.md](README.md), [INSTALL.md](INSTALL.md), and [CHANGELOG.md](CHANGELOG.md) first.

## 1. What this is

**GLKVM Screen Guard** is a Windows 11 tool for **GL.iNet GLKVM RM10**. It detects
whether someone is viewing the PC through the KVM and switches display layouts:
KVM-only while watched, local 4K 165 Hz + HDR after the last viewer leaves.

Current version: **v1.2.1**. English is the default UI language (`en_US`); Chinese
is selectable. The GUI is a configuration/status client over a named pipe; display
work is done by a Session-1 SYSTEM agent. No packaged executable is required — users
run `RunGUI.bat` from the project folder after setup.

Test machine characteristics (not configuration defaults):

- Dual-output Windows 11, RTX 5080.
- Local display `27U8A Pro-S`, DP, 3840×2160@164.99, HDR10.
- KVM display `LGD_MP1.1_`, DVI, connected but output off when idle.
- The RM10 IP is entered by the user; never hardcode it.

## 2. Architecture

```
RM10 (HTTPS polling)  <-->  GLKVMGuard service (Session 0, LocalSystem)
                                   |
                                   | named pipe \\.\pipe\GLKVMGuard-v1
                     +-------------+-------------+
                     |                           |
       Session-1 SYSTEM agent            PyQt6 GUI (user session)
       CCD / HDR only                    config + status only
```

- **Service** (`service.py`, `app/service_runtime.py`): polls RM10, runs the
  detection/state machine, loads config, writes logs, serves the pipe, and supervises
  the agent.
- **Agent** (`agent.py`, `app/agent_runtime.py`): started by the service via a
  duplicated `winlogon` token with `TokenSessionId=1` and `CreateProcessAsUser`.
  It owns all CCD/HDR calls.
- **Pipe** (`app/pipe.py`): JSON lines; GUI and agent are clients. Each side uses a
  single I/O thread plus `PeekNamedPipe`-based non-blocking reads; commands and
  broadcasts are queued and written by that same thread.
- **GUI** (`app/ui/main_window.py`, `app/service_client.py`): start/stop, capture,
  snapshot restore, display table, settings, tray. In service mode it does not start a
  local `PollWorker` or local display actions.
- **Standalone mode**: if the service is not installed (`service.json` absent), the
  GUI can use `PollWorker`/`DisplayWorker` in-process. `GLKVM_STANDALONE=1` forces
  this mode for tests.

## 3. Run and test

```powershell
.\py\Scripts\python.exe main.py                  # console
.\RunGUI.bat                                     # no console
.\py\Scripts\python.exe tests\check_i18n.py      # i18n completeness
.\py\Scripts\python.exe tests\test_detect.py     # detection state machine
.\py\Scripts\python.exe tests\test_displays.py   # CCD read-only + snapshot validation
.\py\Scripts\python.exe tests\smoke_ui.py        # standalone UI smoke; needs GLKVM_TEST_IP
powershell -ExecutionPolicy Bypass -File .\install_service.ps1   # elevated
Get-Service GLKVMGuard
```

- `setup_env.ps1` creates `./py` and installs PyQt6 + pywin32.
- `install.ps1` creates the desktop shortcut; `install_service.ps1` installs/starts the
  service (administrator).
- `tests/smoke_ui.py` sets `GLKVM_STANDALONE=1` and uses a temporary config. It only
  probes TLS without authentication; without `GLKVM_TEST_IP` it skips.
- The service pipe accepts an internal `connect`/`disconnect` command for verification;
  the GUI does not expose those.

## 4. Directory

```
main.py                   GUI entry point
service.py                Windows service entry point (pywin32)
agent.py                  Session-1 SYSTEM agent entry point
RunGUI.bat                user launcher (checks ./py, starts pythonw)
run-debug.bat             console launcher for tracebacks
install.ps1               desktop shortcut
install_service.ps1       service install/repair (elevated)
uninstall_service.ps1     service uninstall (elevated)
app/
  paths.py                Documents/GLKVM Screen Guard, version
  config.py               config.ini, defaults, snapshots
  dpapi.py                user/machine DPAPI
  i18n.py                 langs/*.json
  logs.py                 daily logs + Qt log signal
  theme.py                themes index + QSS + palette
  kvm.py                  RM10 client, TLS pin, streamer/log detection
  poller.py               standalone TestWorker/PollWorker
  ccd.py                  CCD ctypes: query/snapshot/apply/HDR
  binding.py              slot/target resolution
  display_manager.py      capture, connect/disconnect, verify, rollback
  display_worker.py       standalone display worker
  service_paths.py        ProgramData/service.json, machine password
  pipe.py                 named-pipe server/client
  session_launcher.py     winlogon token → Session-1 process
  service_runtime.py      polling, state machine, pipe/agent supervisor
  agent_runtime.py        windowless CCD thread, input-desktop binding
  service_client.py       GUI service client
  ui/main_window.py       four tabs, tray, settings, service status
langs/                    en_US.json / zh_CN.json
themes/                   themes.json + light.qss / dark.qss
assets/icon.ico
tests/                    check_i18n, test_detect, test_displays, smoke_ui
prototype/                early HTML prototype (not product code)
py/                       local venv (gitignored)
```

## 5. Data and configuration

User data (gitignored):

```
<Documents>\GLKVM Screen Guard\
  config.ini
  snapshots\local.json
  logs\YYYY-MM-DD.log
```

Service data (ProgramData, not in git):

```
C:\ProgramData\GLKVM Screen Guard\
  service.json
  password.bin
```

`service.json` may be written with a UTF-8 BOM by PowerShell 5.1; read it with
`encoding="utf-8-sig"`.

Config sections:

- `[connection]` ip, user, password_dpapi, cert_sha256, poll_seconds,
  auto_detect_on_launch
- `[actions]` on_connect, on_disconnect, screen_number, hdr_restore, kvm_screen,
  `kvm_target`, `kvm_slot`, `kvm_slot_key`, `kvm_bind_mode`, `min_primary_hz`,
  `auto_apply`, `min_dwell_seconds`
- `[ui]` language, theme, minimize_to_tray, start_at_login
- `[window]` geometry (base64), last_tab

`kvm_bind_mode`:
- `slot` — use `kvm_slot_key` = adapter + connectorInstance + outputTechnology;
  survives EDID/name/target changes.
- `target` — follow `kvm_target` = adapter + targetId.
- `none` — detect only.

`kvm_slot` is the user-facing slot number; `kvm_screen` is retained for compatibility.
Password is stored only as DPAPI ciphertext. Never log passwords, tokens, or raw
`/api/log` contents.

## 6. Code conventions

- Python 3.10+, dependencies PyQt6 + pywin32.
- Fusion style and native widgets; no custom-drawn controls. QSS for colors/borders.
- User-visible strings go through `i18n.tr("#KEY")`; add keys to both language files.
  Default UI language is `en_US`.
- Set tooltips on interactive controls; log user actions with `log_action`.
- Save settings immediately; save window geometry on close.
- Language changes use `_retranslate()`; theme changes use `ThemeManager.apply`.
- No network I/O on the UI thread; no `SetDisplayConfig` on the UI thread.
- **The service must not call CCD** (Session 0 returns `ERROR_ACCESS_DENIED`); only the
  Session-1 agent may.
- **Do not use blocking read and write concurrently on one synchronous pipe handle.**
  The pipe layer uses one I/O thread, `PeekNamedPipe`, and queued writes; keep it that way.
- **Stop order:** ask the agent to restore, notify shutdown, then set `_stop`; do not
  close the agent pipe before restore is delivered. Do not wait for the pipe accept
  thread during SCM stop (daemon thread).
- Safety gate: when `on_disconnect=restore`, entering the KVM layout requires a valid
  snapshot. Do not bypass `DisplayManager.connect()` or the UI guard.
- Validate after each switch and roll back on failure; never switch on unknown state.
- Do not start packaging/installer work unless the user explicitly asks. The shipped
  entry point is `RunGUI.bat`.

## 7. RM10 API and detection

- Login: `POST /api/auth/login` form `user/passwd`; returns
  `{ok:true,result:{token}}` and `auth_token` cookie.
- Authenticated GET: append `?auth_token=...`; 403 means re-login once; 401 is
  unauthenticated; 429 is rate limiting.
- `/api/streamer`: `result.streamer` may be null. Check `sinks.h264.has_clients`,
  `sinks.jpeg.has_clients`, and `stream.clients`.
- `/api/log`: parse `Registered new client session: WsSession(id=…, {'stream': True, …})`;
  filter `stream=False` and `hid_only=True`.
- When the screen is off, `has_clients` can stay false; a new log session is the wake
  signal. Use `_wake_until` to keep “watching” for 60 seconds.
- A new session ID is the wake signal; do not depend on the RM10 clock (it may be 1970).
  The log timestamp is used only for the initial seed.
- Unknown (null streamer and unreadable log) keeps the previous state; never guess.
- Do not poll `/api/streamer/snapshot`; it leaks file descriptors when there is no signal.
- `/system/clients` is local-device only (HTTP 403).
- Two-step authentication is not supported.

## 8. Technical findings

- **Session 0 cannot access CCD**: `QueryDisplayConfig` returns 5
  (`ERROR_ACCESS_DENIED`); `SetDisplayConfig` cannot work there.
- **Session-1 SYSTEM agent works**: duplicate the `winlogon` token, set
  `TokenSessionId=1`, and use `CreateProcessAsUser` with `winsta0\default`.
- **Input desktop binding is required**: bind in a dedicated windowless STA thread
  with `OpenInputDesktop` + `SetThreadDesktop`; the main thread returns 170
  (`ERROR_BUSY`). Rebind per command because the desktop changes at lock/logon.
- **TLS**: RM10 presents a self-signed certificate with a broken date range
  (1970–1979). Pin only the SHA-256 fingerprint; never enable normal date/chain
  validation.
- **Slot identity**: use adapter + connectorInstance + outputTechnology. Do not use
  LUIDs (change on reboot), monitor names (EDID can change), or a bare target index.
- **Slot numbers**: DP/eDP = 1, HDMI/DVI = 2, others = 3; target ID is the final
  tie-breaker. On the test machine the physical display is screen 1 and KVM is screen 2.
- **HDR** is per-display. On Windows 11 build 26100+, use
  `DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2` (type 15) and `SET_HDR_STATE` (type 16):
  `highDynamicRangeSupported` is bit 4, `highDynamicRangeUserEnabled` bit 5, and the
  active state is `activeColorMode == 2` (HDR); `activeColorMode == 1` is WCG, not HDR.
  Builds below 26100 fall back to the legacy advanced-color types 9/10, which cannot
  separate HDR from WCG. Snapshots store `hdr_api`; HDR restore from a snapshot without
  `hdr_api: "v2"` is skipped on modern Windows until recaptured.
- **Real switch**: Windows external topology first, then `DisplaySwitch.exe /external`
  as fallback; verify for 10 seconds and roll back on failure.
- **Measured**: service/agent KVM-only switch ~1085 ms, local restore ~859 ms,
  service stop ~0.5 s.

## 9. Known work

- Run a final no-user logon-screen check and end-to-end lock/sleep/shutdown checks.
- The GUI still uses read-only `QueryDisplayConfig` for the display table.
- Two-step authentication is not supported.
- Packaging/installers are not part of this release; users run `RunGUI.bat`.

## 10. Do not

- Do not commit `py/`, `tests/tmp/`, user configs, logs, or ProgramData files.
- Do not add a second local poller while the service is active.
- Do not call `SetDisplayConfig` from the UI thread or from Session 0.
- Do not bypass the no-snapshot safety gate.
- Do not change TLS validation to normal chain/date checks.
- Do not log passwords or tokens.
- Do not add internal development phase labels to user-facing docs.
