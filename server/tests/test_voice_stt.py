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

pytest.importorskip("numpy")      # the speech code needs it: the voice CI jobs (and the PC) have it, the plain server job doesn't

from app import flags, voice as voice_mod
from app.db import make_engine
from app.voice import NotInstalled, Voice

PCM = b"\x10\x00" * 16000           # one second of 16 kHz 16-bit mono (content does not matter to the stand-in)


class Whisper:
    """A stand-in faster_whisper module. .made: how each model was built; .calls: each transcribe()."""

    def __init__(self, monkeypatch, *, fail_on=(), language="en", text=" hello"):
        self.made, self.calls = [], []
        self.fail_on, self.language, self.text = set(fail_on), language, text
        self.multilingual = True
        self.segments = None          # [(text, compression_ratio)]: what faster-whisper's Segment carries; None: just the text
        outer = self
        mod = types.ModuleType("faster_whisper")

        class WhisperModel:
            def __init__(self, path, device="cpu", compute_type="default", **kw):
                if (device, compute_type) in outer.fail_on:
                    raise ValueError(f"no {compute_type} on {device}")
                outer.made.append({"path": str(path), "device": device, "compute": compute_type, **kw})
                self.device, self.compute = device, compute_type
                self.model = types.SimpleNamespace(compute_type="float16" if compute_type == "default" else compute_type,
                                                   is_multilingual=outer.multilingual)

            def transcribe(self, audio, **kw):
                outer.calls.append(kw)
                info = types.SimpleNamespace(language=kw.get("language") or outer.language, duration=1.0)
                if outer.segments is not None:
                    return iter([segment(t, r) for t, r in outer.segments]), info
                text = outer.text.get(info.language, "") if isinstance(outer.text, dict) else outer.text   # a text per language
                return iter([types.SimpleNamespace(text=text)]), info

        mod.WhisperModel = WhisperModel
        monkeypatch.setitem(sys.modules, "faster_whisper", mod)
        monkeypatch.setattr(Voice, "installed", staticmethod(lambda: {"stt": True, "en": False, "de": False}))


def segment(text, ratio):
    return types.SimpleNamespace(text=text, compression_ratio=ratio, avg_logprob=-0.1)


LOOP = "Cut, cut, cut, cut, cut, cut, cut, cut, cut, cut, cut, cut, cut, cut, cut, cut."


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


def test_a_decode_that_only_repeats_itself_never_reaches_the_brain_as_the_owners_words(voice, whisper):
    # with the retries gone (temperature 0), Whisper's own "too repetitive: try again" would no longer fire
    whisper.segments = [(LOOP, 29.4)]
    assert voice.transcribe_pcm(PCM, "en").text == ""
    assert voice.devices()["whisper"]["loops_dropped"] == 1


def test_only_the_repeating_piece_is_dropped_the_words_around_it_stay(voice, whisper):
    whisper.segments = [("Remind me to call the dentist.", 1.3), (LOOP, 9.0), ("And buy milk.", 1.1)]
    assert voice.transcribe_pcm(PCM, "en").text == "Remind me to call the dentist. And buy milk."
    assert voice.devices()["whisper"]["loops_dropped"] == 1


@pytest.mark.parametrize("ratio,kept", [(1.3, True), (2.4, True), (2.41, False)])
def test_the_limit_is_whispers_own_above_two_point_four(voice, whisper, ratio, kept):
    whisper.segments = [("a sentence", ratio)]
    assert (voice.transcribe_pcm(PCM, "en").text == "a sentence") is kept


def test_a_fake_segment_without_a_ratio_is_kept(voice, whisper):
    assert voice.transcribe_pcm(PCM, "en").text == "hello" and voice.devices()["whisper"]["loops_dropped"] == 0


def test_with_the_switch_off_whispers_own_retries_are_the_guard_and_nothing_is_dropped_here(voice, whisper):
    voice.tuning = voice_mod.Tuning(temperature_zero=False)
    whisper.segments = [(LOOP, 29.4)]
    assert voice.transcribe_pcm(PCM, "en").text == LOOP and voice.devices()["whisper"]["loops_dropped"] == 0


def test_a_recording_is_left_to_whispers_own_retries_too(voice, whisper):
    whisper.segments = [(LOOP, 29.4)]
    assert voice.transcribe(b"m4a bytes", language="en").text == LOOP and voice.devices()["whisper"]["loops_dropped"] == 0


