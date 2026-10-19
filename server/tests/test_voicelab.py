"""The voice lab (owner's request 2026-10-16): measures the installed candidates on the PC and picks
the fastest that calls the tool, keeps the facts and sounds natural. Here: the fake llama server (a
model that calls the tool on data questions), the fake voice, a scripted judge."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from pathlib import Path

import pytest
from sqlmodel import Session

from app import voicelab, voicellm
from app.brain.voice_local import VoiceModel
from app.db import SettingKV, make_engine
from tests.test_voice_llm import FAKE, fake_hub, free_port
from tests.test_voice_local import local  # noqa: F401  (the app with the fake voice model)
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


def on_the_gpu(monkeypatch, ok: bool = True):
    """This PC has an NVIDIA card; llama.cpp's log says all layers on it (ok) or none (tests/test_gpu.py: the log)."""
    from app import gpu
    monkeypatch.setattr(gpu, "present", lambda: True)
    monkeypatch.setattr(gpu, "llama_device", lambda path: (
        {"device": "gpu", "layers": "31/31", "gpu": "RTX 3060", "ok": True, "reason": None} if ok else
        {"device": "cpu", "layers": "0/31", "gpu": None, "ok": False, "reason": "0/31 layers on the GPU"}))


def test_the_lab_measures_every_candidate_and_picks_one(migrated, tmp_path, monkeypatch):
    pytest.importorskip("numpy")                    # the speech part needs the voice packages (CI: the voice job)
    site = make_site(monkeypatch, tmp_path)
    engine, lb = lab(migrated, tmp_path, site, good_judge, monkeypatch)
    rep = lb.run()
    assert set(rep["models"]) == set(voicellm.model_ids())
    m = rep["models"]["lfm2.5-2.6b"]
    assert m["tool_recall"] == 1.0 and m["false_calls"] == 0.0 and m["first_token_ms"]["n"] > 0
    assert m["card_on_pc"] == 1.0 and m["card_wrong"] == 0.0 and m["card_via_claude"] == 0.0   # the card: no Claude
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
    on_the_gpu(monkeypatch)
    _engine, lb = lab(migrated, tmp_path, site, good_judge, monkeypatch, FAKE_LLAMA_NO_TOOLS="1")
    rep = lb.run()                                  # without the voice packages too: then only the speech part is missing
    assert all(m["tool_recall"] == 0.0 for m in rep["models"].values())
    assert rep["pick"]["model"] is None and "no model passed" in rep["pick"]["why"]
    assert rep["on_gpu"] is True and rep["off_gpu"] == [] and rep["by"] == "owner"
    assert rep["models"]["lfm2.5-2.6b"]["device"] == {"device": "gpu", "layers": "31/31", "ok": True, "reason": None}
    with Session(_engine) as s:                       # owner's decision 2026-10-16: then the old voice
        assert voicellm.active(s, site) is None and voicellm.lab_rejected(voicellm.choice(s))
        voicellm.set_choice(s, "gemma-4-e4b", "owner")   # until they pick one themselves
        assert voicellm.active(s, site) == "gemma-4-e4b"


def test_a_run_off_the_gpu_never_switches_the_voice_off(migrated, tmp_path, monkeypatch):
    """Owner's report 2026-10-19: on the CPU every model is too slow; that says nothing about the GPU."""
    site = make_site(monkeypatch, tmp_path)
    on_the_gpu(monkeypatch, ok=False)                 # the card is there, llama.cpp ran on the CPU
    engine, lb = lab(migrated, tmp_path, site, good_judge, monkeypatch, FAKE_LLAMA_NO_TOOLS="1")
    with Session(engine) as s:
        voicellm.set_choice(s, "gemma-4-e4b", "owner")
    rep = lb.run()
    assert rep["pick"]["model"] is None and "measured off the GPU" in rep["pick"]["why"]
    assert rep["on_gpu"] is False and any("0/31 layers on the GPU" in w for w in rep["off_gpu"])
    with Session(engine) as s:                        # the voice stays as it was
        assert voicellm.choice(s)["model"] == "gemma-4-e4b" and voicellm.active(s, site) == "gemma-4-e4b"
    from app import diagnosis
    text = diagnosis.run(db_url=migrated.db_url, data_dir=migrated.data_dir, app_root=migrated.data_dir / "no-app")["text"]
    assert "on the GPU: NO - " in text and "cpu 0/31" in text


