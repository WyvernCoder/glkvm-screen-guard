"""Service runtime: polling, state machine, pipe server and agent supervisor.

Runs in Session 0 as LocalSystem.  It never calls CCD; it asks the Session-1
agent over the named pipe.  GUI clients use the same pipe for status/config.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import win32api
import win32con

from . import binding, ccd
from .config import Config
from .kvm import CertMismatch, KvmClient, KvmError
from .logs import setup_logging
from .pipe import PipeServer
from .service_paths import (
    PIPE_NAME,
    config_path,
    load_password_machine,
    load_service_config,
    logs_dir,
    resolve_data_dir,
    save_password_machine,
    snapshot_path,
)
from .session_launcher import LaunchError, active_console_session, launch_in_session

log = logging.getLogger("glkvm.service")

STILL_ACTIVE = 259
WTS_CONSOLE_CONNECT = 0x1
WTS_CONSOLE_DISCONNECT = 0x2
WTS_REMOTE_CONNECT = 0x3
WTS_REMOTE_DISCONNECT = 0x4
WTS_SESSION_LOGON = 0x5
WTS_SESSION_LOGOFF = 0x6
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
WTS_SESSION_TERMINATE = 0xA
PBT_APMSUSPEND = 0x4
PBT_APMRESUMEAUTOMATIC = 0x12
PBT_APMRESUMESUSPEND = 0x7


@dataclass
class AgentInfo:
    pid: int = 0
    session: int = 0
    connected: bool = False
    spawned_at: float = 0.0
    last_error: str = ""


class AgentChannel:
    """One service<->agent pipe; serializes commands and matches ids."""

    def __init__(self, channel) -> None:
        self.channel = channel
        self._lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self._next_id = 0
        self._outbox: queue.Queue = queue.Queue()

    def flush(self) -> None:
        while True:
            try:
                message = self._outbox.get_nowait()
            except queue.Empty:
                return
            self.channel.send(message)

    def on_message(self, message: dict) -> None:
        request_id = message.get("id")
        if request_id is None:
            return
        with self._lock:
            item = self._pending.get(int(request_id))
        if item is not None:
            event, box = item
            box.update(message)
            event.set()

    def notify(self, message: dict) -> None:
        """Send a no-response message (used for shutdown)."""
        self._outbox.put(dict(message))

    def command(self, cmd: str, settings: dict | None = None, timeout: float = 75.0) -> dict:
        log.info("Agent command queued: %s", cmd)
        with self._command_lock:
            log.info("Agent command lock acquired: %s", cmd)
            with self._lock:
                self._next_id += 1
                request_id = self._next_id
                event = threading.Event()
                box: dict = {}
                self._pending[request_id] = (event, box)
            try:
                self._outbox.put(
                    {"id": request_id, "cmd": cmd, "settings": settings or {}}
                )
                log.info("Agent command queued for write: %s id=%s", cmd, request_id)
                if not event.wait(timeout):
                    log.warning("Agent command timeout: %s id=%s", cmd, request_id)
                    return {"ok": False, "code": "agent_timeout"}
                log.info("Agent command reply: %s id=%s ok=%s", cmd, request_id, box.get("ok"))
                return dict(box)
            finally:
                with self._lock:
                    self._pending.pop(request_id, None)


class GuiChannel:
    """GUI side with a single pipe I/O thread.

    Broadcasts and responses are queued; only the client's handler thread
    writes to the synchronous named-pipe handle.
    """

    def __init__(self, channel) -> None:
        self.channel = channel
        self._outbox: queue.Queue = queue.Queue()

    def enqueue(self, message: dict) -> None:
        self._outbox.put(message)

    def flush(self) -> None:
        while True:
            try:
                message = self._outbox.get_nowait()
            except queue.Empty:
                return
            self.channel.send(message)


class ServiceRuntime:
    def __init__(self) -> None:
        self.data_dir = resolve_data_dir()
        self.log = setup_logging(logs_dir())
        self._stop = threading.Event()
        self._stopping = False
        self._paused = False
        self._state = "unknown"
        self._reason = ""
        self._last_check = "—"
        self._last_action: dict = {}
        self._last_clients = 0
        self._last_action_ts = 0.0
        self._fail_until = 0.0
        self._watching_since = 0.0
        self._idle_misses = 0
        self._disconnect_done = False
        self._action_busy = False
        self._action_thread: threading.Thread | None = None
        self._cfg: Config | None = None
        self._cfg_mtime = 0.0
        self._client: KvmClient | None = None
        self._client_key: tuple | None = None
        self._no_password_logged = False

        self._gui_lock = threading.Lock()
        self._gui_channels: list = []
        self._agent: AgentChannel | None = None
        self._agent_lock = threading.Lock()
        self._agent_info = AgentInfo()
        self._agent_spawn_lock = threading.Lock()

        self._pipe = PipeServer(self._on_pipe_client, PIPE_NAME)
        self._detection_thread: threading.Thread | None = None
        self._agent_thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.log.info("Service starting (data=%s)", self.data_dir)
        self._load_config(force=True)
        self._paused = not self._cfg_bool("connection", "auto_detect_on_launch", True)
        self._pipe.start()
        self._agent_thread = threading.Thread(
            target=self._agent_supervisor, name="agent-supervisor", daemon=True
        )
        self._agent_thread.start()
        self._detection_thread = threading.Thread(
            target=self._detection_loop, name="detection", daemon=True
        )
        self._detection_thread.start()
        self._broadcast_state()

    def run(self) -> None:
        self.start()
        try:
            while not self._stop.wait(1.0):
                pass
        finally:
            self.stop()

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self.log.info("Service stopping")
        # Keep the agent/GUI pipe handlers alive while asking the agent for a
        # final restore; setting _stop first would close the agent pipe before
        # the command can be delivered.
        try:
            if self._agent_connected():
                self._agent_command("restore", self._action_settings(), timeout=8.0)
        except Exception:
            pass
        try:
            with self._agent_lock:
                agent = self._agent
            if agent is not None:
                agent.notify({"cmd": "shutdown"})
                time.sleep(0.5)
        except Exception:
            pass
        self._stop.set()
        # The pipe accept loop can be blocked in ConnectNamedPipe; never let
        # it delay SCM stop.  The handlers are daemon threads and die with the
        # service process.
        threading.Thread(target=self._pipe.stop, name="pipe-stop", daemon=True).start()
        self._terminate_agent()
        self.log.info("Service stopped")

    # --------------------------------------------------------------- config
    def _load_config(self, force: bool = False) -> Config:
        path = config_path()
        try:
            mtime = path.stat().st_mtime if path.exists() else 0.0
        except OSError:
            mtime = 0.0
        if not force and self._cfg is not None and mtime == self._cfg_mtime:
            return self._cfg
        try:
            self._cfg = Config(path)
            self._cfg_mtime = mtime
            self.log.info("Service config loaded from %s", path)
        except Exception:
            self.log.exception("Could not load service config")
            self._cfg = Config(path)
        return self._cfg

    def _cfg_value(self, section: str, key: str, default: str = "") -> str:
        return self._load_config().get(section, key, default)

    def _cfg_int(self, section: str, key: str, default: int = 0) -> int:
        return self._load_config().get_int(section, key, default)

    def _cfg_bool(self, section: str, key: str, default: bool = False) -> bool:
        return self._load_config().get_bool(section, key, default)

    def _connection(self) -> dict:
        cfg = self._load_config()
        password = load_password_machine()
        if not password and not self._no_password_logged:
            self.log.warning(
                "No machine-scope KVM password yet; open the GUI once to sync it"
            )
            self._no_password_logged = True
        return {
            "ip": cfg.get("connection", "ip").strip(),
            "user": cfg.get("connection", "user").strip(),
            "password": password,
            "pin": cfg.get("connection", "cert_sha256").strip().upper(),
            "poll_seconds": max(1, cfg.get_int("connection", "poll_seconds", 5)),
        }

    def _action_settings(self) -> dict:
        cfg = self._load_config()
        return {
            "snapshot_path": str(snapshot_path()),
            "min_primary_hz": cfg.get_int("actions", "min_primary_hz", 100),
            "hdr_restore": cfg.get_bool("actions", "hdr_restore", True),
            "on_connect": cfg.get("actions", "on_connect", "kvm-only"),
            "on_disconnect": cfg.get("actions", "on_disconnect", "restore"),
            "screen_number": cfg.get_int("actions", "screen_number", 1),
            "kvm_bind_mode": cfg.get("actions", "kvm_bind_mode", "").strip(),
            "kvm_slot": cfg.get_int("actions", "kvm_slot", 0),
            "kvm_slot_key": cfg.get("actions", "kvm_slot_key", "").strip(),
            "kvm_target": cfg.get("actions", "kvm_target", "").strip(),
        }

    def _snapshot_summary(self) -> dict:
        path = snapshot_path()
        snapshot = ccd.load_snapshot(path)
        if snapshot is None:
            return {"exists": False}
        displays = snapshot.get("displays") or []
        first = displays[0] if displays else {}
        errors = ccd.snapshot_errors(
            snapshot, "", self._cfg_int("actions", "min_primary_hz", 100)
        )
        return {
            "exists": True,
            "ok": not errors,
            "errors": errors,
            "width": first.get("width", 0),
            "height": first.get("height", 0),
            "hz": first.get("hz", 0),
            "hdr_supported": first.get("hdr_supported", False),
            "hdr_enabled": first.get("hdr_enabled", False),
            "name": first.get("name", ""),
            "topology": snapshot.get("topology", ""),
            "captured_at": snapshot.get("captured_at", ""),
        }

    # ---------------------------------------------------------------- state
    def _state_payload(self) -> dict:
        with self._agent_lock:
            info = AgentInfo(**self._agent_info.__dict__)
        return {
            "state": self._state,
            "reason": self._reason,
            "last_check": self._last_check,
            "clients": self._last_clients,
            "paused": self._paused,
            "agent_connected": info.connected,
            "agent_pid": info.pid,
            "agent_session": info.session,
            "agent_error": info.last_error,
            "last_action": self._last_action,
            "snapshot": self._snapshot_summary(),
            "service_pid": __import__("os").getpid(),
        }

    def _broadcast_state(self) -> None:
        payload = {"event": "state", "state": self._state_payload()}
        with self._gui_lock:
            channels = list(self._gui_channels)
        for channel in channels:
            try:
                channel.enqueue(payload)
            except Exception:
                pass

    # --------------------------------------------------------------- agent
    def _agent_connected(self) -> bool:
        with self._agent_lock:
            return self._agent is not None and self._agent_info.connected

    def _agent_command(self, cmd: str, settings: dict | None = None, timeout: float = 75.0) -> dict:
        with self._agent_lock:
            agent = self._agent
        if agent is None:
            return {"ok": False, "code": "agent_unavailable"}
        return agent.command(cmd, settings, timeout)

    def _agent_supervisor(self) -> None:
        while not self._stop.wait(2.0):
            if self._stopping:
                return
            if self._agent_connected():
                continue
            with self._agent_spawn_lock:
                if self._stop.is_set():
                    return
                info = self._agent_info
                if info.pid and self._process_alive(info.pid):
                    # Pipe may still be connecting; don't spawn duplicates.
                    if time.time() - info.spawned_at < 30:
                        continue
                try:
                    self._spawn_agent()
                except Exception as exc:
                    with self._agent_lock:
                        self._agent_info.last_error = str(exc)
                    self.log.error("Could not spawn session-1 agent: %s", exc)
                    self._broadcast_state()

    def _process_alive(self, pid: int) -> bool:
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except Exception:
            return False
        try:
            return win32process_GetExitCodeProcess(handle) == STILL_ACTIVE
        finally:
            win32api.CloseHandle(handle)

    def _spawn_agent(self) -> None:
        if self._stopping:
            return
        svc = load_service_config()
        pythonw = svc.get("pythonw") or ""
        agent = svc.get("agent") or ""
        root = svc.get("root") or str(Path(__file__).resolve().parent.parent)
        data_dir = svc.get("data_dir") or str(self.data_dir)
        if not pythonw or not Path(pythonw).exists():
            raise LaunchError(f"service.json pythonw missing: {pythonw!r}")
        if not agent or not Path(agent).exists():
            raise LaunchError(f"service.json agent missing: {agent!r}")
        command = f'"{pythonw}" "{agent}" --data-dir "{data_dir}"'
        pid = launch_in_session(command, desktop="winsta0\\default", cwd=root)
        with self._agent_lock:
            self._agent_info = AgentInfo(
                pid=pid,
                session=active_console_session(),
                connected=False,
                spawned_at=time.time(),
            )
        self.log.info("Agent launch requested: pid=%s", pid)
        self._broadcast_state()

    def _terminate_agent(self) -> None:
        with self._agent_lock:
            pid = self._agent_info.pid
            self._agent = None
            self._agent_info.connected = False
        if not pid:
            return
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
            try:
                win32api.TerminateProcess(handle, 1)
            finally:
                win32api.CloseHandle(handle)
            self.log.info("Terminated stale agent pid=%s", pid)
        except Exception:
            pass

    def _on_agent_ready(self, message: dict) -> None:
        with self._agent_lock:
            self._agent_info.connected = True
            self._agent_info.pid = int(message.get("pid") or self._agent_info.pid or 0)
            self._agent_info.last_error = ""
        self.log.info("Agent connected (pid=%s)", self._agent_info.pid)
        self._broadcast_state()
        threading.Thread(target=self._reconcile, name="agent-reconcile", daemon=True).start()

    def _reconcile(self) -> None:
        """Bring the display in line with the current detection state."""
        self.log.info(
            "Reconcile requested state=%s agent=%s paused=%s",
            self._state,
            self._agent_connected(),
            self._paused,
        )
        if not self._agent_connected() or self._paused or self._stopping:
            return
        try:
            if self._state == "watching":
                self._request_action("connect")
            elif self._state == "idle":
                self._agent_command("restore", self._action_settings(), timeout=20)
        except Exception:
            self.log.exception("Reconcile failed")

    # ----------------------------------------------------------- GUI/pipe
    def _on_pipe_client(self, channel) -> None:
        hello = channel.recv()
        if not hello:
            return
        role = str(hello.get("role") or "")
        if role == "agent":
            channel.send({"ok": True, "role": "agent"})
            agent = AgentChannel(channel)
            with self._agent_lock:
                self._agent = agent
                self._agent_info.connected = True
                self._agent_info.pid = int(hello.get("pid") or 0)
                self._agent_info.session = int(hello.get("session") or 0)
            # The agent sends {"event":"ready"} next; reconcile only from that
            # event, so a ready handshake does not queue two restores.
            try:
                while not self._stop.is_set():
                    agent.flush()
                    message = channel.recv_available()
                    if message is None:
                        if channel.closed:
                            break
                        time.sleep(0.01)
                        continue
                    if message.get("event"):
                        if message.get("event") == "ready":
                            self._on_agent_ready(message)
                        continue
                    agent.on_message(message)
            except Exception:
                log.exception("Agent pipe handler failed")
            finally:
                with self._agent_lock:
                    if self._agent is agent:
                        self._agent = None
                        self._agent_info.connected = False
                self.log.warning("Agent pipe disconnected")
                self._broadcast_state()
            return

        if role == "gui":
            gui = GuiChannel(channel)
            gui.enqueue({"ok": True, "role": "gui", "state": self._state_payload()})
            gui.flush()
            with self._gui_lock:
                self._gui_channels.append(gui)
            try:
                while not self._stop.is_set():
                    gui.flush()
                    request = channel.recv_available()
                    if request is None:
                        if channel.closed:
                            break
                        time.sleep(0.01)
                        continue
                    self._handle_gui(gui, request)
            except Exception:
                log.exception("GUI pipe handler failed")
            finally:
                with self._gui_lock:
                    try:
                        self._gui_channels.remove(gui)
                    except ValueError:
                        pass
            return

        channel.send({"ok": False, "error": "unknown role"})

    def _handle_gui(self, gui: GuiChannel, request: dict) -> None:
        request_id = request.get("id")
        cmd = str(request.get("cmd") or "")
        self.log.info("GUI command: %s id=%s", cmd, request_id)
        try:
            if cmd == "get_state":
                gui.enqueue({"id": request_id, "ok": True, "state": self._state_payload()})
            elif cmd == "reload_config":
                self._load_config(force=True)
                gui.enqueue({"id": request_id, "ok": True})
                self._broadcast_state()
            elif cmd == "sync_password":
                save_password_machine(str(request.get("password") or ""))
                self._load_config(force=True)
                gui.enqueue({"id": request_id, "ok": True})
                self._broadcast_state()
            elif cmd in ("start", "stop"):
                self._paused = cmd == "stop"
                gui.enqueue({"id": request_id, "ok": True, "paused": self._paused})
                self._broadcast_state()
            elif cmd == "query_displays":
                self.log.info("GUI query: waiting for agent")
                result = self._agent_command("query", self._action_settings(), timeout=15)
                self.log.info("GUI query: agent returned %s", result.get("code", result.get("ok")))
                gui.enqueue({**result, "id": request_id})
            elif cmd == "capture":
                result = self._agent_command("capture", self._action_settings(), timeout=60)
                gui.enqueue({**result, "id": request_id})
                self._broadcast_state()
            elif cmd in ("apply_snapshot", "restore"):
                result = self._agent_command("restore", self._action_settings(), timeout=40)
                gui.enqueue({**result, "id": request_id})
                self._broadcast_state()
            elif cmd in ("connect", "disconnect"):
                # Internal/CLI verification hook; the GUI uses the automatic
                # state machine and the snapshot button, not this command.
                result = self._agent_command(cmd, self._action_settings(), timeout=75)
                gui.enqueue({**result, "id": request_id})
                self._broadcast_state()
            elif cmd == "test_connection":
                gui.enqueue({"id": request_id, **self._test_connection()})
            elif cmd == "ping":
                gui.enqueue({"id": request_id, "ok": True, "service_pid": __import__("os").getpid()})
            else:
                gui.enqueue({"id": request_id, "ok": False, "code": "unknown_command"})
        except Exception as exc:
            self.log.exception("GUI command %s failed", cmd)
            try:
                gui.enqueue({"id": request_id, "ok": False, "code": "exception", "error": str(exc)})
            except Exception:
                pass

    def _test_connection(self) -> dict:
        settings = self._connection()
        try:
            client = KvmClient(
                settings["ip"],
                settings["user"],
                settings["password"],
                settings["pin"],
                timeout=6.0,
            )
            if not settings["password"]:
                result = client.probe()
            else:
                result = client.test_connection()
            return {
                "ok": True,
                "result": {
                    "ok": result.ok,
                    "authenticated": result.authenticated,
                    "fingerprint": result.fingerprint,
                    "latency_ms": result.latency_ms,
                    "version": result.version,
                },
            }
        except CertMismatch as exc:
            return {
                "ok": False,
                "code": "cert_mismatch",
                "expected": exc.expected,
                "actual": exc.actual,
                "fingerprint": exc.actual,
            }
        except KvmError as exc:
            return {"ok": False, "code": exc.code, "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "code": "unknown", "message": str(exc)}

    # ------------------------------------------------------------- detection
    def _detection_loop(self) -> None:
        while not self._stop.is_set():
            if self._paused:
                time.sleep(1.0)
                continue
            self._load_config()
            settings = self._connection()
            if not settings["ip"] or not settings["pin"]:
                self._set_state("unknown", "no_config")
                time.sleep(3.0)
                continue
            if not settings["password"]:
                self._set_state("unknown", "no_password")
                time.sleep(3.0)
                continue
            key = (
                settings["ip"],
                settings["user"],
                settings["password"],
                settings["pin"],
                settings["poll_seconds"],
            )
            if key != self._client_key:
                try:
                    self._client = KvmClient(
                        settings["ip"],
                        settings["user"],
                        settings["password"],
                        settings["pin"],
                        timeout=6.0,
                    )
                    self._client_key = key
                except Exception as exc:
                    self.log.error("Could not create KVM client: %s", exc)
                    self._set_state("unknown", "network")
                    time.sleep(3.0)
                    continue
            try:
                detection = self._client.detect()
            except KvmError as exc:
                detection = type(
                    "D",
                    (),
                    {
                        "state": "unknown",
                        "reason": exc.code,
                        "detail": {"message": str(exc)},
                        "fatal": getattr(exc, "fatal", False),
                    },
                )()
            except Exception as exc:
                self.log.exception("Detection failed")
                detection = type(
                    "D",
                    (),
                    {"state": "unknown", "reason": "unknown", "detail": {"message": str(exc)}, "fatal": False},
                )()
            self._apply_detection(detection)
            self._maybe_disconnect()
            if getattr(detection, "fatal", False):
                self._client = None
                self._client_key = None
            time.sleep(max(1, int(settings["poll_seconds"])))

    def _apply_detection(self, detection) -> None:
        old = self._state
        state = str(getattr(detection, "state", "unknown"))
        reason = str(getattr(detection, "reason", ""))
        self._last_check = time.strftime("%H:%M:%S")
        self._last_clients = int(getattr(detection, "clients", 0) or 0)
        if state == "watching":
            if old != "watching":
                self._watching_since = time.time()
                self._idle_misses = 0
                self._disconnect_done = False
                self.log.info("Detection: %s -> watching (%s)", old, reason)
                self._request_action("connect")
            self._state = "watching"
            self._reason = reason
        elif state == "idle":
            if old == "watching" and not self._disconnect_done:
                self._idle_misses += 1
                if self._idle_misses >= 2:
                    self._state = "idle"
                    self._reason = reason
                    self.log.info("Detection: watching -> idle (%s)", reason)
            elif old != "watching":
                self._state = "idle"
                self._reason = reason
        else:
            self._reason = reason
            if old not in ("watching", "idle"):
                self._state = "unknown"
        self._broadcast_state()

    def _maybe_disconnect(self) -> None:
        if self._state != "idle" or self._disconnect_done:
            return
        if self._idle_misses < 2:
            return
        dwell = max(0, self._cfg_int("actions", "min_dwell_seconds", 10))
        if time.time() - self._watching_since < dwell:
            return
        self._disconnect_done = True
        self._request_action("disconnect")

    def _request_action(self, kind: str) -> None:
        if self._stop.is_set() or self._stopping or self._paused:
            return
        if not self._cfg_bool("actions", "auto_apply", True):
            return
        if kind == "connect" and self._cfg_value("actions", "on_connect", "kvm-only") == "hold":
            return
        if kind == "disconnect" and self._cfg_value("actions", "on_disconnect", "restore") == "hold":
            return
        if not self._agent_connected():
            self.log.info("Skipping %s: agent not connected", kind)
            return
        if self._action_busy:
            return
        if time.time() < self._fail_until:
            return
        if time.time() - self._last_action_ts < 5:
            return
        self._action_busy = True
        self._action_thread = threading.Thread(
            target=self._run_action, args=(kind,), name="display-action", daemon=True
        )
        self._action_thread.start()

    def _run_action(self, kind: str) -> None:
        try:
            cmd = "connect" if kind == "connect" else "disconnect"
            self.log.info("Requesting agent %s", cmd)
            result = self._agent_command(cmd, self._action_settings(), timeout=75)
            ok = bool(result.get("ok"))
            self._last_action = {
                "kind": kind,
                "ok": ok,
                "code": result.get("code", ""),
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_ms": (result.get("result") or {}).get("duration_ms", 0),
                "detail": result.get("detail") or {},
            }
            self._last_action_ts = time.time()
            if ok:
                self._fail_until = 0.0
                self.log.info("Agent %s succeeded: %s", cmd, result.get("code"))
            else:
                self._fail_until = time.time() + 60
                self.log.warning("Agent %s failed: %s %s", cmd, result.get("code"), result.get("detail"))
            if kind == "disconnect":
                self._disconnect_done = True
            self._broadcast_state()
        finally:
            self._action_busy = False

    def _set_state(self, state: str, reason: str) -> None:
        if state == self._state and reason == self._reason:
            return
        self._state = state
        self._reason = reason
        self._broadcast_state()

    # ------------------------------------------------------------ controls
    def on_session_change(self, event_type: int) -> None:
        self.log.info("Session change event=%s", event_type)
        if event_type in (
            WTS_CONSOLE_CONNECT,
            WTS_REMOTE_CONNECT,
            WTS_SESSION_LOGON,
            WTS_SESSION_UNLOCK,
        ):
            threading.Thread(target=self._reconcile, name="session-reconcile", daemon=True).start()
        elif event_type in (WTS_SESSION_LOGOFF, WTS_SESSION_TERMINATE):
            self._broadcast_state()

    def on_power_event(self, event_type: int) -> None:
        self.log.info("Power event=%s", event_type)
        if event_type in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
            time.sleep(2.0)
            threading.Thread(target=self._reconcile, name="resume-reconcile", daemon=True).start()

    def on_preshutdown(self) -> None:
        self.log.info("Pre-shutdown: restoring local layout")
        try:
            if self._agent_connected():
                self._agent_command("restore", self._action_settings(), timeout=8.0)
        except Exception:
            pass

    # ------------------------------------------------------------- helpers
    def _load_agent_binding(self):
        with self._agent_lock:
            return self._agent


# A tiny indirection keeps the pywin32 import optional for non-service tests.
def win32process_GetExitCodeProcess(handle) -> int:
    import win32process

    return int(win32process.GetExitCodeProcess(handle))
