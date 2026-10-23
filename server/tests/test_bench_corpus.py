"""The voice latency harness's clips (app/bench_corpus.py): the manifest in the repo, the pick of a few,
the exact end of speech, and the owner's recordings that take precedence over the rendered ones."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from app import bench_corpus as bc


def _tone(seconds: float, rate: int = 24000, lead: float = 0.0, trail: float = 0.0, amp: float = 0.3):
    import numpy as np
    t = np.arange(int(seconds * rate)) / rate
    # a 150 Hz buzz with a 700 Hz and a 1.2 kHz overtone: loud and voiced like a vowel
    x = amp * (np.sin(2 * np.pi * 150 * t) + 0.5 * np.sin(2 * np.pi * 700 * t) + 0.3 * np.sin(2 * np.pi * 1200 * t)) / 1.8
    return np.concatenate([np.zeros(int(lead * rate)), x, np.zeros(int(trail * rate))]).astype(np.float32)


# ---------------------------------------------------------------- the manifest

def test_the_manifest_has_a_real_corpus_in_both_languages_and_every_kind():
    clips = bc.load_manifest()
    assert len(clips) >= 30
    assert {c.lang for c in clips} == {"en", "de"}
    kinds = Counter(c.kind for c in clips)
    assert set(kinds) == set(bc.KINDS)
    assert all(kinds[k] >= 3 for k in ("backchannel", "noise", "other_speaker")), kinds
    assert len({c.id for c in clips}) == len(clips)
    for lang in ("en", "de"):                       # both languages have questions to answer and things to ignore
        mine = Counter(c.kind for c in clips if c.lang == lang)
        assert mine["question"] >= 5 and mine["backchannel"] >= 3 and mine["other_speaker"] >= 3, (lang, mine)
    assert all(len(c.text.split()) <= 12 for c in clips if c.kind in bc.ANSWERED)    # short utterances


def _manifest(tmp_path, clips, schema=1):
    p = tmp_path / "corpus.json"
    p.write_text(json.dumps({"schema": schema, "clips": clips}), encoding="utf-8")
    return p


@pytest.mark.parametrize("bad", [
    {"id": "q1", "lang": "fr", "text": "Bonjour.", "kind": "question"},
    {"id": "q1", "lang": "en", "text": "Hello.", "kind": "shout"},
    {"id": "Q 1", "lang": "en", "text": "Hello.", "kind": "question"},
    {"id": "q1", "lang": "en", "text": "  ", "kind": "question"},
])
def test_a_bad_manifest_entry_is_refused(tmp_path, bad):
    with pytest.raises(bc.CorpusError):
        bc.load_manifest(_manifest(tmp_path, [bad]))


def test_repeated_ids_and_a_wrong_schema_are_refused(tmp_path):
    clip = {"id": "q1", "lang": "en", "text": "Hello?", "kind": "question"}
    with pytest.raises(bc.CorpusError):
        bc.load_manifest(_manifest(tmp_path, [clip, dict(clip)]))
    with pytest.raises(bc.CorpusError):
        bc.load_manifest(_manifest(tmp_path, [clip], schema=2))
    with pytest.raises(bc.CorpusError):
        bc.load_manifest(tmp_path / "missing.json")


def test_a_short_run_still_has_every_kind_and_both_languages():
    clips = bc.load_manifest()
    few = bc.select(clips, 12)
    assert len(few) == 12
    assert {c.kind for c in few} == set(bc.KINDS) and {c.lang for c in few} == {"en", "de"}
    assert [c.id for c in few] == [c.id for c in clips if c.id in {x.id for x in few}]     # manifest order
    assert bc.select(clips, None) == clips and bc.select(clips, 999) == clips
    assert bc.select(clips, 0) == []


# ---------------------------------------------------------------- the end of speech

def test_the_end_of_speech_is_the_last_voiced_sample_not_the_end_of_the_file():
    pytest.importorskip("numpy")
    x = _tone(1.0, lead=0.3, trail=0.6)
    assert bc.speech_end_s(x, 24000) == pytest.approx(1.3, abs=0.02)
    assert bc.speech_end_s(_tone(1.0, rate=48000, trail=2.0), 48000) == pytest.approx(1.0, abs=0.02)
    assert bc.speech_end_s(_tone(0.5, amp=0.0) * 0, 24000) == 0.0            # silence has no speech


def test_a_quiet_tail_far_below_the_speech_is_not_speech():
    pytest.importorskip("numpy")
    import numpy as np
    loud, tail = _tone(0.8), _tone(0.4, amp=0.3 * 0.01)           # 40 dB down: breath, room, not a word
    assert bc.speech_end_s(np.concatenate([loud, tail]), 24000) == pytest.approx(0.8, abs=0.03)


# ---------------------------------------------------------------- the owner's recordings win

def test_the_owners_recording_takes_precedence_over_the_rendered_clip(tmp_path):
    pytest.importorskip("numpy")
    clip = bc.Clip("q_en_01", "en", "What's on my calendar tomorrow?", "question")
    other = bc.Clip("q_en_02", "en", "What's the weather like today?", "question")
    assert bc.load(tmp_path, clip) is None and bc.missing(tmp_path, [clip, other]) == [clip, other]
    bc.write_wav(bc.corpus_dir(tmp_path) / "q_en_01.wav", _tone(1.0, trail=0.6), 24000)
    bc.index_path(tmp_path).write_text(json.dumps({"schema": 1, "clips": {"q_en_01": {"end_s": 1.0, "voice": "af_heart"}}}))
    got = bc.load(tmp_path, clip)
    assert got.source == "tts" and got.voice == "af_heart" and got.end_s == 1.0 and got.rate == 24000
    assert bc.missing(tmp_path, [clip, other]) == [other]
    bc.write_wav(bc.own_dir(tmp_path) / "q_en_01.wav", _tone(1.5, rate=16000, lead=0.2, trail=1.0), 16000)
    mine = bc.load(tmp_path, clip)
    assert mine.source == "own" and mine.rate == 16000 and mine.end_s == pytest.approx(1.7, abs=0.03)
    assert bc.missing(tmp_path, [clip, other]) == [other]                     # a recording alone is enough


def test_a_recording_that_is_not_a_16_bit_wav_says_so(tmp_path):
    pytest.importorskip("numpy")
    (tmp_path / "x.wav").write_bytes(b"RIFF not really a wave")
    with pytest.raises(bc.CorpusError, match="not a WAV"):
        bc.read_wav(tmp_path / "x.wav")


# ---------------------------------------------------------------- speech-like audio and noise (no models)

def test_synthetic_speech_is_the_same_every_time_and_as_long_as_its_words():
    np = pytest.importorskip("numpy")
    a = bc.synth_speech("What's on my calendar tomorrow?")
    assert np.array_equal(a, bc.synth_speech("What's on my calendar tomorrow?"))
    assert not np.array_equal(a, bc.synth_speech("What's the weather like today?"))
    short, long_ = bc.synth_speech("Okay."), bc.synth_speech("Can you walk me through how my week looks like?")
    assert len(long_) > 3 * len(short)
    assert 1.2 < len(a) / 16000 < 3.5 and abs(float(np.abs(a).max()) - 0.5) < 1e-3
    assert bc.speech_end_s(a, 16000) == pytest.approx(len(a) / 16000, abs=0.05)       # no silence around it


def test_every_noise_clip_in_the_manifest_has_a_recipe():
    np = pytest.importorskip("numpy")
    noise = [c for c in bc.load_manifest() if c.kind == "noise"]
    assert len(noise) >= 3
    for c in noise:
        x = bc.synth_noise(c)
        assert len(x) > 2000 and 0.3 < float(np.abs(x).max()) <= 0.36 and bc.recipe_of(c) in bc.RECIPES
        assert np.array_equal(x, bc.synth_noise(c))
    with pytest.raises(bc.CorpusError, match="no noise recipe"):
        bc.synth_noise(bc.Clip("noise_thunder", "en", "(thunder)", "noise"))


# ---------------------------------------------------------------- rendering

class FakeTTS:
    """Kairo's Voice with the speech made by synth_speech; remembers how each line was asked for."""

    def __init__(self, unknown_voices=(), broken_text=None):
        self.asked, self.unknown, self.broken = [], set(unknown_voices), broken_text

    def speak(self, text, language=None, *, voice=None, speed=None):
        import io
        import wave

        import numpy as np
        self.asked.append((text, language, voice, speed))
        if voice in self.unknown:
            raise ValueError(f"no voice {voice}")
        if text == self.broken:
            raise RuntimeError("the voice fell over")
        x = bc.synth_speech(text, rate=24000)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
        return buf.getvalue(), language or "en"


