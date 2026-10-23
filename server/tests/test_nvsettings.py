"""app/nvsettings.py: NVIDIA's "CUDA - Sysmem Fallback Policy" read (and, only on the owner's say, written) through NVAPI
with plain ctypes (voice recipe 9). Checked without a GPU: the struct layout against the sizes gcc computed from nvapi.h's own
definition, the call sequence against a fake driver built from ctypes callbacks, and the CLI the installer and `kairo diag` use."""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import nvsettings as n

SCRIPT = Path(n.__file__)


def test_layout_matches_the_c_header():
    # gcc -m64 on nvapi.h's NVDRS_SETTING_V1 (pack 4): sizeof 12320, version 0x00013020, settingId 4100, settingType 4104,
    # settingLocation 4108, isCurrentPredefined 4112, isPredefinedValid 4116, predefinedValue 4120, currentValue 8220
    s = n.NVDRS_SETTING
    assert ctypes.sizeof(s) == 12320 and n.setting_version() == 0x00013020
    assert [s.settingId.offset, s.settingType.offset, s.settingLocation.offset, s.isCurrentPredefined.offset,
            s.isPredefinedValid.offset, s.predefinedValue.offset, s.currentValue.offset] == [4100, 4104, 4108, 4112, 4116, 4120, 8220]


def test_the_setting_is_the_one_in_nvidias_profile_inspector_list():
    assert n.SETTING_ID == 0x10ECECC9 == 283962569
    assert set(n.VALUES) == {0, 1, 2} and "NO sysmem fallback" in n.VALUES[1]


class FakeDriver:
    """Just enough of the DRS API: one global profile with a dict of dword settings, admin or not.

    generation says which NVAPI interface ids the driver answers, and with which argument list:
      "both"  (default, a current driver) the newer ids with NVIDIA Profile Inspector's five arguments, and the SDK ids too, which must
              then stay unused;
      "new"   only the newer ids;
      "sdk"   only the SDK header's ids with its three and four arguments (an old driver; the caller's extra arguments are harmless
              in the x64 calling conventions, which is also what Profile Inspector relies on when it falls back to these ids)."""

    def __init__(self, admin=True, initial=None, fail_on=None, generation="both"):
        self.admin, self.db, self.saved, self.calls = admin, dict(initial or {}), dict(initial or {}), []
        self.fail_on, self.generation = fail_on, generation
        self.asked, self.sdk_used, self.trailing = [], [], {}
        ptr = ctypes.POINTER
        sess = ctypes.CFUNCTYPE(ctypes.c_int, ptr(ctypes.c_void_p))
        one = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
        prof = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ptr(ctypes.c_void_p))
        sset = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ptr(n.NVDRS_SETTING))
        sget = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ptr(n.NVDRS_SETTING))
        set5 = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ptr(n.NVDRS_SETTING), ctypes.c_uint32, ctypes.c_uint32)
        get5 = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ptr(n.NVDRS_SETTING), ptr(ctypes.c_uint32))
        nothing = ctypes.CFUNCTYPE(ctypes.c_int)

        def call(name, code=0):
            self.calls.append(name)
            return -1 if self.fail_on == name else code

        def create(ph):
            ph[0] = 1
            return call("DRS_CreateSession")

        def get_profile(h, pp):
            pp[0] = 7
            return call("DRS_GetCurrentGlobalProfile")

        def set_setting(h, p, ps):
            s = ps[0]
            assert s.version == n.setting_version() and s.settingType == 0 and s.settingLocation == 0
            if not self.admin:
                return n.NVAPI_INVALID_USER_PRIVILEGE
            self.db[s.settingId] = int.from_bytes(bytes(s.currentValue[:4]), "little")
            return call("DRS_SetSetting")

        def get_setting(h, p, sid, ps):
            assert ps[0].version == n.setting_version()
            if sid not in self.db:
                return n.NVAPI_SETTING_NOT_FOUND
            ps[0].currentValue[:4] = list(self.db[sid].to_bytes(4, "little"))
            return call("DRS_GetSetting")

        def set_setting5(h, p, ps, x, y):
            self.trailing["set"] = (x, y)
            return set_setting(h, p, ps)

        def get_setting5(h, p, sid, ps, px):
            self.trailing["get"] = bool(px)
            if px:
                px[0] = 5                      # a driver may write there: it must be memory of ours, or this crashes
            return get_setting(h, p, sid, ps)

        def sdk(fn):
            def used(*a):
                self.sdk_used.append(fn.__name__)
                return fn(*a)
            return used

        def save(h):
            if not self.admin:
                return n.NVAPI_INVALID_USER_PRIVILEGE
            self.saved = dict(self.db)
            return call("DRS_SaveSettings")
        self.cbs = {
            "Initialize": nothing(lambda: call("Initialize")), "Unload": nothing(lambda: call("Unload")),
            "DRS_CreateSession": sess(create), "DRS_DestroySession": one(lambda h: call("DRS_DestroySession")),
            "DRS_LoadSettings": one(lambda h: call("DRS_LoadSettings")), "DRS_SaveSettings": one(save),
            "DRS_GetCurrentGlobalProfile": prof(get_profile)}
        by_id = {n.IDS[k]: v for k, v in self.cbs.items()}
        if generation in ("both", "new"):
            by_id[n.NEWER_IDS["DRS_SetSetting"]] = set5(set_setting5)
            by_id[n.NEWER_IDS["DRS_GetSetting"]] = get5(get_setting5)
        if generation in ("both", "sdk"):
            watch = (lambda f: sdk(f)) if generation == "both" else (lambda f: f)
            by_id[n.IDS["DRS_SetSetting"]] = sset(watch(set_setting))
            by_id[n.IDS["DRS_GetSetting"]] = sget(watch(get_setting))
        self.by_id = by_id

        def query(i):
            self.asked.append(i)
            return ctypes.cast(by_id[i], ctypes.c_void_p).value if i in by_id else None     # the real one answers NULL for an id it lacks
        self.query = query

    def api(self):
        return n.NvApi(query_interface=self.query)


