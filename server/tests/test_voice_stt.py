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
                self.model = types.SimpleNamespace(compute_type="float16" if compute_type == "default" else compute_type)

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
def db(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        yield s


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


# ---------------------------------------------------------------- the model files (offline at run time)

def test_the_models_load_from_the_installers_folder_and_never_go_online(voice, whisper):
    voice.transcribe_pcm(PCM, "en")
    (made,) = whisper.made
    assert made["local_files_only"] is True and made["path"].replace("\\", "/").endswith("snapshots/abc")
    assert made["device"] == "cuda"


def test_a_model_the_installer_did_not_download_is_reported_not_fetched_in_the_middle_of_a_turn(tmp_path, whisper):
    v = Voice(tmp_path)                                    # no model folders at all
    with pytest.raises(NotInstalled) as e:
        v.transcribe_pcm(PCM, "en")
    assert "Update Kairo" in str(e.value)
    assert whisper.made == []                              # nothing was asked for by name: no download


def test_with_only_the_small_model_downloaded_the_call_runs_on_the_cpu_and_says_why(tmp_path, whisper):
    put_models(tmp_path, voice_mod.WHISPER_CPU)
    v = Voice(tmp_path)
    assert v.transcribe_pcm(PCM, "en").text == "hello"
    assert [m["device"] for m in whisper.made] == ["cpu"]
    w = v.devices()["whisper"]
    assert w["ok"] is False and "not downloaded" in w["reason"] and voice_mod.WHISPER_GPU in w["reason"]


def test_the_folder_lookup_never_raises_for_the_report(tmp_path, voice):
    assert voice.model_folder(voice_mod.WHISPER_GPU).name == "abc"
    assert Voice(tmp_path / "nowhere").model_folder(voice_mod.WHISPER_GPU) is None


def test_offline_models_is_a_fix_so_it_is_on_until_the_owner_says_otherwise(tmp_path):
    assert flags.FLAGS["g2.offline_models"][0] is True and Voice(tmp_path).tuning.offline_models is True


def test_with_offline_models_off_a_missing_model_is_asked_for_by_name_as_before(tmp_path, whisper):
    v = Voice(tmp_path)
    v.tuning = voice_mod.Tuning(offline_models=False)
    v.transcribe_pcm(PCM, "en")
    (made,) = whisper.made
    assert made["path"] == voice_mod.WHISPER_GPU and made["local_files_only"] is False


# ---------------------------------------------------------------- compute type (float16 unless the PC test says otherwise)

def test_int8_float16_waits_for_the_pc_test_so_it_is_off_until_the_owner_says_otherwise(tmp_path):
    assert flags.FLAGS["g2.int8_float16"][0] is False and Voice(tmp_path).tuning.int8_float16 is False


def test_the_gpu_model_is_float16_and_the_cpu_model_int8_by_default(voice, whisper, monkeypatch):
    monkeypatch.delenv("KAIRO_WHISPER_COMPUTE", raising=False)
    voice.transcribe_pcm(PCM, "en")
    assert [(m["device"], m["compute"]) for m in whisper.made] == [("cuda", "float16")]
    whisper.fail_on = {("cuda", "float16")}
    cpu = Voice(voice.models_dir)
    cpu.transcribe_pcm(PCM, "en")
    assert [(m["device"], m["compute"]) for m in whisper.made[1:]] == [("cpu", "int8")]


def test_the_flag_asks_the_gpu_for_int8_float16(voice, whisper, monkeypatch):
    monkeypatch.delenv("KAIRO_WHISPER_COMPUTE", raising=False)
    voice.tuning = voice_mod.Tuning(int8_float16=True)
    voice.transcribe_pcm(PCM, "en")
    assert whisper.made[0]["compute"] == "int8_float16"
    assert voice.devices()["whisper"]["compute_type"] == "int8_float16"


def test_the_environment_variable_is_the_owners_explicit_choice_and_wins_over_the_flag(voice, whisper, monkeypatch):
    monkeypatch.setenv("KAIRO_WHISPER_COMPUTE", "int8_float16")
    voice.transcribe_pcm(PCM, "en")
    assert whisper.made[0]["compute"] == "int8_float16"
    monkeypatch.setenv("KAIRO_WHISPER_COMPUTE", "float16")
    other = Voice(voice.models_dir)
    other.tuning = voice_mod.Tuning(int8_float16=True)
    other.transcribe_pcm(PCM, "en")
    assert whisper.made[1]["compute"] == "float16"


def test_a_gpu_that_cannot_do_int8_float16_keeps_float16_on_the_gpu_and_says_so(voice, whisper, monkeypatch):
    monkeypatch.delenv("KAIRO_WHISPER_COMPUTE", raising=False)
    whisper.fail_on = {("cuda", "int8_float16")}
    voice.tuning = voice_mod.Tuning(int8_float16=True)
    assert voice.transcribe_pcm(PCM, "en").text == "hello"
    assert [(m["device"], m["compute"]) for m in whisper.made] == [("cuda", "float16")]     # not the slow small CPU model
    w = voice.devices()["whisper"]
    assert w["ok"] is True and w["compute_type"] == "float16" and "int8_float16" in w["compute_note"]


# ---------------------------------------------------------------- what devices() reports

def test_devices_report_the_compute_type_the_model_runs_in_and_the_ctranslate2_version(voice, whisper, monkeypatch):
    fake = types.ModuleType("ctranslate2")
    fake.__version__ = "4.8.3"
    monkeypatch.setitem(sys.modules, "ctranslate2", fake)
    monkeypatch.setenv("KAIRO_WHISPER_COMPUTE", "default")           # the model file's own: ctranslate2 says which that is
    voice.transcribe_pcm(PCM, "en")
    w = voice.devices()["whisper"]
    assert w["compute_type"] == "float16" and w["ctranslate2"] == "4.8.3" and w["compute_note"] is None


def test_devices_before_the_model_is_loaded_still_name_the_version_without_importing_the_library(tmp_path, monkeypatch):
    monkeypatch.setattr(Voice, "installed", staticmethod(lambda: {"stt": True, "en": False, "de": False}))
    monkeypatch.delitem(sys.modules, "ctranslate2", raising=False)
    monkeypatch.setattr(voice_mod, "_package_version", lambda name: "4.8.3" if name == "ctranslate2" else None)
    w = Voice(tmp_path).devices()["whisper"]
    assert w["loaded"] is False and w["compute_type"] is None and w["ctranslate2"] == "4.8.3"
    assert "ctranslate2" not in sys.modules


def test_the_latency_harness_records_the_compute_type_the_pc_reported():
    from app import bench_voice
    comps = {"components": {"whisper": {"device": "cuda", "model": "large-v3-turbo", "compute_type": "int8_float16"}}}
    assert bench_voice.config_info(comps)["stt_compute_type"] == "int8_float16"
    comps["components"]["whisper"].pop("compute_type")            # an older Kairo that does not say: as before
    assert bench_voice.config_info(comps)["stt_compute_type"] == "float16"


# ---------------------------------------------------------------- warm-up at start and at each call connect

class Voices:
    """speak() with a tone for each language, as Kairo's own voices would; .said records the requests."""

    def __init__(self, v, *, de=True):
        import io
        import math
        import wave
        self.said, self.de = [], de
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"".join(int(6000 * math.sin(i / 7)).to_bytes(2, "little", signed=True) for i in range(24000 * 2)))
        self.wav = buf.getvalue()
        v.speak = self.speak

    def speak(self, text, language=None):
        self.said.append((text, language))
        return self.wav, ("en" if language == "de" and not self.de else language)


