"""Voice V1: a spoken turn over /ws/voice (app/voicelink.py): audio in, transcript, the answer as
text and as audio sentence by sentence, talked over = stopped, never a silent failure."""

from __future__ import annotations

import io
import json
import struct
import time
import wave

import pytest
from fastapi.testclient import TestClient

from app import voicelink
from app.main import create_app
from app.voice import NotInstalled, Transcript, VoiceError

PCM = b"\x10\x00" * 16000   # one second of (very quiet) audio at 16 kHz


# ---------------------------------------------------------------- sentences

def test_the_first_clause_goes_at_once_then_whole_sentences():
    """Owner's request 2026-10-17: the voice starts after the first clause or 5 words, not the whole reply."""
    s = voicelink.Sentences()
    assert s.feed("Sure") == []
    assert s.feed(", I moved it") == ["Sure,"]
    assert s.feed(" to Friday. Then") == []                  # later pieces are longer: fewer, while one plays
    assert s.feed(" I did. And that was all for today, really. ") == [
        "I moved it to Friday. Then I did. And that was all for today, really."]
    assert s.flush() == ""


def test_no_comma_means_the_first_five_words():
    s = voicelink.Sentences()
    assert s.feed("Your next train leaves at") == []           # the 5th word may still be going on
    assert s.feed(" 7.42 from platform 3. ") == ["Your next train leaves at"]
    assert s.flush() == "7.42 from platform 3."


def test_abbreviations_dates_and_times_are_not_ends():
    s = voicelink.Sentences()
    assert s.feed("Dr. Weber") == []                           # "Dr." is no clause
    assert s.feed(" says hi") == []
    assert s.feed(" again today. ") == ["Dr. Weber says hi again"]
    s = voicelink.Sentences()
    assert s.feed("Am 3. Oktober hast du z.B. Mathe bei Dr. Weber. ") == ["Am 3. Oktober hast du"]
    assert s.flush() == "z.B. Mathe bei Dr. Weber."
    s = voicelink.Sentences()
    assert s.feed("At 7.") == []                               # 7.42 still being written
    assert s.feed("42 then") == []


def test_a_short_answer_is_flushed_whole():
    s = voicelink.Sentences()
    assert s.feed("Ok. ") == ["Ok."]
    assert s.feed("It is on your calendar") == []
    assert s.flush() == "It is on your calendar"


def test_a_long_run_on_sentence_is_cut_so_the_voice_does_not_wait():
    s = voicelink.Sentences()
    text = "First this, " + "and then that and the other thing " * 8
    out = s.feed(text)
    assert out[0] == "First this," and len(out) == 2 and 150 < len(out[1]) <= voicelink.LONG
    assert text[len("First this, "):].startswith(out[1] + " ")   # cut between words


def test_the_hang_up_marker_is_never_spoken():
    s = voicelink.Sentences()
    assert s.feed("Thanks, that helps a lot. [[hang up]]") == ["Thanks,"]
    assert s.flush() == "that helps a lot."
    s = voicelink.Sentences()
    assert s.feed("Bye for now [[hang up]]") == [] and s.flush() == "Bye for now"


# ---------------------------------------------------------------- G5: the old line's speaker

def _old_line_pieces(script, **kw) -> list[str]:
    """Run `script(speaker)` on an old-line Speaker (no voice: only what is queued for it) and return the queue."""
    import asyncio

    async def go():
        async def nothing(_msg):
            return None
        sp = voicelink.Speaker(None, nothing, nothing, "en", 1, **kw)
        script(sp)
        sp.stop()
        out = []
        while not sp.queue.empty():
            piece = sp.queue.get_nowait()
            if piece is not None:
                out.append(piece)
        return out
    return asyncio.run(go())


