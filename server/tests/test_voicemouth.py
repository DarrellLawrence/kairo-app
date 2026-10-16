"""The mouth (owner's decision 2026-10-16): the local voice model talks, Claude works through ask_kairo.
Written before the wiring into /ws/voice. The voice model here is scripted (no GPU in CI)."""

from __future__ import annotations

import asyncio
import json

import pytest

from app import bringup
from app.voicemouth import FILLERS, STILL, Mouth, Unmark


class Speaker:
    def __init__(self):
        self.fed, self.said, self.events = [], [], []

    def feed(self, text):
        self.fed.append(text)
        self.events.append(("feed", text))

    def say(self, text):
        self.said.append(text)
        self.events.append(("say", text))

    @property
    def heard(self):
        return "".join(t for k, t in self.events if k == "feed")


class Model:
    """A scripted voice model: a list of replies, each a list of events (or a callable on messages)."""

    def __init__(self, *replies, delay=0.0):
        self.replies, self.delay, self.calls = list(replies), delay, []

    async def stream(self, messages, *, tools=None, temperature=0.6, max_tokens=300):
        self.calls.append({"messages": json.loads(json.dumps(messages)), "tools": tools})
        reply = self.replies.pop(0)
        for ev in reply(messages) if callable(reply) else reply:
            await asyncio.sleep(self.delay)
            yield ev
        yield {"kind": "done", "finish": "stop", "first_token_ms": 5, "timings": {"cache_n": 100}, "usage": {}}


def text(*pieces):
    return [{"kind": "text", "text": p} for p in pieces]


def tool(request, item=None):
    args = {"request": request, **({"item": item} if item else {})}
    return [{"kind": "tool_start", "name": "ask_kairo"},
            {"kind": "tool_call", "id": "c1", "name": "ask_kairo", "arguments": json.dumps(args)}]


class Brain:
    def __init__(self, answer="Tomorrow: school 8 to 15:30, training at 18:00.", delay=0.0):
        self.answer, self.delay, self.asked = answer, delay, []

    async def __call__(self, request, lines, about):
        self.asked.append((request, list(lines), about))
        await asyncio.sleep(self.delay)
        return self.answer, False


ITEMS = [bringup.Item("L1", "loop", "reply: Lea asks about Friday", "normal"),
         bringup.Item("K2", "question", "What time does training end on Tuesdays?"),
         bringup.Item("T3", "test", "test in maths in 20 hours", "urgent")]


def mouth(model, brain=None, **kw):
    return Mouth(model, ask=brain or Brain(), context="## Right now\nThursday 14:00", items=ITEMS, name="Sam", **kw)


def run(coro):
    return asyncio.run(coro)


def test_small_talk_is_answered_by_the_voice_model_alone():
    m, sp, brain = Model(text("Hey! ", "All good here.")), Speaker(), Brain()
    r = run(mouth(m, brain).turn("hey, how are you?", "en", sp))
    assert sp.heard == "Hey! All good here." and r.text == "Hey! All good here." and r.asked == [] and brain.asked == []
    assert m.calls[0]["tools"][0]["function"]["name"] == "ask_kairo" and len(m.calls[0]["tools"]) == 1
    sysmsg = m.calls[0]["messages"][0]["content"]
    assert "ask_kairo" in sysmsg and "Thursday 14:00" in sysmsg and "L1 (normal, loop)" in sysmsg
    assert "UNTRUSTED DATA" in sysmsg                        # loop texts can come from mail: data


