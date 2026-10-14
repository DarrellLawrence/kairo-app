"""Small secrets the owner types in (school portal password): encrypted on the PC.

On Windows (kairo-home) with DPAPI, bound to the Windows account Kairo runs as
(kairo-svc): another account, a copied file or a backup can't read it. On the
Linux dev laptop there is no DPAPI: the file is only owner-readable (dev data).
Never in the repo, the database, logs, chat or an API answer.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

_DPAPI = b"KAIRO-DPAPI1\n"
_DEV = b"KAIRO-DEV1\n"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):   # 0x1: never show a prompt
        raise OSError(f"DPAPI failed ({ctypes.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def save(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value).encode()
    blob = _DPAPI + _dpapi(raw, True) if sys.platform == "win32" else _DEV + base64.b64encode(raw)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(blob)
    if sys.platform != "win32":
        os.chmod(tmp, 0o600)
    tmp.replace(path)


def load(path: Path) -> dict | None:
    path = Path(path)
    if not path.is_file():
        return None
    blob = path.read_bytes()
    if blob.startswith(_DPAPI):
        return json.loads(_dpapi(blob[len(_DPAPI):], False))
    if blob.startswith(_DEV):
        return json.loads(base64.b64decode(blob[len(_DEV):]))
    raise ValueError("unknown secret format")


def encrypted(path: Path) -> bool:
    return Path(path).is_file() and Path(path).read_bytes().startswith(_DPAPI)


def remove(path: Path) -> bool:
    path = Path(path)
    if path.is_file():
        path.unlink()
        return True
    return False
