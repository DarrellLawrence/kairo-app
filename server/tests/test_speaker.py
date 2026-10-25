"""Only the owner is answered on calls (owner's approval 2026-10-20): app/speaker.py.

Synthetic audio only: tones and a numpy "voice" (harmonics of a pitch shaped by vowel formants), never a
recording of a person. The speaker model is faked by PitchModel (a voice's print is its pitch) except in
the last test, which runs the real CAM++ file when it is on this machine (KAIRO_SPEAKER_TEST_MODEL)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import speaker as sp  # noqa: E402
from app.db import AuditLog, SettingKV, make_engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.settings import REPO_ROOT  # noqa: E402
from tests.test_voice_local import local  # noqa: E402,F401  (the app with the fake voice model)

RATE = 16000
VOWELS = [(730, 1090, 2440), (270, 2290, 3010), (300, 870, 2240), (530, 1840, 2480), (570, 840, 2410), (440, 1020, 2240)]
OWNER_HZ, OTHER_HZ, NEAR_HZ = 110.0, 205.0, 193.0      # PitchModel: 1.0, 0.0 and about 0.2 against the owner


def voice(seconds: float, f0: float = OWNER_HZ, scale: float = 1.0, seed: int = 0, gain: float = 0.3,
          wobble: float = 0.0) -> np.ndarray:
    """A made-up voice: syllables of a vowel each (harmonics of f0 through three formants), short gaps."""
    rng = np.random.default_rng(seed)
    out, done = [], 0.0
    while done < seconds:
        n = int(rng.uniform(0.15, 0.3) * RATE)
        t = np.arange(n) / RATE
        formants = np.array(VOWELS[rng.integers(len(VOWELS))]) * scale
        pitch = f0 * (1 + wobble * np.sin(2 * np.pi * rng.uniform(0.5, 1.5) * (t + done)) + rng.uniform(-wobble, wobble))
        phase = 2 * np.pi * np.cumsum(pitch) / RATE
        k = np.arange(1, int(7600 // (f0 * (1 + 2 * wobble))) + 1)[:, None]
        fk = k * pitch[None, :]
        shape = sum(F ** 2 / np.sqrt((F ** 2 - fk ** 2) ** 2 + (90 * fk) ** 2) for F in formants)
        x = (shape / k * np.sin(k * phase[None, :])).sum(axis=0) * np.hanning(n)
        out.append(x / (np.abs(x).max() + 1e-9))
        gap = int(rng.uniform(0.02, 0.07) * RATE)
        out.append(rng.standard_normal(gap) * 0.003)
        done += n / RATE + gap / RATE
    return (np.concatenate(out)[: int(seconds * RATE)] * gain).astype(np.float32)


def pcm(x) -> bytes:
    return (np.clip(np.asarray(x), -1, 1) * 32767).astype("<i2").tobytes()


def tone(hz: float, seconds: float = 1.0, amp: float = 0.3, noise: float = 0.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * hz * t) + noise * np.random.default_rng(0).standard_normal(len(t))).astype(np.float32)


def pitch_of(x) -> float:
    x = np.asarray(x, dtype=np.float64) - np.mean(x)
    n = 1 << int(np.ceil(np.log2(2 * len(x))))
    ac = np.fft.irfft(np.abs(np.fft.rfft(x, n)) ** 2)[: len(x)]
    lo, hi = RATE // 300, RATE // 80
    return RATE / (lo + int(np.argmax(ac[lo:hi])))


class PitchModel:
    """Stands in for CAM++: a voice's print is its pitch, as an angle (110 Hz -> 0, 205 Hz -> 90 degrees)."""

    device, why, failed, load_ms, warm_ms, loaded = "cpu", None, None, 3, 2, True

    def __init__(self):
        self.calls = 0

    def installed(self) -> bool:
        return True

    def embed(self, samples):
        if len(samples) < sp.MIN_CHECK_S * RATE * 0.9:
            return None
        self.calls += 1
        a = float(np.clip((pitch_of(samples) - OWNER_HZ) / (OTHER_HZ - OWNER_HZ) * np.pi / 2, 0, np.pi))
        return np.array([np.cos(a), np.sin(a)], dtype=np.float32)

    def warm(self):
        pass


