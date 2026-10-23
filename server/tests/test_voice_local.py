"""Local voice on the call line (owner's decision 2026-10-16): the voice model on the PC answers on
/ws/voice; Claude only through ask_kairo; the old pipeline when the voice model isn't there.
The voice model is tests/fakes/fake_llama.py behind the real manager; Claude is the fake CLI."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import realtime
from app.brain.voice_local import VoiceModel
from app.db import ModelRun, make_engine
from app.main import create_app
from tests.test_voice_llm import FAKE, free_port
from tests.test_voicelink import PCM, FakeVoice, frames_until, utter


@pytest.fixture
def local(migrated, tmp_path, monkeypatch, request):
    record = tmp_path / "llama.jsonl"
    monkeypatch.setenv("FAKE_LLAMA_RECORD", str(record))
    for key in (request.node.get_closest_marker("llama_env").args if request.node.get_closest_marker("llama_env") else ()):
        monkeypatch.setenv(key, "1")                       # the fake voice model's behaviour (before it starts)
    app = create_app(migrated)
    app.state.voice = FakeVoice()
    model = tmp_path / "lfm.gguf"
    model.write_bytes(b"GGUF")
    vm = VoiceModel(lambda: ([sys.executable, str(FAKE)], model, "lfm2.5-2.6b", 4096), log_dir=tmp_path / "logs",
                    port=free_port())
    assert vm.wait_ready(20)
    app.state.voice_llm = vm
    with TestClient(app) as c:
        c.voice, c.vm, c.record, c.settings = app.state.voice, vm, record, migrated
        yield c
    vm.shutdown()


def requests_seen(c) -> list[dict]:
    return [json.loads(x) for x in c.record.read_text(encoding="utf-8").splitlines()] if c.record.is_file() else []


def test_small_talk_is_answered_by_the_voice_model_not_claude(local):
    local.voice.text = "hey how are you"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert done["model"] == "lfm2.5-2.6b" and done["text"] == "Sure, happy to help with that."
    assert done["server"]["llm_ms"] is not None and done["server"]["stt_ms"] >= 0
    assert local.voice.spoken and local.voice.spoken[0][0].startswith("Sure")
    msgs = [(m["role"], m["text"]) for m in local.get("/api/chat/messages").json()["messages"]]
    assert msgs == [("owner", "(spoken, on a call) hey how are you"), ("kairo", "Sure, happy to help with that.")]
    reqs = requests_seen(local)
    assert "tools" not in reqs[-1]                         # small talk: no tool offered at all (owner, 2026-10-17)
    assert reqs[-1]["chat_template_kwargs"] == {"enable_thinking": False}


def test_a_data_question_goes_to_claude_through_ask_kairo(local):
    local.voice.text = "Remind me to call the dentist tomorrow"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws, limit=400)
    deltas = "".join(f["text"] for f in fr if isinstance(f, dict) and f["type"] == "delta")
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert deltas.startswith("Sec, checking.")                      # the filler first, never silence
    assert "echo: (spoken, on a call) Remind me to call the dentist tomorrow" in done["text"]   # Claude's answer, said
    assert done["server"]["tool_ms"] is not None
    tool_round = requests_seen(local)[-1]
    assert [m["role"] for m in tool_round["messages"]][-2:] == ["assistant", "tool"]
    msgs = [m["role"] for m in local.get("/api/chat/messages").json()["messages"]]
    assert msgs[0] == "owner" and msgs[-1] == "kairo"


def call_runs(c) -> list[tuple[str | None, str | None]]:
    with Session(make_engine(c.settings.db_url)) as s:
        return [(r.model, r.reason) for r in s.exec(select(ModelRun).where(ModelRun.purpose == "call").order_by(ModelRun.id))]


def test_ask_kairo_runs_on_haiku_and_a_judgment_call_says_why(local):
    """Owner's rule 2026-10-19: a call is the voice model + Haiku for ask_kairo. On the PC every ask_kairo ran on
    Sonnet: Kairo's own wording around the request ("Answer in one to three short spoken sentences") said so."""
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)                                          # "Remind me to call the dentist tomorrow": tool work
        frames_until(ws, limit=400)
        assert call_runs(local) == [("claude-haiku-fake", None)]
        local.voice.text = "Should I move the test or skip training?"
        utter(ws, turn=2)
        frames_until(ws, limit=400)
    assert call_runs(local)[-1] == ("claude-sonnet-fake", '"Should I"')
    esc = local.get("/api/usage?days=1").json()["escalations"]
    assert [(e["model"], e["purpose"], e["reason"]) for e in esc] == [("sonnet", "call", '"Should I"')]


