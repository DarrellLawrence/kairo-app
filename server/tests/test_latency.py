"""Fix plan 31: how long the owner waits, measured where it happens, as p50/p95."""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import latency
from app.db import Capture, Latency, ModelRun, make_engine, utcnow
from app.main import create_app


def test_summary_from_all_sources(migrated):
    engine = make_engine(migrated.db_url)
    now = utcnow()
    for ms in (400, 600, 800, 3000):
        latency.record(engine, "stt", ms)
    latency.record(engine, "call_turn", 2500)        # replaced by app/turns.py: not kept any more
    latency.record(engine, "nonsense", 5)            # unknown kinds are not kept
    latency.record(engine, "tts", -3)                # nor impossible numbers
    with Session(engine) as s:
        s.add(ModelRun(purpose="call", model="haiku", first_text_ms=900, warm=True))
        s.add(ModelRun(purpose="triage", model="haiku", first_text_ms=5000))     # a job: nobody waits
        s.add(Capture(kind="audio", source="voice_note", status="filed", filed_as="voice_note",
                      created_at=now - dt.timedelta(seconds=12), filed_at=now))
        s.commit()
        rows = {r["kind"]: r for r in latency.summary(s)}
    assert rows["stt"] == {"kind": "stt", "title": "Speech to text", "n": 4, "p50_ms": 800, "p95_ms": 3000}
    assert "call_turn" not in rows
    assert rows["first_words_call"]["p50_ms"] == 900 and "first_words_triage" not in rows
    assert 11000 <= rows["voice_note"]["p50_ms"] <= 13000
    assert "tts" not in rows and "nonsense" not in rows


def test_old_rows_go(migrated):
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        s.add(Latency(kind="stt", ms=1, ts=utcnow() - dt.timedelta(days=40)))
        s.add(Latency(kind="stt", ms=2))
        s.commit()
        assert latency.prune(s) == 1
        assert [r.ms for r in s.exec(select(Latency)).all()] == [2]


def test_the_phone_no_longer_posts_single_numbers(migrated):
    """A turn is reported whole, step by step: POST /api/voice/turns (app/turns.py)."""
    with TestClient(create_app(migrated)) as c:
        assert c.post("/api/latency", json={"kind": "call_turn", "ms": 2100}).status_code == 405
        assert c.get("/api/latency").status_code == 200