def taught(tmp_path, engine=None) -> sp.Speaker:
    s = sp.Speaker(PitchModel(), tmp_path / "voice" / "voiceprint.bin", engine)
    s.enroll(pcm(voice(30, OWNER_HZ, seed=1)))
    return s


def turn(gate: sp.CallGate, *pieces: tuple[np.ndarray, bool]) -> sp.Verdict:
    for x, words in pieces:
        gate.words(gate.hear(pcm(x)), words)
    return asyncio.run(gate.decide())


# ---------------------------------------------------------------- the features

def test_the_model_the_check_expects_is_the_one_the_installer_pins():
    from app import voicellm
    assert voicellm.manifest()["speaker"]["path"] == sp.MODEL_NAME


def test_the_features_are_kaldis_fbank_with_the_mean_taken_off():
    assert sp.frame_count(RATE) == 98 and sp.fbank(tone(440)).shape == (98, 80)
    assert sp.fbank(tone(440, 0.02)).shape == (0, 80) and sp.features(tone(440, 0.02)).shape == (0, 80)
    t = np.arange(8000) / RATE
    two = (0.25 * np.sin(2 * np.pi * 440 * t) + 0.1 * np.sin(2 * np.pi * 3000 * t)).astype(np.float32)
    f = sp.fbank(two)
    assert f.shape == (48, 80)
    # kaldi-native-fbank (Kaldi's own code) on the same signal, Hamming window, snip_edges, no dither
    kaldi = [11.7715, 11.9538, 13.2186, 12.514, 22.149, 13.2143]
    assert np.allclose(f[20, [0, 9, 23, 37, 51, 79]], kaldi, atol=2e-3)
    x = tone(440, 1.5, noise=0.01) + tone(2500, 1.5, amp=0.1)
    g = sp.features(x)
    assert np.abs(g.mean(axis=0)).max() < 1e-4                     # CMN: the mean over time is gone
    assert np.allclose(sp.features(x * 0.05), g, atol=2e-3)       # so the phone's gain doesn't matter


def test_a_tone_lands_in_its_mel_band():
    lo, hi = 1127 * np.log(1 + sp.LOW_HZ / 700), 1127 * np.log(1 + sp.HIGH_HZ / 700)
    centres = 700 * (np.exp((lo + (np.arange(80) + 1) * (hi - lo) / 81) / 1127) - 1)
    found = []
    for hz in (300, 1000, 3000, 6000):
        band = int(sp.fbank(tone(hz)).mean(axis=0).argmax())
        assert abs(band - int(np.abs(centres - hz).argmin())) <= 1, hz
        found.append(band)
    assert found == sorted(found)
    banks = sp.mel_banks()
    assert banks.shape == (80, 257) and not banks[:, -1].any() and 0 <= banks.min() and banks.max() <= 1


def test_the_speech_level_follows_the_gain_and_skips_silence():
    x = voice(2.0)
    a, b = sp.level(x), sp.level(x * 0.1)
    # 20 dB quieter (a few of the quietest frames fall under SILENCE_DBFS and stop counting as speech)
    assert abs((a.rms_db - b.rms_db) - 20) < 0.5 and abs((a.peak_db - b.peak_db) - 20) < 0.1
    assert 1.3 < a.speech_s <= 2.0 and abs(a.speech_s - b.speech_s) < 0.2
    assert sp.level(np.zeros(RATE, dtype=np.float32)).speech_s == 0 and sp.level(np.zeros(10)).rms_db is None
    gap = np.concatenate([x, np.zeros(RATE, dtype=np.float32), x])
    assert len(sp.voiced_audio(gap, sp.level(gap))) < len(gap) - 0.8 * RATE


# ---------------------------------------------------------------- the decision