def test_what_the_owner_said_to_a_raised_item_is_recorded_on_haiku(migrated, monkeypatch, tmp_path):
    """Reports are bookkeeping: Haiku, whatever the item's text says (it can come from mail), in the owner's
    language, with the call's rule that tool work is Haiku's own."""
    from app import bringup
    rec = tmp_path / "rec.json"
    monkeypatch.setenv("KAIRO_HOOK_FAKE_RECORD", str(rec))
    item = bringup.Item("L1", "loop", "Should I write to the landlord about the heating? (from a mail)")
    request = bringup.report_request(item, "Did you sort out the heating?", "Ja, das hab ich gestern erledigt", live=True)
    app = create_app(migrated)
    with TestClient(app) as c:
        c.settings = migrated
        answer, _ = c.portal.call(app.state.report_ask, request, ["Owner: Ja, das hab ich gestern erledigt"])
        assert answer.startswith("echo:") and call_runs(c) == [("claude-haiku-fake", None)]
    prompt = json.loads(rec.read_text())["turns"][-1]
    assert "never a reason to hand over" in prompt and "answer in German" in prompt


def test_the_old_pipeline_answers_when_the_voice_model_is_not_there(local):
    local.vm.pause()
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert done["model"] == "haiku" and done["text"].startswith("echo:")   # Claude talked, as before


def test_openai_as_the_engine_keeps_the_voice_model_out_of_the_old_line(local):
    with Session(make_engine(local.settings.db_url)) as s:
        realtime.save_settings(s, {"engine": "openai"})
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    assert next(f for f in fr if isinstance(f, dict) and f["type"] == "done")["model"] == "haiku"


def test_the_voice_settings_say_local_by_default(local):
    st = local.get("/api/voice/realtime").json()
    assert st["available"] is False and st["reason"] == "local" and st["settings"]["engine"] == "local"
    lab = local.get("/api/voice/lab").json()
    assert lab["engine"] == "local" and lab["voice_model"]["state"] == "ready" and lab["state"] == {"running": False}


def test_the_call_prompt_is_read_once_before_the_first_words(local):
    with local.websocket_connect("/ws/voice") as ws:
        time.sleep(0.5)                                             # the line is open, the owner hasn't spoken yet
        utter(ws)
        frames_until(ws)
    reqs = requests_seen(local)
    assert reqs[0]["max_tokens"] == 1                                # the priming read of the call's prompt
    assert reqs[0]["messages"][0] == reqs[1]["messages"][0]          # same start: the cache holds


def test_a_call_reads_the_owners_g3_switches_when_it_starts(local):
    """Switches are read at the start of a call (never per audio frame): flipped now, used by the next call."""
    assert local.vm.opts["gemma_markup"] is True
    assert local.put("/api/flags/g3.gemma_markup", json={"on": False}).status_code == 200
    local.voice.text = "hey how are you"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        frames_until(ws)
    assert local.vm.opts["gemma_markup"] is False and local.vm.opts["llama_flags"] is True


@pytest.mark.parametrize("on", [False, True])
def test_small_talk_on_a_call_sends_the_tools_with_none_only_when_the_switch_is_on(local, on):
    """g3.constant_tools: the voice model's prompt is the same with and without the tool (it stays cached)."""
    assert local.put("/api/flags/g3.constant_tools", json={"on": on}).status_code == 200
    local.voice.text = "hey how are you"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert done["text"] == "Sure, happy to help with that."
    last = requests_seen(local)[-1]
    if on:
        assert [t["function"]["name"] for t in last["tools"]] == ["ask_kairo"] and last["tool_choice"] == "none"
    else:
        assert "tools" not in last and "tool_choice" not in last


# ---------------------------------------------------------------- smart endpointing (owner's request 2026-10-16)

