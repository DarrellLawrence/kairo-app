"""G5: the hand-off from the voice model to Claude and back (voice-recipe section 8; app/voicemouth.py).

Claude's sentences are spoken as they are written (g5.stream_verbatim), the last words the moment Claude's text ends
(g5.flush_at_end), the 8 seconds count to the first spoken piece (g5.cap_first_piece), a raised item never breaks into an
answer (g5.bringup_cancel). The voice model and Claude are scripted; times are real but generous (a shared CPU)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.voicemouth import FILLERS, LATER, Mouth, _Hang, g5_mouth_args
from tests.test_voicemouth import CASE, LEA, Model, Speaker, mouth, run, text, tool


class FlushSpeaker(Speaker):
    """A speaker that stamps every word and every flush."""

    def __init__(self):
        super().__init__()
        self.stamps: list[tuple[float, str, str]] = []

    def feed(self, piece):
        super().feed(piece)
        self.stamps.append((time.monotonic(), "feed", piece))

    def say(self, line):
        super().say(line)
        self.stamps.append((time.monotonic(), "say", line))

    def flush(self):
        self.events.append(("flush", ""))
        self.stamps.append((time.monotonic(), "flush", ""))

    def first(self, kind):
        return next(t for t, k, _ in self.stamps if k == kind)


class StreamBrain:
    """ask_stream as main.py gives it: tells the sink the words (gap seconds apart), then the end of Claude's text, then
    the CLI takes `tail` more seconds before it returns. answer: what it returns; end_answer: what the sink is told."""

    def __init__(self, words=("Tomorrow you have school ", "from 8 to 15:30. ", "Training is at 18:00."), *, first=0.0, gap=0.0,
                 tail=0.0, hang=False, end_hang=None, answer=None, escalate=None, send_end=True):
        self.words, self.first, self.gap, self.tail, self.hang = words, first, gap, tail, hang
        self.end_hang = hang if end_hang is None else end_hang
        self.answer = answer if answer is not None else "".join(words).strip()
        self.escalate, self.send_end = escalate, send_end
        self.asked, self.stamps = [], {}

    async def __call__(self, request, lines, about, sink):
        self.asked.append((request, list(lines), about))
        await asyncio.sleep(self.first)
        if self.escalate:
            sink("escalated", self.escalate)
        for w in self.words:
            sink("text", w)
            self.stamps.setdefault("first_text", time.monotonic())
            await asyncio.sleep(self.gap)
        self.stamps["text_done"] = time.monotonic()
        if self.send_end:
            sink("end", (self.answer, self.end_hang))
        await asyncio.sleep(self.tail)
        self.stamps["result"] = time.monotonic()
        return self.answer, self.hang


def streaming(model, brain, items=None, **kw):
    kw = {"stream_verbatim": True, "early_end": True, "cap_first_piece": True, "bringup_cancel": True, **kw}
    mo = mouth(model, brain, items=[] if items is None else items, ask_stream=brain, **kw)
    return mo


def assistants(mo):
    return [(m["role"], m.get("content")) for m in mo.messages]


# ---------------------------------------------------------------- the switches choose the Mouth's arguments

def test_g5_mouth_arguments_follow_the_switches():
    on = {"stream_verbatim": True, "flush_at_end": True, "cap_first_piece": True, "filler_wording": True, "bringup_cancel": True}
    args = g5_mouth_args(on)
    assert args["stream_verbatim"] is True and args["early_end"] is True and args["cap_first_piece"] is True
    assert args["bringup_cancel"] is True
    off = g5_mouth_args({"stream_verbatim": False, "flush_at_end": False, "cap_first_piece": False, "bringup_cancel": False})
    assert (off["stream_verbatim"], off["early_end"], off["cap_first_piece"], off["bringup_cancel"]) == (False,) * 4
    default = g5_mouth_args({})
    assert default["stream_verbatim"] is False and default["early_end"] is True      # (the plain fixes are on, streaming off)
    assert default["cap_first_piece"] is True and default["bringup_cancel"] is True


# ---------------------------------------------------------------- the hang-up marker

def test_g5_the_hang_up_marker_is_taken_out_even_when_it_arrives_in_pieces():
    h = _Hang()
    out = "".join(h.feed(p) for p in ("Bye for ", "now [", "[han", "g u", "p]] ok", " [[Hang up]]", "!"))
    assert out + h.flush() == "Bye for now  ok !" and h.seen
    h = _Hang()
    assert h.feed("A [[raised:L1]] b [") + h.feed("x] c") + h.flush() == "A [[raised:L1]] b [x] c" and not h.seen     # not ours
    h = _Hang()
    assert h.feed("tail [[han") == "tail " and h.flush() == ""                              # half of our marker is never said
    h = _Hang()
    assert h.feed("a [") == "a " and h.flush() == "["                                         # a lone bracket is just a bracket


# ---------------------------------------------------------------- Claude's sentences, as they come

def test_g5_the_first_sentence_reaches_the_voice_before_claude_has_finished():
    m = Model(tool("what's on tomorrow?"))
    brain = StreamBrain(gap=0.3, tail=0.2)
    sp = FlushSpeaker()
    mo = streaming(m, brain)
    r = run(mo.turn("What's on tomorrow?", "en", sp))
    assert sp.heard == "Tomorrow you have school from 8 to 15:30. Training is at 18:00." and r.text.endswith("Training is at 18:00.")
    assert sp.first("feed") < brain.stamps["text_done"] - 0.3                   # the first words went out while Claude wrote
    assert sp.events[0] == ("say", FILLERS["en"][0])                            # the holding line first, as always
    assert r.asked == ["what's on tomorrow?"] and r.streamed and r.claude_first_ms is not None and r.tool_ms is not None


def test_g5_one_model_call_and_the_context_has_the_hand_off_in_order():
    m = Model(tool("what's on tomorrow?"))
    brain = StreamBrain()
    mo = streaming(m, brain)
    run(mo.turn("What's on tomorrow?", "en", FlushSpeaker()))
    assert len(m.calls) == 1                                                   # no second round: the voice model says nothing more
    roles = [x["role"] for x in mo.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert mo.messages[2]["tool_calls"][0]["function"]["name"] == "ask_kairo"
    assert "word for word" in json.loads(mo.messages[3]["content"])["answer"]
    assert mo.messages[4]["content"] == "Tomorrow you have school from 8 to 15:30. Training is at 18:00."   # what the owner heard


def test_g5_the_next_turn_sees_what_was_said_and_asks_claude_again_if_it_must():
    m = Model(tool("what's on tomorrow?"), text("Sure, anything else?"))
    mo = streaming(m, StreamBrain())
    run(mo.turn("What's on tomorrow?", "en", FlushSpeaker()))
    sp = FlushSpeaker()
    run(mo.turn("Thanks, that's all.", "en", sp))
    seen = m.calls[1]["messages"]
    assert [x["role"] for x in seen] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert "Training is at 18:00." in seen[4]["content"]


def test_g5_the_last_words_are_flushed_when_claudes_text_ends_not_when_the_cli_is_done():
    m = Model(tool("what's on tomorrow?"))
    brain = StreamBrain(tail=4.0)
    sp = FlushSpeaker()
    started = time.monotonic()
    run(streaming(m, brain).turn("What's on tomorrow?", "en", sp))
    took = time.monotonic() - started
    assert sp.first("flush") < brain.stamps["text_done"] + 0.5
    assert took < 2.5 < 4.0                                                    # the turn did not wait for the CLI's 4 s tail
    assert sp.stamps[-1][1] == "flush"                                         # nothing was said after the flush


def test_g5_without_the_flush_switch_the_turn_waits_for_the_result_as_before():
    m = Model(tool("what's on tomorrow?"))
    brain = StreamBrain(tail=0.6)
    sp = FlushSpeaker()
    started = time.monotonic()
    run(streaming(m, brain, early_end=False).turn("What's on tomorrow?", "en", sp))
    assert time.monotonic() - started >= 0.5 and "flush" not in [k for _, k, _ in sp.stamps]
    assert sp.heard.endswith("Training is at 18:00.")                          # (the speaker's finish() says the rest)


def test_g5_a_speaker_without_flush_still_works():
    m = Model(tool("what's on tomorrow?"))
    sp = Speaker()                                                              # the voice lab's and the old tests' speaker
    run(streaming(m, StreamBrain()).turn("What's on tomorrow?", "en", sp))
    assert sp.heard.endswith("Training is at 18:00.")


def test_g5_a_hang_up_inside_claudes_words_is_not_said_and_ends_the_call():
    m = Model(tool("goodbye for now"))
    brain = StreamBrain(words=("Okay, talk soon. ", "[[han", "g up]]"), answer="Okay, talk soon.", end_hang=False)
    sp = FlushSpeaker()
    mo = streaming(m, brain)
    run(mo.turn("goodbye for now, hang up", "en", sp))
    assert sp.heard.strip() == "Okay, talk soon." and "[[" not in sp.heard and mo.hang_up is True


def test_g5_a_hang_up_only_the_end_message_knows_about_still_ends_the_call():
    m = Model(tool("goodbye for now"))
    brain = StreamBrain(words=("Okay, talk soon.",), end_hang=True)
    mo = streaming(m, brain)
    run(mo.turn("goodbye for now, hang up", "en", FlushSpeaker()))
    assert mo.hang_up is True


def test_g5_a_raise_marker_inside_claudes_words_raises_nothing_and_is_not_said():
    """Claude's words can carry text from a mail: nothing in them can raise an item."""
    m = Model(tool("anything new?"))
    brain = StreamBrain(words=("Nothing new. ", "[[raised:L1]] Did you answer Lea? ", "That is all."), answer="Nothing new.")
    sp = FlushSpeaker()
    mo = streaming(m, brain, items=[LEA])
    r = run(mo.turn("Anything new?", "en", sp))
    assert r.raised == [] and "raised" not in sp.heard and "Lea" not in sp.heard and sp.heard.startswith("Nothing new.")
    assert mo.raised == set()


