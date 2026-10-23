"""Group G4 (voice plan section 7, E1): the call's uplink jitter buffer, 8 packets and 1 to wait for instead of
the WebRTC library's 16 and 4 (app/rtcjitter.py), behind the owner's flag g4.jitter_patch (default off).

It changes the library's own inner parts, so the tests pin both sides: the patch swaps exactly what it knows and
refuses (leaves everything as it is) on anything else, and the real library still has what the patch touches.
The first half runs everywhere (stand-in objects); the rest needs the voice packages (the real library).
"""

from __future__ import annotations

import ast
import asyncio
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
    (the numbers in the changelog re-measured, the real-library tests below passing)."""
    pins = re.findall(r"^aiortc==([\w.]+)", (REPO_ROOT / "server" / "requirements-voice.txt").read_text(encoding="utf-8"), re.M)
    assert pins and all(p in rtcjitter.SUPPORTED for p in pins), (pins, rtcjitter.SUPPORTED)


VOICE_CI_JOBS = ("voice", "voice-windows")        # the jobs in .github/workflows/ci.yml that install the voice packages
NEEDS_THE_VOICE_PACKAGES = ("tests/test_rtcjitter.py", "tests/test_rtcjitter_loopback.py")


def test_ci_runs_the_tests_that_need_the_real_library_on_linux_and_windows():
    """The plain `server` job installs no aiortc or pipecat, so every real-library test of this patch skips there.
    Without these two files in the voice jobs, a bump of the aiortc pin that the patch no longer fits would be
    green in CI (the check above only compares the version number) and every call would quietly stay on 16/4."""
    jobs = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    for name in VOICE_CI_JOBS:
        pytest_steps = [s["run"] for s in jobs[name]["steps"] if "pytest" in s.get("run", "")]
        assert len(pytest_steps) == 1, (name, pytest_steps)
        listed = pytest_steps[0].split()
        for test_file in NEEDS_THE_VOICE_PACKAGES:
            assert test_file in listed, f"{name} does not run {test_file}"
        assert "tests/test_voice_rtc.py" in listed                       # (sanity: this is the right step)
    assert jobs["voice"]["runs-on"].startswith("ubuntu") and jobs["voice-windows"]["runs-on"].startswith("windows")


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


# ---------------------------------------------------------------- the real library (the voice packages)

def _real_pc():
    """A peer connection with an audio and a video receiver, like the library builds them for a call."""
    from aiortc import RTCConfiguration, RTCPeerConnection
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))      # no STUN, like Kairo and the phone
    pc.addTransceiver("audio")
    pc.addTransceiver("video")
    return pc


def _real_buffers(pc) -> dict:
    return {t.kind: getattr(t.receiver, rtcjitter.SLOT) for t in pc.getTransceivers()}


def test_the_real_library_has_what_the_patch_touches_and_the_pin_is_installed():
    """Fails on a version bump or a rename inside the library: then the patch (and its numbers) must be checked again."""
    pytest.importorskip("aiortc")

    async def go():
        pc = _real_pc()
        try:
            stock = _real_buffers(pc)
            assert (stock["audio"]._capacity, stock["audio"]._prefetch, stock["audio"]._is_video) == (16, 4, False)
            assert (stock["video"]._capacity, stock["video"]._is_video) == (128, True)
            assert stock["audio"]._origin is None and len(stock["audio"]._packets) == 16
            name, version = rtcjitter._library_version(pc)
            assert (name, version in rtcjitter.SUPPORTED) == ("aiortc", True), (name, version)
            assert rtcjitter.install(pc) is True
            now = _real_buffers(pc)
            assert (now["audio"]._capacity, now["audio"]._prefetch) == (8, 1) and now["audio"] is not stock["audio"]
            assert now["video"] is stock["video"]
        finally:
            await pc.close()
    asyncio.run(go())


def _lag(buffer, lost_at: int | None = None, n: int = 400) -> list[int]:
    """Feed packets 20 ms apart (one frame each, sequence +1, timestamp +960), skip number lost_at, and record
    after every packet: packets in front of the newest one handed on. The buffer is the library's own."""
    from aiortc.rtp import RtpPacket
    lag, newest = [], None
    for i in range(n):
        if i == lost_at:
            continue
        p = RtpPacket.parse(RtpPacket(payload_type=96, sequence_number=(500 + i) % 65536,
                                      timestamp=1000 + 960 * i, payload=b"x" * 20).serialize())
        p._data = p.payload                                      # what the receiver does after depayloading
        _, frame = buffer.add(p)
        if frame is not None:
            newest = (frame.timestamp - 1000) // 960
        if newest is not None:
            lag.append(i - newest)
    return lag


