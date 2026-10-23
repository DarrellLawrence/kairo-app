"""G2: one encoder pass for finding the language (English or German) and writing the words down
(app/voice/langid.py), behind g2.single_encode_langid.

Three kinds of test: the rule that picks the language (runs everywhere); a pin on the faster-whisper
internals the path relies on (runs where faster-whisper is installed; a bump of the pinned version fails it
on purpose); and the real thing against a real model (KAIRO_STT_TEST_MODEL = a model folder, e.g. the small
one; and in the CI voice job, with Kairo's own voices, KAIRO_VOICE_TEST=1)."""

from __future__ import annotations

import inspect
import os
import wave
from pathlib import Path

import pytest

from app.voice import Voice, langid

SPEECH = Path(__file__).parent / "fixtures" / "speech_en.wav"        # "Remind me to call the dentist tomorrow morning."


# ---------------------------------------------------------------- the rule

@pytest.mark.parametrize("probs,prev,expected", [
    ({"en": 0.95, "de": 0.01}, None, ("en", True)),
    ({"en": 0.001, "de": 0.999}, "en", ("de", True)),                  # a clear German turn beats the last turn's English
    ({"en": 0.30, "de": 0.20, "nl": 0.45}, "de", ("en", True)),        # en + de = 0.5: accepted, English is the likelier
    ({"en": 0.004, "de": 0.002, "nl": 0.99}, "de", ("de", False)),     # a 1 s clip heard as Dutch: keep the last turn's language
    ({"en": 0.004, "de": 0.002, "nl": 0.99}, "en", ("en", False)),
    ({"en": 0.004, "de": 0.002, "nl": 0.99}, None, ("en", False)),     # the first turn: the likelier of the two
    ({"en": 0.10, "de": 0.30, "fr": 0.60}, None, ("de", False)),
    ({"en": 0.10, "de": 0.30, "fr": 0.60}, "fr", ("de", False)),       # only English and German are ever used
    ({}, None, ("en", False)),
])
def test_the_language_is_english_or_german_and_is_taken_only_when_the_two_together_carry_half(probs, prev, expected):
    language, _p, accepted = langid.pick_language(probs, prev)
    assert (language, accepted) == expected


# ---------------------------------------------------------------- the pin

def test_the_faster_whisper_internals_the_path_uses_are_the_ones_it_was_checked_against():
    fw = pytest.importorskip("faster_whisper")
    assert fw.__version__ == langid.PINNED, "faster-whisper changed: re-check app/voice/langid.py against its transcribe()"
    from faster_whisper import WhisperModel
    from faster_whisper.transcribe import TranscriptionOptions
    assert list(inspect.signature(WhisperModel.generate_segments).parameters) == [
        "self", "features", "tokenizer", "options", "log_progress", "encoder_output"]
    assert list(inspect.signature(WhisperModel.encode).parameters) == ["self", "features"]
    # the first window reuses a given encoder output: that is what saves the second pass
    assert "if seek > 0 or encoder_output is None" in inspect.getsource(WhisperModel.generate_segments)
    # every option is either a transcribe() argument or one langid sets itself
    import dataclasses
    fields = {f.name for f in dataclasses.fields(TranscriptionOptions)}
    given = set(inspect.signature(WhisperModel.transcribe).parameters)
    assert fields - given <= {"temperatures"}, fields - given


# ---------------------------------------------------------------- the real thing

def real_model():
    path = os.environ.get("KAIRO_STT_TEST_MODEL")
    if not path or not Path(path).is_dir():
        pytest.skip("set KAIRO_STT_TEST_MODEL to a faster-whisper model folder (the small one) to run against a real model")
    pytest.importorskip("faster_whisper")
    from faster_whisper import WhisperModel
    model = WhisperModel(path, device="cpu", compute_type="int8", local_files_only=True)
    model.encodes = 0
    real = model.encode

    def counted(features):
        model.encodes += 1
        return real(features)
    model.encode = counted
    return model


