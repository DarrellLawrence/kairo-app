"""The call path's speech to text (voice plan G2): which options Whisper gets, which language, which
model file and compute type, the warm-up, and the owner's switches (app/flags.py, names g2.*).

faster-whisper is a stand-in here (recorded calls); the real library is only needed by the one test that
pins the internals the single-encode language detection relies on (it runs where faster-whisper is installed).
"""

from __future__ import annotations

import sys
import types

import pytest
from sqlmodel import Session

from app import flags, voice as voice_mod
from app.db import make_engine
from app.voice import NotInstalled, Voice

PCM = b"\x10\x00" * 16000           # one second of 16 kHz 16-bit mono (content does not matter to the stand-in)


class Whisper:
    """A stand-in faster_whisper module. .made: how each model was built; .calls: each transcribe()."""

    def __init__(self, monkeypatch, *, fail_on=(), language="en", text=" hello"):
        self.made, self.calls = [], []
        self.fail_on, self.language, self.text = set(fail_on), language, text
        outer = self
        mod = types.ModuleType("faster_whisper")

        class WhisperModel:
            def __init__(self, path, device="cpu", compute_type="default", **kw):
                if (device, compute_type) in outer.fail_on:
                    raise ValueError(f"no {compute_type} on {device}")
                outer.made.append({"path": str(path), "device": device, "compute": compute_type, **kw})
                self.device, self.compute = device, compute_type

            def transcribe(self, audio, **kw):
                outer.calls.append(kw)
                info = types.SimpleNamespace(language=kw.get("language") or outer.language, duration=1.0)
                return iter([types.SimpleNamespace(text=outer.text)]), info

        mod.WhisperModel = WhisperModel
        monkeypatch.setitem(sys.modules, "faster_whisper", mod)
        monkeypatch.setattr(Voice, "installed", staticmethod(lambda: {"stt": True, "en": False, "de": False}))


@pytest.fixture
def whisper(monkeypatch):
    return Whisper(monkeypatch)


def put_models(root, *names):
    """The folders Update Kairo leaves behind, so loading needs no Hugging Face lookup."""
    for name in names:
        snap = root / "whisper" / f"models--Systran--faster-whisper-{name}" / "snapshots" / "abc"
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "model.bin").write_bytes(b"x")


@pytest.fixture
def voice(tmp_path, whisper):
    put_models(tmp_path, voice_mod.WHISPER_GPU, voice_mod.WHISPER_CPU)
    return Voice(tmp_path)


# ---------------------------------------------------------------- the owner's switches

def g2_flags():
    return sorted(n for n in flags.FLAGS if n.startswith("g2."))


def test_a_call_reads_each_switch_once_and_a_voice_without_a_call_uses_the_defaults(migrated, tmp_path):
    v = Voice(tmp_path)
    assert {n: getattr(v.tuning, n[3:]) for n in g2_flags()} == {n: flags.FLAGS[n][0] for n in g2_flags()}
    with Session(make_engine(migrated.db_url)) as s:
        for name in g2_flags():
            flags.set_flag(s, name, not flags.FLAGS[name][0])      # the owner flips every one
        v.read_flags(s)
        assert {n: getattr(v.tuning, n[3:]) for n in g2_flags()} == {n: not flags.FLAGS[n][0] for n in g2_flags()}
        for name in g2_flags():
            flags.set_flag(s, name, None)                         # ... and back to the defaults
        v.read_flags(s)
        assert {n: getattr(v.tuning, n[3:]) for n in g2_flags()} == {n: flags.FLAGS[n][0] for n in g2_flags()}


# ---------------------------------------------------------------- what Whisper is asked on a call

def test_a_spoken_turn_is_decoded_once_greedily_at_temperature_zero_with_the_voice_filter_on(voice, whisper):
    assert voice.transcribe_pcm(PCM, "en").text == "hello"
    (kw,) = whisper.calls
    assert kw["beam_size"] == 1 and kw["temperature"] == 0.0 and kw["vad_filter"] is True    # VAD stays: off = 12/12 hallucinations
    assert kw["without_timestamps"] is True and kw["condition_on_previous_text"] is False


def test_temperature_zero_is_a_fix_so_it_is_on_until_the_owner_says_otherwise(tmp_path):
    assert flags.FLAGS["g2.temperature_zero"][0] is True and Voice(tmp_path).tuning.temperature_zero is True


def test_temperature_zero_off_leaves_whisper_its_own_retries(voice, whisper):
    voice.tuning = voice_mod.Tuning(temperature_zero=False)
    voice.transcribe_pcm(PCM, "en")
    assert "temperature" not in whisper.calls[0] and whisper.calls[0]["vad_filter"] is True


def test_a_recording_is_still_decoded_with_beam_five_and_never_at_temperature_zero(voice, whisper):
    voice.transcribe(b"m4a bytes", language="en")
    (kw,) = whisper.calls
    assert kw["beam_size"] == 5 and "temperature" not in kw and kw["vad_filter"] is True


# ---------------------------------------------------------------- which language

@pytest.mark.parametrize("mode,expected", [("en", "en"), ("de", "de"), ("auto", None), (None, None)])
def test_an_explicit_language_is_passed_to_whisper_and_auto_is_left_to_it(voice, whisper, mode, expected):
    voice.transcribe_pcm(PCM, mode)
    assert whisper.calls[0]["language"] == expected


def test_german_comes_back_as_german(voice, whisper):
    assert voice.transcribe_pcm(PCM, "de").language == "de"
