"""NVIDIA's "CUDA - Sysmem Fallback Policy": read it, and write it only when the owner asks (voice recipe 9).

When the graphics card is full, the Windows driver can quietly put CUDA memory into shared (main) memory: the
voice does not fail, it gets 2 to 28 times slower. With the policy at 1 ("prefer no sysmem fallback") the same
situation is a loud out-of-memory error instead, which Kairo can see and say. The setting lives in the driver's
profile database (setting id 0x10ECECC9, on the global profile). There is no nvidia-smi option, registry key or
CUDA environment variable for it, only NVAPI's DRS calls, so this module calls those with ctypes (no package).

  python -I nvsettings.py get [--json]    read it (no administrator needed)
  python -I nvsettings.py set 1           write it (an elevated process); 0 = driver default, 1 = prefer no sysmem
                                          fallback, 2 = prefer sysmem fallback. Writes, saves and reads back.

Output of get/set: one line, `policy=<0|1|2|unset|unavailable> <words>`, so the installer can parse it. Exit code:
0 it read (and for set: the read-back is what was asked), 1 the read-back differs, 2 wrong use, 3 it could not
(not Windows, no NVIDIA driver, not elevated, ...). The read-back is the proof: NVAPI's own status is not enough.

This file imports nothing from Kairo and nothing from the network, so the installer can run it as a script with
any Python. Windows only at run time; the struct layout and the call sequence are checked on any OS by
tests/test_nvsettings.py. Every number below comes from NVIDIA's open NVAPI headers (nvapi_interface.h, nvapi.h)
and from NVIDIA Profile Inspector's CustomSettingNames.xml (v3.0.2.3). The driver supports the setting from
536.40 and a global switch from 546.01.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import sys

SETTING_ID = 0x10ECECC9                     # "CUDA - Sysmem Fallback Policy"
VALUES = {0: "driver default", 1: "prefer NO sysmem fallback", 2: "prefer sysmem fallback"}
UNSET = "driver default (not set)"

# nvapi_QueryInterface ids (nvapi_interface.h)
IDS = {
    "Initialize": 0x0150E828, "Unload": 0xD22BDD7E, "GetErrorMessage": 0x6C2D048C,
    "DRS_CreateSession": 0x0694D52E, "DRS_DestroySession": 0xDAD9CFF8, "DRS_LoadSettings": 0x375DBD6B,
    "DRS_SaveSettings": 0xFCBC7E14, "DRS_GetCurrentGlobalProfile": 0x617BFF9F, "DRS_GetBaseProfile": 0xDA8466A0,
    "DRS_GetSetting": 0x73BF8338, "DRS_SetSetting": 0x577DD202,
}
NVAPI_OK, NVAPI_SETTING_NOT_FOUND, NVAPI_INVALID_USER_PRIVILEGE = 0, -160, -137

NVDRS_DWORD_TYPE, NVDRS_CURRENT_PROFILE_LOCATION = 0, 0
UNICODE_MAX, UNION_BYTES = 2048, 4100       # NVAPI_UNICODE_STRING_MAX; NVDRS_BINARY_SETTING = 4 + 4096


class NVDRS_SETTING(ctypes.Structure):      # nvapi.h: #pragma pack(push, 4) ... NVDRS_SETTING_V1
    _pack_ = 4
    _fields_ = [("version", ctypes.c_uint32), ("settingName", ctypes.c_uint16 * UNICODE_MAX),
                ("settingId", ctypes.c_uint32), ("settingType", ctypes.c_uint32), ("settingLocation", ctypes.c_uint32),
                ("isCurrentPredefined", ctypes.c_uint32), ("isPredefinedValid", ctypes.c_uint32),
                ("predefinedValue", ctypes.c_ubyte * UNION_BYTES), ("currentValue", ctypes.c_ubyte * UNION_BYTES)]


def setting_version() -> int:               # MAKE_NVAPI_VERSION(NVDRS_SETTING_V1, 1)
    return ctypes.sizeof(NVDRS_SETTING) | (1 << 16)


class NvApi:
    """query_interface: nvapi64.dll's nvapi_QueryInterface; tests pass a fake one."""

    def __init__(self, query_interface=None):
        if query_interface is None:
            if sys.platform != "win32":
                raise OSError("Windows only: NVIDIA's driver setting is read through nvapi64.dll")
            try:
                query_interface = ctypes.WinDLL("nvapi64.dll").nvapi_QueryInterface
            except (OSError, AttributeError) as exc:
                raise OSError(f"no NVIDIA driver found (nvapi64.dll): {exc}") from exc
            query_interface.restype = ctypes.c_void_p
            query_interface.argtypes = [ctypes.c_uint32]
        self._q = query_interface
        self._fn = {}

    def fn(self, name: str, *argtypes):
        if name not in self._fn:
            ptr = self._q(IDS[name])
            if not ptr:
                raise OSError(f"nvapi_QueryInterface has no {name} (driver too old?)")
            self._fn[name] = ctypes.CFUNCTYPE(ctypes.c_int, *argtypes)(ptr)
        return self._fn[name]

    def check(self, status: int, what: str) -> int:
        if status != NVAPI_OK:
            raise OSError(f"{what} failed: NvAPI_Status {status}" + (" (needs an elevated process)" if status == NVAPI_INVALID_USER_PRIVILEGE else ""))
        return status

    @contextlib.contextmanager
    def session(self):
        """Initialize, CreateSession, LoadSettings ... DestroySession, Unload: closed again whatever fails in between."""
        self.check(self.fn("Initialize")(), "NvAPI_Initialize")
        try:
            h = ctypes.c_void_p()
            self.check(self.fn("DRS_CreateSession", ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(h)), "DRS_CreateSession")
            try:
                self.check(self.fn("DRS_LoadSettings", ctypes.c_void_p)(h), "DRS_LoadSettings")
                yield h
            finally:
                self.fn("DRS_DestroySession", ctypes.c_void_p)(h)
        finally:
            self.fn("Unload")()

    def global_profile(self, h):
        p = ctypes.c_void_p()
        self.check(self.fn("DRS_GetCurrentGlobalProfile", ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(h, ctypes.byref(p)),
                   "DRS_GetCurrentGlobalProfile")
        return p

    def get(self, h, profile, setting_id=SETTING_ID):
        """The effective dword value on that profile, or None when the setting is not set (the driver default)."""
        s = NVDRS_SETTING()
        s.version = setting_version()
        st = self.fn("DRS_GetSetting", ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(NVDRS_SETTING))(
            h, profile, setting_id, ctypes.byref(s))
        if st == NVAPI_SETTING_NOT_FOUND:
            return None
        self.check(st, "DRS_GetSetting")
        return int.from_bytes(bytes(s.currentValue[:4]), "little")

    def set(self, h, profile, value: int, setting_id=SETTING_ID):
        s = NVDRS_SETTING()
        s.version = setting_version()
        s.settingId, s.settingType, s.settingLocation = setting_id, NVDRS_DWORD_TYPE, NVDRS_CURRENT_PROFILE_LOCATION
        s.currentValue[:4] = list(int(value).to_bytes(4, "little"))
        self.check(self.fn("DRS_SetSetting", ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(NVDRS_SETTING))(h, profile, ctypes.byref(s)),
                   "DRS_SetSetting")
        self.check(self.fn("DRS_SaveSettings", ctypes.c_void_p)(h), "DRS_SaveSettings")


def read_policy(api: NvApi | None = None) -> int | None:
    """0, 1 or 2 as the driver has it on the global profile, None when it was never set. OSError when it cannot be read."""
    api = api or NvApi()
    with api.session() as h:
        return api.get(h, api.global_profile(h))


def set_policy(value: int, api: NvApi | None = None) -> int | None:
    """Sets it on the global profile, saves, and reads it back (the read-back is the proof)."""
    api = api or NvApi()
    with api.session() as h:
        profile = api.global_profile(h)
        api.set(h, profile, value)
        return api.get(h, profile)


def label(value: int | None) -> str:
    return UNSET if value is None else VALUES.get(value, f"{value} = a value this driver has and Kairo does not know")


def status(api: NvApi | None = None) -> dict:
    """{available, value, off, label, why}: never raises. off = the policy is 1 (no quiet spill into shared memory)."""
    try:
        value = read_policy(api)
    except (OSError, ValueError, AttributeError, ctypes.ArgumentError) as exc:
        return {"available": False, "value": None, "off": False, "label": "not available", "why": str(exc)[:200]}
    return {"available": True, "value": value, "off": value == 1, "label": label(value), "why": None}


USAGE = "usage: nvsettings.py get [--json] | set <0|1|2>"


def _say(state: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(state))
    elif not state["available"]:
        print(f"policy=unavailable {state['why']}")
    else:
        print(f"policy={'unset' if state['value'] is None else state['value']} {state['label']}")


def main(argv: list[str], api: NvApi | None = None) -> int:
    if argv in (["get"], ["get", "--json"]):
        state = status(api)
        _say(state, argv[-1] == "--json")
        return 0 if state["available"] else 3
    if len(argv) == 2 and argv[0] == "set" and argv[1] in ("0", "1", "2"):
        want = int(argv[1])
        try:
            got = set_policy(want, api)
        except (OSError, ValueError, AttributeError, ctypes.ArgumentError) as exc:
            _say({"available": False, "why": str(exc)[:200]}, False)
            return 3
        _say({"available": True, "value": got, "label": label(got)}, False)
        return 0 if got == want else 1
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
