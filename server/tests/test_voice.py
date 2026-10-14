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
    assert client.get("/api/voice/status").json()["call_options"] == {"brief": False, "checkin": False}


def test_voice_without_the_libraries(client, monkeypatch):
    client.app_.state.voice = Voice(client.app_.state.settings.data_dir / "models")
    monkeypatch.setattr(Voice, "installed", staticmethod(lambda: {"stt": False, "en": False, "de": False}))
    r = client.post("/api/voice/speak", json={"text": "hello"})
    assert r.status_code == 503 and "not installed" in r.json()["detail"]


def test_call_flow(client):
    c = client.post("/api/voice/calls", json={"reason": "Test call"}).json()
    assert c["status"] == "ringing" and client.pushes[-1][:3] == ("Kairo is calling", "Test call", f"/#call/{c['id']}")
    assert client.pushes[-1][3] == {"ignore_quiet": True}   # the owner asked for it right now
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


def test_brief_comes_as_a_call_when_switched_on(client):
    client.post("/api/voice/options/brief", json={"on": True})
    app = client.app_
    app.state.jobs._ask = lambda p, s: __import__("app.chat", fromlist=["AskResult"]).AskResult(
        '{"headline": "Maths test Friday.", "top3": [{"title": "Study maths", "why": "test"}]}')
    run = app.state.jobs.run_job("morning_brief", forced=True)
    assert run.status == "ok"
    title, body, url, _ = client.pushes[-1]
    assert (title, body) == ("Kairo is calling", "Your morning brief") and url.startswith("/#call/")
    call = client.get(f"/api/voice/calls/{url.rsplit('/', 1)[1]}").json()
    assert call["opening"] == "Good morning. Maths test Friday. Number 1: Study maths. Anything you want to change?"


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


@pytest.mark.skipif(os.environ.get("KAIRO_VOICE_TEST") != "1", reason="real voice models: set KAIRO_VOICE_TEST=1 (CI voice job)")
def test_real_voice_round_trip(tmp_path):
    """Kokoro says a sentence, Whisper (CPU) writes it down again; Piper speaks German."""
    models = os.environ.get("KAIRO_VOICE_MODELS") or str(tmp_path / "models")
    v = Voice(models)
    v.download()
    wav, lang = v.speak("Kairo voice test. Please call the dentist tomorrow.")
    assert lang == "en" and wav[:4] == b"RIFF"
    t = v.transcribe(wav, language="en")
    print("\nWHISPER HEARD:", t.text, t.took, v.status()["stt_device"])
    assert "dentist" in t.text.lower()
    de, lang = v.speak("Guten Morgen. Heute hast du eine Prüfung in Mathe.")
    assert lang == "de" and de[:4] == b"RIFF"
    t = v.transcribe(de, language="de")
    print("WHISPER HEARD (de):", t.text)
    assert "mathe" in t.text.lower()