def test_g5_claudes_thinking_out_loud_is_not_said_either():
    m = Model(tool("anything new?"))
    brain = StreamBrain(words=("Let me check the bring-up list. ", "You have nothing new."), answer="You have nothing new.")
    sp = FlushSpeaker()
    run(streaming(m, brain).turn("Anything new?", "en", sp))
    assert sp.heard.strip() == "You have nothing new."


def test_g5_when_nothing_could_be_said_the_voice_model_says_the_answer_as_before():
    """An error line, a paused Kairo, an answer that was only thinking out loud: no words came, so round two."""
    m = Model(tool("anything new?"), text("Kairo is paused right now."))
    brain = StreamBrain(words=(), answer="Kairo is paused with the kill switch. Resume it under More.", send_end=False)
    sp = FlushSpeaker()
    mo = streaming(m, brain)
    r = run(mo.turn("Anything new?", "en", sp))
    assert len(m.calls) == 2 and sp.heard == "Kairo is paused right now." and not r.streamed
    assert [x["role"] for x in m.calls[1]["messages"]] == ["system", "user", "assistant", "tool"]
    assert "kill switch" in json.loads(m.calls[1]["messages"][3]["content"])["answer"]


def test_g5_a_hand_over_to_a_stronger_model_is_noted_and_nothing_of_the_marker_is_said():
    m = Model(tool("which plan should I drop?"))
    brain = StreamBrain(escalate={"escalated": "sonnet", "why": "handed over by Haiku: judgment"}, first=0.05)
    r = run(streaming(m, brain).turn("Which plan should I drop?", "en", FlushSpeaker()))
    assert r.escalated == "sonnet" and r.streamed