def test_whisper_on_the_cpu_means_the_run_was_not_on_the_gpu(monkeypatch):
    from app import gpu
    monkeypatch.setattr(gpu, "present", lambda: True)
    models = {"m": {"device": {"device": "gpu", "layers": "31/31", "ok": True}}, "big": {"error": "not installed: disk"}}
    assert voicelab.ran_on_gpu(models, {"whisper": {"loaded": True, "ok": True}, "piper": {"loaded": True, "ok": False}}) == (True, [])
    ok, why = voicelab.ran_on_gpu(models, {"whisper": {"loaded": True, "ok": False, "reason": "on the CPU: cublas64_12.dll"}})
    assert not ok and why == ["whisper: on the CPU: cublas64_12.dll"]
    monkeypatch.setattr(gpu, "present", lambda: False)
    assert voicelab.ran_on_gpu(models, {}) == (False, ["no NVIDIA driver on this PC"])


def test_a_model_that_ignores_the_card_is_not_picked(migrated, tmp_path, monkeypatch):
    site = make_site(monkeypatch, tmp_path)
    _engine, lb = lab(migrated, tmp_path, site, good_judge, monkeypatch, FAKE_LLAMA_NO_CARD="1")
    rep = lb.run()
    m = rep["models"]["lfm2.5-2.6b"]
    assert m["card_on_pc"] == 0.0 and m["card_wrong"] > 0.5            # it made something up instead
    assert rep["pick"]["model"] is None and "wrong from the card" in rep["pick"]["why"]


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


# ---------------------------------------------------------------- the lab by itself (owner's report 2026-10-19: it never ran)

def _jobs(local, monkeypatch, run_job=None):
    """run_job recorded instead of a 10-minute lab; a voice model is installed."""
    calls = []

    def fake(name, forced=False, **kw):
        calls.append((name, forced, kw))
        return types.SimpleNamespace(status="skipped", error="a call is on")
    monkeypatch.setattr(local.app.state.jobs, "run_job", run_job or fake)
    monkeypatch.setattr(voicellm, "available", lambda site: ["lfm2.5-2.6b"])
    return calls


def _put_last(local, report: dict) -> None:
    with Session(make_engine(local.settings.db_url)) as s:
        row = s.get(SettingKV, voicelab.LAST_KEY) or SettingKV(key=voicelab.LAST_KEY)
        row.value = json.dumps(report)
        s.add(row)
        s.commit()


def _step(local):
    return asyncio.run(local.app.state.lab_step())


def test_a_run_cut_off_by_a_restart_is_cleared_at_start(migrated):
    from fastapi.testclient import TestClient
    from app.main import create_app
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        s.add(SettingKV(key=voicelab.STATE_KEY, value=json.dumps({"running": True, "step": "model LFM",
                                                                  "at": "2026-10-18T09:00:00+00:00"})))
        s.commit()
    app = create_app(migrated)                        # Kairo starts: nothing can be running
    with Session(engine) as s:
        st = voicelab.state(s)
    assert st["running"] is False and "cut off by a restart" in st["step"]
    with TestClient(app) as c:                        # Run isn't blocked by the old flag (here: nothing installed)
        r = c.post("/api/voice/lab/run", json={})
    assert r.status_code == 409 and "no voice model is installed" in r.json()["detail"]


