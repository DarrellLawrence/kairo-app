"""Group G4: what the jitter buffer really costs the phone's voice, measured on a real RTP path (voice plan
section 7, E1; section 11, S3). A fake phone (aiortc, in this process) sends 100 ms tone bursts at a random
moment from a microphone paced in real time; Kairo's side is voicertc.connect() as in a call; the delay is
from the moment the phone hands the first frame of a burst to its encoder until the PC's receiver delivers the
decoded frame (RemoteStreamTrack.recv()). Opus, packetising, DTLS-SRTP, UDP on 127.0.0.1 and the decoder are
all in it, the network is not. Then ONE packet is lost on the way, and the bursts after it are timed again.

The numbers are the sandbox's CPU (a shared 4-core machine, 82 / 282 ms stock, 22 / 121 ms patched), not the PC.
The windows are wide on purpose (the timers of Windows tick every 15 ms, a busy machine adds more); the two
buffers are also compared with each other, from the same run. Needs the voice packages.
"""

from __future__ import annotations

import asyncio
import fractions
import random
import statistics
import time

import pytest

from app import voicertc

BURST_FRAMES = 5                       # 100 ms of tone
WARM_UP_S = 1.5
SETTLE_S = 1.2                         # after the loss: the stock buffer fills up (16 packets = 320 ms) and gives up on the gap


def _needs_pipecat():
    pytest.importorskip("pipecat")
    pytest.importorskip("aiortc")


def _make_mic():
    import numpy as np
    from aiortc import MediaStreamTrack
    from av import AudioFrame
    tone = (6000 * np.sin(2 * np.pi * 1000 * np.arange(960) / 48000)).astype(np.int16)

    class Mic(MediaStreamTrack):
        """48 kHz mono, 20 ms frames, handed over at real-time pace; burst() makes the next 5 frames a tone."""
        kind = "audio"

        def __init__(self):
            super().__init__()
            self.t0, self.n, self.tone_left, self.onsets = None, 0, 0, []

        def burst(self):
            self.tone_left = BURST_FRAMES
            self._first = True

        async def recv(self):
            if self.t0 is None:
                self.t0 = time.perf_counter()
            self.n += 1
            await asyncio.sleep(max(0.0, self.t0 + self.n * 0.02 - time.perf_counter()))
            if self.tone_left:
                if self.tone_left == BURST_FRAMES:
                    self.onsets.append(time.perf_counter())             # the first tone frame goes to the encoder now
                chunk, self.tone_left = tone, self.tone_left - 1
            else:
                chunk = np.zeros(960, dtype=np.int16)
            f = AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
            f.sample_rate, f.pts, f.time_base = 48000, self.n * 960, fractions.Fraction(1, 48000)
            return f
    return Mic()


