"""Voice (phase 3): API with fake engines, calls, brief as a call, voice memos; and a real
end-to-end run (Kokoro speaks, Whisper listens) when KAIRO_VOICE_TEST=1 (CI voice job)."""

from __future__ import annotations

import datetime as dt
import io
import os
import wave

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import capture
from app.db import Call, Capture, make_engine
from app.main import create_app
from app.profile import load_profile
from app.settings import REPO_ROOT
from app.voice import NotInstalled, Transcript, Voice, VoiceError, _clean_for_speech, language_of, to_wav
from app.voice import calls


def test_language_and_cleanup():
    assert language_of("Guten Morgen, du hast heute einen Test und morgen noch einen.") == "de"
    assert language_of("Good morning, you have a test today.") == "en"
    assert language_of("Schöne Grüsse") == "de"
    assert _clean_for_speech("**Top 3:** see [the plan](https://x.y) ```code``` # Title") == "Top 3: see the plan Title"
    long = "Sentence one is here. " * 400
    assert len(_clean_for_speech(long)) <= 3000 and _clean_for_speech(long).endswith(".")


def test_wav_is_valid():
    np = pytest.importorskip("numpy")   # comes with the voice libraries
    data = to_wav(np.zeros(2400, dtype="float32"), 24000)
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 2400)


def test_not_installed_is_a_clear_message(tmp_path, monkeypatch):
    v = Voice(tmp_path)
    monkeypatch.setattr(Voice, "installed", staticmethod(lambda: {"stt": False, "en": False, "de": False}))
    with pytest.raises(NotInstalled):
        v.transcribe(b"x")
    with pytest.raises(NotInstalled):
        v.speak("hello")
    with pytest.raises(VoiceError):
        v.speak("```only code```")


class FakeVoice:
    def __init__(self):
        self.heard = []

    def status(self):
        return {"installed": {"stt": True, "en": True, "de": True}, "stt_device": "cuda"}

    def transcribe(self, audio, language=None):
        self.heard.append(audio)
        if audio == b"broken":
            raise VoiceError("could not understand the audio")
        return Transcript(text="Remind me to call the dentist tomorrow", language="en", seconds=2.1, took=0.4)

    def speak(self, text, language=None):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\0\0" * 100)
        return buf.getvalue(), language or language_of(text)


@pytest.fixture
def client(migrated):
    app = create_app(migrated)
    pushes = []
    app.state.pusher.notify = lambda title, body, url="/", **kw: pushes.append((title, body, url, kw))
    app.state.voice = FakeVoice()
    with TestClient(app) as c:
        c.app_, c.pushes = app, pushes
        yield c


def test_voice_api(client):
    r = client.post("/api/voice/transcribe", content=b"m4a bytes", headers={"Content-Type": "audio/mp4"})
    assert r.json()["text"].startswith("Remind me")
    assert client.post("/api/voice/transcribe", content=b"x", headers={"Content-Type": "text/plain"}).status_code == 400
    assert client.post("/api/voice/transcribe", content=b"broken", headers={"Content-Type": "audio/mp4"}).status_code == 400
    r = client.post("/api/voice/speak", json={"text": "Hallo, du hast heute einen Test."})
    assert r.headers["content-type"] == "audio/wav" and r.headers["x-kairo-language"] == "de" and r.content[:4] == b"RIFF"
    assert client.get("/api/voice/status").json()["call_options"] == {"checkin": False}   # v8 D: the brief is never a call
    kinds = {r["kind"]: r["n"] for r in client.get("/api/latency").json()["rows"]}
    assert kinds == {"stt": 1, "tts": 1}               # fix plan 31: what worked was timed, failures not


def test_voice_without_the_libraries(client, monkeypatch):
    client.app_.state.voice = Voice(client.app_.state.settings.data_dir / "models")
    monkeypatch.setattr(Voice, "installed", staticmethod(lambda: {"stt": False, "en": False, "de": False}))
    r = client.post("/api/voice/speak", json={"text": "hello"})
    assert r.status_code == 503 and "not installed" in r.json()["detail"]


def test_call_flow(client):
    c = client.post("/api/voice/calls", json={"reason": "Test call"}).json()
    assert c["status"] == "ringing" and client.pushes[-1][:3] == ("Kairo is calling", "Test call", f"/#call/{c['id']}")
    assert client.pushes[-1][3] == {"ignore_quiet": True, "source": "call"}   # the owner asked for it right now
    assert client.post(f"/api/voice/calls/{c['id']}/answer").json()["status"] == "answered"
    assert client.post(f"/api/voice/calls/{c['id']}/end").json()["status"] == "ended"
    assert client.post(f"/api/voice/calls/{c['id']}/answer").json()["status"] == "ended"   # can't re-answer
    assert client.post("/api/voice/calls/999/answer").status_code == 404