def test_the_score_decides_with_a_threshold_per_setting():
    for mode in ("normal", "strict"):
        th = sp.THRESHOLD[mode]
        assert sp.speaker_verdict(th + 0.01, 2.0, mode) == (True, "you")
        assert sp.speaker_verdict(th - 0.01, 2.0, mode) == (False, "not_you")
    assert sp.THRESHOLD["strict"] > sp.THRESHOLD["normal"]
    assert sp.speaker_verdict(-1.0, 5.0, "off") == (True, "off")
    assert sp.speaker_verdict(None, 5.0, "normal") == (True, "no score")


def test_a_short_turn_is_ignored_only_when_clearly_someone_else():
    """Under SHORT_S of speech there is too little audio for a weak score to mean anything: a score under the
    threshold but above SHORT_REJECT is answered; only a clearly-other score is ignored."""
    for mode in ("normal", "strict"):
        weak = (sp.SHORT_REJECT[mode] + sp.THRESHOLD[mode]) / 2
        assert sp.speaker_verdict(weak, 0.6, mode) == (True, "short: you")
        assert sp.speaker_verdict(weak, 1.2, mode) == (False, "not_you")       # the same score on a longer turn
        assert sp.speaker_verdict(sp.SHORT_REJECT[mode] - 0.01, 0.6, mode) == (False, "not_you")
        assert sp.speaker_verdict(-1.0, sp.MIN_CHECK_S - 0.05, mode) == (True, "too short to tell")


def test_too_quiet_needs_the_owners_level_on_this_call():
    assert not sp.too_quiet(-60.0, -40.0, [])
    assert not sp.too_quiet(-60.0, -40.0, [(-20.0, -6.0)])                       # one turn isn't a level yet
    owner = [(-20.0, -6.0), (-22.0, -7.0)]
    assert sp.too_quiet(-45.0, -30.0, owner)
    assert not sp.too_quiet(-45.0, -10.0, owner)                                # a loud peak: not far away
    assert not sp.too_quiet(-35.0, -20.0, owner)                                # 14 dB under: still answered


# ---------------------------------------------------------------- one call (fake model)

def test_until_the_voice_is_taught_only_the_quiet_gate_runs(tmp_path):
    s = sp.Speaker(PitchModel(), tmp_path / "voiceprint.bin")
    assert s.check_state() == (False, "until you teach Kairo your voice (Settings → Voice)")
    g = s.gate()
    assert turn(g, (voice(2, OTHER_HZ, seed=2), True)).answer          # someone else, loud: answered (no voiceprint)
    assert turn(g, (voice(2, OWNER_HZ, seed=3), True)).answer
    far = turn(g, (voice(2, OWNER_HZ, seed=4, gain=0.3 * 10 ** (-30 / 20)), True))
    assert (far.answer, far.outcome) == (False, "too_quiet") and far.score is None
    assert far.owner_db is not None and far.rms_db < far.owner_db - sp.QUIET_DROP_DB
    near = turn(g, (voice(2, OWNER_HZ, seed=5, gain=0.3 * 10 ** (-10 / 20)), True))
    assert near.answer                                                       # 10 dB quieter: still answered
    assert s.model.calls == 0                                                # no voiceprint: no model work


def test_after_three_quiet_turns_in_a_row_the_level_is_learned_again(tmp_path):
    g = sp.Speaker(PitchModel(), tmp_path / "voiceprint.bin").gate()
    for seed in (1, 2):
        assert turn(g, (voice(2, seed=seed), True)).answer
    quiet = 0.3 * 10 ** (-30 / 20)
    outcomes = [turn(g, (voice(2, seed=10 + i, gain=quiet), True)).outcome for i in range(sp.QUIET_RESET_AFTER + 1)]
    assert outcomes == ["too_quiet"] * sp.QUIET_RESET_AFTER + ["answered"]   # the owner moved away from the phone


