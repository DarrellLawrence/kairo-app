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