def test_warm_at_connect_is_a_fix_so_it_is_on_until_the_owner_says_otherwise(tmp_path):
    assert flags.FLAGS["g2.warm_at_connect"][0] is True and Voice(tmp_path).tuning.warm_at_connect is True


def test_a_call_connecting_warms_whisper_with_one_sentence_in_each_language_through_the_call_path(voice, whisper, db):
    voices = Voices(voice)
    voice.call_started(db).join(20)
    assert [kw["language"] for kw in whisper.calls] == ["en", "de"]
    assert all(kw["vad_filter"] is True and kw["beam_size"] == 1 and kw["temperature"] == 0.0 for kw in whisper.calls)
    assert [lang for _, lang in voices.said] == ["en", "de"]
    assert voice.warm_ms["whisper"] >= 0


def test_the_warm_sentences_are_made_once_and_not_again_at_the_next_call(voice, whisper):
    voices = Voices(voice)
    voice.warm_call()
    voice._last_decode = 0.0                                # long ago: the next call warms again, from the same audio
    voice.warm_call()
    assert len(voices.said) == 2 and len(whisper.calls) == 4


def test_a_call_that_connects_right_after_a_decode_does_not_warm_again(voice, whisper):
    Voices(voice)
    voice.transcribe_pcm(PCM, "en")
    whisper.calls.clear()
    voice.warm_call()
    assert whisper.calls == []                              # the GPU just worked: it is warm