def test_read_then_set_then_read_back():
    d = FakeDriver()
    assert n.read_policy(d.api()) is None                 # never set: the driver default
    assert n.set_policy(1, d.api()) == 1                  # set, saved, read back
    assert d.saved == {n.SETTING_ID: 1}
    assert n.read_policy(d.api()) == 1


def test_every_session_is_closed_again_even_when_a_call_fails():
    d = FakeDriver()
    n.read_policy(d.api())
    assert d.calls[0] == "Initialize" and d.calls[-2:] == ["DRS_DestroySession", "Unload"]
    d = FakeDriver(fail_on="DRS_LoadSettings")
    with pytest.raises(OSError, match="DRS_LoadSettings"):
        n.read_policy(d.api())
    assert d.calls[-2:] == ["DRS_DestroySession", "Unload"]            # nothing leaks
    d = FakeDriver(fail_on="DRS_CreateSession")
    with pytest.raises(OSError):
        n.read_policy(d.api())
    assert d.calls[-1] == "Unload"


def test_a_standard_user_gets_a_clear_error_not_a_silent_success():
    d = FakeDriver(admin=False)
    with pytest.raises(OSError) as err:
        n.set_policy(1, d.api())
    assert "-137" in str(err.value) and "elevated" in str(err.value)
    assert d.saved == {}
    assert n.read_policy(d.api()) is None                 # reading needs no rights


def test_a_driver_too_old_for_the_call_is_an_error_naming_it():
    api = n.NvApi(query_interface=lambda i: 0)
    with pytest.raises(OSError, match="Initialize"):
        n.read_policy(api)


# ---------------------------------------------------------------- which NVAPI ids and argument lists (review: the ABI was unproven)
# NVIDIA Profile Inspector (the one tool known to work on current drivers) asks for 0x8A2CF5F5 / 0xEA99498D first, falls back to the SDK
# header's 0x577DD202 / 0x73BF8338, and calls either with two trailing arguments (set: 0, 0; get: a pointer to a uint32).