def test_it_waits_while_the_voice_is_off_the_gpu_says_why_and_runs_once_it_is_on(local, monkeypatch, caplog):
    from tests.test_voice_devices import DeviceVoice
    calls = _jobs(local, monkeypatch)
    on_the_gpu(monkeypatch)
    local.vm.pause()
    assert "the voice model is not ready (paused)" in _step(local) and calls == []
    local.vm.resume()
    assert local.vm.wait_ready(20)
    whisper = {"device": "cpu", "loaded": True, "ok": False, "reason": "on the CPU: Library cublas64_12.dll is not found"}
    local.app.state.voice = DeviceVoice(local.voice, {"whisper": whisper, "kokoro": {"loaded": True, "ok": True}})
    caplog.set_level(logging.INFO, logger="kairo")
    for _ in range(3):
        why = _step(local)
        assert "off the GPU" in why and "cublas64_12.dll" in why
    assert calls == [] and sum("cublas64_12.dll" in r.getMessage() for r in caplog.records) == 1   # logged once
    assert "cublas64_12.dll" in local.get("/api/voice/lab").json()["state"]["waiting"]          # the Voice lab page says why
    assert "voice lab waits to run by itself: the voice is off the GPU" in local.post("/api/diagnosis").json()["text"]
    local.app.state.runner._lines = 1                                                          # the GPU fix, during a call
    whisper.update(device="cuda", ok=True, reason=None)
    assert _step(local) == "a call is on" and calls == []
    local.app.state.runner._lines = 0
    assert _step(local) is None
    assert calls == [("voice_lab", True, {"by": "auto"})]
    assert "waiting" not in local.get("/api/voice/lab").json()["state"]


def test_a_report_made_off_the_gpu_runs_again_once_the_gpu_is_there(local, monkeypatch):
    from app import gpu
    calls = _jobs(local, monkeypatch)
    _put_last(local, {"at": "2026-10-18T10:00:00+00:00", "by": "owner", "on_gpu": False, "models": {}})
    monkeypatch.setattr(gpu, "present", lambda: False)        # no NVIDIA card: that report is as good as it gets
    assert _step(local) is None and calls == []
    on_the_gpu(monkeypatch)
    assert _step(local) is None and calls == [("voice_lab", True, {"by": "auto"})]
    _put_last(local, {"at": "2026-10-19T10:00:00+00:00", "by": "auto", "on_gpu": False, "gpu": {"name": "RTX 3060"},
                      "models": {}})                           # its own run on the card: not over and over
    assert _step(local) is None and len(calls) == 1
    _put_last(local, {"at": "2026-10-19T11:00:00+00:00", "by": "owner", "on_gpu": True, "models": {}})
    assert _step(local) is None and len(calls) == 1
    _put_last(local, {"at": "2026-10-17T10:00:00+00:00", "models": {}})   # a run from before the GPU was recorded
    assert _step(local) is None and len(calls) == 2


def test_the_job_skips_when_the_owners_run_got_there_first(local, monkeypatch):
    on_the_gpu(monkeypatch)
    _put_last(local, {"at": "2026-10-19T11:00:00+00:00", "by": "owner", "on_gpu": True, "models": {}})
    run = local.app.state.jobs.run_job("voice_lab", forced=True, by="auto")
    assert run.status == "skipped" and "already ran" in run.error


def test_the_lab_by_itself_never_gives_up(local, monkeypatch):
    """The old loop stopped after 3 tries that left no report (a call, a busy Claude) and died on an error."""
    from app import main
    tries = []

    def run_job(name, forced=False, **kw):
        tries.append(kw)
        if len(tries) == 1:
            raise RuntimeError("the first try breaks")
        return types.SimpleNamespace(status="skipped", error="a call is on")
    _jobs(local, monkeypatch, run_job)
    monkeypatch.setattr(main, "LAB_FIRST_CHECK_S", 0)
    monkeypatch.setattr(main, "LAB_CHECK_EVERY_S", 0.01)

    async def go():
        try:
            await asyncio.wait_for(local.app.state.lab_auto(), 1.5)
        except TimeoutError:
            pass
    asyncio.run(go())
    assert len(tries) > 3 and all(kw == {"by": "auto"} for kw in tries)
