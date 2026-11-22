"""Windows DPAPI helpers for the KVM password.

The password is never written in clear text to ``config.ini``.  DPAPI ties
the ciphertext to the current Windows user by default; the service uses
``machine=True`` so LocalSystem can decrypt it before a user logs in.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes

CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB),
    wintypes.LPCWSTR,
    ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(DATA_BLOB),
]
_crypt32.CryptProtectData.restype = wintypes.BOOL

_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB),
    ctypes.POINTER(wintypes.LPWSTR),
    ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(DATA_BLOB),
]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL

_kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
_kernel32.LocalFree.restype = wintypes.HLOCAL


def _make_blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buf


def protect(text: str, machine: bool = False) -> str:
    if not text:
        return ""
    data = text.encode("utf-8")
    blob_in, _buf = _make_blob(data)
    blob_out = DATA_BLOB()
    flags = CRYPTPROTECT_UI_FORBIDDEN
    if machine:
        flags |= CRYPTPROTECT_LOCAL_MACHINE
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        flags,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        cipher = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)
    return base64.b64encode(cipher).decode("ascii")


def unprotect(encoded: str) -> str:
    if not encoded:
        return ""
    try:
        cipher = base64.b64decode(encoded, validate=True)
    except Exception:
        return ""
    blob_in, _buf = _make_blob(cipher)
    blob_out = DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        return ""
    try:
        plain = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)
    return plain.decode("utf-8", "replace")