def speech_en():
    import numpy as np
    with wave.open(str(SPEECH)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        rate = w.getframerate()
    return x if rate == 16000 else np.interp(np.linspace(0, len(x) - 1, int(len(x) * 16000 / rate)), np.arange(len(x)), x).astype(np.float32)


OPTS = {"beam_size": 1, "condition_on_previous_text": False, "without_timestamps": True, "temperature": 0.0}


def test_finding_the_language_and_writing_the_words_down_share_one_encoder_pass():
    model, audio = real_model(), speech_en()
    segments, info = model.transcribe(audio, language=None, vad_filter=True, **OPTS)
    legacy = " ".join(s.text.strip() for s in segments).strip()
    legacy_encodes, model.encodes = model.encodes, 0
    segments, info = langid.transcribe_en_de(model, audio, prev_language=None, vad_filter=True, **OPTS)
    text = " ".join(s.text.strip() for s in segments).strip()
    assert legacy_encodes == 2 and model.encodes == 1                    # Whisper's own language=None: twice
    assert info.language == "en" and info.language_probability > 0.5 and text == legacy and "dentist" in text.lower()
    assert info.duration == pytest.approx(len(audio) / 16000)


def test_the_words_are_the_same_as_with_the_language_given():
    model, audio = real_model(), speech_en()
    segments, _ = model.transcribe(audio, language="en", vad_filter=True, **OPTS)
    fixed = " ".join(s.text.strip() for s in segments).strip()
    segments, _ = langid.transcribe_en_de(model, audio, prev_language="de", vad_filter=True, **OPTS)
    assert " ".join(s.text.strip() for s in segments).strip() == fixed


def test_quiet_noise_is_filtered_before_any_encoder_pass():
    import numpy as np
    model = real_model()
    noise = (np.random.default_rng(1).standard_normal(16000 * 3) * 0.01).astype(np.float32)
    segments, info = langid.transcribe_en_de(model, noise, prev_language="de", vad_filter=True, **OPTS)
    assert list(segments) == [] and model.encodes == 0 and info.language == "de"


def test_a_voice_with_the_flag_on_hears_english_and_remembers_it(tmp_path):
    model = real_model()
    from app import voice as voice_mod
    v = Voice(tmp_path)
    v._stt, v._stt_device, v._stt_model = model, "cpu", "small"
    v.tuning = voice_mod.Tuning(single_encode_langid=True)
    pcm = (speech_en() * 32767).astype("<i2").tobytes()
    t = v.transcribe_pcm(pcm, "auto")
    assert t.language == "en" and "dentist" in t.text.lower() and model.encodes == 1
    assert v._last_language == "en"


@pytest.mark.skipif(os.environ.get("KAIRO_VOICE_TEST") != "1", reason="real voice models: set KAIRO_VOICE_TEST=1 (CI voice job)")
def test_english_and_german_in_kairos_own_voices_are_found_in_one_pass(tmp_path):
    """CI voice job: Kokoro and Piper say a sentence each, Whisper (CPU, base) finds the language and writes it down."""
    from app import voice as voice_mod
    from app.voicelink import wav_pcm
    import numpy as np
    v = Voice(os.environ.get("KAIRO_VOICE_MODELS") or str(tmp_path / "models"))
    v.download()
    v.tuning = voice_mod.Tuning(single_encode_langid=True)
    model = v._load_stt()
    model.encodes = 0
    real = model.encode

    def counted(features):
        model.encodes += 1
        return real(features)
    model.encode = counted
    for text, lang in (("Kairo voice test. Please call the dentist tomorrow.", "en"),
                       ("Guten Morgen. Heute hast du eine Prüfung in Mathe.", "de")):
        wav, said = v.speak(text, lang)
        pcm, rate = wav_pcm(wav)
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        x16 = np.interp(np.arange(0, len(x), rate / 16000), np.arange(len(x)), x).astype("<i2").tobytes()
        model.encodes = 0
        t = v.transcribe_pcm(x16, "auto")
        assert said == lang and t.language == lang and t.text, (lang, t)
        assert model.encodes == 1
