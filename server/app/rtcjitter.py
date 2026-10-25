"""Group G4 of the voice plan (section 7, E1): a smaller jitter buffer for the phone's voice, behind the flag
g4.jitter_patch (default off: owner's decision, it changes the WebRTC library's own inner parts).

Why: the library keeps the phone's voice in a jitter buffer before it decodes it. For audio it holds 16 packets
and waits for 4 later ones to arrive before it hands the first on, so every turn is heard 80 ms late (82.5 ms
measured from the phone to the decoded frame, sandbox CPU). One lost packet makes it worse for good: the buffer
waits for the gap until it is full, and the rest of the call runs 280 ms late. With room for 8 packets and
waiting for 1, it is 20 ms (22 ms measured), and after a loss 120 ms. A buffer of 4 dropped 6.5 % of the audio
under network jitter, so 8 is the floor.

How: voicertc.connect() hands over the new call's connection right after its receivers were built (the audio
receiver builds its buffer itself, in its constructor, so this has to happen per connection and before the
first packet) and this swaps that one object. Nothing is changed for good: no setting of the library, no code
of it replaced, the next call starts from the library's own numbers again unless the flag says so.

What it refuses: anything it was not written for. The library version has to be one of SUPPORTED, the receiver
has to hold the buffer under the name this expects, of the class and numbers (16/4, empty, not video) the
library builds, and a buffer built with 8/1 has to report 8/1. If any of that is different, nothing is touched,
the reason goes to the log, and the call runs on the library's own buffer. It never raises.

This module imports no networking and does not name the library: voicertc.py (which does, and is listed in
outbound.MODULES) passes the connection's peer connection in, and the library's name comes from where its
classes live. test_rtcjitter.py pins the imports.
"""

from __future__ import annotations

import importlib.metadata
import logging

log = logging.getLogger("kairo.rtcjitter")

SUPPORTED = ("1.15.0",)           # the versions checked (requirements-voice.txt pins one of them); a bump fails a test
STOCK = (16, 4)                   # (capacity, prefetch) the library builds for audio
CAPACITY, PREFETCH = 8, 1         # what this gives audio instead
SLOT = "_RTCRtpReceiver__jitter_buffer"    # the receiver's private (name-mangled) field that holds the buffer


class _Refused(Exception):
    """Something is not as this was written for: the reason is for the log."""


def _library_version(pc) -> tuple[str, str | None]:
    """(name, installed version) of the package the peer connection's class comes from."""
    name = type(pc).__module__.partition(".")[0]
    try:
        return name, importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return name, None


def _shape(buffer) -> tuple:
    return getattr(buffer, "_capacity", None), getattr(buffer, "_prefetch", None)


def _plan(pc) -> tuple[str, str, list]:
    """(library, version, [(receiver, old buffer, new buffer)]) or _Refused. Changes nothing."""
    name, version = _library_version(pc)
    if version not in SUPPORTED:
        raise _Refused(f"{name} {version or 'is not installed'}, this was written for {' / '.join(SUPPORTED)}")
    receivers = [t.receiver for t in pc.getTransceivers() if t.kind == "audio"]
    if not receivers:
        raise _Refused("no audio receiver")
    swaps = []
    for receiver in receivers:
        old = getattr(receiver, SLOT, None)
        if old is None:
            raise _Refused(f"the audio receiver has no {SLOT}")
        capacity, prefetch = _shape(old)
        if type(old).__name__ != "JitterBuffer" or (capacity, prefetch) != STOCK or getattr(old, "_is_video", None) is not False:
            raise _Refused(f"the audio buffer is {type(old).__name__} {capacity}/{prefetch}"
                           f"{' (video)' if getattr(old, '_is_video', None) else ''}, not the stock {STOCK[0]}/{STOCK[1]}")
        if getattr(old, "_origin", 0) is not None:
            raise _Refused("the audio buffer already holds packets")
        new = type(old)(capacity=CAPACITY, prefetch=PREFETCH)
        if _shape(new) != (CAPACITY, PREFETCH) or getattr(new, "_origin", 0) is not None or not callable(getattr(new, "add", None)):
            raise _Refused(f"a buffer built with {CAPACITY}/{PREFETCH} is {_shape(new)[0]}/{_shape(new)[1]}")
        swaps.append((receiver, old, new))
    return name, version, swaps


def install(pc) -> bool:
    """Give the audio receiver(s) of this peer connection a buffer of 8 packets that waits for 1. True when done;
    False when it refused (the reason is logged and the connection is exactly as it was). Call it right after the
    connection's offer was taken, before any packet arrives. Never raises."""
    try:
        name, version, swaps = _plan(pc)
        done = []
        try:
            for receiver, old, new in swaps:
                setattr(receiver, SLOT, new)
                done.append((receiver, old))
        except Exception:
            for receiver, old in done:                   # all or nothing
                setattr(receiver, SLOT, old)
            raise
    except Exception as e:
        log.warning("rtcjitter: left as it is: %s", e if isinstance(e, _Refused) else f"{type(e).__name__}: {e}")
        return False
    log.info("rtcjitter: audio jitter buffer %d/%d -> %d/%d (%s %s)", *STOCK, CAPACITY, PREFETCH, name, version)
    return True