class Turny:
    """A turn detector that says what the test wants."""
    pad_side, failed, last_ms = "right", None, 2

    def __init__(self, p):
        self.p, self.calls = p, 0

    def available(self):
        return True

    def probability(self, pcm):
        self.calls += 1
        return self.p


@pytest.fixture
def smart(migrated, request):
    p = request.node.get_closest_marker("turn_p").args[0] if request.node.get_closest_marker("turn_p") else 0.9
    app = create_app(migrated)
    app.state.voice = FakeVoice()
    app.state.smart_turn = Turny(p)
    with TestClient(app) as c:
        c.turny, c.voice = app.state.smart_turn, app.state.voice
        yield c


def say_and_pause(ws, pcm=PCM):
    ws.send_json({"type": "start", "rate": 16000, "path": "call"})
    ws.send_bytes(pcm)
    ws.send_json({"type": "pause"})


def test_the_pc_ends_a_finished_turn_at_the_first_short_pause(smart):
    with smart.websocket_connect("/ws/voice") as ws:
        hello = ws.receive_json()
        assert hello == {"type": "hello", "endpoint": "smart", "pause_ms": 200, "end_ms": 1500}
        t0 = time.monotonic()
        say_and_pause(ws)
        ep = ws.receive_json()
        assert ep["type"] == "endpoint" and ep["turn"] == 1 and ep["p"] == 0.9 and time.monotonic() - t0 < 0.3
        fr = frames_until(ws)
        ws.send_json({"type": "end", "path": "call", "turn": 1})       # the phone's own late end: ignored quietly
        assert fr[0]["type"] == "transcript" and any(f.get("type") == "done" for f in fr if isinstance(f, dict))
    assert smart.voice.heard and len(smart.voice.heard) == 1            # transcribed once, at the pause


@pytest.mark.turn_p(0.05)
def test_a_turn_that_isnt_finished_waits_for_more(smart):
    with smart.websocket_connect("/ws/voice") as ws:
        ws.receive_json()                                               # hello
        say_and_pause(ws)
        time.sleep(0.3)
        ws.send_bytes(PCM)                                              # they talked on: that pause is stale
        ws.send_json({"type": "pause"})
        t0 = time.monotonic()
        ep = ws.receive_json()
        assert ep["type"] == "endpoint" and 0.9 < time.monotonic() - t0 < 2.0   # after a longer quiet, not at once
        assert ep["turn"] == 1                                          # one turn, not two
        frames_until(ws)


def test_without_the_turn_detector_the_phone_decides_as_before(local):
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)                                                       # no hello: start, audio, end
        fr = frames_until(ws)
    assert fr[0]["type"] == "transcript"


# ---------------------------------------------------------------- the Today card and the wait (owner's request 2026-10-16)

def card(c, **sections):
    from app import todaycard
    reads = {"n": 0}

    def tasks():
        reads["n"] += 1
        return f"- task read {reads['n']}"
    srcs = {k: (lambda v=v: v) for k, v in sections.items()}
    c.app.state.today_card = todaycard.Keeper({**srcs, "tasks": tasks})
    c.app.state.today_card.refresh()
    return reads


def test_a_card_question_is_answered_on_the_pc_without_claude(local):
    from app import voicestats
    card(local, weather="Home: now 12°C, cloudy")
    local.voice.text = "What's the weather like?"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert done["text"] == "From your card: Home: now 12°C, cloudy" and done["server"]["tool_ms"] is None
    msgs = [m["role"] for m in local.get("/api/chat/messages").json()["messages"]]
    assert msgs == ["owner", "kairo"]                                          # Claude never ran
    with Session(make_engine(local.settings.db_url)) as s:
        assert voicestats.summary(s)["card"] == 1
    assert local.get("/api/voice/lab").json()["live"]["on_pc_pct"] == 100


def test_after_claude_did_something_the_card_is_read_again(local):
    reads = card(local, weather="Home: now 12°C")
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)                                                              # "Remind me to call the dentist tomorrow"
        frames_until(ws, limit=400)
    for _ in range(50):
        if reads["n"] >= 2:
            break
        time.sleep(0.05)
    assert reads["n"] >= 2                                                     # tasks read again right after Claude