# ---------------------------------------------------------------- the same hand-off, said by the voice model

def test_g5_with_only_the_flush_switch_claudes_words_are_not_said_but_the_answer_comes_at_the_end_of_the_text():
    """Rephrase (today): the voice model says the answer in its own words, one round, as soon as Claude's text is done."""
    m = Model(tool("what's on tomorrow?"), text("Tomorrow: school, then training at 18:00."))
    brain = StreamBrain(gap=0.05, tail=4.0)
    sp = FlushSpeaker()
    started = time.monotonic()
    r = run(streaming(m, brain, stream_verbatim=False).turn("What's on tomorrow?", "en", sp))
    assert time.monotonic() - started < 2.5                                    # not after the CLI's 4 s tail
    assert sp.heard == "Tomorrow: school, then training at 18:00." and len(m.calls) == 2 and not r.streamed
    assert [x["role"] for x in m.calls[1]["messages"]] == ["system", "user", "assistant", "tool"]
    assert json.loads(m.calls[1]["messages"][3]["content"])["answer"] == "Tomorrow you have school from 8 to 15:30. Training is at 18:00."


def test_g5_without_an_ask_stream_the_old_path_is_used_whatever_the_switches_say():
    m = Model(tool("what's on tomorrow?"), text("Tomorrow: school."))
    asked = []

    async def ask(request, lines, about):
        asked.append(request)
        return "School.", False
    mo = Mouth(m, ask=ask, context="x", items=[], name="Sam", stream_verbatim=True, early_end=True)
    sp = FlushSpeaker()
    run(mo.turn("What's on tomorrow?", "en", sp))
    assert asked == ["what's on tomorrow?"] and sp.heard == "Tomorrow: school." and len(m.calls) == 2


