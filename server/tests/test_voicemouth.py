"""The mouth (owner's decision 2026-10-16): the local voice model talks, Claude works through ask_kairo.
Written before the wiring into /ws/voice. The voice model here is scripted (no GPU in CI)."""

from __future__ import annotations

import asyncio
import json

import pytest

from app import bringup
from app.voicemouth import FILLERS, LATER, Mouth, Unmark, small_talk


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


def mouth(model, brain=None, items=None, **kw):
    return Mouth(model, ask=brain or Brain(), context="## Right now\nThursday 14:00", items=ITEMS if items is None else items,
                 name="Sam", **kw)


def run(coro):
    return asyncio.run(coro)


def test_chat_is_answered_by_the_voice_model_alone_and_its_only_tool_is_ask_kairo():
    m, sp, brain = Model(text("Hey! ", "All good here.")), Speaker(), Brain()
    r = run(mouth(m, brain).turn("tell me something nice", "en", sp))
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


def test_claude_gets_8_seconds_on_a_call_then_kairo_gets_back_with_a_push():
    """Owner's request 2026-10-17: one holding line, never "still on it"; after the cap Kairo says it will
    get back, the call moves on, and the answer comes as a push (and in the next turn)."""
    m = Model(tool("check my mail"), text("Sure, and your mail: nothing new."))
    late = []
    mo = mouth(m, Brain(answer="Nothing new in your mail.", delay=0.3), items=[], ask_timeout_s=0.1,
               late=lambda req, ans: late.append((req, ans)))

    async def go():
        sp1 = Speaker()
        r1 = await mo.turn("check my mail", "en", sp1)
        await asyncio.sleep(0.4)                                    # Claude's answer comes after the call moved on
        sp2 = Speaker()
        r2 = await mo.turn("ok and what's next", "en", sp2)
        return sp1, r1, sp2, r2
    sp1, r1, sp2, r2 = run(go())
    assert sp1.said == [FILLERS["en"][0], LATER["en"]] and r1.deferred and r1.tool_ms < 300
    assert late == [("check my mail", "Nothing new in your mail.")]
    assert "Nothing new in your mail." in m.calls[-1]["messages"][-1]["content"]   # said in the next turn too
    assert len(m.calls) == 2                                        # no extra round after "I'll get back to you"


def test_the_holding_line_is_said_once_a_turn():
    m = Model(tool("a"), tool("b"), text("Done."))
    sp = Speaker()
    run(mouth(m, items=[]).turn("first a then b", "en", sp))
    assert sp.said == [FILLERS["en"][0]]


def test_small_talk_never_goes_to_claude():
    for said in ("Hello", "hey Kairo!", "hi there", "how are you?", "thanks", "thank you so much", "okay cool",
                 "bye", "good morning", "hallo", "wie geht's", "danke", "tschüss", "[14:05] hey"):
        assert small_talk(said), said
    for said in ("hey what's on tomorrow?", "remind me to call mom", "thanks, and when is my train?", "ok add milk",
                 "hello can you check my mail", ""):
        assert not small_talk(said), said
    m, brain = Model(text("Anytime!")), Brain()
    r = run(mouth(m, brain).turn("thanks a lot", "en", Speaker()))
    assert m.calls[0]["tools"] is None and brain.asked == [] and r.route == "talk"


def test_the_conversation_only_grows_at_the_end_so_the_cache_holds():
    m = Model(text("Good, you?"), tool("tasks"), text("You have two tasks."), text("Bye!"))
    mo = mouth(m)
    run(mo.turn("how's it going", "en", Speaker()))
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
    m = Model(text("Good, you?"), text("Sure."))
    mo = mouth(m)
    run(mo.turn("how are you", "en", Speaker()))
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


# ---------------------------------------------------------------- the wait (owner's request 2026-10-16)

LEA = bringup.Item("L1", "loop", "reply: Lea asks if Friday works (Lea)", "normal",
                   says={"en": "Did you answer Lea about Friday yet?", "de": "Hast du Lea wegen Freitag geantwortet?"})
CASE = bringup.Item("L2", "loop", "promise: order the phone case", "normal",
                    says={"en": "Did you order the phone case yet?", "de": "Hast du die Handyhülle bestellt?"})