def test_voices_are_dealt_out_and_strangers_never_sound_like_the_owner_voices():
    clips = bc.load_manifest()
    rows = bc.plan(clips)
    en_owner = {v for c, v, _, _ in rows if c.lang == "en" and c.kind != "other_speaker" and c.kind != "noise"}
    en_other = {v for c, v, _, _ in rows if c.lang == "en" and c.kind == "other_speaker"}
    assert len(en_owner) >= 3 and len(en_other) >= 2 and not en_owner & en_other
    de_other = [(p) for c, _, _, p in rows if c.lang == "de" and c.kind == "other_speaker"]
    assert all(p and p != 1.0 for p in de_other)                  # the one German voice, its pitch moved
    assert len({s for c, _, s, _ in rows if c.lang == "en"}) >= 2           # and several speaking rates


def test_render_writes_each_clip_with_its_exact_end_of_speech_and_silence_after(tmp_path):
    np = pytest.importorskip("numpy")
    clips = bc.select(bc.load_manifest(), 14)
    tts = FakeTTS()
    done = bc.render(tmp_path, tts, clips, say=lambda *_: None)
    assert done["rendered"] == 14 and done["kept"] == 0 and done["failed"] == {}
    index = bc.read_index(tmp_path)
    for c in clips:
        got = bc.load(tmp_path, c)
        assert got.source == "tts" and got.rate == 24000 and abs(float(np.abs(got.samples).max()) - (0.35 if c.kind == "noise" else 0.5)) < 0.01
        tail = got.samples[int((got.end_s + 0.05) * got.rate):]                        # after the end of speech: silence
        assert len(tail) >= int((bc.TRAIL_S - 0.1) * got.rate) and float(np.abs(tail).max()) < 1e-3, c.id
        assert index[c.id]["dur_s"] == pytest.approx(len(got.samples) / got.rate, abs=0.001)
    asked = {text for text, *_ in tts.asked}
    assert not any(c.text in asked for c in clips if c.kind == "noise")              # noise is made, not spoken
    done2 = bc.render(tmp_path, tts, clips, say=lambda *_: None)                      # once: nothing again
    assert done2["rendered"] == 0 and done2["kept"] == 14 and len(tts.asked) == len(asked)
    assert bc.render(tmp_path, tts, clips[:2], force=True, say=lambda *_: None)["rendered"] == 2


