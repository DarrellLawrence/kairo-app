from fastapi.testclient import TestClient

from app.main import create_app


def converse(ws, text, thread_id=None):
    ws.send_json({"type": "message", "text": text, "thread_id": thread_id})
    frames = []
    while True:
        f = ws.receive_json()
        frames.append(f)
        if f["type"] in ("done", "error"):
            return frames


def test_chat_streams_and_persists(migrated):
    with TestClient(create_app(migrated)) as client, client.websocket_connect("/ws/chat") as ws:
        frames = converse(ws, "hello kairo")
        assert frames[0]["type"] == "thread"
        assert any(f["type"] == "delta" for f in frames)
        assert frames[-1] == {"type": "done", "text": "echo: hello kairo", "message_id": frames[-1]["message_id"]}
        tid = frames[0]["id"]
        r = client.get("/api/chat/messages").json()
        assert r["thread_id"] == tid
        assert [(m["role"], m["text"]) for m in r["messages"]] == [("owner", "hello kairo"), ("kairo", "echo: hello kairo")]


def test_follow_up_resumes_same_brain_session(migrated, tmp_path, monkeypatch):
    import json

    rec = tmp_path / "rec.json"
    monkeypatch.setenv("KAIRO_HOOK_FAKE_RECORD", str(rec))
    with TestClient(create_app(migrated)) as client, client.websocket_connect("/ws/chat") as ws:
        first = converse(ws, "one")
        assert "--resume" not in json.loads(rec.read_text())["argv"]
        second = converse(ws, "two", thread_id=first[0]["id"])
        assert second[0]["id"] == first[0]["id"]
        argv = json.loads(rec.read_text())["argv"]
        assert "--resume" in argv


def test_usage_limit_is_reported_not_crashed(migrated, monkeypatch):
    monkeypatch.setenv("KAIRO_HOOK_FAKE_MODE", "usage_limit")
    with TestClient(create_app(migrated)) as client, client.websocket_connect("/ws/chat") as ws:
        frames = converse(ws, "hi")
        assert frames[-1]["type"] == "error"
        assert frames[-1]["status"] == "usage_limited"
        assert client.get("/health").json()["brain"] == "usage_limited"


def test_bad_input_is_rejected(migrated):
    with TestClient(create_app(migrated)) as client, client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "message", "text": "   "})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "nope"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "message", "text": "x" * 20_001})
        assert ws.receive_json()["type"] == "error"


def test_hanging_up_mid_answer_still_saves_the_reply(migrated, monkeypatch):
    """v7: say something on a call and hang up; Kairo finishes and keeps the answer."""
    import time

    monkeypatch.setenv("KAIRO_HOOK_FAKE_MODE", "pause")
    with TestClient(create_app(migrated)) as client:
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"type": "message", "text": "(spoken, on a call) remind me about the dentist"})
            tid = ws.receive_json()["id"]
            assert ws.receive_json()["type"] == "delta"
        deadline = time.time() + 15
        while time.time() < deadline:
            msgs = client.get("/api/chat/messages").json()["messages"]
            if any(m["role"] == "kairo" for m in msgs):
                break
            time.sleep(0.2)
        assert [m["role"] for m in msgs] == ["owner", "kairo"]
        assert msgs[-1]["text"] == "echo: (spoken, on a call) remind me about the dentist"


def test_every_message_gets_its_own_answer(migrated, tmp_path, monkeypatch):
    """Owner's bug report: two different messages must reach the model as themselves and get two
    different replies, never a replayed brief."""
    import json

    rec = tmp_path / "rec.json"
    monkeypatch.setenv("KAIRO_HOOK_FAKE_RECORD", str(rec))
    with TestClient(create_app(migrated)) as client, client.websocket_connect("/ws/chat") as ws:
        a = converse(ws, "Quiz me for my Mathe test on Friday")
        first = json.loads(rec.read_text())
        b = converse(ws, "Move gym to tomorrow")
        second = json.loads(rec.read_text())
    assert first["stdin"] == "Quiz me for my Mathe test on Friday"
    assert second["stdin"] == "Move gym to tomorrow"
    assert a[-1]["text"] != b[-1]["text"]
    system = second["argv"][second["argv"].index("--system-prompt-file") + 1]
    text = open(system, encoding="utf-8").read()
    assert "Morning brief" not in text and "Answer first" in text and "Right now:" in text


def test_a_call_runs_on_a_warm_claude_process(migrated, tmp_path, monkeypatch):
    """Fix plan 30: Claude is started when the call starts and serves every turn of it; hanging up
    closes it."""
    import json
    import time

    rec = tmp_path / "rec.json"
    monkeypatch.setenv("KAIRO_HOOK_FAKE_RECORD", str(rec))
    with TestClient(create_app(migrated)) as client:
        brain = client.app.state.runner.brain
        call = client.post("/api/voice/calls", json={"outgoing": True}).json()
        deadline = time.monotonic() + 15
        while brain._warm is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert brain._warm is not None
        with client.websocket_connect("/ws/chat") as ws:
            one = converse(ws, "(spoken, on a call) one")
            two = converse(ws, "(spoken, on a call) two")
        assert one[-1]["text"] == "echo: (spoken, on a call) one"
        assert two[-1]["text"] == "echo: (spoken, on a call) two"
        data = json.loads(rec.read_text())
        assert data["turns"] == ["(spoken, on a call) one", "(spoken, on a call) two"]   # one process
        assert "--input-format" in data["argv"]
        client.post(f"/api/voice/calls/{call['id']}/end")
        assert brain._warm is None


def test_typing_during_a_call_retires_the_warm_process(migrated):
    """A typed message continues the conversation without the warm process, which would then be a
    turn behind: it goes, and the call's next turn starts a fresh one."""
    import time

    with TestClient(create_app(migrated)) as client:
        brain = client.app.state.runner.brain
        client.post("/api/voice/calls", json={"outgoing": True})
        deadline = time.monotonic() + 15
        while brain._warm is None and time.monotonic() < deadline:
            time.sleep(0.05)
        with client.websocket_connect("/ws/chat") as ws:
            converse(ws, "(spoken, on a call) one")
            old = brain._warm
            assert old is not None and old.session_id
            converse(ws, "typed meanwhile")
            assert brain._warm is not old
            old.proc.wait(timeout=10)