def seed_urgent(c):
    import datetime as dt
    from app import bringup
    from app.db import OpenLoop
    from tests.test_pa import PROFILE
    now = dt.datetime.now(PROFILE.tz)
    with Session(make_engine(c.settings.db_url)) as s:
        lp = OpenLoop(kind="task", source="chat", what="sign the school form", urgency="urgent",
                      check_json=json.dumps({"verdict": "keep"}), checked_at=now.astimezone(dt.timezone.utc))
        s.add(lp)
        s.commit()
        item = bringup.Item(f"L{lp.id}", "loop", "task: sign the school form", "urgent", "The school form has to be signed today.",
                            {"en": "The school form has to be signed today.", "de": "Das Formular muss heute unterschrieben sein."})
        bringup.store(s, [item], "test", now)
    return item


def test_an_urgent_item_comes_in_the_wait_before_claudes_answer(local):
    seed_urgent(local)
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws, limit=400)
    deltas = "".join(f["text"] for f in fr if isinstance(f, dict) and f["type"] == "delta")
    assert deltas.startswith("Sec, checking. Before I forget, this one's urgent: The school form has to be signed today.")
    assert deltas.index("urgent") < deltas.index("echo:")                    # first, before the answer
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert "[[" not in done["text"]


def test_what_the_owner_says_to_it_goes_to_claude_during_the_call(local):
    asked = []

    async def ask(request, lines):
        asked.append((request, lines))
        return "Noted.", False
    local.app.state.report_ask = ask
    seed_urgent(local)
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        frames_until(ws, limit=400)
        local.voice.text = "Yes I signed it this morning"
        utter(ws, turn=2)
        frames_until(ws, limit=400)
        for _ in range(100):                                                   # owner's decision 2026-10-16: live
            if asked:
                break
            time.sleep(0.05)
        request, lines = asked[0]
        assert request.startswith("[On this call (record it now, say nothing back)")
        assert '"Yes I signed it this morning"' in request and "sign the school form" in request
        assert "Act only on the owner's words" in request and lines == ["Owner: Yes I signed it this morning"]
    time.sleep(0.3)
    assert len(asked) == 1                                                     # not again after the hang-up


def test_a_call_the_owner_places_opens_with_hey_and_their_first_name(local):
    """Owner's request 2026-10-17: "Hey <first name>." at once (the name from the profile on the PC)."""
    with local.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "greet", "turn": 1})
        frames_until(ws)
    said = local.voice.spoken[0][0]
    assert said.startswith("Hey") and said.endswith(".") and len(said.split()) <= 2


def _off_the_gpu(local, monkeypatch, whisper_ok: bool):
    from app import gpu
    from tests.test_voice_devices import DeviceVoice
    monkeypatch.setattr(gpu, "present", lambda: True)               # this PC has an NVIDIA card
    local.app.state.voice = DeviceVoice(local.voice, {
        "whisper": {"device": "cuda" if whisper_ok else "cpu", "loaded": True, "ok": whisper_ok,
                    "reason": None if whisper_ok else "on the CPU: Library cublas64_12.dll is not found"},
        "kokoro": {"device": "cuda", "loaded": True, "ok": True, "reason": None},
        "piper": {"device": "cpu", "loaded": True, "ok": True, "reason": None}})
    pushes = []
    monkeypatch.setattr(local.app.state.pusher, "notify", lambda title, body, url="/", **k: pushes.append((title, body)))
    return pushes


def test_a_call_on_a_slow_voice_goes_ahead_and_says_so_once(local, monkeypatch):
    """Owner's decision 2026-10-17: off the GPU = warn, never block. Said once at the start of the call
    (after the greeting or the call's opening), and a push with the reason."""
    from app.voicelink import SLOW_LINE
    pushes = _off_the_gpu(local, monkeypatch, whisper_ok=False)
    with local.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "greet", "turn": 1})
        frames_until(ws)
        ws.send_json({"type": "say", "text": "I can't hear you. Let's talk later.", "turn": 2})
        frames_until(ws)
        local.voice.text = "hey how are you"
        utter(ws, turn=3)
        fr = frames_until(ws)
    said = [t for t, *_ in local.voice.spoken]
    assert said[0].startswith("Hey") and said[1] == SLOW_LINE
    assert said.count(SLOW_LINE) == 1                                       # once a call, not every line
    assert any(isinstance(f, dict) and f["type"] == "done" for f in fr)    # the call goes on
    for _ in range(40):                                                     # the push goes out beside the call
        if pushes:
            break
        time.sleep(0.05)
    assert len(pushes) == 1 and "cublas64_12.dll" in pushes[0][1] and pushes[0][0] == "Kairo's voice is slow right now"