def test_a_voice_that_is_missing_falls_back_and_a_clip_that_fails_does_not_stop_the_rest(tmp_path):
    pytest.importorskip("numpy")
    clips = [c for c in bc.load_manifest() if c.lang == "en" and c.kind in ("question", "backchannel")][:6]
    tts = FakeTTS(unknown_voices={"af_heart"}, broken_text=clips[1].text)
    done = bc.render(tmp_path, tts, clips, say=lambda *_: None)
    assert list(done["failed"]) == [clips[1].id] and done["rendered"] == 5
    first = bc.read_index(tmp_path)[clips[0].id]
    assert first["voice"] == "default"                                                # af_heart was not there
    assert bc.missing(tmp_path, clips) == [clips[1]]


def test_the_command_line_lists_and_renders(tmp_path, monkeypatch, capsys):
    pytest.importorskip("numpy")
    monkeypatch.setattr(bc, "default_voice", lambda data_dir: FakeTTS())
    assert bc.main(["list", "--data-dir", str(tmp_path), "--only", "q_en_01"]) == 0
    assert "NOT RENDERED" in capsys.readouterr().out
    assert bc.main(["render", "--data-dir", str(tmp_path), "--only", "q_en_01,noise_fan"]) == 0
    assert "2 rendered" in capsys.readouterr().out
    bc.write_wav(bc.own_dir(tmp_path) / "q_en_01.wav", bc.synth_speech("hi there", rate=16000), 16000)
    bc.main(["list", "--data-dir", str(tmp_path), "--only", "q_en_01"])
    assert "own recording" in capsys.readouterr().out
