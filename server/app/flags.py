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

    # -- G6 turn-taking
    "g6.empty_turn_fix": (True, "A sound over Kairo that Whisper can't turn into words (a cough, a chair) no longer makes it answer "
                                "your last question a second time. Measured in the research: it did, every time. Takes effect "
                                "at the next call."),

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
                                      "Off for now, because the PC does not send that message yet (the turn-taking build adds it): switched on without it, "
                                      "the screen stays on 'Speaking' while you talk over an answer that is already written. "
                                      "Off: the old way, any loud sound on the phone."),
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