def test_a_dropped_turn_teaches_the_voice_nothing_and_is_counted_per_turn(voice, whisper):
    whisper.segments = [(LOOP, 29.4)]
    voice.transcribe_pcm(PCM, "de")
    voice.transcribe_pcm(PCM, "de")
    assert voice._last_language is None and voice.devices()["whisper"]["loops_dropped"] == 2


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


def test_a_call_connecting_warms_whisper_with_one_sentence_through_the_call_path(voice, whisper, db):
    # one, not two: a sentence of the owner's that arrives while a warm-up decode runs waits for that decode
    # (CTranslate2 cannot be interrupted), so a connect warms with exactly one
    voices = Voices(voice)
    voice.call_started(db).join(20)
    (kw,) = whisper.calls
    assert kw["language"] == "en" and kw["vad_filter"] is True and kw["beam_size"] == 1 and kw["temperature"] == 0.0
    assert voices.said == [(voice_mod.WARM_TEXT["en"], "en")]                    # and only that sentence is made
    assert voice.warm_ms["whisper"] >= 0


def test_a_call_warms_with_the_sentence_in_the_language_the_last_call_ended_in(voice, whisper, db):
    voices = Voices(voice)
    voice._last_language = "de"
    voice.call_started(db).join(20)
    assert [kw["language"] for kw in whisper.calls] == ["de"] and voices.said == [(voice_mod.WARM_TEXT["de"], "de")]
    assert voice._last_language is None                                          # the new call still starts clean


def test_the_start_up_warm_up_keeps_both_languages_nobody_is_talking_then(voice, whisper, monkeypatch):
    Voices(voice)
    monkeypatch.setattr(voice, "_load_kokoro", lambda: None)
    voice.warm()
    assert [kw["language"] for kw in whisper.calls] == ["en", "de"]


def test_on_the_cpu_there_is_no_cold_card_so_a_connect_warms_nothing_and_blocks_nobody(voice, whisper, db):
    whisper.fail_on = {("cuda", "float16")}                                      # the PC's card is not usable: the small model on the CPU
    Voices(voice)
    voice.call_started(db).join(20)
    assert whisper.made[-1]["device"] == "cpu" and whisper.calls == [] and "whisper" not in voice.warm_ms


def test_the_warm_sentences_are_made_once_and_not_again_at_the_next_call(voice, whisper):
    voices = Voices(voice)
    voice.warm_call()
    voice._last_decode = 0.0                                # long ago: the next call warms again, from the same audio
    voice.warm_call()
    assert len(voices.said) == 1 and len(whisper.calls) == 2


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


def test_without_the_german_voice_a_german_call_is_warmed_in_english(voice, whisper):
    Voices(voice, de=False)
    voice.warm_call("de")
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


# ---------------------------------------------------------------- "English and German": one pass (g2.single_encode_langid)

class OnePass:
    """langid.transcribe_en_de as a stand-in: records its arguments and answers in the language it is told to."""

    def __init__(self, monkeypatch, language="de", text="guten morgen"):
        from app.voice import langid
        self.calls, self.language, self.text, self.error, self.ratio = [], language, text, None, 1.2
        monkeypatch.setattr(langid, "transcribe_en_de", self)

    def __call__(self, model, audio, *, prev_language=None, vad_filter=True, **opts):
        self.calls.append({"prev": prev_language, "vad_filter": vad_filter, **opts})
        if self.error is not None:
            raise self.error
        info = types.SimpleNamespace(language=self.language, language_probability=0.9, accepted=True, duration=1.0)
        return [segment(self.text, self.ratio)], info


def test_single_encode_waits_for_the_pc_test_so_it_is_off_until_the_owner_says_otherwise(tmp_path):
    assert flags.FLAGS["g2.single_encode_langid"][0] is False and Voice(tmp_path).tuning.single_encode_langid is False


def test_with_the_flag_off_auto_is_left_to_whisper_as_before(voice, whisper, monkeypatch):
    one = OnePass(monkeypatch)
    assert voice.transcribe_pcm(PCM, "auto").language == "en"
    assert one.calls == [] and whisper.calls[0]["language"] is None


def test_with_the_flag_on_auto_is_found_in_one_pass_and_whisper_is_never_asked_with_none(voice, whisper, monkeypatch):
    one = OnePass(monkeypatch)
    voice.tuning = voice_mod.Tuning(single_encode_langid=True)
    t = voice.transcribe_pcm(PCM, "auto")
    assert (t.language, t.text) == ("de", "guten morgen") and whisper.calls == []
    (call,) = one.calls
    assert call["prev"] is None and call["vad_filter"] is True and call["beam_size"] == 1 and call["temperature"] == 0.0
    assert call["without_timestamps"] is True and call["condition_on_previous_text"] is False


