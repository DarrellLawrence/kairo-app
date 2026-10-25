"""G3 mouth (overnight build 2026-10-23): the switches (app/flags.py, "g3.*") and where the voice model, the
call and the voice lab read them. The behaviour behind each switch is tested next to its code (test_voice_llm,
test_voice_toolcalls, test_voicemouth, test_speaker_install); here: the defaults, and that each switch reaches the
place that uses it, in both positions."""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import flags, voicelab, voicellm
from app.brain.voice_local import VoiceModel
from app.db import make_engine
from app.main import create_app
from app.voicemouth import g3_options
from tests.test_voice_llm import FAKE, free_port
from tests.test_voice_local import local  # noqa: F401  (the app with the fake voice model)

#            flag                  default  what an owner who turns it on gets
DEFAULTS = {"g3.llama_flags": True, "g3.gemma_markup": True, "g3.swa_full": False, "g3.constant_tools": False,
            "g3.gemma_first": False, "g3.smart_turn_pin": False}


def test_the_g3_switches_have_the_planned_defaults():
    for name, default in DEFAULTS.items():
        assert name in flags.FLAGS and flags.FLAGS[name][0] is default, name
    assert sorted(n for n in flags.FLAGS if n.startswith("g3.")) == sorted(DEFAULTS)


def test_the_options_follow_the_switches_both_ways(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        assert g3_options(s) == {"llama_flags": True, "swa_full": False, "gemma_markup": True, "constant_tools": False}
        flags.set_flag(s, "g3.llama_flags", False)
        flags.set_flag(s, "g3.swa_full", True)
        flags.set_flag(s, "g3.gemma_markup", False)
        flags.set_flag(s, "g3.constant_tools", True)
        assert g3_options(s) == {"llama_flags": False, "swa_full": True, "gemma_markup": False, "constant_tools": True}
        for name in DEFAULTS:
            flags.set_flag(s, name, None)
        assert g3_options(s) == {"llama_flags": True, "swa_full": False, "gemma_markup": True, "constant_tools": False}


def test_the_app_gives_its_voice_model_the_owners_switches(migrated):
    with TestClient(create_app(migrated)) as c:
        vm = c.app.state.voice_llm
        assert vm.read_options() == {"llama_flags": True, "swa_full": False, "gemma_markup": True, "constant_tools": False}
        assert c.put("/api/flags/g3.swa_full", json={"on": True}).status_code == 200
        assert c.put("/api/flags/g3.llama_flags", json={"on": False}).status_code == 200
        assert vm.read_options()["swa_full"] is True and vm.read_options()["llama_flags"] is False


def test_the_voice_lab_starts_its_servers_like_the_real_one(migrated, tmp_path, monkeypatch):
    """The lab measures what will run: it reads the same switches the real server does."""
    from tests.test_voicelab import lab, make_site
    site = make_site(monkeypatch, tmp_path)
    engine, the_lab = lab(migrated, tmp_path, site, None, monkeypatch)
    with Session(engine) as s:
        flags.set_flag(s, "g3.swa_full", True)
    started = []
    real = VoiceModel.read_options

    def seen(self):
        started.append(real(self))
        return started[-1]
    monkeypatch.setattr(VoiceModel, "read_options", seen)
    report = {"models": {}}
    the_lab._models(report, "auto")
    assert started and all(o["swa_full"] is True for o in started)


@pytest.mark.parametrize("on", [False, True])
def test_the_voice_lab_talks_with_the_call_layout_the_switch_says(migrated, tmp_path, monkeypatch, on):
    """The before/after gate (voice lab with the switch off, then on) only means something if the lab uses it."""
    from tests.test_voicelab import lab, make_site
    site = make_site(monkeypatch, tmp_path)
    engine, the_lab = lab(migrated, tmp_path, site, None, monkeypatch)
    with Session(engine) as s:
        flags.set_flag(s, "g3.constant_tools", on)
    made = []
    real = voicelab.Mouth

    class Spy(real):
        def __init__(self, *a, **kw):
            made.append((kw.get("constant_tools"), kw.get("context_block")))
            super().__init__(*a, **kw)
    monkeypatch.setattr(voicelab, "Mouth", Spy)
    the_lab._models({"models": {}}, "auto")
    assert made and set(made) == {(on, on)}


def test_the_voice_model_warms_up_the_start_every_call_shares(migrated):
    """What it reads in advance is exactly the system message a call would start with (same name, same language)."""
    from app.voicemouth import static_system
    with TestClient(create_app(migrated)) as c:
        vm = c.app.state.voice_llm
        name = c.app.state.profile.name
        assert vm.warm_messages() == [{"role": "system", "content": static_system(name, "auto")}]
        assert c.put("/api/voice/realtime/settings", json={"language": "en"}).status_code == 200
        assert vm.warm_messages() == [{"role": "system", "content": static_system(name, "en")}]


# ---------------------------------------------------------------- g3.gemma_first: the pick order (nothing is downloaded or removed)

def fake_site(tmp_path, ids=("lfm2.5-2.6b", "gemma-4-e4b", "qwen3.5-9b")):
    """A complete voicellm folder with these models 'installed' (the manifest's order is lfm, gemma, qwen)."""
    import json
    site = tmp_path / "voicellm-site"
    models = {}
    for mid in ids:
        f = site / "models" / mid / "m.gguf"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        models[mid] = {"file": f.relative_to(site).as_posix()}
    (site / voicellm.INSTALLED).write_text(json.dumps({"models": models, "skipped": []}), encoding="utf-8")
    (site / voicellm.COMPLETE).write_text("ok\n", encoding="ascii")
    return site


def test_without_the_switch_the_first_installed_model_is_still_the_first_in_the_list(migrated, tmp_path):
    site = fake_site(tmp_path)
    with Session(make_engine(migrated.db_url)) as s:
        assert voicellm.model_ids()[0] == "lfm2.5-2.6b"                  # voicellm.json is untouched: LFM stays in it
        assert voicellm.available(site) == ["lfm2.5-2.6b", "gemma-4-e4b", "qwen3.5-9b"]
        assert voicellm.active(s, site) == "lfm2.5-2.6b"


def test_with_the_switch_gemma_is_first_and_the_others_keep_their_order(migrated, tmp_path):
    site = fake_site(tmp_path)
    with Session(make_engine(migrated.db_url)) as s:
        flags.set_flag(s, "g3.gemma_first", True)
        assert voicellm.preferred(s) == "gemma-4-e4b"
        assert voicellm.in_pick_order(voicellm.available(site), voicellm.preferred(s)) == ["gemma-4-e4b", "lfm2.5-2.6b", "qwen3.5-9b"]
        assert voicellm.active(s, site) == "gemma-4-e4b"
        flags.set_flag(s, "g3.gemma_first", None)                         # back to the default: off
        assert voicellm.preferred(s) is None and voicellm.active(s, site) == "lfm2.5-2.6b"


def test_the_switch_changes_nothing_when_gemma_is_not_installed(migrated, tmp_path):
    site = fake_site(tmp_path, ids=("lfm2.5-2.6b", "qwen3.5-9b"))
    with Session(make_engine(migrated.db_url)) as s:
        flags.set_flag(s, "g3.gemma_first", True)
        assert voicellm.in_pick_order(voicellm.available(site), "gemma-4-e4b") == ["lfm2.5-2.6b", "qwen3.5-9b"]
        assert voicellm.active(s, site) == "lfm2.5-2.6b"


def test_a_pick_the_lab_or_the_owner_made_stands_in_both_positions(migrated, tmp_path):
    site = fake_site(tmp_path)
    with Session(make_engine(migrated.db_url)) as s:
        voicellm.set_choice(s, "qwen3.5-9b", "owner")
        for on in (False, True, None):
            flags.set_flag(s, "g3.gemma_first", on)
            assert voicellm.active(s, site) == "qwen3.5-9b"
        voicellm.set_choice(s, None, "lab")                               # nothing passed: still the old voice
        assert voicellm.active(s, site) is None


def test_the_lab_prefers_gemma_when_it_passes_even_if_another_model_is_faster():
    base = {"tool_recall": 1.0, "false_calls": 0.0, "fidelity": 1.0, "natural": {"en": 4.0, "de": 3.5}}
    models = {"lfm2.5-2.6b": {**base, "first_word_ms": {"p50": 90}}, "gemma-4-e4b": {**base, "first_word_ms": {"p50": 140}}}
    assert voicelab.pick(models)[0] == "lfm2.5-2.6b"                      # the old rule: the fastest
    mid, why = voicelab.pick(models, prefer="gemma-4-e4b")
    assert mid == "gemma-4-e4b" and "preferred" in why and "lfm2.5-2.6b" in why and "90" in why
    models["gemma-4-e4b"]["tool_recall"] = 0.2                            # it does not pass: the fastest that does
    mid, why = voicelab.pick(models, prefer="gemma-4-e4b")
    assert mid == "lfm2.5-2.6b" and "gemma-4-e4b" in why
    assert voicelab.pick({"gemma-4-e4b": models["gemma-4-e4b"]}, prefer="gemma-4-e4b")[0] is None
    assert voicelab.pick({"lfm2.5-2.6b": models["lfm2.5-2.6b"]}, prefer="gemma-4-e4b")[0] == "lfm2.5-2.6b"   # not measured


def test_the_lab_reads_the_switch_when_it_starts_a_run(migrated, tmp_path, monkeypatch):
    from tests.test_voicelab import lab, make_site
    site = make_site(monkeypatch, tmp_path)
    engine, the_lab = lab(migrated, tmp_path, site, None, monkeypatch)
    assert the_lab._prefer() is None
    with Session(engine) as s:
        flags.set_flag(s, "g3.gemma_first", True)
    assert the_lab._prefer() == "gemma-4-e4b"


# ---------------------------------------------------------------- g3.smart_turn_pin: which turn-detector file calls load

def test_calls_get_the_downloaded_pinned_file_only_when_the_switch_is_on(migrated, tmp_path, monkeypatch):
    pinned = tmp_path / "smart-turn-v3.2-cpu.onnx"
    monkeypatch.setattr(voicellm, "smart_turn_pinned_path", lambda site: pinned)
    with Session(make_engine(migrated.db_url)) as s:
        assert voicellm.smart_turn_for_calls(s, tmp_path) is None                  # default off: pipecat's own copy
        flags.set_flag(s, "g3.smart_turn_pin", True)
        assert voicellm.smart_turn_for_calls(s, tmp_path) == str(pinned)
        monkeypatch.setattr(voicellm, "smart_turn_pinned_path", lambda site: None)   # not the pinned file / not installed
        assert voicellm.smart_turn_for_calls(s, tmp_path) is None
        flags.set_flag(s, "g3.smart_turn_pin", None)
        assert voicellm.smart_turn_for_calls(s, tmp_path) is None


def test_the_app_hands_a_call_the_model_the_switch_says(local, monkeypatch, tmp_path):
    """/api/rtc/offer reads the switch at the start of each call and gives build_call the file (or None: pipecat's copy)."""
    pytest.importorskip("pipecat")
    pytest.importorskip("aiortc")
    import asyncio
    from app import voicertc
    from tests import test_voice_rtc as rtc
    pinned = tmp_path / "smart-turn-v3.2-cpu.onnx"
    monkeypatch.setattr(voicellm, "smart_turn_pinned_path", lambda site: pinned)
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    local.app.state.voice = rtc.ToneVoice(local.voice)
    seen = []
    original = voicertc.build_call

    def spy(*a, **k):
        seen.append(k.get("smart_turn_model"))
        return original(*a, **{**k, "smart_turn_model": None})        # the stand-in file is not a model: load pipecat's own
    monkeypatch.setattr(voicertc, "build_call", spy)
    asyncio.run(rtc._app_call(local, seconds=2))
    assert local.put("/api/flags/g3.smart_turn_pin", json={"on": True}).status_code == 200
    asyncio.run(rtc._app_call(local, seconds=2))
    assert local.put("/api/flags/g3.smart_turn_pin", json={"on": False}).status_code == 200
    asyncio.run(rtc._app_call(local, seconds=2))
    assert seen == [None, str(pinned), None]


# ---------------------------------------------------------------- the lab counts spoken call markup (the gate: 0 of N)

def test_spoken_markup_is_told_from_ordinary_words():
    said = ["<|tool_call>call:ask_kairo{request:<|\"|>x<|\"|>}<tool_call|>", "Sure.<tool_call|>", "ok <|tool_response>", "a</think>b",
            "call:ask_kairo{request: 'x'}", "[TOOL_CALLS]", "<tool_call>{\"name\": \"ask_kairo\"}"]
    for text in said:
        assert voicelab.spoke_markup(text), text
    for text in ["Sure, I'll call the dentist tomorrow.", "Call: that is what the card says.", "Your tool call count is two.",
                 "Chemistry lab at 10:40 in room 214.", "", None]:
        assert not voicelab.spoke_markup(text), text


def test_the_lab_summary_and_the_pick_know_about_spoken_markup():
    raw = {"ttft": [10], "first_text": [10], "tok_s": [], "cold_ms": {}, "need": 1, "hits": 1, "small": 1, "false": 0, "facts_n": 1,
           "facts_ok": 1, "card_n": 1, "card_pc": 1, "card_wrong": 0, "card_claude": 0, "errors": [], "spoken_n": 40, "markup": 2}
    s = voicelab._summary(raw)
    assert s["markup_spoken"] == 2 and s["spoken_replies"] == 40
    base = {"tool_recall": 1.0, "false_calls": 0.0, "fidelity": 1.0, "natural": {"en": 4.0, "de": 3.5}, "first_word_ms": {"p50": 90}}
    mid, why = voicelab.pick({"a": {**base, "markup_spoken": 2, "spoken_replies": 40}, "b": {**base, "first_word_ms": {"p50": 200},
                                                                                          "markup_spoken": 0}}, refuse_markup=True)
    assert mid == "b" and "markup" in why and "2" in why
    assert voicelab.pick({"a": {**base}})[0] == "a"                       # an older report without the number is fine


def test_the_markup_rule_only_acts_when_gemma_first_is_on():
    """Leaving a model out for spoken markup moves the voice model: it is part of the owner's Gemma decision (16.6), not a side effect.
    Switch off (the default): the lab picks exactly as before, and the count is still reported."""
    base = {"tool_recall": 1.0, "false_calls": 0.0, "fidelity": 1.0, "natural": {"en": 4.0, "de": 3.5}}
    models = {"lfm2.5-2.6b": {**base, "first_word_ms": {"p50": 90}, "markup_spoken": 1, "spoken_replies": 40},
              "gemma-4-e4b": {**base, "first_word_ms": {"p50": 140}, "markup_spoken": 0, "spoken_replies": 40}}
    mid, why = voicelab.pick(models)                                      # off: nothing about markup, the fastest wins as before
    assert mid == "lfm2.5-2.6b" and "markup" not in why
    mid, why = voicelab.pick(models, refuse_markup=True)                  # on: LFM is left out, Gemma is picked
    assert mid == "gemma-4-e4b" and "markup" in why and "lfm2.5-2.6b" in why
    assert voicelab.pick({"lfm2.5-2.6b": models["lfm2.5-2.6b"]}, refuse_markup=True)[0] is None
    assert voicelab.pick({"lfm2.5-2.6b": models["lfm2.5-2.6b"]})[0] == "lfm2.5-2.6b"


@pytest.mark.parametrize("on", [False, True])
def test_the_lab_run_leaves_out_a_model_that_spoke_markup_only_with_gemma_first(migrated, tmp_path, monkeypatch, on):
    from tests.test_voicelab import lab, make_site
    pytest.importorskip("numpy")
    site = make_site(monkeypatch, tmp_path)
    engine, the_lab = lab(migrated, tmp_path, site, None, monkeypatch)
    monkeypatch.setattr(voicelab, "spoke_markup", lambda text: True)      # every reply counts as spoken markup
    with Session(engine) as s:
        flags.set_flag(s, "g3.gemma_first", on)
    rep = the_lab.run()
    assert all(m["markup_spoken"] == m["spoken_replies"] > 0 for m in rep["models"].values())    # reported either way
    if on:
        assert rep["pick"]["model"] is None and "spoke tool-call markup" in rep["pick"]["why"]
    else:
        assert rep["pick"]["model"] is not None and "markup" not in rep["pick"]["why"]
        with Session(engine) as s:
            assert voicellm.active(s, site) == rep["pick"]["model"]


def test_the_lab_run_reports_how_many_replies_carried_markup(migrated, tmp_path, monkeypatch):
    from tests.test_voicelab import lab, make_site
    pytest.importorskip("numpy")
    site = make_site(monkeypatch, tmp_path)
    engine, the_lab = lab(migrated, tmp_path, site, None, monkeypatch)
    rep = the_lab.run()
    m = rep["models"]["lfm2.5-2.6b"]
    assert m["markup_spoken"] == 0 and m["spoken_replies"] > 0
