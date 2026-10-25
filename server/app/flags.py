"""Owner-only switches for changes that wait for the owner's OK or for a measurement on the PC (overnight build
rules: every change ships behind a flag). Each flag has a default: on for plain fixes, off for anything the
owner has to decide or the PC has to prove first. The owner flips them in More -> System -> Experiments
(GET/PUT /api/flags, owner-only like every route). The model can't: settings_set doesn't know these keys.

Groups add their flags under their own marker, so parallel branches merge cleanly.
"""

from __future__ import annotations

from sqlmodel import Session

from .db import SettingKV

PREFIX = "flag:"

# name -> (default, what it changes, in the owner's words)
FLAGS: dict[str, tuple[bool, str]] = {
    # -- G1 Claude CLI
    "g1.cli_no_summary": (False, "Claude answers about 2 seconds sooner: it stops writing a summary after each answer, and "
                                 "the next message no longer waits for it. This uses an undocumented Claude Code setting, "
                                 "so it stays off until you decide; Voice speed warns if a Claude update makes it stop "
                                 "working."),

    # -- G2 Whisper
    "g2.temperature_zero": (True, "On calls, Whisper writes each sentence down once and never retries with random variation "
                                  "when it is unsure: the same words every time, and no slow second pass. A piece that only "
                                  "repeats itself (\"cut, cut, cut ...\") is dropped instead of retried, and counted in Voice speed."),
    "g2.offline_models": (True, "Whisper only loads the model files the installer already downloaded and never goes online to look "
                                "for them while Kairo runs. A missing model is reported, not fetched in the middle of a sentence."),
    "g2.int8_float16": (False, "Whisper runs in a lighter number format on the graphics card (int8_float16): about 0.8 GB less "
                               "graphics memory, speed the same. Wait for the PC test (speed and word errors on your voice) "
                               "before you switch it on. Takes effect at the next call."),
    "g2.warm_at_connect": (True, "When a call or the voice button connects, Whisper writes down one short test sentence in the "
                                 "background (in the language of your last call), so your first real sentence does not meet a "
                                 "cold graphics card. A sentence you say during that one test waits for it to finish. Not "
                                 "done when Whisper runs on the CPU."),
    "g2.single_encode_langid": (False, "Calls set to English and German: Whisper listens once, picks English or German from that same "
                                       "pass and writes the words down. Today it listens twice (an estimated 0.2 to 1.4 s per "
                                       "sentence). Wait for the PC test (P2) and a check on your own short sentences (P25) "
                                       "before you switch it on."),

    # -- G3 mouth
    "g3.llama_flags": (True, "The voice model's server keeps no extra copies of old prompts in the PC's memory "
                             "(--cache-ram 0) and at most 8 saved checkpoints (--ctx-checkpoints 8): the memory use "
                             "grew about 33 MiB with every call. Applies the next time the voice model starts."),
    "g3.gemma_markup": (True, "Gemma's tool-call markup (<|tool_call>...<tool_call|>) is taken out of what Kairo says on a call, "
                              "like the other models' markers; without it the voice would read it out when Gemma talks."),
    "g3.constant_tools": (False, "The voice model's start of every call stays the same: its system text and tool list never change "
                                 "(a turn that must not call the tool says 'none'), and today's card and the bring-up list "
                                 "come in the first message instead, slowest-changing first. Less of the prompt is read again at "
                                 "the start of a call and when a turn switches between small talk and a question. Waits for the "
                                 "voice lab before/after on the PC (the card moves from the system role to a user message). "
                                 "Applies from the next call."),
    "g3.swa_full": (False, "Gemma's cache kept whole (--swa-full): +280 MiB of graphics-card memory, so the cached start of the "
                           "prompt survives from one turn to the next. Wait for the PC check (the card's memory) before "
                           "turning it on. Applies the next time the voice model starts."),

    "g3.gemma_first": (False, "Gemma 4 E4B is the voice model Kairo starts with, and the voice lab picks it whenever it passes "
                              "the lab's checks, even if another model answers faster. LFM2.5 2.6B was measured speaking its own "
                              "reasoning aloud (2 of 3 probe turns), which the lab does not catch. Nothing is downloaded or removed: "
                              "LFM stays installed and can still be picked by hand in Voice lab. With this on, the lab also leaves "
                              "out any model that spoke tool-call markup in its test replies (the report shows the count either way). "
                              "A model the lab or you already picked stays until the lab is run again. Gemma uses about 3.5 GiB "
                              "of graphics memory: turn it on after the PC memory check."),

    "g3.smart_turn_pin": (False, "Calls load the exact Smart Turn v3.2 file the installer downloaded and checked (pinned revision and "
                                 "checksum in voicellm.json) instead of the copy inside the Pipecat package. The same bytes today, so "
                                 "nothing sounds different; it only keeps a later package update from changing the turn detector "
                                 "unnoticed. Does nothing yet: voicellm.json carries no pin until you approve the separate update that "
                                 "adds it (that update makes the installer download llama.cpp again, about 624 MiB). Even then, if the "
                                 "downloaded file is not the pinned one, calls keep using Pipecat's copy. Applies from the next call."),

    # -- G4 jitter buffer
    "g4.jitter_patch": (False, "Calls: your voice reaches Kairo about 60 ms sooner on every turn, and one lost packet no longer "
                               "makes the rest of the call 280 ms late (about 120 ms instead). It changes how the call library "
                               "buffers incoming audio (its inner parts; only the version Kairo ships, any other is left alone). "
                               "Takes effect on the next call."),

    # -- G5 hand-off
    "g5.stream_verbatim": (False, "When Kairo has to ask Claude something on a call, you hear Claude's own sentences as they are "
                                  "written, word for word, instead of the voice model saying them again in its own words "
                                  "after Claude is done. A short answer starts about half a second sooner and a long one "
                                  "(about 150 words) about one and a half seconds sooner (estimates from timelines measured "
                                  "on a test machine, not this PC). Off until you decide: it replaces your choice of "
                                  "2026-10-16 that the voice model says the answer. A hang-up request from Claude still "
                                  "hangs up. If Claude breaks off half way, the voice model tells you so. "
                                  "Takes effect at the next call."),
    "g5.second_piece": (True, "Kairo's voice cuts an answer into pieces so it can start before the answer is written. The first piece "
                              "is cut after five words (or the first comma), as you asked. Until now the second piece was cut "
                              "after five words as well, in the middle of a sentence, so every answer was cut twice there; "
                              "now only once. The second piece is now a whole sentence, which a voice running on the processor "
                              "(not the graphics card) can need a moment for: on a test machine's processor that left a pause "
                              "of half a second to a second in 2 to 3 of 8 answers; turn this off if you hear it. On the old line, "
                              "the holding line no longer jumps in front of words that were already written. Takes effect at "
                              "the next call."),
    "g5.flush_at_end": (True, "Kairo goes on as soon as Claude's text is finished, not when the Claude program has also finished "
                              "writing its own summary of the turn (about 2 seconds later, measured). The voice model gets "
                              "the answer then, and with the streaming switch the last words are spoken at once instead of "
                              "waiting for the full stop. Takes effect at the next call."),
    "g5.cap_first_piece": (True, "The 8 seconds Claude gets on a call count until the first words of the answer are spoken. An "
                                 "answer that has begun is never cut off by them, however long it is. Only matters with the "
                                 "streaming switch on: without it, the answer is spoken whole after Claude is done, so "
                                 "the 8 seconds count until then, as before. Takes effect at the next call."),
    "g5.filler_wording": (True, "The short line Kairo says while Claude works no longer claims to be doing something it may not be "
                                "doing (\"Pulling that up\" for a note to save). English: \"Okay, one moment.\", \"Let me check "
                                "that.\", \"One sec.\" German: \"Okay, einen Moment.\", \"Ich schau kurz nach.\", \"Sekunde.\" "
                                "Takes effect at the next call."),
    "g5.bringup_cancel": (True, "While Claude works, Kairo may raise one thing from its list after a short wait. With the streaming "
                                "switch on, that is dropped as soon as Claude's first words are being spoken, so Kairo never "
                                "breaks into the middle of an answer. Without the streaming switch it changes nothing: "
                                "a raise that has not been said yet is already dropped when the answer arrives or you talk. "
                                "Takes effect at the next call."),
    "g5.warm_on_call_open": (True, "Opening the Call screen (or the voice button) also starts Claude's conversation program in the "
                                   "background, if it is not already running, so the first question on the call does not wait about "
                                   "a second for it to start. It only starts the program; nothing is sent to Claude. Takes effect "
                                   "at once."),

    # -- G6 turn-taking
    "g6.empty_turn_fix": (True, "A sound over Kairo that Whisper can't turn into words (a cough, a chair) no longer makes it answer "
                                "your last question a second time. Measured in the research: it did, every time. Takes effect "
                                "at the next call."),
    "g6.tap_lag_fix": (True, "Tapping 'I'm done' works when the PC is busy. Before, if the PC was slow, the tap could arrive before the "
                             "words you had said had reached the part that ends the turn, be thrown away as 'nothing was said', and "
                             "the turn stayed open as long as the room stayed loud (in the test the answer came 7 seconds late, at the "
                             "hang-up). Seen in 2 of 30 test runs with the PC busy (load 7 or more on 4 cores); the first report said "
                             "1 in 4. Now the tap waits for the words, and the speech ends where you tapped, not where the PC had got "
                             "to: the last words before the tap are no longer lost. Takes effect at the next call."),
    "g6.smart_turn_3s": (False, "Off until you decide (it costs speed). When the turn detector thinks you have not finished (a sentence that trails off) and you go quiet, Kairo "
                               "waits up to 3.0 seconds before it answers, instead of 0.5. Sentences it thinks are finished are "
                               "answered as fast as before. The price, measured in the research: the detector wrongly calls about 4 "
                               "of 100 finished sentences unfinished, and those answers come 2.5 seconds later, so the average turn is "
                               "0.1 second slower, and there is a 1 in 3 chance of at least one such wait in 10 turns. Your own rate is "
                               "measured on the PC (P9). Off (for now): 0.5 seconds, as before. Takes effect at the next call."),
    "g6.ignore_windows": (False, "Off until you try it, together with g6.transcript_barge_in. A sound on your side is not heard as you starting to talk during the first 0.4 seconds of Kairo's voice "
                                "(its own voice is loudest in the microphone then) nor during 1 second after the phone's audio "
                                "stalled or the call connected (a burst of noise). If you really go on talking past that moment "
                                "you are heard from your first word. Off (for now): any sound counts at once, as before. Takes effect at the "
                                "next call."),
    "g6.transcript_barge_in": (False, "Off until you decide (it makes Kairo slower to stop). Talking over Kairo stops it only when your words are real: a stop word (stop, wait, nein, moment ...) "
                                     "or at least two words that are not all 'mhm', 'ja', 'genau', 'okay', 'thank you', and not Kairo's own "
                                     "voice coming back through the microphone. A cough, a laugh or an 'mhm' no longer cuts Kairo off, and the "
                                     "call screen switches to 'Listening' only when Kairo really stopped. The price, estimated from the "
                                     "research (not measured on the PC): it now waits for your first pause and for Whisper, so a short 'Stop.' "
                                     "stops Kairo after about 1 to 2 seconds instead of about 0.3 (0.99 to 2.09 s), and a longer sentence "
                                     "over Kairo after 1.7 to 2.8 s. Acceptance row 4 says 'at once': switching this on breaks that on purpose, "
                                     "so the row needs your update first. "
                                     "Off (for now): any sound stops Kairo at once, as before. Takes effect at the next call."),
    "g6.long_speech_cut": (False, "Needs g6.transcript_barge_in. If you talk over Kairo for a whole second without a pause, Kairo stops "
                                  "even before Whisper has written your words down, so a long sentence over Kairo stops it after about "
                                  "1.5 seconds at most (the plan said 1.3; the test sandbox measured 1.55; without this it waits for your "
                                  "first pause: 1.7 to 2.8 seconds for a 1.2 second sentence, longer for a longer one). The price: a long laugh, a hum or your own cough that goes on for a "
                                  "second also stops Kairo, and so does its own voice if it comes back through the microphone that long. "
                                  "Off until you have tried a few calls. Takes effect at the next call."),

    # -- G7 phone
    "g7.mic_constraints": (True, "On a call the phone asks the microphone for echo cancelling, noise reduction and automatic volume. "
                                  "An iPhone only does the echo cancelling and ignores the other two. Off: the old request (echo cancelling and noise reduction)."),
    "g7.call_audio_mode": (True, "A call puts the iPhone's audio into call mode before the microphone opens and plays nothing else while it is open "
                                 "(no silent test sounds), so Kairo's voice reaches you only through the call connection, the one way the iPhone cancels "
                                 "the echo. Applies to calls, the voice button and the sound check's call test. "
                                 "Off: the old start (normal audio first, then call mode, with silent test sounds)."),
    "g7.call_logs": (True, "A call writes more about the microphone and the audio to the PC's log: what the phone asked for and got, whether the iPhone "
                           "muted the microphone, every audio-mode change, the first time Kairo's voice is really heard on the phone, and at hang-up how "
                           "long the call was, whether Kairo was ever heard, and how many sound packets arrived or were lost. Nothing you say. "
                           "Off: the usual short log."),
    "g7.no_aec_warning": (True, "A call warns you on screen when the phone says echo cancelling is off, because Kairo could then hear itself and cut itself off. "
                                 "Off: no warning."),
    "g7.listen_on_barge_in": (False, "The call screen switches to 'Listening' when the PC says you talked over Kairo (or that its answer was cut off), "
                                      "not when the phone hears any sound. A cough or an 'mhm' no longer flips the screen while Kairo goes on talking. "
                                      "The PC sends that message now, whenever it cuts Kairo off (the turn-taking build, g6), with g6.transcript_barge_in "
                                      "on or off. Off for now, until the iPhone echo check on the PC (P12) is done and you have tried the turn-taking "
                                      "switches. Off: the old way, any loud sound on the phone."),
    "g7.sound_check_rtc": (True, "The sound check's call test starts the real call connection and has the PC say a line over it, instead of playing a "
                                  "sound inside the app, so it proves what a call really sounds like. Off: the old test."),

    # -- G8 Windows
    "g8.embed_batch4": (True, "The memory search reads your notes 4 pieces at a time instead of 16. While it works it keeps about half "
                              "the main memory (1.4 GB instead of 2.9 GB on the processor, measured on a test machine) and gives the "
                              "same answers and about the same indexing speed. Takes effect the next time the memory model starts."),
    "g8.stagger_boot": (True, "When Kairo starts, the voice model, then Whisper and Kokoro, then the memory model load one after the other "
                              "instead of all at once, and the free graphics memory is read in between and logged. Before, the memory "
                              "model checked the card before the voice model had taken its share and could pick the GPU when it did not "
                              "fit. Takes effect the next time Kairo starts."),
    "g8.sysmem_readback": (False, "Kairo's voice report also shows NVIDIA's driver setting 'CUDA sysmem fallback' as Kairo's own account sees it "
                                  "(1 = a full graphics card is an error, anything else = the driver may quietly spill into slow shared "
                                  "memory). Off until the read has been proven on the PC: `kairo diag` reads it itself either way."),

    # -- G9 speech text
    "g9.speaknorm": (False, "Kairo says numbers, times, dates and amounts as words, so its voices read them right: "
                            "14:30 becomes 'vierzehn Uhr dreißig', 12,50 Franken becomes 'zwölf Franken fünfzig', 14:10 "
                            "becomes 'ten past two'. What you read on the screen stays as written, and a number is never "
                            "split from its unit when Kairo starts to talk. Listen to it on the PC before you switch it on. "
                            "Takes effect at the next call or voice reply."),
    "g9.speaknorm_en_digit_times": (False, "Only counts when the switch above is on: English times are said 'two ten' "
                                           "instead of 'ten past two'. Try both and keep the one that sounds better. Takes "
                                           "effect at the next call."),

    # -- G10 fast paths
    "g10.fast_paths": (True, "Kairo answers 'What time is it?', 'What's the date?' and 'What day is it?' (and the German ones) "
                             "itself, by a rule on the PC, the moment you finish: the voice model is not asked at all. Only when "
                             "the whole sentence is that question: 'what time is my train' or 'what time is it in Tokyo' still go "
                             "to the voice model. Off: those questions go to the voice model as before. Takes effect at the next call."),
    "g10.fast_agenda": (True, "Kairo answers 'When is my next appointment?', 'What is my next lesson?' and 'When is the next train?' (and "
                              "the German ones) itself, from the same calendar, school timetable and connection it already shows on "
                              "the Today card, without asking the voice model or Claude. Only while that was read in the last few "
                              "minutes and could be read (a calendar that could not be read is never an empty calendar), and only "
                              "for the plain question: 'when is the next train to Bern' or 'my next appointment with the dentist' "
                              "still go to the voice model. A train question is not answered with a bus or the other way round. "
                              "Off: those questions go to the voice model as before. Takes effect at the next call."),
    "g10.timers": (True, "On a call you can say 'Set a timer for ten minutes', 'Stell einen Timer auf zehn Minuten', 'Remind me in an "
                         "hour to call Lea', 'How much time is left on my timer?' or 'Cancel the timer'. Kairo keeps the timer itself "
                         "and sends a push to your phone when it is up, also at night (quiet hours do not hold it back), with a "
                         "ten-minute time-to-live and high urgency so the phone is woken for it. No phone with notifications on: "
                         "Kairo says so and sets nothing. If a push does not get through, it is tried again for up to half "
                         "an hour and the failure shows in Run diagnosis. iOS plays no alarm sound for a web push, a Focus or "
                         "Low Power mode can silence or delay it, and how fast and how reliably it reaches your iPhone is not "
                         "measured yet (test P22): do not trust it with the oven until you have tried it. A timer touches only "
                         "you, so it needs no approval. Off: those sentences go to the voice model as before (it has no timer). "
                         "A timer that is already set still rings. Takes effect at the next call."),

    # -- G11 router
    "g11.router": (False, "Kairo writes down, for every call to the model, which route it took and why (More -> Usage, "
                          "\"Routes\"), after checking the privacy class of the content. Today the route is always your "
                          "Claude subscription, so nothing else changes. Off: no log and no check, as before."),
    "g11.local_route": (False, "The check whether a mail needs a reply (only that job) may run on the voice model on this PC "
                               "instead of Claude, but only after the router lab has passed it (not yet: the lab needs the "
                               "PC), only when the voice model's server has a second slot for jobs, and never while a call "
                               "is open; an answer that is not exactly right goes to Claude. Needs g11.router too. Off: "
                               "everything runs on Claude."),

}


def on(s: Session, name: str) -> bool:
    """The flag's value: the owner's choice, else its default. An unknown name is off (default-deny)."""
    if name not in FLAGS:
        return False
    row = s.get(SettingKV, PREFIX + name)
    if row is None or row.value not in ("0", "1"):
        return FLAGS[name][0]
    return row.value == "1"


def listing(s: Session) -> list[dict]:
    return [{"name": n, "on": on(s, n), "default": d, "what": w} for n, (d, w) in sorted(FLAGS.items())]


def set_flag(s: Session, name: str, value: bool | None) -> list[dict]:
    """value None goes back to the default."""
    if name not in FLAGS:
        raise KeyError(name)
    row = s.get(SettingKV, PREFIX + name)
    if value is None:
        if row is not None:
            s.delete(row)
    else:
        row = row or SettingKV(key=PREFIX + name)
        row.value = "1" if value else "0"
        s.add(row)
    s.commit()
    return listing(s)