FORM = bringup.Item("L3", "loop", "task: sign the school form", "urgent",
                    says={"en": "The school form has to be signed today.", "de": "Das Schulformular muss heute unterschrieben sein."})


def test_the_wait_is_a_holding_line_then_one_item_that_fits():
    m = Model(tool("is Friday free?"), text("Friday is free after 4."))
    sp = Speaker()
    mo = mouth(m, Brain(answer="Friday: free after 16:00.", delay=0.3), items=[CASE, LEA], wait_after_s=0.05)
    r = run(mo.turn("Is Friday free?", "en", sp))
    assert sp.said == [FILLERS["en"][0], "Quick one: Did you answer Lea about Friday yet?"]   # fits what they asked
    assert r.waited == ["L1"] and r.raised == ["L1"] and "Friday is free after 4." in sp.heard
    assert "[[raised:L1]] Quick one: Did you answer Lea" in m.calls[1]["messages"][2]["content"]   # the model knows
    assert sp.events.index(("say", sp.said[1])) < sp.events.index(("feed", "Friday is free after 4."))


def test_a_fast_answer_never_waits_for_the_item():
    m, sp = Model(tool("is Friday free?"), text("Free after 4.")), Speaker()
    r = run(mouth(m, Brain(delay=0.0), items=[LEA], wait_after_s=0.3).turn("Is Friday free?", "en", sp))
    assert sp.said == [FILLERS["en"][0]] and r.waited == []                # the answer was there: no detour


def test_urgent_comes_first_before_the_answer_even_when_fast():
    m, sp = Model(tool("is Friday free?"), text("Free after 4.")), Speaker()
    r = run(mouth(m, Brain(delay=0.0), items=[LEA, FORM], wait_after_s=5).turn("Is Friday free?", "en", sp))
    assert sp.said[1] == "Before I forget, this one's urgent: The school form has to be signed today." and r.waited == ["L3"]
    assert sp.events.index(("say", sp.said[1])) < sp.events.index(("feed", "Free after 4."))


def test_one_item_per_wait_never_twice_and_the_cap_holds():
    m = Model(tool("mail?"), text("No mail."), tool("tasks?"), text("Two tasks."), tool("weather?"), text("Sunny."))
    mo = mouth(m, Brain(delay=0.2), items=[LEA, CASE, bringup.Item("K9", "question", "q", says={"en": "When is training?"})],
               wait_after_s=0.02)
    said = []
    for q in ("any mail?", "my tasks?", "the weather?"):
        sp = Speaker()
        run(mo.turn(q, "en", sp))
        said.append(sp.said[1:])
    assert len(said[0]) == 1 and len(said[1]) == 1 and said[0] != said[1]   # one each, never the same
    assert said[2] == []                                                    # two per call: then just the holding line


def test_nothing_worth_saying_means_only_the_holding_line():
    m, sp = Model(tool("mail?"), text("Nothing.")), Speaker()
    unphrased = [bringup.Item("L7", "loop", "reply: something from a mail")]   # Claude hasn't phrased it
    run(mouth(m, Brain(delay=0.2), items=unphrased, wait_after_s=0.02).turn("any mail?", "en", sp))
    assert sp.said == [FILLERS["en"][0]]


def test_german_wait_lines_are_german():
    m, sp = Model(tool("Freitag frei?"), text("Freitag ab 16 Uhr frei.")), Speaker()
    run(mouth(m, Brain(delay=0.2), items=[LEA], wait_after_s=0.02).turn("Ist Freitag frei?", "de", sp))
    assert sp.said == [FILLERS["de"][0], "Kurz: Hast du Lea wegen Freitag geantwortet?"]


def test_what_the_owner_answers_goes_back_to_claude_after_the_call():
    m = Model(tool("is Friday free?"), text("Free after 4."), text("Nice, noted."))
    mo = mouth(m, Brain(delay=0.2), items=[LEA], wait_after_s=0.02)
    run(mo.turn("Is Friday free?", "en", Speaker()))
    run(mo.turn("Yeah I told her Friday works.", "en", Speaker()))
    assert mo.reports() == [(LEA, "Quick one: Did you answer Lea about Friday yet?", "Yeah I told her Friday works.")]
    req = bringup.report_request(*mo.reports()[0])
    assert "Yeah I told her Friday works." in req and "done (close it), later (snooze it" in req
    assert "UNTRUSTED DATA" in req                                       # the loop's text can come from mail