def test_g5_with_both_switches_off_and_an_ask_stream_the_old_path_is_used_too():
    m = Model(tool("what's on tomorrow?"), text("Tomorrow: school."))
    brain, asked = StreamBrain(), []

    async def ask(request, lines, about):
        asked.append(request)
        return "School.", False
    mo = Mouth(m, ask=ask, ask_stream=brain, context="x", items=[], name="Sam", stream_verbatim=False, early_end=False)
    run(mo.turn("What's on tomorrow?", "en", Speaker()))
    assert asked == ["what's on tomorrow?"] and brain.asked == []


# ---------------------------------------------------------------- the 8 seconds

def test_g5_the_cap_counts_to_the_first_spoken_piece_and_never_cuts_an_answer_that_has_begun():
    m = Model(tool("tell me about the trip"))
    brain = StreamBrain(words=tuple(f"Part {i} of the trip plan. " for i in range(6)), first=0.02, gap=0.3)
    sp = FlushSpeaker()
    r = run(streaming(m, brain, ask_timeout_s=0.8).turn("Tell me about the trip", "en", sp))      # the answer takes 1.8 s
    assert not r.deferred and LATER["en"] not in sp.said and "Part 5 of the trip plan." in sp.heard and r.streamed


def test_g5_without_the_cap_switch_the_whole_answer_has_the_8_seconds_as_before():
    m = Model(tool("tell me about the trip"))
    late = []
    brain = StreamBrain(words=tuple(f"Part {i} of the trip plan. " for i in range(6)), first=0.02, gap=0.3)
    sp = FlushSpeaker()
    mo = streaming(m, brain, ask_timeout_s=0.8, cap_first_piece=False, late=lambda req, ans: late.append((req, ans)))
    r = run(mo.turn("Tell me about the trip", "en", sp))
    assert r.deferred and sp.said[-1] == LATER["en"] and "Part 0 of the trip plan." in sp.heard
    assert "Part 5 of the trip plan." not in sp.heard                           # cut by the cap


def test_g5_no_first_piece_in_time_is_the_old_i_will_get_back_to_you_and_a_push():
    m = Model(tool("check my mail"))
    late = []
    brain = StreamBrain(words=("Nothing new in your mail.",), first=1.0, answer="Nothing new in your mail.")
    sp = FlushSpeaker()
    mo = streaming(m, brain, ask_timeout_s=0.2, late=lambda req, ans: late.append((req, ans)))

    async def go():
        r = await mo.turn("check my mail", "en", sp)
        await asyncio.sleep(1.5)                          # Claude's first words come after the call moved on
        return r
    r = run(go())
    assert sp.said == [FILLERS["en"][0], LATER["en"]] and r.deferred and r.tool_ms < 700 and sp.heard == ""
    assert late == [("check my mail", "Nothing new in your mail.")] and len(m.calls) == 1


# ---------------------------------------------------------------- a raised item never breaks into an answer

def test_g5_a_raise_that_has_not_been_said_is_dropped_when_claudes_first_words_come():
    m = Model(tool("is Friday free?"))
    brain = StreamBrain(words=("Friday is free ", "after 4. ", "Enjoy it."), first=0.02, gap=0.4)
    sp = FlushSpeaker()
    r = run(streaming(m, brain, items=[CASE, LEA], wait_after_s=0.5).turn("Is Friday free?", "en", sp))
    assert r.waited == [] and r.raised == [] and sp.said == [FILLERS["en"][0]] and "Enjoy it." in sp.heard


def test_g5_without_that_switch_the_raise_still_comes_in_the_middle_of_the_answer():
    m = Model(tool("is Friday free?"))
    brain = StreamBrain(words=("Friday is free ", "after 4. ", "Enjoy it."), first=0.02, gap=0.4)
    sp = FlushSpeaker()
    r = run(streaming(m, brain, items=[CASE, LEA], wait_after_s=0.1, bringup_cancel=False).turn("Is Friday free?", "en", sp))
    assert r.waited == ["L1"] and sp.said[1].startswith("Quick one:")