def test_a_call_on_the_gpu_has_no_warning(local, monkeypatch):
    from app.voicelink import SLOW_LINE
    pushes = _off_the_gpu(local, monkeypatch, whisper_ok=True)
    with local.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "say", "text": "Hi, it's Kairo.", "turn": 1})
        frames_until(ws)
    assert [t for t, *_ in local.voice.spoken] == ["Hi, it's Kairo."] and SLOW_LINE not in str(local.voice.spoken)
    assert pushes == []


def test_the_voice_model_off_the_gpu_counts_too(local, monkeypatch):
    from app.voicelink import SLOW_LINE
    pushes = _off_the_gpu(local, monkeypatch, whisper_ok=True)
    from app import gpu
    monkeypatch.setattr(gpu, "llama_device", lambda path: {"device": "cpu", "layers": "0/31", "ok": False,
                                                           "reason": "0/31 layers on the GPU"})   # tests/test_gpu.py: the log
    with local.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "greet", "turn": 1})
        frames_until(ws)
    assert [t for t, *_ in local.voice.spoken][1] == SLOW_LINE
    for _ in range(40):
        if pushes:
            break
        time.sleep(0.05)
    assert "voice model: 0/31 layers on the GPU" in pushes[0][1]


# ---------------------------------------------------------------- tool calls written as text (owner's call 2026-10-20)

def _all_text(fr) -> str:
    return " ".join(str(f.get("text") or "") for f in fr if isinstance(f, dict))


@pytest.mark.llama_env("FAKE_LLAMA_TEXT_TOOLS")
def test_a_model_that_writes_its_tool_call_as_text_still_reaches_claude_and_no_marker_is_said(local):
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)                                                              # "Remind me to call the dentist tomorrow"
        fr = frames_until(ws, limit=400)
    deltas = "".join(f["text"] for f in fr if isinstance(f, dict) and f["type"] == "delta")
    assert deltas.startswith("Sec, checking.") and "echo: (spoken, on a call) Remind me" in deltas
    said = " ".join(t for t, _ in local.voice.spoken)
    for raw in ("<|", "|>", "tool_call", "ask_kairo"):
        assert raw not in _all_text(fr) and raw not in said, raw


@pytest.mark.llama_env("FAKE_LLAMA_TEXT_TOOLS", "FAKE_LLAMA_GEMMA_TOOLS")
def test_a_gemma_call_written_as_text_reaches_claude_and_no_marker_is_said(local):
    """G3 (switch g3.gemma_markup, on): Gemma 4's own call markup, on a real call, end to end."""
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)                                                              # "Remind me to call the dentist tomorrow"
        fr = frames_until(ws, limit=400)
    deltas = "".join(f["text"] for f in fr if isinstance(f, dict) and f["type"] == "delta")
    assert deltas.startswith("Sec, checking.") and "echo: (spoken, on a call) Remind me" in deltas
    said = " ".join(t for t, _ in local.voice.spoken)
    for raw in ("<|", "|>", "tool_call", "tool_response", "ask_kairo", "call:ask"):
        assert raw not in _all_text(fr) and raw not in said, raw


@pytest.mark.llama_env("FAKE_LLAMA_TEXT_TOOLS", "FAKE_LLAMA_GEMMA_TOOLS", "FAKE_LLAMA_ALWAYS_TOOL")
def test_small_talk_answered_with_only_a_gemma_call_gets_a_short_line_not_silence_or_markup(local):
    from app.voicemouth import NEUTRAL
    local.voice.text = "thanks"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert done["text"] == NEUTRAL["en"] and [t for t, _ in local.voice.spoken] == [NEUTRAL["en"]]
    assert "<|" not in _all_text(fr) and "tool_call" not in _all_text(fr)