def test_the_owners_answer_goes_to_claude_during_the_call():
    sent = []
    m = Model(tool("is Friday free?"), text("Free after 4."), text("Nice, noted."), text("Sure."))
    mo = mouth(m, Brain(delay=0.2), items=[LEA], wait_after_s=0.02, report=lambda *r: sent.append(r))
    run(mo.turn("Is Friday free?", "en", Speaker()))
    assert sent == []
    run(mo.turn("Yeah I told her Friday works.", "en", Speaker()))
    assert sent == [(LEA, "Quick one: Did you answer Lea about Friday yet?", "Yeah I told her Friday works.")]
    run(mo.turn("ok", "en", Speaker()))
    assert len(sent) == 1 and mo.reports() == []                          # once, and nothing left for later


def test_the_answer_rides_along_when_the_next_words_go_to_claude_anyway():
    sent, brain = [], Brain(delay=0.2)
    m = Model(tool("is Friday free?"), text("Free after 4."), tool("and Saturday?"), text("Saturday too."))
    mo = mouth(m, brain, items=[LEA], wait_after_s=0.02, report=lambda *r: sent.append(r))
    run(mo.turn("Is Friday free?", "en", Speaker()))
    run(mo.turn("Yes I answered her. And Saturday?", "en", Speaker()))
    request = brain.asked[1][0]
    assert request.startswith("and Saturday?") and "On this call (record it now" in request
    assert '"Yes I answered her. And Saturday?"' in request and sent == []   # one Claude turn, no extra wait


def test_an_item_the_voice_model_passed_on_itself_is_not_reported_twice():
    m = Model(tool("is Friday free?"), text("Free after 4."), tool("told Lea Friday works", item="L1"), text("Noted."))
    mo = mouth(m, Brain(delay=0.2), items=[LEA], wait_after_s=0.02)
    run(mo.turn("Is Friday free?", "en", Speaker()))
    run(mo.turn("I told Lea Friday works.", "en", Speaker()))
    assert mo.reports() == []


def test_talked_over_in_the_wait_the_answer_still_comes_in_the_next_turn():
    async def go():
        m = Model(tool("did Tom reply?"), text("Ha, okay."), text("Tom replied: he's in."))
        mo = mouth(m, Brain(answer="Tom replied: he's in.", delay=0.4), items=[LEA], wait_after_s=0.02)
        t = asyncio.get_running_loop().create_task(mo.turn("did Tom reply?", "en", Speaker()))
        await asyncio.sleep(0.1)                                   # the item was said; they answer it
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        sp = Speaker()
        r = await mo.turn("Yes I answered Lea.", "en", sp)
        assert sp.heard == "Ha, okay. Tom replied: he's in." and r.text.endswith("Tom replied: he's in.")
        assert "is here: Tom replied: he's in." in m.calls[2]["messages"][-1]["content"]
        assert mo.reports()[0][2] == "Yes I answered Lea." and mo.pending == []
    run(go())


# ---------------------------------------------------------------- the Today card

CARD = {"now": "Friday 16 October 2026", "events": "- Fri 18:00 Training", "weather": "Home: now 12°C, cloudy",
        "trip": "Home to School:\n- 07:12, S3, platform 2"}


def test_the_card_is_the_context_and_only_changed_parts_follow():
    m = Model(text("Twelve degrees."), text("Now it's sunny."))
    mo = Mouth(m, ask=Brain(), context=dict(CARD), items=[], name="Sam")
    run(mo.turn("what's the weather?", "en", Speaker()))
    sysmsg = m.calls[0]["messages"][0]["content"]
    assert "# Today card" in sysmsg and "## Weather\nHome: now 12°C, cloudy" in sysmsg and "UNTRUSTED DATA" in sysmsg
    assert mo.update_card({**CARD, "weather": "Home: now 14°C, sunny"}) == ["weather"]
    assert mo.update_card({**CARD, "weather": "Home: now 14°C, sunny"}) == []                 # nothing new: no note
    run(mo.turn("and now?", "en", Speaker()))
    last = m.calls[1]["messages"][-1]["content"]
    assert "## Weather\nHome: now 14°C, sunny" in last and "## Next events" not in last
    assert m.calls[1]["messages"][0] == m.calls[0]["messages"][0]                         # the cache holds