def test_g5_words_written_before_a_line_of_kairos_own_are_not_jumped_by_it():
    """A holding line that comes after words the model had already written used to be queued first; the words
    came after it. Now the line waits its turn, like on the WebRTC speaker."""
    def script(sp):
        sp.feed("Let me see what")
        sp.say("Okay, one moment.")
        sp.feed("Your next train leaves at 14:32 from platform two and arrives in Bern.")
        sp.finish()
    assert _old_line_pieces(script) == ["Let me see what", "Okay, one moment.", "Your next train leaves at",
                                        "14:32 from platform two and arrives in Bern."]
    assert _old_line_pieces(script, second_piece=False)[0] == "Okay, one moment."   # off: the old order, the line first


def test_g5_flush_lets_the_last_words_of_a_sentence_out_and_the_speaker_goes_on():
    def script(sp):
        sp.feed("Your train is on time. See you")
        sp.flush()
        sp.feed("Anything else? Just ask me later.")
        sp.finish()
    assert _old_line_pieces(script) == ["Your train is on time.", "See you", "Anything else?", "Just ask me later."]


def test_g5_flush_does_nothing_when_nothing_is_waiting_or_it_is_over():
    def script(sp):
        sp.flush()
        sp.feed("Done. ")
        sp.finish()
        sp.flush()
    assert _old_line_pieces(script) == ["Done."]


def test_noise_transcripts_are_dropped():
    assert voicelink.heard("Untertitel im Auftrag des ZDF, 2021") == ""
    assert voicelink.heard("Subtitles by the Amara.org community") == ""
    assert voicelink.heard("  thank you  ") == "thank you"


def test_audio_frame_header():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x01\x00" * 10)
    pcm, rate = voicelink.wav_pcm(buf.getvalue())
    assert (pcm, rate) == (b"\x01\x00" * 10, 24000)
    f = voicelink.frame(rate, 5, 3, pcm)
    assert struct.unpack("<III", f[:12]) == (24000, 5, 3) and f[12:] == pcm


# ---------------------------------------------------------------- the line

class FakeVoice:
    def __init__(self, text="Remind me to call the dentist tomorrow", language="en"):
        self.text, self.language = text, language
        self.heard: list[int] = []
        self.spoken: list[tuple[str, str]] = []
        self.missing = False
        self.slow = 0.0

    def status(self):
        return {"installed": {"stt": True, "en": True, "de": True}, "stt_device": "cuda"}

    def transcribe_pcm(self, pcm, language=None):
        self.heard.append(len(pcm))
        time.sleep(self.slow)
        if self.missing:
            raise NotInstalled("Speech recognition is not installed on the PC (run Update Kairo).")
        if len(pcm) < 3200:
            raise VoiceError("no audio")
        return Transcript(text=self.text, language=self.language, seconds=len(pcm) / 32000, took=0.05)

    def speak(self, text, language=None):
        self.spoken.append((text, language))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\0\0" * 240)
        return buf.getvalue(), language or "en"


@pytest.fixture
def line(migrated, request):
    """The app with a fake voice. A test's fake_mode marker sets how the fake Claude answers
    (the brain reads its environment when the app starts)."""
    mode = request.node.get_closest_marker("fake_mode")
    mp = pytest.MonkeyPatch()
    if mode:
        mp.setenv("KAIRO_HOOK_FAKE_MODE", mode.args[0])
    app = create_app(migrated)
    app.state.voice = FakeVoice()
    try:
        with TestClient(app) as c:
            c.voice = app.state.voice
            yield c
    finally:
        mp.undo()


def frames_until(ws, *, last="spoken", stop_after=None, limit=200):
    """Frames (dicts, or ("audio", rate, index, n_bytes)) up to and including the `last` type."""
    out = []
    for _ in range(limit):
        m = ws.receive()
        if m.get("bytes") is not None:
            rate, turn, index = struct.unpack("<III", m["bytes"][:12])
            out.append(("audio", rate, turn, index, len(m["bytes"]) - 12))
            continue
        f = json.loads(m["text"])
        out.append(f)
        if stop_after and f.get("type") == stop_after:
            return out
        if f.get("type") == last or (f.get("type") == "error" and f.get("status") not in ("voice",)):
            return out
    raise AssertionError(f"no {last!r} frame: {out}")