def test_the_owner_already_talking_comes_first(voice, whisper):
    Voices(voice)
    voice._active = 1                                       # a real sentence is being written down
    voice.warm_call()
    assert whisper.calls == []


def test_without_the_german_voice_only_english_is_warmed(voice, whisper):
    Voices(voice, de=False)
    voice.warm_call()
    assert [kw["language"] for kw in whisper.calls] == ["en"]


def test_without_any_voice_a_second_of_noise_still_warms_the_kernels(voice, whisper):
    voice.speak = lambda text, language=None: (_ for _ in ()).throw(NotInstalled("no voice"))
    voice.warm_call()
    assert [kw["language"] for kw in whisper.calls] == ["en"] and whisper.calls[0]["vad_filter"] is False


def test_warm_at_connect_off_leaves_the_model_cold_until_the_first_sentence(voice, whisper, db):
    Voices(voice)
    flags.set_flag(db, "g2.warm_at_connect", False)
    assert voice.call_started(db) is None and voice.tuning.warm_at_connect is False
    assert whisper.calls == [] and whisper.made == []


def test_the_start_up_warm_up_uses_speech_and_the_call_path_too(voice, whisper, monkeypatch):
    voices = Voices(voice)
    monkeypatch.setattr(voice, "_load_kokoro", lambda: None)
    voice.warm()
    assert [kw["language"] for kw in whisper.calls] == ["en", "de"] and whisper.calls[0]["vad_filter"] is True
    assert voice.warm_ms["whisper"] >= 0 and ("Hi.", "en") in voices.said


def test_flipping_the_compute_flag_loads_the_model_again_at_the_next_call(voice, whisper, monkeypatch):
    monkeypatch.delenv("KAIRO_WHISPER_COMPUTE", raising=False)
    Voices(voice)
    voice.transcribe_pcm(PCM, "en")
    voice.tuning = voice_mod.Tuning(int8_float16=True)        # what read_flags does after the owner flips it
    voice.warm_call()
    assert [m["compute"] for m in whisper.made] == ["float16", "int8_float16"]
    assert voice.devices()["whisper"]["compute_type"] == "int8_float16"
    voice.warm_call()
    assert len(whisper.made) == 2                            # not again while nothing changed


def test_a_compute_that_fell_back_to_float16_is_not_reloaded_at_every_call(voice, whisper, monkeypatch):
    monkeypatch.delenv("KAIRO_WHISPER_COMPUTE", raising=False)
    whisper.fail_on = {("cuda", "int8_float16")}
    voice.tuning = voice_mod.Tuning(int8_float16=True)
    Voices(voice)
    voice.warm_call()
    voice.warm_call()
    assert [m["compute"] for m in whisper.made] == ["float16"]


def test_a_call_connecting_after_the_owner_flipped_the_compute_flag_loads_the_model_again(voice, whisper, db, monkeypatch):
    monkeypatch.delenv("KAIRO_WHISPER_COMPUTE", raising=False)
    Voices(voice)
    voice.transcribe_pcm(PCM, "en")
    flags.set_flag(db, "g2.int8_float16", True)
    voice.call_started(db).join(20)
    assert [m["compute"] for m in whisper.made] == ["float16", "int8_float16"]
    assert voice.devices()["whisper"]["compute_type"] == "int8_float16"


def test_a_database_that_cannot_be_read_never_stops_a_call(voice, whisper):
    thread = voice.call_started(object())                    # the last switches stay (warm-up on by default)
    assert thread is not None and voice.tuning == voice_mod.Tuning()
    thread.join(20)
