"""Launch a process as SYSTEM into the interactive console session.

The approach duplicates the Session-1 winlogon token, sets TokenSessionId, and
TokenSessionId, then CreateProcessAsUser on winsta0\\default.  Only a
LocalSystem service holds the required privileges.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

import win32api
import win32con
import win32security
import win32ts

log = logging.getLogger("glkvm.session")

PRIVILEGES = (
    "SeDebugPrivilege",
    "SeAssignPrimaryTokenPrivilege",
    "SeIncreaseQuotaPrivilege",
    "SeTcbPrivilege",
)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
userenv = ctypes.WinDLL("userenv", use_last_error=True)

TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
MAXIMUM_ALLOWED = 0x02000000
SECURITY_IMPERSONATION = 2
TOKEN_PRIMARY = 1
TOKEN_SESSION_ID = 12
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.DuplicateTokenEx.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.POINTER(wintypes.HANDLE),
]
advapi32.DuplicateTokenEx.restype = wintypes.BOOL
advapi32.SetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
]
advapi32.SetTokenInformation.restype = wintypes.BOOL
advapi32.CreateProcessAsUserW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFO),
    ctypes.POINTER(PROCESS_INFORMATION),
]
advapi32.CreateProcessAsUserW.restype = wintypes.BOOL
userenv.CreateEnvironmentBlock.argtypes = [
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.BOOL,
]
userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL


class LaunchError(Exception):
    pass


def _last_error(where: str) -> LaunchError:
    return LaunchError(f"{where} failed: win32={ctypes.get_last_error()}")


def enable_privileges() -> list[str]:
    enabled: list[str] = []
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY,
    )
    try:
        for name in PRIVILEGES:
            try:
                luid = win32security.LookupPrivilegeValue(None, name)
                win32security.AdjustTokenPrivileges(
                    token, False, [(luid, win32security.SE_PRIVILEGE_ENABLED)]
                )
                enabled.append(name)
            except Exception as exc:
                log.warning("Could not enable %s: %s", name, exc)
    finally:
        win32api.CloseHandle(token)
    return enabled


def active_console_session() -> int:
    session = win32ts.WTSGetActiveConsoleSessionId()
    if session in (-1, 0xFFFFFFFF):
        return 1
    return int(session)


def find_winlogon(session_id: int) -> int:
    try:
        processes = win32ts.WTSEnumerateProcesses(None)
    except Exception as exc:
        raise LaunchError(f"WTSEnumerateProcesses failed: {exc}") from exc
    for session, pid, name, _sid in processes:
        if int(session) == int(session_id) and str(name).lower() == "winlogon.exe":
            return int(pid)
    for session, pid, name, _sid in processes:
        if str(name).lower() == "winlogon.exe":
            return int(pid)
    raise LaunchError(f"winlogon.exe not found in session {session_id}")


def launch_in_session(
    command_line: str,
    *,
    session_id: int | None = None,
    desktop: str = "winsta0\\default",
    cwd: str = r"C:\Windows\System32",
) -> int:
    if session_id is None:
        session_id = active_console_session()
    enable_privileges()
    winlogon_pid = find_winlogon(session_id)
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, winlogon_pid)
    if not process:
        raise _last_error("OpenProcess(winlogon)")
    token = wintypes.HANDLE()
    dup = wintypes.HANDLE()
    env = ctypes.c_void_p()
    env_ok = False
    try:
        if not advapi32.OpenProcessToken(
            process, TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY | TOKEN_QUERY, ctypes.byref(token)
        ):
            raise _last_error("OpenProcessToken(winlogon)")
        if not advapi32.DuplicateTokenEx(
            token,
            MAXIMUM_ALLOWED,
            None,
            SECURITY_IMPERSONATION,
            TOKEN_PRIMARY,
            ctypes.byref(dup),
        ):
            raise _last_error("DuplicateTokenEx")
        session = wintypes.DWORD(int(session_id))
        if not advapi32.SetTokenInformation(
            dup, TOKEN_SESSION_ID, ctypes.byref(session), ctypes.sizeof(session)
        ):
            raise _last_error("SetTokenInformation(TokenSessionId)")

        env_ok = bool(userenv.CreateEnvironmentBlock(ctypes.byref(env), dup, False))
        startup = STARTUPINFO()
        startup.cb = ctypes.sizeof(STARTUPINFO)
        startup.lpDesktop = desktop
        info = PROCESS_INFORMATION()
        cmd = ctypes.create_unicode_buffer(command_line)
        ok = advapi32.CreateProcessAsUserW(
            dup,
            None,
            cmd,
            None,
            None,
            False,
            CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
            env if env_ok else None,
            cwd,
            ctypes.byref(startup),
            ctypes.byref(info),
        )
        if not ok:
            raise _last_error("CreateProcessAsUserW")
        kernel32.CloseHandle(info.hThread)
        kernel32.CloseHandle(info.hProcess)
        log.info("Launched in session %s: pid=%s cmd=%s", session_id, info.dwProcessId, command_line)
        return int(info.dwProcessId)
    finally:
        if env_ok:
            userenv.DestroyEnvironmentBlock(env)
        if dup:
            kernel32.CloseHandle(dup)
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)