def utter(ws, pcm=PCM, path="call", pause=False, turn=1):
    ws.send_json({"type": "start", "rate": 16000})
    ws.send_bytes(pcm)
    if pause:
        ws.send_json({"type": "pause"})
    ws.send_json({"type": "end", "path": path, "turn": turn})


def test_a_spoken_turn_comes_back_as_text_and_audio(line):
    with line.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    kinds = [f[0] if isinstance(f, tuple) else f["type"] for f in fr]
    assert kinds[0] == "transcript" and fr[0]["text"] == "Remind me to call the dentist tomorrow"
    assert "delta" in kinds and "done" in kinds and kinds[-1] == "spoken"
    assert kinds.index("sentence") < kinds.index("say") < kinds.index("audio")
    audio = [f for f in fr if isinstance(f, tuple)]
    assert audio[0][1:] == (24000, 1, 0, 480)
    assert all(f["turn"] == 1 for f in fr if isinstance(f, dict))   # every frame says which turn it is
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert done["server"]["stt_ms"] >= 0 and done["model"] == "haiku" and done["interrupted"] is False
    assert " ".join(t for t, _ in line.voice.spoken) == "echo: (spoken, on a call) Remind me to call the dentist tomorrow"
    assert line.voice.spoken[0] == ("echo:", "en")                  # the first clause goes to the voice at once
    msgs = line.get("/api/chat/messages").json()["messages"]
    assert [(m["role"], m["text"]) for m in msgs] == [
        ("owner", "(spoken, on a call) Remind me to call the dentist tomorrow"),
        ("kairo", "echo: (spoken, on a call) Remind me to call the dentist tomorrow")]


def test_spoken_in_chat_is_the_plain_message(line):
    with line.websocket_connect("/ws/voice") as ws:
        utter(ws, path="chat_voice")
        frames_until(ws)
    assert line.get("/api/chat/messages").json()["messages"][0]["text"] == "Remind me to call the dentist tomorrow"


def test_the_transcript_made_at_the_pause_is_used(line):
    with line.websocket_connect("/ws/voice") as ws:
        utter(ws, pause=True)
        frames_until(ws, last="transcript")
        assert line.voice.heard == [len(PCM)]          # once, at the pause; the end reused it
        frames_until(ws)
        ws.send_json({"type": "start"})               # talked on after a pause: transcribed again, all of it
        ws.send_bytes(PCM)
        ws.send_json({"type": "pause"})
        ws.send_bytes(PCM)
        ws.send_json({"type": "end", "path": "call"})
        frames_until(ws)
    assert line.voice.heard[1:] == [len(PCM), 2 * len(PCM)]


def test_nothing_heard_is_said_not_answered(line):
    with line.websocket_connect("/ws/voice") as ws:
        utter(ws, pcm=b"\0\0" * 100)
        fr = frames_until(ws, last="transcript")
    assert fr == [{"type": "transcript", "text": "", "language": None, "stt_ms": fr[0]["stt_ms"], "turn": 1}]
    assert line.get("/api/chat/messages").json()["messages"] == []


def test_speech_recognition_missing_is_a_clear_error(line):
    line.voice.missing = True
    with line.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws, last="error")
    assert fr[-1]["status"] == "voice_missing" and "not installed" in fr[-1]["message"]


def test_a_greeting_is_spoken_without_the_model(line, tmp_path, monkeypatch):
    rec = tmp_path / "rec.json"
    monkeypatch.setenv("KAIRO_HOOK_FAKE_RECORD", str(rec))
    with line.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "say", "text": "Hi, it's Kairo.", "turn": 1})
        fr = frames_until(ws)
    assert [f[0] if isinstance(f, tuple) else f["type"] for f in fr] == ["sentence", "say", "audio", "spoken"]
    assert line.voice.spoken == [("Hi, it's Kairo.", "en")]
    time.sleep(0.3)
    assert not rec.exists() or json.loads(rec.read_text())["turns"] == []