def test_a_data_question_gets_a_filler_at_once_then_claudes_answer():
    m = Model(tool("what's on tomorrow?"), text("Tomorrow you've got school ", "from 8 to 15:30, then training at 18:00."))
    sp, brain = Speaker(), Brain()
    mo = mouth(m, brain)
    r = run(mo.turn("What's on tomorrow?", "en", sp))
    assert sp.events[0] == ("say", FILLERS["en"][0])        # the line is never silent while Claude works
    assert brain.asked[0][0] == "what's on tomorrow?" and brain.asked[0][1] == ["Owner: What's on tomorrow?"]
    assert "15:30" in sp.heard and r.asked == ["what's on tomorrow?"] and r.tool_ms is not None
    roles = [x["role"] for x in m.calls[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert m.calls[1]["messages"][2]["tool_calls"][0]["id"] == "c1"
    assert json.loads(m.calls[1]["messages"][3]["content"])["answer"].startswith("Tomorrow: school")
    assert r.first_text_ms is not None and r.first_text_ms <= r.tool_ms + 50


def test_german_gets_a_german_filler():
    m, sp = Model(tool("Was steht morgen an?"), text("Morgen hast du Schule.")), Speaker()
    run(mouth(m).turn("Was steht morgen an?", "de", sp))
    assert sp.said[0] in FILLERS["de"]


def test_a_slow_brain_gets_one_still_on_it():
    m, sp = Model(tool("check my mail"), text("Nothing new.")), Speaker()
    run(mouth(m, Brain(delay=0.3), still_after_s=0.1).turn("check my mail", "en", sp))
    assert sp.said == [FILLERS["en"][0], STILL["en"]]


def test_the_conversation_only_grows_at_the_end_so_the_cache_holds():
    m = Model(text("Hi!"), tool("tasks"), text("You have two tasks."), text("Bye!"))
    mo = mouth(m)
    run(mo.turn("hi", "en", Speaker()))
    run(mo.turn("what are my tasks?", "en", Speaker()))
    run(mo.turn("thanks, bye", "en", Speaker()))
    for a, b in zip(m.calls, m.calls[1:]):
        assert b["messages"][:len(a["messages"])] == a["messages"]   # every request starts like the one before


def test_bring_up_markers_are_never_spoken_and_two_is_enough():
    m = Model(text("Sure. [[rai", "sed:L1]] By the way, Lea asked about Friday."), text("[[raised:K2]] Quick one: when does training end?"),
              text("Okay."))
    mo = mouth(m)
    sp = Speaker()
    r1 = run(mo.turn("ok", "en", sp))
    assert "[[" not in sp.heard and r1.raised == ["L1"] and "Lea asked" in sp.heard
    run(mo.turn("cool", "en", Speaker()))
    run(mo.turn("yes", "en", Speaker()))
    assert "Enough brought up" in m.calls[2]["messages"][-1]["content"]     # after two, the model is told


def test_an_answer_to_an_item_goes_to_claude_with_what_it_was():
    m, brain = Model(tool("training ends at 19:30", item="K2"), text("Got it.")), Brain(answer="Saved.")
    run(mouth(m, brain).turn("It ends at half seven.", "en", Speaker()))
    assert brain.asked[0][2] == "bring-up item K2 (question: What time does training end on Tuesdays?)"


def test_claudes_new_bring_up_list_comes_with_the_next_words():
    m = Model(text("Hi."), text("Sure."))
    mo = mouth(m)
    run(mo.turn("hi", "en", Speaker()))
    mo.update_items([bringup.Item("L1", "loop", "x", "urgent", say="Lea needs an answer today.")])
    run(mo.turn("what's up?", "en", Speaker()))
    last = m.calls[1]["messages"][-1]["content"]
    assert last.startswith("<kairo-note>") and "Lea needs an answer today." in last and last.endswith("what's up?")


def test_talked_over_mid_sentence_keeps_what_was_said():
    async def go():
        m = Model(text("Well, ", "let me ", "think about ", "that for a long time."), text("Okay."), delay=0.05)
        mo, sp = mouth(m), Speaker()
        t = asyncio.get_running_loop().create_task(mo.turn("tell me a story", "en", sp))
        await asyncio.sleep(0.12)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert mo.messages[-1]["role"] == "assistant" and mo.messages[-1]["content"].endswith("…")
        await mo.turn("actually never mind", "en", Speaker())
        assert m.calls[1]["messages"][-2]["role"] == "assistant"
    run(go())


def test_talked_over_while_claude_works_the_answer_comes_later():
    async def go():
        m = Model(tool("did Tom reply?"), text("Yes, he did."))
        brain = Brain(answer="Tom replied: he's in.", delay=0.2)
        mo, sp = mouth(m, brain), Speaker()
        t = asyncio.get_running_loop().create_task(mo.turn("did Tom reply?", "en", sp))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert [x["role"] for x in mo.messages[-3:]] == ["assistant", "tool", "assistant"]   # still a valid conversation
        await asyncio.sleep(0.3)
        assert any("Tom replied: he's in." in n for n in mo.notes)    # Claude's answer wasn't lost
        await mo.turn("so?", "en", Speaker())
        assert "Tom replied" in m.calls[1]["messages"][-1]["content"]
    run(go())


def test_unmark_handles_pieces_and_plain_brackets():
    u = Unmark()
    out = u.feed("a [") + u.feed("[rai") + u.feed("sed:t3]] b [[x]] c") + u.flush()
    assert out == "a b [[x]] c" and u.raised == ["T3"]
