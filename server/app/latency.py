"""Fix plan item 31: how long the owner waits, as p50/p95 (More -> System -> Speed, Run diagnosis).

Measured where it happens: speech to text and Kairo's voice on the PC (each request), a call turn
on the phone (you stop talking -> Kairo's voice starts; POST /api/latency), Claude's first words
from model_runs (fix plan 26/30), a voice note from its capture (upload -> filed).
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, delete, select

from .db import Capture, Latency, ModelRun, utcnow

KEEP_DAYS = 30
CLIENT_KINDS = {"call_turn"}            # what the phone may report
MAX_MS = 10 * 60 * 1000
TITLES = {
    "call_turn": "Call: you stop, Kairo speaks",
    "first_words_call": "Claude's first words (call)",
    "first_words_chat": "Claude's first words (chat)",
    "stt": "Speech to text",
    "tts": "Kairo's voice (one sentence)",
    "voice_note": "Voice note: upload to done",
}


def record(engine, kind: str, ms: float) -> None:
    ms = int(ms)
    if kind not in TITLES or not 0 <= ms <= MAX_MS:
        return
    with Session(engine) as s:
        s.add(Latency(kind=kind, ms=ms))
        s.commit()


def prune(s: Session, now: dt.datetime | None = None) -> int:
    cutoff = (now or utcnow()) - dt.timedelta(days=KEEP_DAYS)
    n = len(s.exec(select(Latency.id).where(Latency.ts < cutoff)).all())
    s.exec(delete(Latency).where(Latency.ts < cutoff))
    s.commit()
    return n


def pct(values: list[float], p: float) -> int | None:
    v = sorted(values)
    return int(v[min(len(v) - 1, int(len(v) * p))]) if v else None


def _aware(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def summary(s: Session, days: int = 7, now: dt.datetime | None = None) -> list[dict]:
    """One row per kind with data: {kind, title, n, p50_ms, p95_ms}, in TITLES order."""
    since = (now or utcnow()) - dt.timedelta(days=days)
    values: dict[str, list[float]] = {k: [] for k in TITLES}
    for kind, ms in s.exec(select(Latency.kind, Latency.ms).where(Latency.ts >= since)).all():
        if kind in values:
            values[kind].append(ms)
    for purpose, ms in s.exec(select(ModelRun.purpose, ModelRun.first_text_ms).where(ModelRun.ts >= since)
                              .where(ModelRun.first_text_ms.is_not(None))
                              .where(ModelRun.purpose.in_(("chat", "call")))).all():
        values[f"first_words_{purpose}"].append(ms)
    for created, filed in s.exec(select(Capture.created_at, Capture.filed_at).where(Capture.created_at >= since)
                                 .where(Capture.filed_as == "voice_note").where(Capture.filed_at.is_not(None))).all():
        values["voice_note"].append((_aware(filed) - _aware(created)).total_seconds() * 1000)
    return [{"kind": k, "title": TITLES[k], "n": len(v), "p50_ms": pct(v, .5), "p95_ms": pct(v, .95)}
            for k, v in values.items() if v]