@pytest.mark.fake_mode("long")
def test_talking_over_kairo_stops_the_answer_and_keeps_what_was_said(line):
    with line.websocket_connect("/ws/voice") as ws:
        utter(ws)
        frames_until(ws, last="delta")
        ws.send_json({"type": "stop"})
        fr = frames_until(ws, last="done")
        assert fr[-1]["interrupted"] is True
        brain = line.app.state.runner.brain
        warm = brain._warm
        assert warm is not None and warm.proc.poll() is None   # the conversation process lives on
        utter(ws, turn=2)                                     # and answers the next turn at once
        fr = frames_until(ws, last="done")
        assert fr[-1]["turn"] == 2
        assert fr[-1]["interrupted"] is False and brain._warm is warm
    msgs = line.get("/api/chat/messages").json()["messages"]
    assert msgs[1]["role"] == "kairo" and msgs[1]["text"].endswith("…")


def test_talking_on_before_kairo_answers_makes_one_message(line):
    """A pause long enough to end the turn, then more words while Kairo is still transcribing: the
    first part isn't answered on its own (that answer would hold up the real one)."""
    line.voice.slow = 0.4
    with line.websocket_connect("/ws/voice") as ws:
        utter(ws, turn=1)
        ws.send_json({"type": "stop", "turn": 1})
        utter(ws, turn=2)
        fr = frames_until(ws, last="done")
        assert fr[-1]["turn"] == 2
    said = line.voice.text
    msgs = line.get("/api/chat/messages").json()["messages"]
    assert [(m["role"], m["text"]) for m in msgs] == [
        ("owner", f"(spoken, on a call) {said} {said}"), ("kairo", f"echo: (spoken, on a call) {said} {said}")]


def test_the_stronger_model_is_announced_with_a_filler(line, monkeypatch):
    line.voice.text = "Hmm, and that other thing [[fake-escalate]]"
    with line.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    model = next(f for f in fr if isinstance(f, dict) and f["type"] == "model")
    assert model == {"type": "model", "model": "sonnet", "escalated": True, "why": "handed over by Haiku", "turn": 1}
    assert line.voice.spoken[0] == ("One moment.", "en")
    assert next(f for f in fr if isinstance(f, dict) and f["type"] == "done")["model"] == "sonnet"
    assert "[[sonnet]]" not in json.dumps(fr)


@pytest.mark.fake_mode("pause")
def test_hanging_up_mid_answer_still_saves_it(line):
    with line.websocket_connect("/ws/voice") as ws:
        utter(ws)
        frames_until(ws, last="transcript")
    deadline = time.time() + 15
    while time.time() < deadline:
        msgs = line.get("/api/chat/messages").json()["messages"]
        if any(m["role"] == "kairo" for m in msgs):
            break
        time.sleep(0.2)
    assert [m["role"] for m in msgs] == ["owner", "kairo"]


def test_too_long_is_refused_in_words(line, monkeypatch):
    monkeypatch.setattr(voicelink, "MAX_BYTES", len(PCM))
    with line.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "start"})
        ws.send_bytes(PCM)
        ws.send_bytes(PCM)
        fr = frames_until(ws, last="error")
        assert fr[-1]["status"] == "too_long"
        ws.send_json({"type": "end", "path": "call"})
        assert frames_until(ws, last="error")[-1]["status"] == "no_audio"


def test_another_site_cannot_open_the_line(line):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with line.websocket_connect("/ws/voice", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_jobs_keep_off_the_brain_while_the_line_is_open(line, monkeypatch):
    runner = line.app.state.runner
    with line.websocket_connect("/ws/voice"):
        assert runner._lines == 1
    deadline = time.time() + 5
    while runner._lines and time.time() < deadline:
        time.sleep(0.05)
    assert runner._lines == 0


def test_the_app_gives_the_owner_a_quiet_minute(migrated, monkeypatch):
    monkeypatch.delenv("KAIRO_OWNER_QUIET_S", raising=False)
    from app import chat
    assert create_app(migrated).state.runner.quiet_s == chat.OWNER_QUIET_S == 60
