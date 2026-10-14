"""Voice notes (v7 item 7): record, send, hang up; Kairo transcribes and handles it."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import capture
from app.brain import BrainEvent, EventKind
from app.main import create_app
from app.voicenote import VoiceNotes
from tests.test_voice import FakeVoice


@pytest.fixture
def client(migrated, request, tmp_path, monkeypatch):
    for k, v in getattr(request, "param", {}).items():   # the fake CLI reads these when the app is built
        monkeypatch.setenv(k, v.format(tmp=tmp_path))
    app = create_app(migrated)
    pushes = []
    app.state.pusher.notify = lambda title, body, url="/", **kw: pushes.append((title, body, url))
    app.state.voice = FakeVoice()
    with TestClient(app) as c:
        c.app_, c.pushes = app, pushes
        c.token = capture.token(app.state.settings.secrets_dir)
        yield c


def wait_for(client, cap_id, done=("filed", "new", "later"), seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        cap = next(c for c in client.get("/api/captures").json() if c["id"] == cap_id)
        if cap["status"] in done:
            return cap
        time.sleep(0.2)
    raise AssertionError(f"capture {cap_id} still {cap['status']}")


def send(client, body=b"m4a bytes", ctype="audio/mp4", token=True):
    headers = {"Content-Type": ctype}
    if token:
        headers["Authorization"] = f"Bearer {client.token}"
    return client.post("/api/voice-note", content=body, headers=headers)


def test_needs_the_token_and_audio(client):
    assert send(client, token=False).status_code == 401
    assert send(client, body=b"hi", ctype="text/plain").status_code == 400
    assert send(client, body=b"x", ctype="image/jpeg").status_code == 400


@pytest.mark.parametrize("client", [{"KAIRO_HOOK_FAKE_RECORD": "{tmp}/rec.json"}], indirect=True)
def test_voice_note_is_handled_like_a_chat_message(client, tmp_path):
    rec = tmp_path / "rec.json"
    r = send(client)
    assert r.status_code == 200 and r.json()["message"] == "Got it."
    cap = wait_for(client, r.json()["id"])
    assert cap["status"] == "filed" and cap["filed_as"] == "voice_note"
    assert cap["result"]["reply"] == "echo: (voice note) Remind me to call the dentist tomorrow"
    msgs = client.get("/api/chat/messages").json()["messages"]
    assert [(m["role"], m["text"]) for m in msgs] == [
        ("owner", "(voice note) Remind me to call the dentist tomorrow"),
        ("kairo", "echo: (voice note) Remind me to call the dentist tomorrow")]
    run = json.loads(rec.read_text())
    assert run["env"]["KAIRO_HOOK_ORIGIN"] == "voice_note"      # gate: no approval cards from a voice note
    assert run["argv"][run["argv"].index("--model") + 1] == "sonnet"
    assert client.pushes == []                                  # not urgent: no push


def test_app_upload_works_without_token(client):
    r = client.post("/api/voice-notes", content=b"m4a bytes", headers={"Content-Type": "audio/mp4"})
    assert wait_for(client, r.json()["id"])["status"] == "filed"


def test_unreadable_audio_waits_for_the_capture_job(client):
    r = send(client, body=b"broken")
    assert wait_for(client, r.json()["id"])["status"] == "later"


@pytest.mark.parametrize("client", [{"KAIRO_HOOK_FAKE_MODE": "usage_limit"}], indirect=True)
def test_model_down_leaves_it_for_the_capture_job(client):
    cap = wait_for(client, send(client).json()["id"])
    assert cap["status"] == "new" and cap["text"].startswith("Remind me")


def test_kill_switch_only_stores_it(client):
    client.post("/api/kill-switch", json={"paused": True})
    cap = wait_for(client, send(client).json()["id"])
    assert cap["status"] == "new"


class StubRunner:
    class brain:
        name = "claude_cli"

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    async def stream(self, prompt, **kw):
        self.calls.append((prompt, kw))
        yield BrainEvent(EventKind.result, text=self.reply, session_id="s1")


def test_urgent_notes_push(migrated):
    app = create_app(migrated)
    pushes = []
    app.state.pusher.notify = lambda title, body, url="/", **kw: pushes.append((title, body, url))
    app.state.voice = FakeVoice()
    runner = StubRunner("URGENT: Your train leaves in 20 minutes, go now.")
    vn = VoiceNotes(engine=app.state.voice_notes.engine, inbox_dir=app.state.voice_notes.inbox_dir,
                    profile=app.state.voice_notes.profile, runner=runner, state=app.state, system=lambda **k: "sys",
                    tools=["t"], pick_model=lambda p: "sonnet", paused=lambda: False)
    cap = vn.accept(b"m4a bytes", "audio/mp4", "voice_note")
    asyncio.run(vn.process(cap.id))
    assert runner.calls[0][1]["origin"] == "voice_note"
    assert pushes == [("Kairo", "Your train leaves in 20 minutes, go now.", "/#chat")]
