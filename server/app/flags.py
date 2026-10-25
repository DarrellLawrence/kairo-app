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

    # -- G3 mouth

    # -- G4 jitter buffer

    # -- G5 hand-off

    # -- G6 turn-taking

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