def test_the_owners_words_carry_the_time_and_questions_are_counted():
    import datetime as dt
    m = Model(text("At 18:00."), tool("note it"), text("Noted."), text("Haha."))
    mo = mouth(m, items=[], clock=lambda: dt.datetime(2026, 10, 16, 14, 5))
    r1 = run(mo.turn("When is training?", "en", Speaker()))
    r2 = run(mo.turn("Note that I paid", "en", Speaker()))
    r3 = run(mo.turn("lol okay", "en", Speaker()))
    assert m.calls[0]["messages"][-1]["content"] == "[14:05] When is training?"
    assert (r1.route, r2.route, r3.route) == ("card", "claude", "talk")


def test_a_long_call_starts_fresh_before_the_context_is_full():
    m = Model(text("Sure."), text("Okay."))
    mo = mouth(m, items=[], context_tokens=4096)
    run(mo.turn("first thing", "en", Speaker()))
    mo.lines = []                                                        # as after a Claude call
    mo.prompt_tokens = 4000
    run(mo.turn("second thing", "en", Speaker()))
    msgs = m.calls[1]["messages"]
    assert [x["role"] for x in msgs] == ["system", "user"] and "Earlier on this call" in msgs[1]["content"]
    assert "Owner: first thing" in msgs[1]["content"]


# ---------------------------------------------------------------- Voice V3: speculation

def test_the_first_round_started_at_the_pause_is_used_when_the_turn_is_the_same():
    m, sp = Model(text("Sure, ", "I'll remind you."), delay=0.01), Speaker()
    mo = mouth(m)

    async def go():
        assert mo.speculate("remind me to call the dentist")
        await asyncio.sleep(0.05)                              # the turn is confirmed a little later
        return await mo.turn("remind me to call the dentist", "en", sp)
    r = run(go())
    assert len(m.calls) == 1 and mo.speculation_used == 1      # one generation: the early one
    assert sp.heard == "Sure, I'll remind you." and r.text == "Sure, I'll remind you."
    assert [x["role"] for x in mo.messages] == ["system", "user", "assistant"]


def test_a_speculation_on_other_words_is_thrown_away_and_never_reaches_claude():
    m = Model(tool("call the dentist"), text("Okay, tomorrow at nine."))
    sp, brain = Speaker(), Brain()
    mo = mouth(m, brain)

    async def go():
        mo.speculate("remind me to call")                      # they paused mid-thought...
        await asyncio.sleep(0.01)
        assert brain.asked == []                               # speculation only runs ahead: no Claude
        return await mo.turn("remind me to call the dentist tomorrow at nine", "en", sp)   # ...and went on
    r = run(go())
    assert len(m.calls) == 2 and mo.speculation_used == 0 and mo.speculated == 1
    assert m.calls[1]["messages"][-1]["content"].endswith("remind me to call the dentist tomorrow at nine")
    assert r.text == "Okay, tomorrow at nine." and brain.asked == []


def test_talking_on_drops_the_speculation_and_a_changed_card_does_too():
    m = Model(text("A"), text("B"), text("C"))
    mo = Mouth(m, ask=Brain(), context={"now": "Thursday", "weather": "sunny"}, items=[], name="Sam")

    async def go():
        mo.speculate("what's the weather")
        await asyncio.sleep(0.01)
        mo.drop_speculation()                                  # the owner talked on
        mo.speculate("what's the weather")
        await asyncio.sleep(0.01)
        mo.update_card({"now": "Thursday", "weather": "rain at 15:00"})   # the card changed in between
        return await mo.turn("what's the weather", "en", Speaker())
    r = run(go())
    assert mo.speculation_used == 0 and len(m.calls) == 3 and r.text == "C"
    assert "rain at 15:00" in m.calls[2]["messages"][-1]["content"]


# ---------------------------------------------------------------- greetings and dropped calls (owner's call 2026-10-20)