def test_the_newer_ids_are_the_ones_profile_inspector_asks_for_first():
    assert n.NEWER_IDS == {"DRS_SetSetting": 0x8A2CF5F5, "DRS_GetSetting": 0xEA99498D}
    assert n.IDS["DRS_SetSetting"] == 0x577DD202 and n.IDS["DRS_GetSetting"] == 0x73BF8338


def test_a_current_driver_is_asked_for_the_newer_ids_first_and_the_old_ones_stay_unused():
    d = FakeDriver(generation="both")
    api = d.api()
    assert n.set_policy(1, api) == 1 and d.saved == {n.SETTING_ID: 1}
    assert d.sdk_used == []
    assert n.NEWER_IDS["DRS_SetSetting"] in d.asked and n.NEWER_IDS["DRS_GetSetting"] in d.asked
    assert n.IDS["DRS_SetSetting"] not in d.asked and n.IDS["DRS_GetSetting"] not in d.asked    # the old ids are never even asked for
    assert (api.answered["DRS_SetSetting"], api.answered["DRS_GetSetting"]) == (0x8A2CF5F5, 0xEA99498D)


def test_the_newer_calls_get_their_two_trailing_arguments():
    d = FakeDriver(generation="new")
    n.set_policy(1, d.api())
    assert d.trailing == {"set": (0, 0), "get": True}          # set: two zeros; get: a pointer to memory of ours the driver may write


def test_an_older_driver_without_the_newer_ids_is_served_by_the_sdk_ids():
    d = FakeDriver(generation="sdk")
    api = d.api()
    assert n.set_policy(1, api) == 1 and n.read_policy(d.api()) == 1
    assert d.asked[d.asked.index(n.NEWER_IDS["DRS_SetSetting"]) + 1] == n.IDS["DRS_SetSetting"]      # asked the newer, then fell back
    assert (api.answered["DRS_SetSetting"], api.answered["DRS_GetSetting"]) == (0x577DD202, 0x73BF8338)


def test_a_driver_with_neither_id_for_a_call_is_an_error_naming_the_call():
    d = FakeDriver(generation="both")
    for gone in (n.NEWER_IDS["DRS_GetSetting"], n.IDS["DRS_GetSetting"]):
        d.by_id.pop(gone)
    with pytest.raises(OSError, match="DRS_GetSetting"):
        n.read_policy(d.api())
    assert d.calls[-2:] == ["DRS_DestroySession", "Unload"]


def test_the_status_and_a_failure_say_which_interface_answered():
    d = FakeDriver(generation="both")
    got = n.status(d.api())
    assert "GetSetting 0xEA99498D (newer id)" in got["via"]
    got = n.status(FakeDriver(generation="sdk").api())
    assert "GetSetting 0x73BF8338 (SDK id)" in got["via"]
    with pytest.raises(OSError) as err:
        n.set_policy(1, FakeDriver(admin=False).api())
    assert "0x8A2CF5F5" in str(err.value)                       # which id was called when it failed
    assert n.status(FakeDriver(admin=True, fail_on="DRS_LoadSettings").api())["via"] is None


def test_status_says_what_the_policy_is_in_words():
    d = FakeDriver()
    got = n.status(d.api())
    assert got == {"available": True, "value": None, "off": False, "label": n.UNSET, "why": None, "via": got["via"]} and got["via"]
    n.set_policy(1, d.api())
    got = n.status(d.api())
    assert got["value"] == 1 and got["off"] is True and "NO sysmem fallback" in got["label"] and got["why"] is None
    n.set_policy(2, d.api())
    got = n.status(d.api())
    assert got["value"] == 2 and got["off"] is False and "prefer sysmem fallback" in got["label"]
    d.db[n.SETTING_ID] = 9                                # a value this driver invented: shown, not hidden
    assert n.status(d.api())["label"].startswith("9 ")


def test_status_never_raises_it_says_why_it_cannot_tell():
    got = n.status(FakeDriver(fail_on="DRS_LoadSettings").api())
    assert got["available"] is False and got["value"] is None and got["off"] is False and "DRS_LoadSettings" in got["why"]