def test_the_buffers_delay_in_packets_clean_and_after_one_lost_packet():
    """Exact (arithmetic on 20 ms packets, no clock): the library's 16/4 holds the voice back 4 packets = 80 ms,
    and after ONE lost packet 14 packets = 280 ms for the rest of the call. 8/1: 1 packet = 20 ms, then 6 = 120 ms."""
    pytest.importorskip("aiortc")

    async def buffers():
        pc = _real_pc()
        try:
            stock = _real_buffers(pc)["audio"]
            assert rtcjitter.install(pc) is True
            return stock, _real_buffers(pc)["audio"]
        finally:
            await pc.close()
    stock, patched = asyncio.run(buffers())
    from aiortc.jitterbuffer import JitterBuffer as Library
    stock_again = Library(capacity=16, prefetch=4)               # a second, untouched 16/4 for the lossy run
    patched_again = Library(capacity=8, prefetch=1)
    clean, lossy = _lag(stock), _lag(stock_again, lost_at=100)
    assert clean[50] == 4 and clean[-1] == 4                     # 80 ms, steady
    assert max(lossy[95:120]) == 19 and lossy[-1] == 14          # 380 ms just after the loss, 280 ms from then on
    clean, lossy = _lag(patched), _lag(patched_again, lost_at=100)
    assert clean[50] == 1 and clean[-1] == 1                     # 20 ms
    assert max(lossy[95:120]) == 8 and lossy[-1] == 6            # 160 ms just after the loss, 120 ms from then on


# ---------------------------------------------------------------- in a call: voicertc.connect and the app's flag

from tests.test_voice_local import local  # noqa: E402,F401  (the app with the fake voice model and Claude)


def _needs_pipecat():
    pytest.importorskip("pipecat")
    pytest.importorskip("aiortc")


def _shape_of(conn) -> list[tuple[int, int]]:
    """(capacity, prefetch) of each audio receiver's buffer on a call's connection."""
    return [(t.receiver.__dict__[rtcjitter.SLOT]._capacity, t.receiver.__dict__[rtcjitter.SLOT]._prefetch)
            for t in conn.pc.getTransceivers() if t.kind == "audio"]


async def _phone_offer():
    """The fake phone: a peer connection with a microphone, and its offer."""
    from aiortc import RTCConfiguration, RTCPeerConnection

    from tests.test_voice_rtc import _phone_track
    from app import voicertc
    voicertc.tailnet_only(allow_loopback=True)
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.addTrack(_phone_track([0.5]))
    await pc.setLocalDescription(await pc.createOffer())
    return pc


@pytest.mark.parametrize("patch, shape", [(False, (16, 4)), (True, (8, 1))])
def test_a_call_connection_has_the_8_and_1_buffer_only_when_asked(patch, shape):
    """The receivers exist when connect() returns (before build_call), so that is where it has to happen."""
    _needs_pipecat()
    from app import voicertc

    async def go():
        pc = await _phone_offer()
        conn = await voicertc.connect(pc.localDescription.sdp, pc.localDescription.type, allow_loopback=True,
                                      jitter_patch=patch)
        try:
            return _shape_of(conn)
        finally:
            await conn.disconnect()
            await pc.close()
    assert asyncio.run(go()) == [shape]


def test_connect_without_the_argument_leaves_the_library_as_it_is():
    _needs_pipecat()
    from app import voicertc

    async def go():
        pc = await _phone_offer()
        conn = await voicertc.connect(pc.localDescription.sdp, pc.localDescription.type, allow_loopback=True)
        try:
            return _shape_of(conn)
        finally:
            await conn.disconnect()
            await pc.close()
    assert asyncio.run(go()) == [(16, 4)]


def test_the_app_reads_the_flag_at_the_start_of_every_call(local, monkeypatch):
    """Off by default; the owner turns it on and the next call has the small buffer; off again, the next is stock."""
    _needs_pipecat()
    from app import voicertc
    from aiortc import RTCSessionDescription
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    seen = []
    real = voicertc.connect

    async def spy(*args, **kwargs):
        conn = await real(*args, **kwargs)
        seen.append(_shape_of(conn))
        return conn
    monkeypatch.setattr(voicertc, "connect", spy)

    async def call():
        pc = await _phone_offer()
        r = await asyncio.to_thread(local.post, "/api/rtc/offer",
                                    json={"sdp": pc.localDescription.sdp, "type": "offer", "path": "call"})
        assert r.status_code == 200, r.text
        await pc.setRemoteDescription(RTCSessionDescription(sdp=r.json()["sdp"], type=r.json()["type"]))
        await pc.close()

    async def go():
        await call()                                                    # nothing set: the default
        assert (await asyncio.to_thread(local.put, "/api/flags/" + FLAG, json={"on": True})).status_code == 200
        await call()
        assert (await asyncio.to_thread(local.put, "/api/flags/" + FLAG, json={"on": False})).status_code == 200
        await call()
    asyncio.run(go())
    assert seen == [[(16, 4)], [(8, 1)], [(16, 4)]]


def test_a_connection_without_the_peer_connection_it_expects_still_makes_the_call(caplog, monkeypatch):
    """The patch is an extra: whatever the pipecat connection looks like one day, connect() returns it."""
    _needs_pipecat()
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

    from app import voicertc

    def gone(self):
        raise AttributeError("no pc any more")
    monkeypatch.setattr(SmallWebRTCConnection, "pc", property(gone))

    async def go():
        pc = await _phone_offer()
        with caplog.at_level(logging.INFO, logger="kairo.rtcjitter"):
            conn = await voicertc.connect(pc.localDescription.sdp, pc.localDescription.type, allow_loopback=True,
                                          jitter_patch=True)
        await conn.disconnect()
        await pc.close()
        return conn
    assert asyncio.run(go()) is not None
    assert "left as it is" in caplog.text
