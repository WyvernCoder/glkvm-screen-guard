# GLKVM Screen Guard

A Windows 11 desktop tool for **GL.iNet GLKVM RM10**. It watches whether someone is
viewing the machine through GLKVM and switches the display layout automatically,
then restores the local 4K 165 Hz + HDR layout when the last viewer leaves.

Current version: **v1.2.2**. Polling and the switch state machine run in a LocalSystem
Windows service; display changes are executed by a Session-1 SYSTEM display agent.
The GUI is a configuration/status client over a named pipe, so it does not need to
stay open. **English is the default UI language**; Chinese is available in Settings.
A Chinese README is kept at [README.zh-CN.md](README.zh-CN.md).

## Features

- **Windows service** `GLKVMGuard` (LocalSystem, automatic start) polls the RM10 and
  owns the viewer-detection state machine.
- **Session-1 display agent** duplicates the `winlogon` token, sets
  `TokenSessionId=1`, and runs `CreateProcessAsUser`. CCD/HDR run only in the agent's
  dedicated windowless thread, rebinding the current input desktop for every command.
- **Named pipe** `\\.\pipe\GLKVMGuard-v1` connects the GUI, service, and agent. The
  implementation uses single-I/O-thread peek/read to avoid synchronous-pipe deadlocks;
  requests/responses are matched by ID.
- **Display engine**: current-topology reading, local snapshot capture/validation,
  slot lock (connector-based or target-ID-based), KVM-only / duplicate / extend /
  keep-current actions, 10-second verification, automatic rollback, and failure
  cooldown.
- **Automatic switching**: viewer detection from `/api/streamer` + `/api/log`, leave
  debounce, minimum dwell, and a master switch that can disable switching while
  keeping detection.
- **Operational paths**: logon/lock/unlock, power resume, pre-shutdown, tray residency,
  light/dark themes, bilingual UI, and daily logs.
- **Service controls in Settings**: start, stop, and uninstall `GLKVMGuard` with UAC.
  Uninstall keeps user config/logs/snapshots; start reconnects the GUI automatically.
- **Data separation**: user config/logs/snapshot under Documents; service metadata and
  machine-scope DPAPI password under ProgramData.

## Requirements

- Windows 11, Python 3.10+, PyQt6, pywin32.
- GL.iNet GLKVM RM10 (kvmd 4.82 GL fork) on the local network.
- A dual-output PC; HDR is handled only through the system “Use HDR” switch.
- Administrator rights for the service installation.
- Two-step authentication is not supported.

## Quick start

No packaged executable is required: the app runs from this folder through
`RunGUI.bat`. The three setup commands are explained below.

