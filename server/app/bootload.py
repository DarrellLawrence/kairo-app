"""Staggered model loads at boot (voice recipe 9, flag g8.stagger_boot).

The voice model (llama.cpp), Whisper, Kokoro and the memory model all want the graphics card. Started together, the memory model's
"is there 3000 MB free?" check (main.py _memory_on_cpu) looked at the card before llama.cpp had taken its share, so it picked the GPU
and one of them lost: a spill into shared memory (a silent slowdown) or an out-of-memory error. Here each load starts only after the one
before has settled, and the free graphics memory is read before and after each one (the log line and the trail say what each took, so
nobody has to measure it by hand any more). The sequence never stops on one failure and never waits for ever on one stage.

No network, no model: this only orders things Kairo already starts. The stages are made in main.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("kairo.boot")

POLL_S = 0.5            # how often a stage that is still loading is looked at
LLAMA_WAIT_S = 200      # the voice model gets 180 s to start (voice_local.START_TIMEOUT_S), then it is "failed"
VOICE_WAIT_S = 300      # Whisper and Kokoro from a cold disk (their own load is not cut: it goes on in its thread)


@dataclass
class Stage:
    name: str
    start: Callable[[], object]                   # blocking (run in a thread): starts the load, may return when it is done
    settled: Callable[[], bool] | None = None     # polled until True: the load has finished (loaded or failed)
    timeout_s: float = 200.0                      # for start and for the polling together


async def run(stages: list[Stage], *, free_mb: Callable[[], int | None], trail: list[dict] | None = None,
              poll_s: float | None = None) -> list[dict]:
    """The stages one after the other. Returns the trail (also filled as it goes when one is passed):
    {stage, ms, free_before_mb, free_after_mb, used_mb, settled, timed_out, error} per stage."""
    trail = [] if trail is None else trail
    poll = POLL_S if poll_s is None else poll_s
    for stage in stages:
        before = await asyncio.to_thread(free_mb)
        began = time.monotonic()
        error, timed_out = None, False
        try:
            await asyncio.wait_for(asyncio.to_thread(stage.start), stage.timeout_s)
        except asyncio.TimeoutError:
            timed_out = True                      # the thread goes on loading; the sequence does not wait for it any longer
        except Exception as exc:                  # one part failing must not keep the others from starting
            error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("boot: %s failed to start", stage.name)
        settled = error is None and not timed_out
        if settled and stage.settled is not None:
            while not stage.settled():
                if time.monotonic() - began > stage.timeout_s:
                    settled, timed_out = False, True
                    break
                await asyncio.sleep(poll)
        after = await asyncio.to_thread(free_mb)
        used = before - after if before is not None and after is not None else None
        trail.append({"stage": stage.name, "ms": int((time.monotonic() - began) * 1000), "free_before_mb": before,
                      "free_after_mb": after, "used_mb": used, "settled": settled, "timed_out": timed_out, "error": error})
        log.info("boot: %s %s in %.1f s; free graphics memory %s MB before, %s MB after (took %s MB)", stage.name,
                 "loaded" if settled else "did not settle" + (" in time" if timed_out else f" ({error})"),
                 trail[-1]["ms"] / 1000, before, after, used)
    return trail