@pytest.mark.parametrize("mode", ["en", "de"])
def test_an_explicit_language_never_needs_the_detection(voice, whisper, monkeypatch, mode):
    one = OnePass(monkeypatch)
    voice.tuning = voice_mod.Tuning(single_encode_langid=True)
    voice.transcribe_pcm(PCM, mode)
    assert one.calls == [] and whisper.calls[0]["language"] == mode


def test_the_last_turns_language_is_what_an_unsure_turn_falls_back_to(voice, whisper, monkeypatch):
    one = OnePass(monkeypatch, language="de")
    voice.tuning = voice_mod.Tuning(single_encode_langid=True)
    voice.transcribe_pcm(PCM, "auto")
    one.language = "en"
    voice.transcribe_pcm(PCM, None)
    assert [c["prev"] for c in one.calls] == [None, "de"]
    one.text = ""                                                     # a noise-only piece says nothing about the language
    one.language = "de"
    voice.transcribe_pcm(PCM, "auto")
    one.text = "hello"
    voice.transcribe_pcm(PCM, "auto")
    assert [c["prev"] for c in one.calls] == [None, "de", "en", "en"]


def test_the_one_pass_path_drops_a_repeating_decode_too(voice, whisper, monkeypatch):
    one = OnePass(monkeypatch, language="de", text=LOOP)
    one.ratio = 12.0
    voice.tuning = voice_mod.Tuning(single_encode_langid=True)
    t = voice.transcribe_pcm(PCM, "auto")
    assert t.text == "" and voice.devices()["whisper"]["loops_dropped"] == 1 and voice._last_language is None


def test_a_new_call_starts_without_the_last_calls_language(voice, whisper, monkeypatch, db):
    one = OnePass(monkeypatch, language="de")
    flags.set_flag(db, "g2.single_encode_langid", True)
    voice.read_flags(db)
    voice.transcribe_pcm(PCM, "auto")
    voice.call_started(db).join(20)
    voice.transcribe_pcm(PCM, "auto")
    assert [c["prev"] for c in one.calls] == [None, None]


def test_the_warm_up_does_not_teach_the_voice_a_language(voice, whisper, monkeypatch):
    one = OnePass(monkeypatch, language="de")
    voice.tuning = voice_mod.Tuning(single_encode_langid=True)
    Voices(voice)
    voice.warm_call()
    voice.transcribe_pcm(PCM, "auto")
    assert [c["prev"] for c in one.calls] == [None]


def test_an_english_only_model_has_nothing_to_find(voice, whisper, monkeypatch):
    one = OnePass(monkeypatch)
    whisper.multilingual = False
    voice.tuning = voice_mod.Tuning(single_encode_langid=True)
    voice.transcribe_pcm(PCM, "auto")
    assert one.calls == [] and whisper.calls[0]["language"] is None


def test_if_the_single_pass_ever_breaks_the_turn_is_still_heard_the_old_way_and_it_says_so(voice, whisper, monkeypatch):
    one = OnePass(monkeypatch)
    one.error = RuntimeError("TranscriptionOptions() got an unexpected keyword argument")
    voice.tuning = voice_mod.Tuning(single_encode_langid=True)
    assert voice.transcribe_pcm(PCM, "auto").text == "hello"          # Whisper's own language=None
    assert whisper.calls[-1]["language"] is None
    w = voice.devices()["whisper"]
    assert "unexpected keyword" in w["language_detection_note"] and w["language_detection"].startswith("Whisper's own")
    voice.transcribe_pcm(PCM, "auto")
    assert len(one.calls) == 1                                        # not tried again on every turn


def test_a_graphics_card_error_inside_the_single_pass_still_moves_the_call_to_the_cpu(voice, whisper, monkeypatch):
    one = OnePass(monkeypatch)
    one.error = RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
    voice.tuning = voice_mod.Tuning(single_encode_langid=True)
    voice.transcribe_pcm(PCM, "auto")
    assert voice.status()["stt_device"] == "cpu"
    assert [c["device"] for c in whisper.made] == ["cuda", "cpu"]


