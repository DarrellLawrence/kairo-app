"""Voice G9: speech-text normalisation in the calls (flags g9.speaknorm and g9.speaknorm_en_digit_times).

What goes to the voice is changed, what the phone shows is not. Off (the default) nothing changes, not even
where the first piece of an answer is cut. The WebRTC call needs the voice packages (Pipecat, aiortc); run it with
the voice Python."""

from __future__ import annotations

import asyncio
import io
import wave

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import flags, speaknorm, voicelink, voicertc
from app.db import make_engine
from app.main import create_app
from app.voicelink import Sentences, first_clause
from app.voicertc import RtcSpeaker
from tests.test_voice_rtc import FakeVoice as RtcFakeVoice
from tests.test_voice_rtc import _app_call, _call, _needs_pipecat
from tests.test_voice_local import local  # noqa: F401  (the app with the fake voice model and Claude)
from tests.test_voicelink import FakeVoice as LineFakeVoice
from tests.test_voicelink import frames_until, utter

# ---------------------------------------------------------------- the switches


def test_the_flags_are_off_until_the_owner_has_listened(migrated):
    for name in ("g9.speaknorm", "g9.speaknorm_en_digit_times"):
        assert flags.FLAGS[name][0] is False
    with Session(make_engine(migrated.db_url)) as s:
        assert speaknorm.for_call(s) is None
        flags.set_flag(s, "g9.speaknorm_en_digit_times", True)
        assert speaknorm.for_call(s) is None                   # the second switch alone does nothing
        flags.set_flag(s, "g9.speaknorm", True)
        flags.set_flag(s, "g9.speaknorm_en_digit_times", False)
        assert speaknorm.for_call(s)("Your meeting is at 14:10.", "en") == ("Your meeting is at ten past two.", "en")
        flags.set_flag(s, "g9.speaknorm_en_digit_times", True)
        assert speaknorm.for_call(s)("Your meeting is at 14:10.", "en")[0] == "Your meeting is at two ten."
        flags.set_flag(s, "g9.speaknorm", False)
        assert speaknorm.for_call(s) is None


def test_the_hook_keeps_the_language_it_found_and_never_fails():
    hook = speaknorm.hook()
    assert hook("Dein Termin ist um 14:30", None) == ("Dein Termin ist um vierzehn Uhr dreißig", "de")   # found, and kept for the voice
    assert hook("Hallo zusammen", None) == ("Hallo zusammen", None)                                     # nothing to change: untouched
    assert hook("Ruhig bleiben.", "de") == ("Ruhig bleiben.", "de")


