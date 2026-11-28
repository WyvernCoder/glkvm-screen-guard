"""Minimal GLKVM / PiKVM HTTP client for RM10.

Scope: authenticate, verify the pinned TLS fingerprint, read
``/api/streamer`` and ``/api/log``, and turn them into a three-state
detection result (watching / idle / unknown).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import re
import ssl
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger("glkvm.kvm")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class KvmError(Exception):
    code = "error"
    fatal = False


class NetworkError(KvmError):
    code = "network"


class AuthError(KvmError):
    code = "auth"
    fatal = True


class TwoStepError(KvmError):
    code = "two_step"
    fatal = True


class RateLimitError(KvmError):
    code = "rate"
    fatal = True


class HttpError(KvmError):
    code = "http"


class CertMismatch(KvmError):
    code = "cert"
    fatal = True

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected.upper()
        self.actual = actual.upper()
        super().__init__("TLS certificate fingerprint mismatch")


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class Detection:
    state: str                      # watching | idle | unknown
    reason: str                     # streamer | log_new | log_wake | log_only | no_clients | streamer_null | error
    clients: int = 0
    streamer_present: bool = False
    latency_ms: int = 0
    fingerprint: str = ""
    fatal: bool = False
    detail: dict = field(default_factory=dict)


@dataclass
class TestResult:
    ok: bool
    authenticated: bool
    fingerprint: str
    latency_ms: int
    streamer_present: bool = False
    clients: int = 0
    version: str = ""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
_LINE_TIME = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
_SESSION_RE = re.compile(
    r"Registered new client session:\s*WsSession\(id=(\d+),\s*\{([^}]*)\}"
)
_STREAM_TRUE = re.compile(r"['\"]stream['\"]\s*:\s*True")
_HID_ONLY = re.compile(r"['\"]hid_only['\"]\s*:\s*True")


class KvmClient:
    def __init__(
        self,
        ip: str,
        user: str,
        password: str,
        pin: str = "",
        timeout: float = 6.0,
        wake_seconds: int = 60,
    ) -> None:
        host = (ip or "").strip()
        if not host:
            raise ValueError("empty IP")
        if "://" in host:
            host = host.split("://", 1)[1]
        host = host.rstrip("/")
        self.port = 443
        if host.count(":") == 1:
            name, maybe_port = host.rsplit(":", 1)
            if maybe_port.isdigit():
                host, self.port = name, int(maybe_port)
        self.host = host
        self.user = (user or "").strip()
        self.password = password or ""
        self.pin = (pin or "").strip().upper()
        self.timeout = timeout
        self.wake_seconds = max(10, int(wake_seconds))

        self.last_fingerprint = ""
        self._token: str | None = None
        self._token_ts = 0.0
        self._known_session: str | None = None
        self._wake_until = 0.0

    # ------------------------------------------------------------------ TLS
    def _connect(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            conn = http.client.HTTPSConnection(
                self.host, self.port, context=ctx, timeout=self.timeout
            )
            conn.connect()
        except (OSError, ssl.SSLError) as exc:
            raise NetworkError(str(exc)) from exc

        try:
            sock = conn.sock
            der = sock.getpeercert(binary_form=True) if sock is not None else None
            if not der:
                raise NetworkError("no peer certificate")
            fingerprint = hashlib.sha256(der).hexdigest().upper()
            self.last_fingerprint = fingerprint
            if self.pin and fingerprint != self.pin:
                raise CertMismatch(self.pin, fingerprint)
            return conn, fingerprint
        except Exception:
            conn.close()
            raise

    # -------------------------------------------------------------- HTTP
    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        auth: bool = True,
        retry: bool = True,
        timeout: float | None = None,
    ):
        old_timeout = self.timeout
        if timeout is not None:
            self.timeout = timeout
        try:
            for _ in range(2):
                conn, _fingerprint = self._connect()
                try:
                    headers = {
                        "Accept": "application/json, text/plain",
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                        "User-Agent": "GLKVM-Screen-Guard/1.0",
                    }
                    target = path
                    if auth and self._token:
                        sep = "&" if "?" in target else "?"
                        target = f"{target}{sep}auth_token={urllib.parse.quote(self._token)}"
                    if body is not None:
                        headers["Content-Type"] = "application/x-www-form-urlencoded"
                    conn.request(method, target, body=body, headers=headers)
                    resp = conn.getresponse()
                    try:
                        data = resp.read()
                    except http.client.IncompleteRead as exc:
                        # The RM10 sometimes closes a large /api/log response
                        # early; keep what arrived instead of losing the poll.
                        data = exc.partial
                    status = resp.status
                    cookie = resp.getheader("Set-Cookie") or ""
                except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                    raise NetworkError(str(exc)) from exc
                finally:
                    conn.close()

                if status == 403 and auth and retry:
                    self._token = None
                    self.login()
                    retry = False
                    continue
                return status, data, cookie
            raise HttpError("request retry exhausted")
        finally:
            self.timeout = old_timeout

    # -------------------------------------------------------------- login
    @staticmethod
    def _error_message(text: str) -> str:
        try:
            obj = json.loads(text)
            result = obj.get("result") or {}
            return str(result.get("error_msg") or result.get("error") or "")
        except Exception:
            return ""

    def login(self) -> None:
        if not self.user or not self.password:
            raise AuthError("missing username or password")
        body = urllib.parse.urlencode(
            {"user": self.user, "passwd": self.password}
        ).encode("utf-8")
        status, data, cookie = self._request(
            "POST", "/api/auth/login", body=body, auth=False, retry=False
        )
        text = data.decode("utf-8", "replace")
        low = text.lower()

        if "two_step" in low or "two-step" in low:
            raise TwoStepError("two-step authentication is not supported")
        if status == 429:
            raise RateLimitError("too many login attempts")
        if status in (401, 403):
            raise AuthError(self._error_message(text) or "authentication failed")
        if status != 200:
            raise HttpError(f"HTTP {status}")

        token = None
        try:
            obj = json.loads(text)
        except Exception:
            obj = {}
        if obj:
            if obj.get("ok") is False:
                raise AuthError(self._error_message(text) or "authentication failed")
            result = obj.get("result") or {}
            token = result.get("token")
        if not token:
            match = re.search(r"auth_token=([^;]+)", cookie)
            if match:
                token = match.group(1)
        if not token:
            raise AuthError("no auth token in response")
        self._token = token
        self._token_ts = time.time()
        log.info("Authenticated with %s", self.host)

    # -------------------------------------------------------------- endpoints
    def probe(self) -> TestResult:
        """TLS + unauthenticated reachability check (no login attempt)."""
        started = time.perf_counter()
        status, _data, _cookie = self._request(
            "GET", "/api/auth/check", auth=False, retry=False
        )
        latency = int((time.perf_counter() - started) * 1000)
        return TestResult(
            ok=True,
            authenticated=False,
            fingerprint=self.last_fingerprint,
            latency_ms=latency,
            streamer_present=False,
        )

    def get_streamer(self) -> dict | None:
        if not self._token:
            self.login()
        status, data, _cookie = self._request("GET", "/api/streamer")
        if status != 200:
            raise HttpError(f"/api/streamer HTTP {status}")
        try:
            obj = json.loads(data.decode("utf-8", "replace"))
        except Exception as exc:
            raise HttpError("invalid /api/streamer JSON") from exc
        result = obj.get("result") or {}
        streamer = result.get("streamer")
        return streamer if isinstance(streamer, dict) else None

    def get_log(self) -> str:
        if not self._token:
            self.login()
        status, data, _cookie = self._request("GET", "/api/log", timeout=12)
        if status != 200:
            raise HttpError(f"/api/log HTTP {status}")
        text = data.decode("utf-8", "replace")
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "result" in obj:
                result = obj["result"]
                text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        except Exception:
            pass
        return text[-200_000:]

    def get_info(self) -> dict:
        try:
            status, data, _cookie = self._request("GET", "/api/info")
            if status == 200:
                obj = json.loads(data.decode("utf-8", "replace"))
                return obj.get("result") or {}
        except Exception:
            pass
        return {}

    # -------------------------------------------------------------- detection
    @staticmethod
    def parse_latest_stream_session(text: str) -> tuple[str | None, datetime | None]:
        latest_id = None
        latest_time = None
        for line in text.splitlines():
            match = _SESSION_RE.search(line)
            if not match:
                continue
            attrs = match.group(2)
            if not _STREAM_TRUE.search(attrs) or _HID_ONLY.search(attrs):
                continue
            latest_id = match.group(1)
            time_match = _LINE_TIME.search(line)
            if time_match:
                try:
                    latest_time = datetime.strptime(time_match.group(1), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    latest_time = None
        return latest_id, latest_time

    def _streamer_clients(self, streamer: dict) -> tuple[bool, int]:
        sinks = streamer.get("sinks") or {}
        h264 = bool((sinks.get("h264") or {}).get("has_clients"))
        jpeg = bool((sinks.get("jpeg") or {}).get("has_clients"))
        stream = streamer.get("stream") or {}
        try:
            mjpeg = int(stream.get("clients") or 0)
        except Exception:
            mjpeg = 0
        watching = h264 or jpeg or mjpeg > 0
        return watching, max(mjpeg, 1 if watching else 0)

    def detect(self) -> Detection:
        started = time.perf_counter()
        if not self._token:
            self.login()

        streamer = self.get_streamer()
        streamer_present = streamer is not None
        watching = False
        clients = 0
        if streamer_present:
            watching, clients = self._streamer_clients(streamer)

        fingerprint = self.last_fingerprint

        if watching:
            self._wake_until = 0.0
            return Detection(
                state="watching",
                reason="streamer",
                clients=clients,
                streamer_present=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
                fingerprint=fingerprint,
            )

        # Shut down the streamer and no signal means "has_clients" can never
        # become true.  A new stream=True WsSession in /api/log is the wake-up.
        log_text = ""
        log_error = None
        try:
            log_text = self.get_log()
        except KvmError as exc:
            log_error = exc

        latest_id = None
        latest_time = None
        if log_text:
            latest_id, latest_time = self.parse_latest_stream_session(log_text)

        now = datetime.now()
        if latest_id:
            if self._known_session is None:
                # First contact: remember the newest session.  Only a recent
                # one counts as a viewer; older forgotten tabs must not wake.
                self._known_session = latest_id
                recent = latest_time is not None and (now - latest_time).total_seconds() <= 120
                if recent:
                    self._wake_until = time.time() + self.wake_seconds
                    return Detection(
                        state="watching",
                        reason="log_new",
                        clients=max(clients, 1),
                        streamer_present=streamer_present,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        fingerprint=fingerprint,
                        detail={"session": latest_id},
                    )
            elif latest_id != self._known_session:
                # A new id while we are running is a new connection.  Do not
                # trust its timestamp: the RM10 clock/certificate is often
                # wrong (1970), so "fresh" would never be true.
                self._known_session = latest_id
                self._wake_until = time.time() + self.wake_seconds
                return Detection(
                    state="watching",
                    reason="log_new",
                    clients=max(clients, 1),
                    streamer_present=streamer_present,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    fingerprint=fingerprint,
                    detail={"session": latest_id},
                )

        if self._wake_until and time.time() < self._wake_until:
            return Detection(
                state="watching",
                reason="log_wake",
                clients=max(clients, 1),
                streamer_present=streamer_present,
                latency_ms=int((time.perf_counter() - started) * 1000),
                fingerprint=fingerprint,
            )

        if streamer_present:
            return Detection(
                state="idle",
                reason="no_clients",
                clients=0,
                streamer_present=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
                fingerprint=fingerprint,
            )

        if log_text:
            return Detection(
                state="idle",
                reason="log_only",
                clients=0,
                streamer_present=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
                fingerprint=fingerprint,
            )

        detail = {"log_error": log_error.code if isinstance(log_error, KvmError) else ""}
        return Detection(
            state="unknown",
            reason="streamer_null",
            clients=0,
            streamer_present=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            fingerprint=fingerprint,
            detail=detail,
        )

    # -------------------------------------------------------------- test
    def test_connection(self) -> TestResult:
        started = time.perf_counter()
        self.login()
        streamer = self.get_streamer()
        clients = 0
        if streamer is not None:
            _watching, clients = self._streamer_clients(streamer)
        info = self.get_info()
        version = ""
        try:
            version = (
                (info.get("meta") or {}).get("server", {}).get("version")
                or (info.get("meta") or {}).get("version")
                or ""
            )
        except Exception:
            version = ""
        return TestResult(
            ok=True,
            authenticated=True,
            fingerprint=self.last_fingerprint,
            latency_ms=int((time.perf_counter() - started) * 1000),
            streamer_present=streamer is not None,
            clients=clients,
            version=str(version),
        )