def test_with_the_voice_taught_someone_else_is_ignored_and_the_owner_answered(tmp_path):
    s = taught(tmp_path)
    assert s.check_state() == (True, None)
    g = s.gate()
    me = turn(g, (voice(2.0, OWNER_HZ, seed=2), True))
    assert me.answer and me.score > 0.95 and me.why == "you"
    other = turn(g, (voice(2.0, OTHER_HZ, seed=3), True))
    assert (other.answer, other.outcome) == (False, "not_you") and other.score < 0.05
    near = turn(g, (voice(2.0, NEAR_HZ, seed=4), True))
    assert sp.SHORT_REJECT["normal"] < near.score < sp.THRESHOLD["normal"] and not near.answer
    short = turn(g, (voice(0.6, NEAR_HZ, seed=5), True))                     # same weak score, 0.6 s of speech
    assert short.answer and short.why == "short: you" and short.speech_s < sp.SHORT_S
    far_me = turn(g, (voice(2.0, OWNER_HZ, seed=6, gain=0.3 * 10 ** (-30 / 20)), True))
    assert far_me.answer and far_me.why == "you"                             # quiet, but surely the owner
    assert me.ms >= 0 and me.wait_ms < 1000 and me.speech_s > 1.0


def test_only_pieces_with_words_make_up_the_turn(tmp_path):
    g = taught(tmp_path).gate()
    v = turn(g, (voice(1.5, OTHER_HZ, seed=7), False), (voice(1.5, OWNER_HZ, seed=8), True))
    assert v.answer and v.score > 0.95                                       # the noise before the words: left out
    g.hear(pcm(voice(1.0, OTHER_HZ, seed=9)))                                # still being written down: next turn
    nothing = asyncio.run(g.decide())
    assert nothing.answer and nothing.score is None and len(g.pieces) == 1


def test_the_setting_off_turns_the_speaker_check_off(tmp_path, migrated):
    engine = make_engine(migrated.db_url)
    s = taught(tmp_path, engine)
    assert s.mode() == "normal"
    s.set_mode("off")
    assert s.check_state() == (False, "set to off (Settings → Voice)")
    assert turn(s.gate(), (voice(2.0, OTHER_HZ, seed=3), True)).answer
    s.set_mode("strict")
    assert not turn(s.gate(), (voice(2.0, NEAR_HZ, seed=4), True)).answer
    with pytest.raises(sp.SpeakerError):
        s.set_mode("loud")


def test_a_slow_or_broken_check_answers_the_turn(tmp_path, monkeypatch):
    s = taught(tmp_path)

    def broken(samples):
        raise RuntimeError("CUDA went away")
    s.model.embed = broken
    v = turn(s.gate(), (voice(2.0, OTHER_HZ, seed=3), True))
    assert v.answer and "CUDA went away" in v.why
    monkeypatch.setattr(sp, "WAIT_MAX_S", 0.05)
    s.measure = lambda pcm, on: time.sleep(0.5)
    late = turn(s.gate(), (voice(1.0), True))
    assert late.answer and late.why == "slow" and late.wait_ms < 400


def test_an_ignored_turn_never_logs_or_keeps_its_words(tmp_path, migrated, caplog):
    engine = make_engine(migrated.db_url)
    s = taught(tmp_path, engine)
    g = s.gate()
    inner = SimpleNamespace(transcribe_pcm=lambda pcm, language=None: SimpleNamespace(text="the secret plan", language="en"),
                            speak=lambda text, language=None: (b"", "en"))
    heard = sp.Listening(inner, g)
    caplog.set_level(logging.DEBUG)
    assert heard.transcribe_pcm(pcm(voice(2.0, OTHER_HZ, seed=3))).text == "the secret plan"
    assert heard.speak("hi") == (b"", "en")                                  # everything else passes through
    v = asyncio.run(g.decide())
    assert v.outcome == "not_you"
    s.writer.submit(lambda: None).result()                                  # the record is written
    assert "ignored: not you" in caplog.text and "secret" not in caplog.text
    with Session(engine) as db:
        row = db.get(SettingKV, sp.STATS_KEY)
        assert "secret" not in row.value and json.loads(row.value)[0][2] == "not_you"


# ---------------------------------------------------------------- the app: teach, settings, Voice speed, diagnosis

