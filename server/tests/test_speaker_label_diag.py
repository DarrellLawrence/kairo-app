"""G13: `kairo diag` and the Voice check page say when voice requests are unchecked (no voiceprint, or the check is off):
then every voice request can change things, as before, and that is a WARN. Written before the code."""

from __future__ import annotations

import json

from sqlmodel import Session

from app import flags
from app import speaker as sp
from app.db import make_engine
from tests.test_diag import comp, pc, run, voice_parts, vl  # noqa: F401  (fixtures: C:\Kairo in miniature)
from tests.test_speaker import PitchModel, taught

FLAG = "g13.speaker_label_gate"


def gate_row(**kw):
    state = "on" if kw.get("checked", True) else kw.pop("state", "off")
    return {"flag": True, "checked": True, "state": state, "note": None, **kw}


def speaker(**kw):
    return {**comp("cuda"), "on": True, "label_gate": gate_row(**kw)}


def test_confirmed_voices_only_is_a_pass(pc, vl):
    _, by_id = run(pc, live=voice_parts(speaker=speaker()))
    c = by_id["speaker.label_gate"]
    assert c["status"] == "PASS" and "changes nothing" in c["detail"]


def test_no_voiceprint_means_unchecked_and_is_a_warn_that_says_why(pc, vl):
    _, by_id = run(pc, live=voice_parts(speaker={**comp("cuda"), "on": False,
                                                 "label_gate": gate_row(checked=False, note="until you teach Kairo your voice (Settings → Voice)")}))
    c = by_id["speaker.label_gate"]
    assert c["status"] == "WARN" and "unchecked" in c["detail"] and "until you teach Kairo your voice" in c["detail"]
    assert "can change things" in c["detail"]


def test_the_check_switched_off_is_a_warn_too(pc, vl):
    _, by_id = run(pc, live=voice_parts(speaker={**comp("cuda"), "on": False,
                                                 "label_gate": gate_row(checked=False, note="set to off (Settings → Voice)")}))
    assert by_id["speaker.label_gate"]["status"] == "WARN" and "set to off" in by_id["speaker.label_gate"]["detail"]


def test_a_taught_voice_whose_check_cannot_run_is_a_fail_that_says_what_happens(pc, vl):
    """Review finding: voiceprint taught, setting not off, but the check cannot run. Every voice request is then
    unsure: nothing it asks changes anything. That is the owner's problem to fix, so diag says it plainly."""
    _, by_id = run(pc, live=voice_parts(speaker={**comp("cuda"), "on": False, "label_gate": gate_row(
        checked=False, state="broken", note="the model failed: ORT self-check failed")}))
    c = by_id["speaker.label_gate"]
    assert c["status"] == "FAIL" and "the model failed: ORT self-check failed" in c["detail"]
    assert "confirmed as yours" in c["detail"] and "change nothing" in c["detail"] and "off" in c["detail"]


def test_the_flag_off_is_a_warn_whatever_the_check_does(pc, vl):
    _, by_id = run(pc, live=voice_parts(speaker=speaker(flag=False)))
    c = by_id["speaker.label_gate"]
    assert c["status"] == "WARN" and FLAG in c["detail"]


def test_an_older_server_that_does_not_say_adds_nothing(pc, vl):
    _, by_id = run(pc, live=voice_parts(speaker={**comp("cuda"), "on": True}))
    assert "speaker.label_gate" not in by_id
    _, by_id = run(pc)                                        # no server at all
    assert "speaker.label_gate" not in by_id


def test_the_check_belongs_to_the_speaker_group(pc, vl):
    _, by_id = run(pc, live=voice_parts(speaker=speaker()))
    assert by_id["speaker.label_gate"]["group"] == "Speaker check (CAM++)"


# ---------------------------------------------------------------- the server's own account of it (Speaker.device)

def test_the_device_row_carries_the_gate_state(tmp_path, migrated):
    engine = make_engine(migrated.db_url)
    s = sp.Speaker(PitchModel(), tmp_path / "voiceprint.bin", engine)          # not taught
    row = s.device()["label_gate"]
    assert row == {"flag": True, "checked": False, "state": "off", "note": "until you teach Kairo your voice (Settings → Voice)"}
    s2 = taught(tmp_path / "t", engine)
    assert s2.device()["label_gate"] == {"flag": True, "checked": True, "state": "on", "note": None}
    s2.model.failed = "ORT self-check failed"
    assert s2.device()["label_gate"] == {"flag": True, "checked": False, "state": "broken",
                                         "note": "the model failed: ORT self-check failed"}
    s2.model.failed = None
    with Session(engine) as db:
        flags.set_flag(db, FLAG, False)
    assert s2.device()["label_gate"]["flag"] is False
    s3 = sp.Speaker(PitchModel(), tmp_path / "x" / "voiceprint.bin")           # no database: the flag's default
    assert s3.device()["label_gate"]["flag"] is True


def test_the_device_row_never_carries_a_voiceprint(tmp_path, migrated):
    s = taught(tmp_path, make_engine(migrated.db_url))
    assert "vector" not in json.dumps(s.device())