def test_unanswered_call_is_missed(migrated):
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        c = Call(reason="x", opening="y", created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20))
        s.add(c)
        s.commit()
        assert calls.get(s, c.id).status == "missed"


def test_the_brief_is_a_push_to_read_and_listen_never_a_call(client):
    """v8 D: Kairo rings only for important things or real questions; the brief is a short push."""
    assert client.post("/api/voice/options/brief", json={"on": True}).status_code in (400, 404)
    app = client.app_
    app.state.jobs._ask = lambda p, s: __import__("app.chat", fromlist=["AskResult"]).AskResult(
        '{"headline": "Maths test Friday.", "top3": [{"title": "Study maths", "why": "test"}]}')
    run = app.state.jobs.run_job("morning_brief", forced=True)
    assert run.status == "ok"
    title, body, url, _ = client.pushes[-1]
    assert (title, body, url) == ("Morning brief", "Maths test Friday.", "/#today/listen")
    assert client.get("/api/voice/calls").json() == []


def test_voice_memos_are_transcribed_then_sorted(migrated, tmp_path):
    engine = make_engine(migrated.db_url)
    profile = load_profile(REPO_ROOT / "config")
    with Session(engine) as s:
        cap = capture.store_file(s, tmp_path, profile, data=b"m4a", content_type="audio/x-m4a", note=None, source="shortcut")
        assert cap.status == "later"
        proc = capture.Processor(engine=engine, vault_dir=tmp_path / "vault", profile=profile,
                                 transcribe=lambda b: "Call the dentist tomorrow")
        assert proc.transcribe_memos(s) == 1
        cap = s.get(Capture, cap.id)
        assert (cap.status, cap.text) == ("new", "Call the dentist tomorrow")
        # Without speech-to-text the memo just waits.
        cap2 = capture.store_file(s, tmp_path, profile, data=b"m4a", content_type="audio/mp4", note=None, source="shortcut")
        failing = capture.Processor(engine=engine, vault_dir=tmp_path, profile=profile,
                                    transcribe=lambda b: (_ for _ in ()).throw(NotInstalled("no")))
        assert failing.transcribe_memos(s) == 0 and s.get(Capture, cap2.id).status == "later"


def heard(expected: str, text: str, share: float = 0.75) -> bool:
    """Most words came through, allowing near misses: CI's small CPU Whisper hears Piper's "Mathe"
    as "Matte" now and then (diagnosis D1.13). One misheard word must not fail the round trip;
    a wrong language or garbage still does."""
    import difflib
    import re
    want = re.findall(r"\w+", expected.lower())
    got = re.findall(r"\w+", text.lower())
    close = sum(1 for w in want if difflib.get_close_matches(w, got, n=1, cutoff=0.75))
    return close >= share * len(want)


def test_near_misses_count_and_garbage_does_not():
    assert heard("Heute hast du eine Prüfung in Mathe.", "Heute hast du eine Prüfung in Matte.")
    assert heard("Please call the dentist tomorrow.", "please call the dentist tomorrow")
    assert not heard("Heute hast du eine Prüfung in Mathe.", "Today you have an exam in math.")
    assert not heard("Please call the dentist tomorrow.", "")


@pytest.mark.skipif(os.environ.get("KAIRO_VOICE_TEST") != "1", reason="real voice models: set KAIRO_VOICE_TEST=1 (CI voice job)")
def test_real_voice_round_trip(tmp_path):
    """Kokoro says a sentence, Whisper (CPU) writes it down again; Piper speaks German."""
    models = os.environ.get("KAIRO_VOICE_MODELS") or str(tmp_path / "models")
    v = Voice(models)
    v.download()
    en_text = "Kairo voice test. Please call the dentist tomorrow."
    wav, lang = v.speak(en_text)
    assert lang == "en" and wav[:4] == b"RIFF"
    t = v.transcribe(wav, language="en")
    print("\nWHISPER HEARD:", t.text, t.took, v.status()["stt_device"])
    assert heard(en_text, t.text) and "dentist" in t.text.lower()
    de_text = "Guten Morgen. Heute hast du eine Prüfung in Mathe."
    de, lang = v.speak(de_text)
    assert lang == "de" and de[:4] == b"RIFF"
    t = v.transcribe(de, language="de")
    print("WHISPER HEARD (de):", t.text)
    assert heard(de_text, t.text), t.text
    # Voice V1: the phone streams 16 kHz PCM; the fast path (no file, greedy) hears it too, language found by itself
    import numpy as np
    from app.voicelink import wav_pcm
    pcm, rate = wav_pcm(wav)
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    x16 = np.interp(np.arange(0, len(x), rate / 16000), np.arange(len(x)), x).astype("<i2").tobytes()
    t = v.transcribe_pcm(x16)
    print("WHISPER HEARD (pcm):", t.text, t.took, t.language)
    assert t.language == "en" and heard(en_text, t.text) and "dentist" in t.text.lower()