@pytest.fixture
def app_client(migrated):
    app = create_app(migrated)
    app.state.speaker.model = PitchModel()
    with TestClient(app) as c:
        c.settings = migrated
        yield c


def enroll(c, x, ctype="audio/l16; rate=16000"):
    return c.post("/api/voice/speaker/enroll", content=pcm(x), headers={"content-type": ctype})


def test_teach_kairo_your_voice_keeps_an_encrypted_voiceprint_in_the_data_folder(app_client):
    c = app_client
    st = c.get("/api/voice/speaker").json()
    assert st["enrolled"] is None and st["check"] is False and "teach Kairo your voice" in st["why_off"]
    assert st["mode"] == "normal" and st["modes"] == ["strict", "normal", "off"]
    assert enroll(c, voice(30), "audio/mp4").status_code == 400
    short = enroll(c, voice(4))
    assert short.status_code == 400 and "s of speech" in short.json()["detail"]
    ok = enroll(c, voice(30, seed=1))
    assert ok.status_code == 200, ok.text
    st = ok.json()
    assert st["check"] is True and st["enrolled"]["at"] and st["enrolled"]["speech_s"] >= sp.ENROLL_MIN_S
    assert "vector" not in json.dumps(st)
    stored = c.settings.data_dir / sp.VOICEPRINT
    assert stored.is_file() and REPO_ROOT not in stored.resolve().parents
    raw = stored.read_bytes()
    assert raw.startswith(b"KAIRO-") and b"vector" not in raw                 # secretbox (DPAPI on the PC)
    with Session(make_engine(c.settings.db_url)) as s:
        assert [a.outcome for a in s.exec(select(AuditLog).where(AuditLog.action == "speaker.enroll"))]
    gone = c.delete("/api/voice/speaker").json()
    assert gone["enrolled"] is None and not stored.exists() and gone["check"] is False


def test_two_voices_while_teaching_are_refused(app_client):
    both = np.concatenate([voice(15, OWNER_HZ, seed=1), voice(15, OTHER_HZ, seed=2)])
    r = enroll(app_client, both)
    assert r.status_code == 400 and "more than one voice" in r.json()["detail"]
    assert not (app_client.settings.data_dir / sp.VOICEPRINT).exists()


def test_the_setting_is_strict_normal_or_off(app_client):
    assert app_client.put("/api/voice/speaker/settings", json={"mode": "strict"}).json()["mode"] == "strict"
    assert app_client.put("/api/voice/speaker/settings", json={"mode": "loud"}).status_code == 400
    off = app_client.put("/api/voice/speaker/settings", json={"mode": "off"}).json()
    assert off["mode"] == "off" and off["check"] is False


def test_teaching_is_for_the_owner_only_and_same_origin(migrated, monkeypatch):
    from tests.test_owner_only import GUEST, OWNER, TAILNET, pin
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "LOCAL_HOSTS", {"testclient", "127.0.0.1"})
    strict = migrated.model_copy(update={"owner_check": True})
    pin(strict, "owner@example.com")
    with TestClient(create_app(strict), base_url=TAILNET) as c:
        for method in ("GET", "DELETE"):
            assert c.request(method, "/api/voice/speaker", headers=GUEST).status_code == 403
        assert c.post("/api/voice/speaker/enroll", content=pcm(voice(12)), headers={**GUEST, "content-type": "audio/l16"}).status_code == 403
        cross = c.post("/api/voice/speaker/enroll", content=pcm(voice(12)),
                       headers={**OWNER, "content-type": "audio/l16", "origin": "https://evil.example"})
        assert cross.status_code == 403
        assert c.get("/api/voice/speaker", headers=OWNER).status_code == 200


def test_without_the_model_the_check_is_off_and_says_model_missing(migrated):
    with TestClient(create_app(migrated)) as c:
        st = c.get("/api/voice/speaker").json()
        assert st["check"] is False and "model missing" in st["why_off"]
        assert enroll(c, voice(30)).status_code == 503
        d = c.get("/api/voice/devices").json()
        spk = d["components"]["speaker"]
        assert spk["ok"] is False and "model missing" in spk["reason"] and spk["loaded"] is False


