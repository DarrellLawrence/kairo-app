"""The voice lab (owner's request 2026-10-16): measures the installed candidates on the PC and picks
the fastest that calls the tool, keeps the facts and sounds natural. Here: the fake llama server (a
model that calls the tool on data questions), the fake voice, a scripted judge."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlmodel import Session

from app import voicelab, voicellm
from app.brain.voice_local import VoiceModel
from app.db import make_engine
from tests.test_voice_llm import FAKE, fake_hub, free_port
from tests.test_voicelink import FakeVoice


class FakeTurn:
    pad_side = "right"
    failed = None
    last_ms = 3

    def available(self):
        return True

    def probability(self, pcm):          # long audio = a whole sentence = done
        return 0.9 if len(pcm) > 20000 else 0.2


def make_site(monkeypatch, tmp_path):
    fake_hub(monkeypatch)
    voicellm.download(tmp_path / "site", log_=lambda *_: None, free_bytes=lambda: 10 ** 15)
    return tmp_path / "site"


def lab(migrated, tmp_path, site, judge, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    engine = make_engine(migrated.db_url)
    vm = VoiceModel(lambda: None, log_dir=tmp_path / "logs")
    return engine, voicelab.Lab(engine=engine, data_dir=migrated.data_dir, site_dir=site, voice_model=vm, voice=FakeVoice(),
                                smart_turn=FakeTurn(), judge=judge, tools_dir=tmp_path / "tools",
                                server_prefix=lambda binary: [sys.executable, str(FAKE)], port=free_port())


def good_judge(prompt):
    labels = sorted(set(__import__("re").findall(r"## Assistant ([A-Z])", prompt)))
    return json.dumps({lab: {"en": 4.5, "de": 4.0, "note": "natural"} for lab in labels})


def test_the_lab_measures_every_candidate_and_picks_one(migrated, tmp_path, monkeypatch):
    pytest.importorskip("numpy")                    # the speech part needs the voice packages (CI: the voice job)
    site = make_site(monkeypatch, tmp_path)
    engine, lb = lab(migrated, tmp_path, site, good_judge, monkeypatch)
    rep = lb.run()
    assert set(rep["models"]) == set(voicellm.model_ids())
    m = rep["models"]["lfm2.5-2.6b"]
    assert m["tool_recall"] == 1.0 and m["false_calls"] == 0.0 and m["first_token_ms"]["n"] > 0
    assert m["natural"]["en"] == 4.5 and m["load_s"] is not None and "samples" in m
    assert rep["pick"]["model"] in voicellm.model_ids() and "fastest that passes" in rep["pick"]["why"]
    assert rep["speech"]["stt"]["auto"]["ms"]["n"] == 6 and rep["speech"]["stt"]["en"]["ms"]["n"] == 3
    assert rep["speech"]["smart_turn"]["pad_side"] in ("right", "left")
    assert rep["kyutai"]["state"] == "not installed"
    with Session(engine) as s:
        assert voicellm.choice(s)["model"] == rep["pick"]["model"] and voicellm.choice(s)["by"] == "lab"
        assert voicelab.last(s)["pick"] == rep["pick"] and voicelab.state(s)["running"] is False
    assert lb.vm.state != "paused"                                   # the voice model gets the GPU back


def test_a_model_that_never_calls_the_tool_is_not_picked(migrated, tmp_path, monkeypatch):
    site = make_site(monkeypatch, tmp_path)
    _engine, lb = lab(migrated, tmp_path, site, good_judge, monkeypatch, FAKE_LLAMA_NO_TOOLS="1")
    rep = lb.run()                                  # without the voice packages too: then only the speech part is missing
    assert all(m["tool_recall"] == 0.0 for m in rep["models"].values())
    assert rep["pick"]["model"] is None and "no model passed" in rep["pick"]["why"]


def test_the_pick_rule():
    base = {"tool_recall": 1.0, "false_calls": 0.0, "fidelity": 1.0, "natural": {"en": 4.0, "de": 3.5}}
    models = {"fast": {**base, "first_word_ms": {"p50": 120}}, "slow": {**base, "first_word_ms": {"p50": 300}},
              "sloppy": {**base, "tool_recall": 0.5, "first_word_ms": {"p50": 50}},
              "robot": {**base, "natural": {"en": 2.0, "de": 2.0}, "first_word_ms": {"p50": 60}}}
    mid, why = voicelab.pick(models)
    assert mid == "fast" and "sloppy" in why and "robot" in why
    german_off = {"a": {**base, "natural": {"en": 4.0, "de": 1.0}, "first_word_ms": {"p50": 100}}}
    assert voicelab.pick(german_off)[0] is None and voicelab.pick(german_off, "en")[0] == "a"   # English-only mode


def test_facts_and_words():
    assert voicelab.has_facts("School from eight, training at 18:00", (("8", "eight"), ("18", "six")))
    assert not voicelab.has_facts("School tomorrow", (("8", "eight"),))
    assert voicelab.wer("call the dentist tomorrow", "call a dentist tomorrow") == 0.25


def test_resampling():
    np = pytest.importorskip("numpy")               # the voice packages (CI: the voice job)
    x = np.ones(24000, np.float32)
    assert len(voicelab.resample(x, 24000, 16000)) == 16000


def test_the_judge_is_blind_and_its_answer_is_parsed():
    p = voicelab.judge_prompt({"A": {"en": [{"owner": "hi", "kairo": "Hey!"}], "de": []}})
    assert "Assistant A" in p and "lfm" not in p.lower()
    assert voicelab.parse_judge('noise {"A": {"en": 4, "de": 3.5, "note": "ok"}} tail', ["A", "B"]) == {
        "A": {"en": 4.0, "de": 3.5, "note": "ok"}}


def test_the_end_to_end_estimate_adds_up():
    rep = {"speech": {"smart_turn": {"pad_side": "right", "right": {"ms": {"p50": 20}}},
                      "stt": {"auto": {"ms": {"p50": 150}}}, "tts": {"en": {"first_clause_ms": {"p50": 120}}}},
           "models": {"m": {"first_word_ms": {"p50": 90}}}, "pick": {"model": "m"}}
    est = voicelab.estimate(rep)
    assert est == {"endpoint": 220, "stt": 150, "voice_model_first_word": 90, "first_audio": 120, "total": 580}


def test_run_diagnosis_shows_the_lab(migrated, tmp_path, monkeypatch):
    from app import diagnosis
    site = make_site(monkeypatch, tmp_path)
    _engine, lb = lab(migrated, tmp_path, site, good_judge, monkeypatch)
    lb.run()
    text = diagnosis.run(db_url=migrated.db_url, data_dir=migrated.data_dir, app_root=migrated.data_dir / "no-app")["text"]
    section = text.split("Local voice (voice model on the PC")[1].split("Last self-test")[0]
    assert "voice lab" in section and "lfm2.5-2.6b |" in section and "pick" in section


def test_the_turn_detector_reads_whisper_features():
    np = pytest.importorskip("numpy")               # the voice packages (CI: the voice job)
    from app import smartturn
    assert smartturn.features(np.zeros(16000, np.float32)).shape == (1, 80, 800)


def test_without_the_voice_packages_there_is_no_turn_detector(monkeypatch, tmp_path):
    from app import smartturn
    model = tmp_path / "smart-turn.onnx"
    model.write_bytes(b"onnx")
    monkeypatch.setattr(smartturn, "np", None)
    st = smartturn.SmartTurn(model)
    assert not st.available() and st.probability(b"\x00\x00" * 1600) is None   # the quiet rule decides