@pytest.mark.llama_env("FAKE_LLAMA_TEXT_TOOLS", "FAKE_LLAMA_ALWAYS_TOOL")
def test_hello_on_a_call_is_the_greeting_at_once_and_never_the_voice_model(local):
    local.voice.text = "Hello"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert done["text"].startswith("Hey") and done["text"].endswith(".") and len(done["text"].split()) <= 2
    assert local.voice.spoken[0][0] == done["text"] and "<|" not in _all_text(fr)
    assert [r["max_tokens"] for r in requests_seen(local)] == [1]            # only the call's priming read


@pytest.mark.llama_env("FAKE_LLAMA_TEXT_TOOLS", "FAKE_LLAMA_ALWAYS_TOOL")
def test_small_talk_answered_with_only_a_raw_call_gets_a_short_line_not_silence(local):
    from app.voicemouth import NEUTRAL
    local.voice.text = "thanks"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert done["text"] == NEUTRAL["en"] and [t for t, _ in local.voice.spoken] == [NEUTRAL["en"]]
    assert "<|" not in _all_text(fr) and "tools" not in requests_seen(local)[-1]
    msgs = [(m["role"], m["text"]) for m in local.get("/api/chat/messages").json()["messages"]]
    assert msgs == [("owner", "(spoken, on a call) thanks"), ("kairo", NEUTRAL["en"])]      # Claude never ran


def test_the_old_voice_says_plain_speech_and_the_chat_keeps_claudes_words(local):
    """The voice model is off: Claude talks on the call itself. Its notes are never said or shown on the call."""
    local.vm.pause()
    local.voice.text = "Note L23 (urgent, loop) and card #12 for me"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    said = " ".join(t for t, _ in local.voice.spoken)
    shown = "".join(f["text"] for f in fr if isinstance(f, dict) and f["type"] == "delta")
    for bad in ("L23", "(urgent, loop)", "#12"):
        assert bad not in said and bad not in shown and bad not in done["text"], bad
    assert said.startswith("echo: (spoken, on a call) Note and card for me")
    last = local.get("/api/chat/messages").json()["messages"][-1]
    assert last["role"] == "kairo" and "L23 (urgent, loop)" in last["text"]   # the conversation keeps what Claude wrote



# ---------------------------------------------------------------- bring-ups: said or pushed, never both (owner's rule 2026-10-20)

def test_an_item_pushed_recently_is_not_said_on_the_call(local):
    import datetime as dt
    from app.db import PushLog
    item = seed_urgent(local)
    with Session(make_engine(local.settings.db_url)) as s:
        s.add(PushLog(ts=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1), source="loops", title="Kairo",
                      body="Sign the school form", url=f"/#loops/{item.id[1:]}", key="k", status="sent"))
        s.commit()
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws, limit=400)
    deltas = "".join(f["text"] for f in fr if isinstance(f, dict) and f["type"] == "delta")
    assert deltas.startswith("Sec, checking.") and "school form" not in deltas and "echo:" in deltas


def test_an_item_said_on_the_call_is_not_pushed_too(local):
    import datetime as dt
    from app import loops
    from app.db import OpenLoop
    item = seed_urgent(local)
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws, limit=400)
    assert "school form" in "".join(f["text"] for f in fr if isinstance(f, dict) and f["type"] == "delta")
    row = None
    for _ in range(100):
        with Session(make_engine(local.settings.db_url)) as s:
            row = s.get(OpenLoop, int(item.id[1:]))
        if row.raised:
            break
        time.sleep(0.05)
    assert row.raised == 1 and loops.aware(row.next_at) > dt.datetime.now(dt.timezone.utc)   # delivered: no push now



def test_hello_on_the_old_line_is_the_greeting_too_never_claude(local):
    """The voice model is off (the lab runs, it crashed): "hello" still gets the cached greeting at once."""
    local.vm.pause()
    local.voice.text = "Hello?"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert done["model"] == "greeting" and done["text"].startswith("Hey") and len(done["text"].split()) <= 2
    assert [t for t, _ in local.voice.spoken] == [done["text"]]
    msgs = [(m["role"], m["text"]) for m in local.get("/api/chat/messages").json()["messages"]]
    assert msgs == [("owner", "(spoken, on a call) Hello?"), ("kairo", done["text"])]      # Claude never ran