def test_devices_say_which_language_finding_is_in_use(voice, whisper, monkeypatch):
    OnePass(monkeypatch)
    voice.transcribe_pcm(PCM, "en")
    assert voice.devices()["whisper"]["language_detection"].startswith("Whisper's own")
    voice.tuning = voice_mod.Tuning(single_encode_langid=True)
    w = voice.devices()["whisper"]
    assert w["language_detection"] == "one pass, English or German" and w["language_detection_note"] is None


# ---------------------------------------------------------------- the owner's switches also apply to the start-up load

def test_the_start_up_load_follows_the_owners_switches_not_the_defaults(migrated, tmp_path, whisper, monkeypatch):
    monkeypatch.delenv("KAIRO_WHISPER_COMPUTE", raising=False)
    put_models(tmp_path, voice_mod.WHISPER_GPU, voice_mod.WHISPER_CPU)
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        flags.set_flag(s, "g2.int8_float16", True)               # chosen after the PC test, then Kairo restarted
        flags.set_flag(s, "g2.temperature_zero", False)
    v = Voice(tmp_path, engine=engine)
    monkeypatch.setattr(v, "_load_kokoro", lambda: None)
    v.warm()
    assert [m["compute"] for m in whisper.made] == ["int8_float16"]      # loaded once, in the owner's type
    assert v.tuning.int8_float16 is True and v.tuning.temperature_zero is False
    assert whisper.calls and all("temperature" not in kw for kw in whisper.calls)     # and the warm-up asks the same way


def test_a_start_up_without_a_database_uses_the_defaults(voice, whisper, monkeypatch):
    monkeypatch.setattr(voice, "_load_kokoro", lambda: None)
    voice.warm()
    assert voice.tuning == voice_mod.Tuning() and whisper.made[0]["compute"] == "float16"


def test_a_database_that_cannot_be_read_at_start_up_does_not_stop_the_warm_up(tmp_path, whisper, monkeypatch):
    put_models(tmp_path, voice_mod.WHISPER_GPU, voice_mod.WHISPER_CPU)
    v = Voice(tmp_path, engine=object())
    monkeypatch.setattr(v, "_load_kokoro", lambda: None)
    v.warm()
    assert v.tuning == voice_mod.Tuning() and whisper.made and whisper.made[0]["compute"] == "float16"


# ---------------------------------------------------------------- the self-test: a German round trip through the call path

@pytest.fixture
def selftest_voice(voice, whisper, monkeypatch):
    """A Voice whose three voice packages import and whose voices (stand-ins) say something in each language."""
    for name in ("kokoro_onnx", "piper"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    whisper.text = {"en": " Kairo voice test.", "de": " Guten Morgen, das ist ein Test."}
    voices = Voices(voice)
    monkeypatch.setattr(voice_mod, "wav_problems", lambda wav: [])
    return voice, voices


def test_the_self_test_writes_a_german_sentence_down_again_through_the_call_path(selftest_voice, whisper):
    voice, voices = selftest_voice
    r = voice.selftest()
    assert r["ok"] is True and r["german_ok"] is True and "morgen" in r["heard_de"].lower()
    german = [kw for kw in whisper.calls if kw["language"] == "de"][-1]
    assert german["vad_filter"] is True and german["beam_size"] == 1 and german["temperature"] == 0.0       # as a call asks
    assert ("Guten Morgen, das ist ein Test.", "de") in voices.said


def test_a_german_sentence_heard_wrong_is_reported_but_never_turns_the_self_test_red(selftest_voice, whisper):
    """The self-test decides whether an update rolls back; a German sentence a small model misheard must not."""
    voice, _ = selftest_voice
    whisper.text = {"en": " Kairo voice test.", "de": " Ruf Mama an."}
    r = voice.selftest()
    assert r["ok"] is True and r["german_ok"] is False and r["heard_de"] == "Ruf Mama an."
    assert any("German" in n and "Ruf Mama an." in n for n in r["notes"])


def test_without_the_german_voice_the_round_trip_is_not_run(selftest_voice, whisper):
    voice, _ = selftest_voice
    Voices(voice, de=False)
    r = voice.selftest()
    assert r["german_ok"] is None and r["heard_de"] is None and r["ok"] is False and "German voice" in r["reason"]
    assert all(kw["language"] != "de" for kw in whisper.calls)


def test_the_self_test_does_not_teach_the_voice_a_language(selftest_voice):
    voice, _ = selftest_voice
    voice.selftest()
    assert voice._last_language is None