def test_voice_speed_and_diagnosis_show_ignored_turns_without_words(app_client):
    s = app_client.app.state.speaker
    for outcome, score, why in (("not_you", 0.08, "not_you"), ("too_quiet", None, "too quiet / far"),
                                ("answered", 0.81, "you"), ("not_you", 0.12, "not_you")):
        s.record(sp.Verdict(outcome == "answered", outcome, why, score=score, rms_db=-24.0, owner_db=-21.0,
                            speech_s=1.4, ms=6, wait_ms=1, at=s.now().isoformat(timespec="seconds")))
    st = app_client.get("/api/voice/speaker").json()
    assert st["counts"] == {"answered": 1, "not_you": 2, "too_quiet": 1}
    titles = [x["title"] for x in st["ignored"]]
    assert titles == ["ignored: not you", "ignored: too quiet / far", "ignored: not you"]
    assert st["ignored"][0]["score"] == 0.12 and st["ignored"][0]["at"]
    assert all(set(x) == {"at", "reason", "title", "score", "level_db", "owner_db", "speech_s"} for x in st["ignored"])
    assert st["ms"]["p50"] == 6 and st["device"] == "cpu"
    text = app_client.post("/api/diagnosis").json()["text"]
    assert "Speaker check" in text and "2 ignored (not you), 1 ignored (too quiet / far)" in text
    assert "voice taught: no" in text and "speaker:" in text                  # and its device row


# ---------------------------------------------------------------- the old line (/ws/voice)

def test_on_the_old_line_someone_else_is_ignored_and_the_owner_answered(migrated):
    from tests.test_voicelink import FakeVoice, frames_until, utter
    app = create_app(migrated)
    app.state.voice = FakeVoice()
    app.state.speaker.model = PitchModel()
    app.state.speaker.enroll(pcm(voice(30, seed=1)))
    with TestClient(app) as c, c.websocket_connect("/ws/voice") as ws:
        utter(ws, pcm(voice(2.0, OTHER_HZ, seed=3)), turn=1)
        f = ws.receive_json()
        assert f["type"] == "ignored" and f["reason"] == "not_you" and f["turn"] == 1 and "text" not in f
        utter(ws, pcm(voice(2.0, OWNER_HZ, seed=4)), turn=2)
        fr = frames_until(ws)
        assert fr[0]["type"] == "transcript" and fr[0]["turn"] == 2 and fr[-1]["type"] == "spoken"
        utter(ws, pcm(voice(2.0, OTHER_HZ, seed=5)), path="chat_voice", turn=3)   # the voice button: always answered
        assert frames_until(ws)[0]["type"] == "transcript"


# ---------------------------------------------------------------- the WebRTC call (needs the call packages)

def test_over_webrtc_an_ignored_turn_gets_no_answer():
    pytest.importorskip("pipecat")
    pytest.importorskip("aiortc")
    from app import voicertc
    from tests import test_voice_rtc as rtc
    asked: list[str] = []
    gated: list[int] = []

    async def answer(text, language, number, speaker):
        asked.append(text)
        speaker.finish("Sure.")

    async def never() -> bool:
        gated.append(1)
        return False
    original = voicertc.build_call
    try:
        voicertc.build_call = lambda *a, **k: original(*a, **{**k, "gate": never})
        phone = asyncio.run(rtc._call(answer, wait_for=lambda p: bool(gated), timeout=15))
    finally:
        voicertc.build_call = original
    assert gated and asked == [] and not any(e.get("type") == "transcript" for e in phone.events)


class ConstModel(PitchModel):
    """Every voice gets the same print (set by the test)."""

    def __init__(self, vec):
        super().__init__()
        self.vec = np.asarray(vec, dtype=np.float32)

    def embed(self, samples):
        self.calls += 1
        return self.vec if len(samples) >= sp.MIN_CHECK_S * RATE * 0.9 else None