def test_a_bug_in_the_rules_is_never_a_silent_call(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("bug")
    monkeypatch.setattr(speaknorm, "normalize", broken)
    assert speaknorm.hook()("Um 14:30 Uhr.", "de") == ("Um 14:30 Uhr.", "de")


# ---------------------------------------------------------------- where the first piece is cut

def test_the_first_piece_does_not_end_on_a_number_when_the_rules_are_on():
    text = "Dein Termin ist um 14:30 Uhr. Danach "
    assert first_clause(text) == len("Dein Termin ist um 14:30 ")                              # as always: five words
    assert first_clause(text, whole_numbers=True) == len("Dein Termin ist um 14:30 Uhr.")      # "Uhr" stays with its time
    assert first_clause("Es kostet nur noch 12,50 Franken pro", whole_numbers=True) == len("Es kostet nur noch 12,50 Franken ")
    assert first_clause("Es kostet nur noch 12,50 ", whole_numbers=True) is None               # waits for the unit
    plain = "Du hast noch fünf neue Mails"
    assert first_clause(plain, whole_numbers=True) == first_clause(plain) == len("Du hast noch fünf neue ")   # no digit: unchanged


def test_the_first_piece_is_not_cut_in_front_of_a_number_that_the_word_before_it_explains():
    """The rules see one piece at a time: "CHF" | "120.50", "am" | "23.10.", "in" | "1 Minute" must not be parted."""
    text = "Die letzte Rechnung war CHF 120.50 und ist offen."
    assert first_clause(text) == len("Die letzte Rechnung war CHF ")                               # off: as always, five words
    assert first_clause(text, whole_numbers=True) == len("Die letzte Rechnung war CHF 120.50 und ")  # on: the amount stays whole
    assert first_clause("Der nächste Bus fährt in 1 Minute ab.", whole_numbers=True) == len("Der nächste Bus fährt in 1 Minute ")
    assert first_clause("Die Frist ist am 23.10. und dann", whole_numbers=True) == len("Die Frist ist am 23.10. und ")
    assert first_clause("Die Frist ist am 23.10. um 9 Uhr.", whole_numbers=True) is None      # "um 9 Uhr." goes on: no end yet
    # the next word has not arrived yet: it may be a number, so the cut waits
    assert first_clause("Du hast noch fünf neue ", whole_numbers=True) is None
    assert first_clause("Du hast noch fünf neue Mails", whole_numbers=True) == len("Du hast noch fünf neue ")
    assert first_clause("Du hast noch fünf neue  Mails", whole_numbers=True) == len("Du hast noch fünf neue ")   # two spaces
    assert first_clause("Du hast noch fünf neue ") == len("Du hast noch fünf neue ")          # off: no waiting
    # "Fr." and the weekday abbreviations are no sentence end in front of a number (off: they are, as always)
    assert first_clause("Dein Termin kostet heute Fr. 12.50 und mehr.") == len("Dein Termin kostet heute Fr.")
    assert first_clause("Dein Termin kostet heute Fr. 12.50 und mehr.", whole_numbers=True) == len("Dein Termin kostet heute Fr. 12.50 und ")
    assert first_clause("Das Treffen ist am Mo. 23.10. um 8 Uhr. Danach", whole_numbers=True) == len("Das Treffen ist am Mo. 23.10. um 8 Uhr.")
    assert first_clause("Das Treffen ist am Mo. 23.10. um 8 Uhr. Danach") == len("Das Treffen ist am Mo.")   # off: cut at "Mo.", as always
    assert first_clause("Das war es. 3 Mails sind neu.", whole_numbers=True) == len("Das war es.")   # a real sentence end still cuts


def _streamed(speaker, text):
    """The pieces an answer comes out as when it streams in one word at a time."""
    for word in text.split(" "):
        speaker.feed(word + " ")
    speaker.finish()
    out = []
    while not speaker.queue.empty():
        piece = speaker.queue.get_nowait()
        if piece is not None:
            out.append(piece)
    return out


def test_the_amount_goes_in_the_first_piece_and_the_rest_follows():
    async def run():
        return _streamed(RtcSpeaker(1, whole_numbers=True), "Die letzte Rechnung war CHF 120.50 und ist offen.")
    assert asyncio.run(run()) == ["Die letzte Rechnung war CHF 120.50 und", "ist offen."]


@pytest.mark.parametrize("text,lang,said", [
    ("Die letzte Rechnung war CHF 120.50 und ist offen.", "de", "Die letzte Rechnung war einhundertzwanzig Franken fünfzig und ist offen."),
    ("Your last bill was CHF 12.50 and is open now.", "en", "Your last bill was twelve francs fifty and is open now."),
    ("Your last bill was EUR 12.50 and is open now.", "en", "Your last bill was twelve euros fifty and is open now."),
    ("Dein Termin kostet heute Fr. 12.50 und mehr.", "de", "Dein Termin kostet heute zwölf Franken fünfzig und mehr."),
    ("Der nächste Bus fährt in 1 Minute ab.", "de", "Der nächste Bus fährt in einer Minute ab."),
    ("Der nächste Test ist am 23.10. um 9 Uhr.", "de", "Der nächste Test ist am dreiundzwanzigsten Oktober um neun Uhr."),
    ("Heute ist leider schon der 23. Oktober.", "de", "Heute ist leider schon der dreiundzwanzigste Oktober."),
])
def test_an_answer_that_streams_in_is_said_like_the_same_answer_in_one_piece(text, lang, said):
    assert speaknorm.normalize(text, lang) == said                      # (the rules on the whole text)

    async def run():
        pieces = _streamed(RtcSpeaker(1, whole_numbers=True), text)
        hook = speaknorm.hook()
        return pieces, " ".join(hook(p, lang)[0] for p in pieces)
    pieces, heard = asyncio.run(run())
    assert heard == said, pieces                                    # cut in pieces or not, it is said as the whole text


def test_sentences_and_the_rtc_speaker_hold_the_unit_with_the_number_only_when_asked():
    off, on = Sentences(), Sentences(whole_numbers=True)
    assert off.feed("Dein Termin ist um 14:30 Uhr. ") == ["Dein Termin ist um 14:30"]
    assert on.feed("Dein Termin ist um 14:30 ") == []
    assert on.feed("Uhr. Danach") == ["Dein Termin ist um 14:30 Uhr."]

    def pieces(speaker):
        speaker.feed("Es kostet nur noch 12,50 Franken pro Monat.")
        speaker.finish()
        out = []
        while not speaker.queue.empty():
            piece = speaker.queue.get_nowait()
            if piece is not None:
                out.append(piece)
        return out
    assert pieces(RtcSpeaker(1)) == ["Es kostet nur noch 12,50", "Franken pro Monat."]
    assert pieces(RtcSpeaker(1, whole_numbers=True)) == ["Es kostet nur noch 12,50 Franken", "pro Monat."]


# ---------------------------------------------------------------- the old line (/ws/voice)

def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\0\0" * 240)
    return buf.getvalue()


class Voice:
    def __init__(self):
        self.spoken: list[tuple[str, str | None]] = []

    def speak(self, text, language=None):
        self.spoken.append((text, language))
        return _wav(), language or "en"


def _speaker_run(norm):
    async def run():
        voice, sent = Voice(), []

        async def send(message):
            sent.append(message)
        speaker = voicelink.Speaker(voice, send, send, "de", 1, norm=norm)
        speaker.say("Dein Termin ist um 14:30 Uhr.")
        speaker.finish()
        await speaker.done()
        return voice.spoken, [m["text"] for m in sent if isinstance(m, dict) and m.get("type") == "say"]
    return asyncio.run(run())


def test_the_old_lines_speaker_speaks_the_words_and_shows_the_text():
    spoken, shown = _speaker_run(speaknorm.hook())
    assert spoken == [("Dein Termin ist um vierzehn Uhr dreißig.", "de")] and shown == ["Dein Termin ist um 14:30 Uhr."]
    spoken, shown = _speaker_run(None)
    assert spoken == [("Dein Termin ist um 14:30 Uhr.", "de")] and shown == ["Dein Termin ist um 14:30 Uhr."]


@pytest.fixture
def line(migrated):
    app = create_app(migrated)
    app.state.voice = LineFakeVoice("Dein Termin ist um 14:30 Uhr", "de")
    with TestClient(app) as c:
        c.voice = app.state.voice
        yield c


def test_a_turn_on_the_old_line_says_the_time_as_words_when_the_switch_is_on(line):
    assert line.put("/api/flags/g9.speaknorm", json={"on": True}).status_code == 200
    with line.websocket_connect("/ws/voice") as ws:
        utter(ws)
        frames = frames_until(ws)
    said = " ".join(t for t, _ in line.voice.spoken)
    assert "vierzehn Uhr dreißig" in said and "14:30" not in said
    shown = " ".join(f["text"] for f in frames if isinstance(f, dict) and f.get("type") == "say")
    assert "14:30 Uhr" in shown                                   # the captions are as written


def test_a_turn_on_the_old_line_is_unchanged_when_the_switch_is_off(line):
    with line.websocket_connect("/ws/voice") as ws:
        utter(ws)
        frames_until(ws)
    said = " ".join(t for t, _ in line.voice.spoken)
    assert "14:30" in said and "vierzehn" not in said


def test_a_line_the_phone_asks_for_is_normalised_too(line):
    line.put("/api/flags/g9.speaknorm", json={"on": True})
    with line.websocket_connect("/ws/voice") as ws:
        ws.send_json({"type": "say", "text": "Um 14:30 Uhr geht es los.", "turn": 1})
        frames_until(ws)
    assert ("Um vierzehn Uhr dreißig geht es los.", "de") in line.voice.spoken


# ---------------------------------------------------------------- a call over WebRTC

class GermanVoice(RtcFakeVoice):
    def transcribe_pcm(self, pcm, language=None):
        got = super().transcribe_pcm(pcm, language)
        got.language = "de"
        return got


def _call_with(norm, voice, said):
    _needs_pipecat()
    real = voicertc.build_call

    async def answer(text, language, number, speaker):
        speaker.feed(said)
        speaker.finish()
        return {"route": "talk", "asked": False}

    async def run():
        orig = voicertc.build_call
        voicertc.build_call = lambda *a, **kw: real(*a, norm=norm, **kw)
        try:
            return await _call(answer, voice=voice)
        finally:
            voicertc.build_call = orig
    return asyncio.run(run())


def test_a_call_says_the_time_as_words_and_shows_it_as_written():
    voice = RtcFakeVoice()
    phone = _call_with(speaknorm.hook(), voice, "Your meeting is at 14:10 sharp.")
    assert voice.spoken == ["Your meeting is at ten past two sharp."], phone.events          # one piece: "sharp" stays with it
    said = [e["text"] for e in phone.events if e.get("type") == "say"]
    assert said == ["Your meeting is at 14:10 sharp."]
    voice = GermanVoice()
    _call_with(speaknorm.hook(), voice, "Dein Termin ist um 14:30 Uhr.")
    assert voice.spoken == ["Dein Termin ist um vierzehn Uhr dreißig."]


def test_a_call_with_the_switch_off_is_cut_and_spoken_as_before():
    voice = GermanVoice()
    _call_with(None, voice, "Dein Termin ist um 14:30 Uhr.")
    assert voice.spoken == ["Dein Termin ist um 14:30", "Uhr."]


def test_a_call_through_the_app_reads_the_switch_once_when_it_starts(local, monkeypatch):   # noqa: F811
    _needs_pipecat()
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    seen, real = [], voicertc.build_call

    def spy(*args, **kwargs):
        seen.append(kwargs.get("norm"))
        return real(*args, **kwargs)
    monkeypatch.setattr(voicertc, "build_call", spy)
    local.app.state.openers = voicertc.OpenerCache(lambda text, lang: (b"\x00\x10" * 2400, 24000))
    for on in (False, True):
        assert local.put("/api/flags/g9.speaknorm", json={"on": on}).status_code == 200
        asyncio.run(_app_call(local, seconds=2.0))
    assert seen[0] is None and callable(seen[1])
