"""Group G4 (voice plan section 7, E1): the call's uplink jitter buffer, 8 packets and 1 to wait for instead of
the WebRTC library's 16 and 4 (app/rtcjitter.py), behind the owner's flag g4.jitter_patch (default off).

It changes the library's own inner parts, so the tests pin both sides: the patch swaps exactly what it knows and
refuses (leaves everything as it is) on anything else, and the real library still has what the patch touches.
This file runs everywhere (stand-in objects). The checks against the real library (aiortc and Pipecat), the
delay measurement and the call itself are in tests/test_voice_rtc.py, the file both voice CI jobs run.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlmodel import Session

from app import flags, rtcjitter
from app.db import make_engine
from app.settings import REPO_ROOT

FLAG = "g4.jitter_patch"


# ---------------------------------------------------------------- stand-ins for the library's parts

class JitterBuffer:
    """What the library builds for a receiver (same name and private fields), without the library."""

    def __init__(self, capacity: int, prefetch: int = 0, is_video: bool = False):
        self._capacity, self._prefetch, self._is_video = capacity, prefetch, is_video
        self._origin = None
        self._packets = [None] * capacity

    def add(self, packet):
        return False, None


def _receiver(buffer):
    r = SimpleNamespace()
    if buffer is not None:
        setattr(r, rtcjitter.SLOT, buffer)
    return r


def _pc(*transceivers):
    return SimpleNamespace(getTransceivers=lambda: list(transceivers))


def _audio(buffer=None):
    buffer = JitterBuffer(16, 4) if buffer is None else buffer
    return SimpleNamespace(kind="audio", receiver=_receiver(buffer))


def _video():
    return SimpleNamespace(kind="video", receiver=_receiver(JitterBuffer(128, 0, True)))


def _buf(transceiver) -> JitterBuffer:
    return getattr(transceiver.receiver, rtcjitter.SLOT)


@pytest.fixture
def library(monkeypatch):
    """The stand-in library is the installed version the patch was written for (tests change it)."""
    state = {"version": rtcjitter.SUPPORTED[0]}
    monkeypatch.setattr(rtcjitter, "_library_version", lambda pc: ("webrtclib", state["version"]))
    return state


# ---------------------------------------------------------------- the flag

def test_the_flag_is_off_until_the_owner_turns_it_on(migrated):
    default, what = flags.FLAGS[FLAG]
    assert default is False and "call" in what.lower()          # patches the library's inner parts: the owner's decision
    with Session(make_engine(migrated.db_url)) as s:
        assert flags.on(s, FLAG) is False
        flags.set_flag(s, FLAG, True)
        assert flags.on(s, FLAG) is True
        flags.set_flag(s, FLAG, None)
        assert flags.on(s, FLAG) is False


# ---------------------------------------------------------------- what it swaps

def test_it_gives_the_audio_receiver_a_buffer_of_8_packets_that_waits_for_1(library, caplog):
    audio, video = _audio(), _video()
    old_audio, old_video = _buf(audio), _buf(video)
    with caplog.at_level(logging.INFO, logger="kairo.rtcjitter"):
        assert rtcjitter.install(_pc(audio, video)) is True
    new = _buf(audio)
    assert new is not old_audio and (new._capacity, new._prefetch, new._is_video) == (8, 1, False)
    assert _buf(video) is old_video                              # video is not touched
    assert "16/4 -> 8/1" in caplog.text and "webrtclib 1.15.0" in caplog.text


# ---------------------------------------------------------------- when it refuses: nothing is touched

def _refused(pc, caplog, why: str):
    """install() says no, logs why, and leaves every receiver with the very buffer it had."""
    before = [(t, getattr(t.receiver, rtcjitter.SLOT, None)) for t in pc.getTransceivers()]
    with caplog.at_level(logging.INFO, logger="kairo.rtcjitter"):
        assert rtcjitter.install(pc) is False
    for t, buf in before:
        assert getattr(t.receiver, rtcjitter.SLOT, None) is buf
    assert why in caplog.text, caplog.text
    assert "left as it is" in caplog.text


@pytest.mark.parametrize("version", ["1.14.0", "1.15.1", "1.16.0", "2.0.0", None])
def test_another_version_of_the_library_is_left_alone(library, caplog, version):
    library["version"] = version
    audio = _audio()
    _refused(_pc(audio), caplog, "not installed" if version is None else version)
    assert (_buf(audio)._capacity, _buf(audio)._prefetch) == (16, 4)


def test_no_audio_receiver_means_nothing_to_do(library, caplog):
    _refused(_pc(_video()), caplog, "no audio receiver")
    _refused(_pc(), caplog, "no audio receiver")


def test_a_receiver_without_the_buffer_it_expects_is_left_alone(library, caplog):
    _refused(_pc(SimpleNamespace(kind="audio", receiver=_receiver(None))), caplog, rtcjitter.SLOT)


@pytest.mark.parametrize("buffer, why", [
    (JitterBuffer(8, 1), "8/1"),                                # already patched, or the library changed its numbers
    (JitterBuffer(32, 4), "32/4"),
    (JitterBuffer(16, 2), "16/2"),
    (JitterBuffer(16, 4, is_video=True), "video"),
    (SimpleNamespace(_capacity=16, _prefetch=4, _is_video=False, _origin=None), "SimpleNamespace"),   # not that class
])
def test_a_buffer_that_is_not_the_stock_audio_one_is_left_alone(library, caplog, buffer, why):
    audio = _audio(buffer)
    _refused(_pc(audio), caplog, why)


def test_a_buffer_that_already_holds_packets_is_left_alone(library, caplog):
    b = JitterBuffer(16, 4)
    b._origin = 1234                                            # packets arrived: swapping would drop them
    _refused(_pc(_audio(b)), caplog, "holds packets")


def test_a_buffer_that_cant_be_rebuilt_as_8_and_1_is_left_alone(library, caplog):
    class Stubborn(JitterBuffer):
        def __init__(self, capacity, prefetch=0, is_video=False):
            super().__init__(16, 4, is_video)                   # ignores what it is asked for

    Stubborn.__name__ = "JitterBuffer"
    _refused(_pc(_audio(Stubborn(16, 4))), caplog, "8/1")

    class Broken(JitterBuffer):
        def __init__(self, capacity, prefetch=0, is_video=False):
            if capacity == 8:
                raise TypeError("new signature")
            super().__init__(capacity, prefetch, is_video)

    Broken.__name__ = "JitterBuffer"
    _refused(_pc(_audio(Broken(16, 4))), caplog, "new signature")


def test_it_is_all_or_nothing_with_two_audio_receivers(library, caplog):
    first, second = _audio(), _audio(JitterBuffer(16, 3))
    _refused(_pc(first, second), caplog, "16/3")
    assert (_buf(first)._capacity, _buf(first)._prefetch) == (16, 4)


def test_a_failure_while_swapping_puts_the_old_buffers_back(library, caplog):
    class Receiver:
        """The second one refuses a new buffer: the first, already swapped, must get its own back."""

        def __init__(self, fails: bool):
            object.__setattr__(self, "fails", fails)
            object.__setattr__(self, rtcjitter.SLOT, JitterBuffer(16, 4))

        def __setattr__(self, name, value):
            if name == rtcjitter.SLOT and self.fails:
                raise AttributeError("read only")
            object.__setattr__(self, name, value)

    one, two = (SimpleNamespace(kind="audio", receiver=Receiver(f)) for f in (False, True))
    first_before, second_before = _buf(one), _buf(two)
    with caplog.at_level(logging.INFO, logger="kairo.rtcjitter"):
        assert rtcjitter.install(_pc(one, two)) is False
    assert _buf(one) is first_before and _buf(two) is second_before
    assert "read only" in caplog.text and "left as it is" in caplog.text


def test_it_never_raises_a_call_goes_on_whatever_the_library_does(caplog):
    def boom():
        raise RuntimeError("no transceivers today")

    with caplog.at_level(logging.INFO, logger="kairo.rtcjitter"):
        assert rtcjitter.install(SimpleNamespace(getTransceivers=boom)) is False
        assert rtcjitter.install(object()) is False
        assert rtcjitter.install(None) is False
    assert caplog.text.count("left as it is") == 3


# ---------------------------------------------------------------- the pin and the module's own limits

def test_the_pinned_library_version_is_one_the_patch_was_checked_against():
    """A bump of the pin in requirements-voice.txt fails here until the patch has been checked on the new version
    (the numbers in the changelog re-measured, the real-library tests in test_voice_rtc.py passing)."""
    pins = re.findall(r"^aiortc==([\w.]+)", (REPO_ROOT / "server" / "requirements-voice.txt").read_text(encoding="utf-8"), re.M)
    assert pins and all(p in rtcjitter.SUPPORTED for p in pins), (pins, rtcjitter.SUPPORTED)


VOICE_CI_JOBS = ("voice", "voice-windows")        # the jobs in .github/workflows/ci.yml that install the voice packages
NEEDS_THE_VOICE_PACKAGES = re.compile(r"""pytest\.importorskip\(\s*["'](?:aiortc|pipecat)""")


def test_a_test_file_that_skips_without_the_voice_packages_runs_in_both_voice_ci_jobs():
    """The plain `server` job installs no aiortc or pipecat, so a test that needs them skips there. If no voice job
    lists its file, it never runs anywhere in CI: a bump of the aiortc pin that the jitter patch no longer fits would
    stay green and every call would quietly keep the library's 16/4 buffer. (A new file needs an edit of ci.yml, and
    a pull request that touches .github/ is merged by the owner by hand, so the jitter tests live in test_voice_rtc.py.)"""
    jobs = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    listed = {}
    for name in VOICE_CI_JOBS:
        pytest_steps = [s["run"] for s in jobs[name]["steps"] if "pytest" in s.get("run", "")]
        assert len(pytest_steps) == 1, (name, pytest_steps)
        listed[name] = set(pytest_steps[0].split())
    assert jobs["voice"]["runs-on"].startswith("ubuntu") and jobs["voice-windows"]["runs-on"].startswith("windows")
    tests = Path(__file__).parent
    needing = sorted(f.name for f in tests.glob("test_*.py")
                     if f.name != Path(__file__).name and NEEDS_THE_VOICE_PACKAGES.search(f.read_text(encoding="utf-8")))
    assert "test_voice_rtc.py" in needing                                # (sanity: the scan sees the call tests)
    for name in VOICE_CI_JOBS:
        missing = [f for f in needing if f"tests/{f}" not in listed[name]]
        assert not missing, f"job {name} does not run {missing}: they skip in the plain server job and would never run"


def test_the_patch_module_opens_no_connection_and_imports_nothing_that_could():
    """It only swaps one in-memory object that the caller (voicertc.py) hands over: it imports the logging and
    the package-metadata modules and nothing else, so it does not belong in outbound.MODULES."""
    tree = ast.parse((Path(rtcjitter.__file__)).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
    assert imported <= {"__future__", "importlib.metadata", "logging"}, imported