def test_a_call_through_the_app_ignores_a_voice_that_is_not_the_owners(local, monkeypatch):  # noqa: F811
    """The app's own wiring: the phone's audio reaches the gate next to Whisper (speaker.Listening), the
    turn is ignored at voicertc's hook, the phone is told (reason and score) and nothing answers."""
    pytest.importorskip("pipecat")
    pytest.importorskip("aiortc")
    from app import voicertc
    from app.voicertc import OpenerCache
    from tests import test_voice_rtc as rtc
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    local.app.state.voice = rtc.ToneVoice(local.voice)
    local.app.state.openers = OpenerCache(lambda text, lang: (b"\x00\x10" * 2400, 24000))
    model = ConstModel([1.0, 0.0])
    local.app.state.speaker.model = model
    local.app.state.speaker.enroll(pcm(voice(30, seed=1)))                 # the owner's print: (1, 0)
    model.vec = np.array([0.0, 1.0], dtype=np.float32)                     # whoever talks now: (0, 1)
    phone = asyncio.run(rtc._app_call(local, seconds=8))
    kinds = [e.get("type") for e in phone.events]
    ignored = [e for e in phone.events if e.get("type") == "ignored"]
    assert ignored and ignored[0]["reason"] == "not_you" and ignored[0]["score"] < 0.05, kinds
    assert "transcript" not in kinds and "done" not in kinds and not phone.audio_after
    st = local.get("/api/voice/speaker").json()
    assert st["counts"]["not_you"] >= 1 and st["ignored"][0]["title"] == "ignored: not you"


# ---------------------------------------------------------------- loading the model: never a wrong runtime

class FakeRuntime:
    """onnxruntime as 1.24 behaves with this model: with its graph optimizations on, the output is wrong for
    any length that isn't a multiple of 100 frames (cuda_too: on the GPU even without them)."""

    def __init__(self, cuda_too=False):
        rt = self

        class Session:
            def __init__(self, path, sess_options=None, providers=None):
                p = providers[0]
                self.provider = p if isinstance(p, str) else p[0]
                self.optimised = sess_options.graph_optimization_level != "off"

            def get_inputs(self):
                return [SimpleNamespace(name="feats")]

            def get_providers(self):
                return [self.provider]

            def run(self, _names, feeds):
                x = feeds["feats"]
                v = x.mean(axis=1)
                broken = self.optimised or (rt.cuda_too and self.provider == "CUDAExecutionProvider")
                return [np.roll(v, 7) if broken and x.shape[1] % 100 else v]

        class Options:
            graph_optimization_level = "all"
        self.cuda_too, self.__version__ = cuda_too, "1.24.4"
        self.InferenceSession, self.SessionOptions = Session, Options
        self.GraphOptimizationLevel = SimpleNamespace(ORT_DISABLE_ALL="off")


@pytest.mark.parametrize("cuda_too, device, skipped", [(False, "cuda", "CUDA optimised"),
                                                       (True, "cpu", "CUDA optimised, CUDA, CPU optimised")])
def test_a_runtime_that_computes_the_model_wrong_is_never_used(tmp_path, monkeypatch, cuda_too, device, skipped):
    import sys
    from app import voice as voice_mod
    f = tmp_path / sp.MODEL_NAME
    f.write_bytes(b"onnx")
    monkeypatch.setitem(sys.modules, "onnxruntime", FakeRuntime(cuda_too))
    monkeypatch.setattr(voice_mod, "_ort_cuda", lambda site: ([("CUDAExecutionProvider", {}), "CPUExecutionProvider"], None))
    m = sp.SpeakerModel(f)
    assert m.embed(voice(1.0)) is not None
    assert m.device == device and skipped in m.checked and not m._session.optimised
    if device == "cpu":
        assert "wrong on the GPU" in m.why
    row = sp.Speaker(m, tmp_path / "voiceprint.bin").device()
    assert row["checked"] == m.checked


# ---------------------------------------------------------------- the real model, when it is here