def test_hello_gets_the_greeting_at_once_without_the_model_or_claude():
    """ "Hello" was answered by a raw tool call: now it never even reaches the voice model. The greeting is
    one of the cached lines, so it plays at once."""
    m, brain = Model(text("Sure.")), Brain()
    mo = mouth(m, brain, greeting="Hey Sam.")
    for said in ("Hello", "hey!", "hi Kairo", "Hallo?", "[14:05] hey there"):
        sp = Speaker()
        r = run(mo.turn(said, "en", sp))
        assert sp.said == ["Hey Sam."] and sp.fed == [] and r.text == "Hey Sam." and r.route == "talk", said
        assert r.first_text_ms is not None and r.raised == [] and r.asked == []
    assert m.calls == [] and brain.asked == []
    assert [x["role"] for x in mo.messages[-2:]] == ["user", "assistant"] and mo.messages[-1]["content"] == "Hey Sam."
    assert not mo.speculate("hello")                                    # nothing runs ahead for it either
    before = json.loads(json.dumps(mo.messages))
    run(mo.turn("cool, what's on tomorrow?", "en", Speaker()))
    assert m.calls[0]["messages"][:len(before)] == before                # the conversation only grew at the end


def test_the_greeting_defaults_to_the_first_name():
    sp = Speaker()
    run(Mouth(Model(), ask=Brain(), context="", items=[], name="Sam Rivera").turn("hey", "en", sp))
    assert sp.said == ["Hey Sam."]


def test_a_reply_that_was_only_a_dropped_call_never_leaves_the_owner_in_silence():
    """The voice model's whole reply was a tool call that was dropped (not offered, no request): nothing
    came out of the stream. Small talk gets a short neutral line, a real question a short ask-again."""
    from app.voicemouth import AGAIN, NEUTRAL
    sp = Speaker()
    r = run(mouth(Model([]), items=[]).turn("thanks", "en", sp))
    assert sp.said == [NEUTRAL["en"]] and r.text == NEUTRAL["en"]
    sp = Speaker()
    run(mouth(Model([]), items=[]).turn("hey Kairo how are you?", "en", sp))
    assert sp.said == ["Hey Sam."]                                      # small talk that greets: the greeting
    sp = Speaker()                                                        # a real question: holding line, then no call
    r = run(mouth(Model([{"kind": "tool_start", "name": "ask_kairo"}]), items=[]).turn("what's on tomorrow?", "de", sp))
    assert sp.said == [FILLERS["de"][0], AGAIN["de"]] and r.asked == []
    mo = mouth(Model([]), items=[])
    run(mo.turn("thanks", "en", Speaker()))
    assert mo.messages[-1] == {"role": "assistant", "content": NEUTRAL["en"]}   # what was said is what it sees


def test_claudes_answer_is_said_even_when_the_voice_model_then_says_nothing():
    m, sp = Model(tool("what's on tomorrow?"), []), Speaker()
    r = run(mouth(m, Brain(answer="School at 8, training at 18:00."), items=[]).turn("What's on tomorrow?", "en", sp))
    assert sp.said == [FILLERS["en"][0], "School at 8, training at 18:00."] and r.asked == ["what's on tomorrow?"]


def test_the_first_tool_call_of_a_round_is_the_one_used():
    two = tool("first") + [{"kind": "tool_call", "id": "c2", "name": "ask_kairo", "arguments": json.dumps({"request": "second"})}]
    brain = Brain()
    run(mouth(Model(two, text("Ok.")), brain, items=[]).turn("do the first thing", "en", Speaker()))
    assert [a[0] for a in brain.asked] == ["first"]


def test_yes_to_a_question_kairo_asked_keeps_the_tool():
    """ "Should I add that?" "Yes." must still be able to reach Claude; a plain "yes" otherwise is just chat."""
    assert small_talk("yes") and not small_talk("yes", after_question=True)
    assert small_talk("thanks", after_question=True)
    m = Model(text("Should I add milk to your list?"), tool("add milk to the list"), text("Done, it's on the list."))
    brain = Brain(answer="Added milk to the shopping list.")
    mo = mouth(m, brain, items=[])

    async def go():
        await mo.turn("I'm out of milk", "en", Speaker())
        return await mo.turn("yes", "en", Speaker())
    r = run(go())
    assert m.calls[1]["tools"] is not None and brain.asked and r.route == "claude"