### 1. Create the local Python environment

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_env.ps1
```

This creates the local `.\py` virtual environment and installs the two runtime
dependencies, **PyQt6** (GUI) and **pywin32** (Windows service/named pipe). It needs
internet access and does not require Administrator rights. Run it once; if `.\py`
already exists, you can skip it. If your Python installation is in a non-default
location, run the script and check its output.

### 2. Create the desktop shortcut

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

This creates the **GLKVM Screen Guard** shortcut on the current user's Desktop and
points it at this folder's Python/GUI. It does not install the service and does not
need Administrator rights. You can skip it and launch `RunGUI.bat` directly.

### 3. Install the Windows service

```powershell
# Run this in an elevated PowerShell.
powershell -ExecutionPolicy Bypass -File .\install_service.ps1
```

This installs and starts the **GLKVMGuard** service (LocalSystem, automatic start),
writes `C:\ProgramData\GLKVM Screen Guard\service.json`, sets the service's Python
environment, and syncs the machine-scope KVM password from the user config. The
service is what provides background/pre-login detection. Re-run the same script to
repair or upgrade the service; use `uninstall_service.ps1` to remove it.

### 4. Launch the GUI

Double-click the desktop shortcut, or run:

```powershell
.\RunGUI.bat
```

`RunGUI.bat` checks that `.\py` exists and starts `main.py` with `pythonw.exe`. It
does not require a separate packaged build. The service keeps running when the GUI is
closed; the GUI is only for configuration and status.

## First use

1. Open **Connection** and enter the RM10 IP (for example `192.168.1.100`), username,
   and KVM web password.
2. Click **Test connection**. On first use, confirm the certificate SHA-256 fingerprint;
   the tool pins that fingerprint and does not rely on certificate dates.
3. Open **Displays** and confirm the KVM target is locked. Choose `Screen 2 (LGD_MP1.1_)`
   (connector slot, survives EDID changes) or `Follow monitor LGD_MP1.1_ (t8448)`
   (current target ID). If EDID changes are a concern, use **Screen 2**.
4. Open **Actions** and click **Capture current display settings**. This records the
   local snapshot and is required before automatic KVM entry is allowed.
5. Make sure **Enable GLKVM screen guard** is on.
6. The service polls continuously. **Start/Stop** in the GUI pauses/resumes the
   service polling.
7. To verify automatic switching, open the KVM web viewer for a few seconds and close
   it; the local layout returns about 10 seconds later. Use **Apply current display
   snapshot** for a manual restore.

## Data locations

| Content | Path |
|---|---|
| Configuration | `<Documents>\GLKVM Screen Guard\config.ini` |
| Display snapshot | `<Documents>\GLKVM Screen Guard\snapshots\local.json` |
| Logs | `<Documents>\GLKVM Screen Guard\logs\YYYY-MM-DD.log` |
| Service metadata / machine password | `C:\ProgramData\GLKVM Screen Guard\` |
| Program/dependencies | this repository + `./py` (local venv, not committed) |

## Development

```powershell
.\py\Scripts\python.exe main.py                  # console
.\RunGUI.bat                                     # GUI, no console
.\py\Scripts\python.exe tests\check_i18n.py      # i18n completeness
.\py\Scripts\python.exe tests\test_detect.py     # detection state machine (offline)
.\py\Scripts\python.exe tests\test_displays.py   # CCD read-only + snapshot validation
.\py\Scripts\python.exe tests\smoke_ui.py        # UI smoke (needs GLKVM_TEST_IP)
```

- Python 3.10+; dependencies are PyQt6 and pywin32.
- Fusion style and native widgets; no custom-drawn widgets.
- Strings live in `langs/*.json`; themes in `themes/*.qss`.
- Service polling/state machine: `app/service_runtime.py`; agent:
  `app/agent_runtime.py`; pipe: `app/pipe.py`.
- `smoke_ui.py` uses a temporary config and only performs an unauthenticated probe;
  set `GLKVM_TEST_IP` to run it against a real RM10.
- English (`en_US`) is the default UI language; switch to Chinese in Settings.

## Current limitations

- The no-user logon screen and end-to-end lock/sleep/shutdown tests are still pending;
  the service has SESSIONCHANGE/POWEREVENT/PRESHUTDOWN handlers wired.
- HDR state is exact on Windows 11 24H2+ (build 26100+) via the split HDR/WCG API.
  Older builds fall back to the legacy advanced-color API, which cannot distinguish
  HDR from WCG.
- After upgrading from v1.2.0 or earlier, restart the service and capture the local
  snapshot once. Old snapshots lack `hdr_api` and are not used to turn HDR on.
- The GUI still performs a read-only `QueryDisplayConfig` to fill the display table;
  switching/capture/restore all go through the service and agent.
- Two-step authentication is not supported.
- No standalone installer is provided; run `RunGUI.bat` from this folder after the
  setup steps above.

## Documentation

- [INSTALL.md](INSTALL.md): step-by-step installation and troubleshooting
- [AGENTS.md](AGENTS.md): developer handoff and architecture notes
- [README.zh-CN.md](README.zh-CN.md): Chinese README
- [CHANGELOG.md](CHANGELOG.md): release history

## License

MIT. See [LICENSE](LICENSE).