def test_owner_calls_kairo(client):
    """'Call Kairo' Shortcut: the call starts answered, nothing rings, no push."""
    c = client.post("/api/voice/calls", json={"outgoing": True}).json()
    assert c["status"] == "answered" and c["reason"] == "You called Kairo"
    assert client.pushes == []


def _fake_whisper(monkeypatch, tmp_path, behave):
    """A stand-in faster_whisper module: behave(device, path) builds or raises."""
    import sys
    import types
    mod = types.ModuleType("faster_whisper")
    made = []

    class WhisperModel:
        def __init__(self, path, device="cpu", compute_type="int8", **kw):
            made.append((device, str(path), kw))
            behave(device, str(path), kw)
            self.device = device

        def transcribe(self, audio, **kw):
            if getattr(self, "fail", None):
                raise RuntimeError(self.fail)
            info = types.SimpleNamespace(language="en", duration=1.0)
            return iter([types.SimpleNamespace(text=" hello")]), info

    mod.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    monkeypatch.setattr(Voice, "installed", staticmethod(lambda: {"stt": True, "en": False, "de": False}))
    return made


def test_model_load_failure_is_a_clear_error_not_a_crash(tmp_path, monkeypatch):
    """Real use: 'speech recognition failed (500)'. A broken model load must say why (503), never crash."""
    def behave(device, path, kw):
        raise PermissionError("[WinError 5] Access is denied: 'whisper\\.locks'")
    _fake_whisper(monkeypatch, tmp_path, behave)
    v = Voice(tmp_path)
    with pytest.raises(NotInstalled) as e:
        v.transcribe(b"audio")
    assert "Access is denied" in str(e.value) and "Access is denied" in v.status()["last_error"]


def test_downloaded_model_loads_offline_from_its_folder(tmp_path, monkeypatch):
    """No Hugging Face lookups (network, lock files) at run time when Update Kairo already fetched the model."""
    from app.voice import WHISPER_CPU
    snap = tmp_path / "whisper" / f"models--Systran--faster-whisper-{WHISPER_CPU}" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    made = _fake_whisper(monkeypatch, tmp_path, lambda d, p, kw: (_ for _ in ()).throw(RuntimeError("no CUDA")) if d == "cuda" else None)
    v = Voice(tmp_path)
    assert v.transcribe(b"audio").text == "hello"
    assert made[-1][0] == "cpu" and made[-1][1] == str(snap)


def test_gpu_that_fails_at_run_time_falls_back_to_cpu(tmp_path, monkeypatch):
    """CUDA can load the model but fail on first use (missing cuBLAS DLL): switch to the CPU and retry once."""
    made = _fake_whisper(monkeypatch, tmp_path, lambda d, p, kw: None)
    v = Voice(tmp_path)
    v._load_stt().fail = "Library cublas64_12.dll is not found or cannot be loaded"
    assert v.transcribe(b"audio").text == "hello"
    assert v.status()["stt_device"] == "cpu" and made[-1][0] == "cpu"


def test_failed_load_is_not_retried_on_every_word(tmp_path, monkeypatch):
    """A broken install answers fast instead of trying (and failing) slowly for each sentence."""
    calls_ = []
    def behave(device, path, kw):
        calls_.append(device)
        raise OSError("model.bin missing")
    _fake_whisper(monkeypatch, tmp_path, behave)
    v = Voice(tmp_path)
    for _ in range(3):
        with pytest.raises(NotInstalled):
            v.transcribe(b"audio")
    assert len(calls_) == 2   # GPU try + CPU try, once


def test_unexpected_voice_error_is_never_a_bare_500(client):
    class Boom(FakeVoice):
        def transcribe(self, audio, language=None):
            raise RuntimeError("something odd")
    client.app_.state.voice = Boom()
    r = client.post("/api/voice/transcribe", content=b"m4a", headers={"Content-Type": "audio/mp4"})
    assert r.status_code == 503 and "something odd" in r.json()["detail"]
