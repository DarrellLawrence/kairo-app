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

#            flag                  default  what an owner who turns it on gets
DEFAULTS = {"g3.llama_flags": True, "g3.gemma_markup": True, "g3.swa_full": False, "g3.constant_tools": False}


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
        assert vm.read_options() == {"llama_flags": True, "swa_full": False, "gemma_markup": True}
        assert c.put("/api/flags/g3.swa_full", json={"on": True}).status_code == 200
        assert c.put("/api/flags/g3.llama_flags", json={"on": False}).status_code == 200
        assert vm.read_options()["swa_full"] is True and vm.read_options()["llama_flags"] is False


def test_the_voice_lab_starts_its_servers_like_the_real_one(migrated, tmp_path, monkeypatch):
    """The lab measures what will run: same flags (it used to have its own, older, args)."""
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