def test_not_windows_is_a_clean_not_available(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    got = n.status()
    assert got["available"] is False and "Windows" in got["why"] and got["value"] is None
    with pytest.raises(OSError, match="Windows"):
        n.read_policy()


def test_windows_without_an_nvidia_driver_is_a_clean_not_available(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def no_dll(name):
        raise FileNotFoundError(f"Could not find module '{name}'")
    monkeypatch.setattr(ctypes, "WinDLL", no_dll, raising=False)
    got = n.status()
    assert got["available"] is False and "nvapi64.dll" in got["why"]


# ---------------------------------------------------------------- the command line (installer, kairo diag, P17)

def test_cli_get_prints_one_parsable_line_and_exits_by_whether_it_could_read(capsys):
    d = FakeDriver()
    assert n.main(["get"], api=d.api()) == 0
    line = capsys.readouterr().out.splitlines()
    assert len(line) == 1 and line[0].startswith("policy=unset " + n.UNSET) and "GetSetting 0xEA99498D (newer id)" in line[0]
    n.set_policy(1, d.api())
    assert n.main(["get"], api=d.api()) == 0
    assert capsys.readouterr().out.startswith("policy=1 ")
    assert n.main(["get", "--json"], api=d.api()) == 0
    assert json.loads(capsys.readouterr().out)["value"] == 1


def test_cli_get_off_windows_exits_3_and_says_so(capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert n.main(["get"]) == 3
    out = capsys.readouterr().out
    assert out.startswith("policy=unavailable ") and "Windows" in out


def test_cli_set_writes_reads_back_and_compares(capsys):
    d = FakeDriver()
    assert n.main(["set", "1"], api=d.api()) == 0
    out = capsys.readouterr().out
    assert out.startswith("policy=1 ") and d.saved == {n.SETTING_ID: 1}
    assert len(out.splitlines()) == 1 and "SetSetting 0x8A2CF5F5 (newer id)" in out and "GetSetting 0xEA99498D (newer id)" in out
    import re      # the installer's two patterns (KairoSetup.psm1) still read this line
    assert re.match(r"^policy=1(\s|$)", out) and re.match(r"^policy=", out)


def test_cli_set_exits_1_when_the_read_back_is_not_what_was_asked(capsys):
    d = FakeDriver()
    api = d.api()
    real_set = api.set
    api.set = lambda h, profile, value, setting_id=n.SETTING_ID: real_set(h, profile, 0, setting_id)   # the driver keeps its own value
    assert n.main(["set", "1"], api=api) == 1
    assert "policy=0 " in capsys.readouterr().out


def test_cli_set_without_the_rights_exits_3_with_the_reason_and_changes_nothing(capsys):
    d = FakeDriver(admin=False)
    assert n.main(["set", "1"], api=d.api()) == 3
    out = capsys.readouterr().out
    assert out.startswith("policy=unavailable ") and "elevated" in out and d.saved == {}


@pytest.mark.parametrize("argv", [[], ["get", "now"], ["set"], ["set", "3"], ["set", "x"], ["set", "1", "2"], ["frobnicate"]])
def test_cli_wrong_use_exits_2_and_writes_nothing(argv, capsys):
    d = FakeDriver()
    assert n.main(argv, api=d.api()) == 2
    assert d.saved == {} and d.calls == []
    assert "usage" in capsys.readouterr().err.lower()


def test_the_file_runs_alone_under_isolated_python_from_any_folder(tmp_path):
    """The installer runs it as a script with the system Python (python -I <file> get): stdlib only, no `app` import."""
    out = subprocess.run([sys.executable, "-I", str(SCRIPT), "get", "--json"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert out.returncode in (0, 3), out.stderr
    got = json.loads(out.stdout)
    assert set(got) == {"available", "value", "off", "label", "why", "via"}
    if sys.platform != "win32":
        assert out.returncode == 3 and got["available"] is False and "Windows" in got["why"]


def test_the_module_imports_nothing_from_kairo_and_touches_no_network():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "from app" not in text and "from ." not in text and "import app" not in text
    for word in ("socket", "http", "urllib", "requests"):
        assert word not in text, word