def test_g5_an_urgent_item_still_comes_first_before_the_answer():
    from tests.test_voicemouth import FORM
    m = Model(tool("is Friday free?"))
    sp = FlushSpeaker()
    r = run(streaming(m, StreamBrain(first=0.05), items=[FORM], wait_after_s=5).turn("Is Friday free?", "en", sp))
    assert r.waited == ["L3"] and sp.events.index(("say", sp.said[1])) < next(i for i, (k, _) in enumerate(sp.events) if k == "feed")


def test_g5_talking_over_the_wait_drops_the_raise_that_was_waiting():
    m = Model(tool("is Friday free?"))
    brain = StreamBrain(words=("Friday is free.",), first=3.0)
    sp = FlushSpeaker()
    mo = streaming(m, brain, items=[LEA], wait_after_s=0.8)

    async def go():
        t = asyncio.get_running_loop().create_task(mo.turn("Is Friday free?", "en", sp))
        await asyncio.sleep(0.15)                       # the owner talks on before the item was due
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        await asyncio.sleep(1.0)                        # (past the time the item would have been said)
    run(go())
    assert sp.said == [FILLERS["en"][0]]


# ---------------------------------------------------------------- talked over

def test_g5_talked_over_mid_answer_the_voice_model_knows_what_was_heard():
    m = Model(tool("tell me about the trip"))
    brain = StreamBrain(words=("The trip starts on Friday. ", "You leave at 8 from Bern. ", "Bring a jacket."), first=0.02, gap=0.25,
                        tail=0.1)
    sp = FlushSpeaker()
    mo = streaming(m, brain)

    async def go():
        t = asyncio.get_running_loop().create_task(mo.turn("Tell me about the trip", "en", sp))
        while not any(k == "feed" for _, k, _ in sp.stamps):
            await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    run(go())
    roles = [x["role"] for x in mo.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert "talked on" in json.loads(mo.messages[3]["content"])["answer"]
    assert mo.messages[4]["content"].endswith(" …") and "The trip starts on Friday." in mo.messages[4]["content"]


def test_g5_the_rest_of_a_talked_over_answer_comes_in_the_next_turn_with_what_was_already_heard():
    m = Model(tool("tell me about the trip"), text("Sure, the jacket."))
    brain = StreamBrain(words=("The trip starts on Friday. ", "You leave at 8 from Bern. ", "Bring a jacket."), first=0.02, gap=0.2,
                        tail=0.0)
    sp = FlushSpeaker()
    mo = streaming(m, brain)

    async def go():
        t = asyncio.get_running_loop().create_task(mo.turn("Tell me about the trip", "en", sp))
        while not any(k == "feed" for _, k, _ in sp.stamps):
            await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        await asyncio.sleep(1.5)                                 # Claude finishes while the owner talks
        return await mo.turn("Wait, what should I bring?", "en", FlushSpeaker())
    run(go())
    last = m.calls[-1]["messages"][-1]["content"]
    assert "came after they talked on" in last and "Bring a jacket." in last
    assert "already heard" in last and "The trip starts on Friday." in last      # so it is not said twice


def test_g5_an_answer_that_was_fully_said_before_the_talk_over_is_not_said_again():
    m = Model(tool("what's on tomorrow?"), text("Sure."))
    brain = StreamBrain(words=("School at 8.",), tail=0.5)
    sp = FlushSpeaker()
    mo = streaming(m, brain)

    async def go():
        await mo.turn("What's on tomorrow?", "en", sp)          # returns at the end of Claude's text
        await asyncio.sleep(0.7)
        return await mo.turn("thanks", "en", FlushSpeaker())
    run(go())
    assert "came after they talked on" not in m.calls[-1]["messages"][-1]["content"] and mo.notes == []


def test_g5_a_bug_in_the_ask_never_kills_the_call():
    m = Model(tool("what's on tomorrow?"), text("Sorry, I could not get that."))

    async def broken(request, lines, about, sink):
        raise RuntimeError("bug")
    mo = mouth(m, items=[], ask_stream=broken, stream_verbatim=True, early_end=True)
    sp = FlushSpeaker()
    r = run(mo.turn("What's on tomorrow?", "en", sp))
    assert "RuntimeError" in json.loads(m.calls[1]["messages"][3]["content"])["answer"] and not r.streamed
    assert sp.heard == "Sorry, I could not get that."