def test_the_real_model_scores_a_voice_higher_against_itself(tmp_path):
    """KAIRO_SPEAKER_TEST_MODEL=<path to wespeaker_en_voxceleb_CAM++.onnx> and onnxruntime: CAM++ with this
    frontend on made-up voices. Skipped when either isn't on this machine."""
    path = os.environ.get("KAIRO_SPEAKER_TEST_MODEL")
    if not path or not Path(path).is_file():
        pytest.skip("KAIRO_SPEAKER_TEST_MODEL not set (the CAM++ file)")
    pytest.importorskip("onnxruntime")
    model = sp.SpeakerModel(Path(path))
    a1, a2 = model.embed(voice(3, 110, 1.0, seed=1, wobble=0.06)), model.embed(voice(3, 110, 1.0, seed=2, wobble=0.06))
    b = model.embed(voice(3, 205, 1.17, seed=3, wobble=0.06))
    assert a1.shape == (512,) and abs(float(np.linalg.norm(a1)) - 1) < 1e-4 and model.device in ("cpu", "cuda")
    assert float(a1 @ a2) > float(a1 @ b) + 0.1 and float(a1 @ a2) > float(a2 @ b) + 0.1
    s = sp.Speaker(model, tmp_path / "voiceprint.bin")
    m = s.measure(pcm(voice(2.0, seed=4, wobble=0.06)), True)
    assert m.emb is not None and m.ms >= 0


# ---------------------------------------------------------------- G13: the label over WebRTC (needs the call packages)
# The same end-to-end checks as tests/test_speaker_label_calls.py (old line), here because CI's voice jobs run this file.

G13_WRITE = 'Remind me to call the dentist tomorrow [[fake-tool tasks_create {"title": "call the dentist"}]]'


def _g13_call(local, monkeypatch, *, slow: bool):
    """A WebRTC call whose speaker check says 'owner' for everyone, optionally too slow. Returns (what the phone saw,
    the labels the brain runner was asked to write)."""
    pytest.importorskip("pipecat")
    pytest.importorskip("aiortc")
    from app import speaker_label as sl
    from app import voicertc
    from app.voicertc import OpenerCache
    from tests import test_voice_rtc as rtc
    labels: list[str] = []
    real_begin = sl.VoiceTurns.begin

    def spy(self, label):
        if label is not None:
            labels.append(label)
        return real_begin(self, label)
    monkeypatch.setattr(sl.VoiceTurns, "begin", spy)
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    local.app.state.openers = OpenerCache(lambda text, lang: (b"\x00\x10" * 2400, 24000))
    local.voice.text = G13_WRITE
    local.app.state.voice = rtc.ToneVoice(local.voice)
    local.app.state.speaker.model = ConstModel([1.0, 0.0])
    local.app.state.speaker.enroll(pcm(voice(30, seed=1)))                  # the owner's print: (1, 0), and so is every voice's
    if slow:                                                                # the check is on, and too slow for this turn
        monkeypatch.setattr(sp, "WAIT_MAX_S", 0.05)
        real_measure = local.app.state.speaker.measure

        def late(pcm_, on):
            time.sleep(0.5)
            return real_measure(pcm_, on)
        local.app.state.speaker.measure = late
    return asyncio.run(rtc._app_call(local, seconds=25)), labels


def test_over_webrtc_a_turn_the_check_could_not_confirm_cannot_change_things(local, monkeypatch):  # noqa: F811
    from app.db import Task
    phone, labels = _g13_call(local, monkeypatch, slow=True)
    done = [e for e in phone.events if e.get("type") == "done"]
    assert done and "TOOL-BLOCKED tasks_create" in done[0]["text"], phone.events      # the real hook said no
    assert labels == ["unsure"]
    with Session(make_engine(local.settings.db_url)) as s:
        assert list(s.exec(select(Task))) == [] and s.get(SettingKV, "voice.turn") is None


def test_over_webrtc_an_owner_turn_is_not_blocked(local, monkeypatch):  # noqa: F811
    phone, labels = _g13_call(local, monkeypatch, slow=False)
    done = [e for e in phone.events if e.get("type") == "done"]
    assert done and "TOOL-BLOCKED" not in done[0]["text"], phone.events               # (no tool server here: the call itself errors)
    assert labels == ["owner"]