async def _measure(patch: bool, bursts: int, lose_one: bool, seed: int = 3) -> dict:
    """Delay (ms) of each burst, before and (with lose_one) after one lost uplink packet."""
    import numpy as np
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.rtcdtlstransport import RTCDtlsTransport
    rng = random.Random(seed)
    voicertc.tailnet_only(allow_loopback=True)
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    mic = _make_mic()
    pc.addTrack(mic)
    await pc.setLocalDescription(await pc.createOffer())
    conn = await voicertc.connect(pc.localDescription.sdp, pc.localDescription.type, allow_loopback=True,
                                  jitter_patch=patch)
    answer = conn.get_answer()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

    arrivals: list[float] = []
    quiet = True

    async def listen(track):
        nonlocal quiet
        while True:
            try:
                f = await track.recv()
            except Exception:
                return
            x = f.to_ndarray().astype(np.float64)
            level = 20 * np.log10(max(np.sqrt(np.mean(x * x)) / 32768.0, 1e-9))
            if quiet and level > -30:
                arrivals.append(time.perf_counter())
                quiet = False
            elif level < -45:
                quiet = True
    track = next(t.receiver.track for t in conn.pc.getTransceivers() if t.kind == "audio")
    reader = asyncio.get_running_loop().create_task(listen(track))

    lost = {"arm": False, "n": 0, "transport": None}
    send = RTCDtlsTransport._send_rtp

    async def lossy(self, data):
        if self is lost["transport"] and lost["arm"] and not 192 <= data[1] <= 223:     # not RTCP
            lost["arm"], lost["n"] = False, lost["n"] + 1
            return None                                                                # gone on the way to the PC
        return await send(self, data)
    RTCDtlsTransport._send_rtp = lossy
    try:
        deadline = time.perf_counter() + 15
        while not (pc.connectionState == conn.pc.connectionState == "connected") and time.perf_counter() < deadline:
            await asyncio.sleep(0.05)
        assert pc.connectionState == conn.pc.connectionState == "connected"
        lost["transport"] = pc.getSenders()[0].transport
        await asyncio.sleep(WARM_UP_S)

        async def run_bursts(n: int) -> list[float]:
            first = len(mic.onsets)
            for _ in range(n):
                await asyncio.sleep(rng.uniform(0.45, 0.6))
                mic.burst()
            await asyncio.sleep(0.6)
            delays, j = [], 0
            for t in mic.onsets[first:]:
                while j < len(arrivals) and arrivals[j] < t:
                    j += 1
                if j < len(arrivals) and arrivals[j] - t < 1.0:
                    delays.append((arrivals[j] - t) * 1000)
                    j += 1
            return delays
        out = {"clean": await run_bursts(bursts)}
        if lose_one:
            lost["arm"] = True
            while lost["n"] == 0:
                await asyncio.sleep(0.01)
            await asyncio.sleep(SETTLE_S)
            out["after_loss"] = await run_bursts(bursts)
        out["lost"] = lost["n"]
        out["bursts"] = bursts
        return out
    finally:
        RTCDtlsTransport._send_rtp = send
        reader.cancel()
        await conn.disconnect()
        await pc.close()


def _p50(xs: list[float]) -> float:
    return statistics.median(xs)


def _run(patch: bool, lose_one: bool = True, bursts: int = 8) -> dict:
    out = asyncio.run(_measure(patch, bursts, lose_one))
    label = "8/1 (patched)" if patch else "16/4 (stock)"
    for key in ("clean", "after_loss"):
        if key in out:
            xs = sorted(out[key])
            print(f"\nuplink jitter buffer {label}, {key}: p50 {_p50(xs):.1f} ms, min {xs[0]:.1f}, max {xs[-1]:.1f}, "
                  f"{len(xs)}/{out['bursts']} bursts heard")
    return out


@pytest.fixture(scope="module")
def runs():
    """One call per setting (about 12 s each), both under the same load; the tests below read them."""
    _needs_pipecat()
    return {"stock": _run(patch=False), "patched": _run(patch=True)}


def test_the_stock_buffer_delays_every_turn_80_ms_and_one_lost_packet_makes_it_280(runs):
    out = runs["stock"]
    assert out["lost"] == 1 and len(out["clean"]) >= 6 and len(out["after_loss"]) >= 6
    assert 50 < _p50(out["clean"]) < 140, out["clean"]            # E1: 82.5 ms = 4 packets of 20 ms + about 2.5 ms
    assert 220 < _p50(out["after_loss"]) < 400, out["after_loss"]  # E1: 280 ms for the rest of the call


def test_the_patched_buffer_delays_20_ms_and_after_a_lost_packet_120(runs):
    out = runs["patched"]
    assert out["lost"] == 1 and len(out["clean"]) >= 6 and len(out["after_loss"]) >= 6
    assert 5 < _p50(out["clean"]) < 60, out["clean"]               # E1: 22.3 ms
    assert 80 < _p50(out["after_loss"]) < 210, out["after_loss"]   # E1: 120-144 ms with the buffer's 6 packets


def test_the_patch_takes_60_ms_off_every_turn_and_160_ms_after_a_loss(runs):
    """Both runs saw the same machine at the same time, so this holds when the absolute numbers drift."""
    saved_clean = _p50(runs["stock"]["clean"]) - _p50(runs["patched"]["clean"])
    saved_after_loss = _p50(runs["stock"]["after_loss"]) - _p50(runs["patched"]["after_loss"])
    print(f"\nsaved per turn: {saved_clean:.1f} ms; after one lost packet: {saved_after_loss:.1f} ms")
    assert 25 < saved_clean < 95                                   # 3 packets of 20 ms
    assert 90 < saved_after_loss < 240                            # 8 packets of 20 ms
