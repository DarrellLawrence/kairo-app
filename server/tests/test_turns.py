"""Owner's request 2026-10-15: voice is too slow and doesn't always play. Every turn is measured
step by step, on the phone and the PC (end of your speech or Send -> speech to text -> waiting for
the brain -> the model's first words -> first sentence -> its voice -> playback), with the model and
whether the audio played. More -> System -> Voice speed shows p50/p95 per step for this version and
the one before (before/after), and the last turns. Written before app/turns.py."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import turns
from app.db import VoiceTurn, make_engine
from app.main import create_app


def call_marks(total=2400):
    """Cumulative ms since the end of the owner's speech, as the phone reports them."""
    return {"endpoint": 500, "transcribed": 900, "sent": 910, "first_words": 1800, "first_sentence": 1900,
            "audio_ready": 2300, "playing": total}


def post(c, **kw):
    body = {"turn_id": "t1", "path": "call", "marks": call_marks(), "server": {"stt_ms": 250, "queue_ms": 40},
            "model": "haiku", "outcome": "played", "device": {"ios": "18.1", "standalone": True}}
    body.update(kw)
    return c.post("/api/voice/turns", json=body)


def test_steps_from_the_phones_marks_and_the_pcs_numbers():
    steps = turns.steps(call_marks(), {"stt_ms": 250, "queue_ms": 40}, "call")
    assert steps == {"endpoint_ms": 500, "upload_ms": 150, "stt_ms": 250, "queue_ms": 40, "first_words_ms": 850,
                     "first_sentence_ms": 100, "tts_ms": 400, "play_start_ms": 100, "total_ms": 2400}


def test_a_typed_message_counts_to_its_first_words():
    steps = turns.steps({"sent": 5, "first_words": 1200}, {"queue_ms": 0}, "chat_text")
    assert steps["total_ms"] == 1200 and steps["first_words_ms"] == 1195 and "tts_ms" not in steps


def test_a_spoken_turn_that_never_played_has_no_total():
    assert "total_ms" not in turns.steps({"endpoint": 500, "first_words": 1500}, {}, "call")


@pytest.mark.parametrize("bad", [
    {"path": "elsewhere"}, {"outcome": "great"}, {"marks": {"playing": "soon"}}, {"marks": {"playing": 10**9}},
    {"marks": {"playing": -5}}, {"model": "x" * 50},
])
def test_bad_reports_are_refused(migrated, bad):
    with TestClient(create_app(migrated)) as c:
        assert post(c, **bad).status_code == 400


def test_reports_are_stored_and_summarised_per_version(migrated, monkeypatch):
    with TestClient(create_app(migrated)) as c:
        for i, total in enumerate((9000, 11000, 20000)):
            assert post(c, turn_id=f"a{i}", marks=call_marks(total), version="2026.10.15.1").status_code == 200
        for i, total in enumerate((1200, 1400, 2800)):
            assert post(c, turn_id=f"b{i}", marks=call_marks(total), version="2026.10.16.1").status_code == 200
        assert post(c, turn_id="b9", outcome="blocked", reason="NotAllowedError", version="2026.10.16.1",
                    marks={"endpoint": 500, "transcribed": 800, "sent": 810, "first_words": 1500}).status_code == 200
        rep = c.get("/api/voice/turns?days=7").json()
    now, before = rep["versions"][0], rep["versions"][1]
    assert now["version"] == "2026.10.16.1" and before["version"] == "2026.10.15.1"
    call_now = now["paths"]["call"]
    assert call_now["n"] == 4 and call_now["played"] == 3 and call_now["steps"]["total_ms"]["p50"] == 1400
    assert before["paths"]["call"]["steps"]["total_ms"]["p95"] == 20000
    assert call_now["failures"] == {"blocked: NotAllowedError": 1}
    last = rep["recent"][0]
    assert last["model"] == "haiku" and last["outcome"] == "blocked" and last["path"] == "call"
    assert rep["targets"]["call"] == {"p50": 1500, "p95": 3000}


def test_the_pc_stamps_the_version_when_the_phone_leaves_it_out(migrated):
    with TestClient(create_app(migrated)) as c:
        assert post(c, version=None).status_code == 200
    with Session(make_engine(migrated.db_url)) as s:
        assert s.get(VoiceTurn, 1).version


def test_old_turns_are_pruned(migrated):
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        s.add(VoiceTurn(ts=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40), version="v", path="call",
                        outcome="played", steps_json="{}"))
        s.add(VoiceTurn(version="v", path="call", outcome="played", steps_json="{}"))
        s.commit()
        assert turns.prune(s) == 1


def test_every_reply_says_which_model_and_how_long_the_pc_took(migrated):
    with TestClient(create_app(migrated)) as c, c.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "message", "text": "hi"})
        while (f := ws.receive_json())["type"] not in ("done", "error"):
            pass
    assert f["type"] == "done"
    assert f["model"] in ("haiku", "sonnet", "opus")
    assert set(f["server"]) >= {"queue_ms", "first_words_ms"} and all(isinstance(v, int) for v in f["server"].values())
