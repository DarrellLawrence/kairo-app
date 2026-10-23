"""The voice latency harness (app/bench_voice.py): the results schema, the stage arithmetic, the percentiles
and rates, the results folder, and the fake phone (app/bench_phone.py). What needs the call packages, Pipecat and
aiortc, skips without them like test_voice_rtc.py."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from app import bench_voice as bv
from app.bench_corpus import Clip
from app.latency import pct

MACHINE = {"gpu": None, "vram_gb": None, "ram_gb": 16.0, "cpu": "Test CPU"}
CONFIG = {"vad_stop_s": 0.2, "turn_analyzer": "smart-turn-v3", "stt_model": "mock", "stt_compute_type": "none",
          "llm_model": None, "tts_en": "mock", "tts_de": "mock"}


def clip(kind: str, i: int = 1, lang: str = "en") -> Clip:
    return Clip(f"{kind[:2]}_{lang}_{i:02d}", lang, "Some words.", kind)


def turn(kind="question", total=700, i=1, **kw):
    stages = kw.pop("stages", {"vad_stop": 200, "turn_decision": 10, "stt_final": 150, "llm_first_token": 140,
                               "tts_first_audio": 20, "network": total - 520 if total else None})
    return bv.turn_record(clip(kind, i), stages=stages if total is not None else None, total_ms=total, **kw)


def doc(turns_, **kw):
    return bv.build_result(turns_, mode=kw.pop("mode", "mock"), version="2026.10.23.0600-abc1234", git_sha="abc1234",
                           machine=MACHINE, config=CONFIG, **kw)


# ---------------------------------------------------------------- the stages of one turn

SERVER = {"vad_ms": 200, "stt_ms": 150, "turn_at_ms": 360, "first_token_at_ms": 500, "first_audio_at_ms": 520, "tts_ms": 20,
          "llm_ms": 140, "cached_opener": False}


def test_the_stages_add_up_to_the_clients_total_with_turns_py_doing_the_arithmetic():
    st = bv.stages_from_server(SERVER, 600)
    assert st == {"vad_stop": 200, "turn_decision": 10, "stt_final": 150, "llm_first_token": 140, "tts_first_audio": 20,
                  "network": 80}
    assert sum(st.values()) == 600
    assert list(st) == list(bv.STAGES)


def test_a_turn_without_the_servers_numbers_has_a_total_and_no_stages():
    assert set(bv.stages_from_server(None, 600).values()) == {None}
    assert set(bv.stages_from_server({"vad_ms": 200}, 600).values()) == {None}
    assert set(bv.stages_from_server(SERVER, None).values()) == {None}


def test_the_network_stage_never_goes_below_zero_and_a_slow_pause_shows_as_turn_decision():
    st = bv.stages_from_server(SERVER, 500)               # the client saw the audio earlier than the server's clock says
    assert st["network"] == 0
    slow = dict(SERVER, turn_at_ms=700, first_token_at_ms=840, first_audio_at_ms=860)          # Smart Turn said "not done": 0.5 s pause
    assert bv.stages_from_server(slow, 950)["turn_decision"] == 350


# ---------------------------------------------------------------- percentiles and rates

def test_percentiles_are_voice_speeds_own_over_the_turns_that_got_a_reply():
    totals = [400, 450, 500, 520, 600, 650, 700, 800, 900, 1200]
    s = bv.summarize([turn(total=t, i=i) for i, t in enumerate(totals, 1)] + [turn("backchannel", None, 11, interrupted=False)])
    assert s["n"] == 10
    assert (s["p50_ms"], s["p90_ms"], s["p95_ms"]) == (pct(totals, .5), pct(totals, .9), pct(totals, .95)) == (650, 1200, 1200)
    assert s["by_stage"]["stt_final"] == {"p50": 150, "p95": 150}
    assert s["by_stage"]["network"] == {"p50": pct([t - 520 for t in totals], .5), "p95": pct([t - 520 for t in totals], .95)}
    assert s["target_p95_ms"] == 1000 and s["pass"] is False                     # p95 over the target


def test_the_rates_count_the_right_turns():
    t = [turn(total=600, i=1), turn(total=None, i=2, missed=True), turn("command", 700, 3), turn("statement", 650, 4),
         turn("backchannel", None, 1, interrupted=True), turn("backchannel", None, 2, interrupted=False),
         turn("noise", None, 3, interrupted=False), turn("noise", None, 4, interrupted=None),         # not tested: bot was quiet
         turn("other_speaker", None, 1, speaker_accept=True), turn("other_speaker", None, 2, speaker_accept=False),
         turn("other_speaker", None, 3, speaker_accept=None)]
    s = bv.summarize(t)
    assert s["missed_turn_rate"] == 0.25                                          # 1 of the 4 real utterances
    assert s["false_interrupt_rate"] == pytest.approx(1 / 3, abs=1e-4)           # 1 of the 3 tests that could be made
    assert s["speaker_false_accept_rate"] == 0.5                                  # 1 of the 2 labelled strangers


def test_rates_that_cannot_be_known_are_null_and_never_fail_the_run():
    s = bv.summarize([turn(total=600), turn(total=700, i=2), turn("other_speaker", None, 1)])
    assert s["false_interrupt_rate"] is None and s["speaker_false_accept_rate"] is None and s["missed_turn_rate"] == 0.0
    assert s["pass"] is True


def test_pass_needs_p95_in_time_no_missed_turn_and_at_most_5_percent_false_interrupts():
    ok = [turn(total=900, i=i) for i in range(1, 6)]
    assert bv.summarize(ok)["pass"] is True
    assert bv.summarize(ok + [turn(total=1001, i=9)])["pass"] is False                         # p95 1001 ms
    assert bv.summarize(ok + [turn(total=None, i=9, missed=True)])["pass"] is False             # one missed turn
    twenty = [turn("backchannel", None, i, interrupted=(i == 1)) for i in range(1, 21)]
    assert bv.summarize(ok + twenty)["false_interrupt_rate"] == 0.05 and bv.summarize(ok + twenty)["pass"] is True
    twentyone = twenty + [turn("noise", None, 1, interrupted=True)]
    assert bv.summarize(ok + twentyone)["pass"] is False                                        # 2 of 21
    nothing = bv.summarize([turn(total=None, i=1, missed=True)])
    assert nothing["n"] == 0 and nothing["p95_ms"] is None and nothing["pass"] is False


def test_a_synthetic_voice_the_speaker_check_refuses_is_no_missed_turn_but_the_owners_recording_refused_is():
    refused_tts = turn(total=None, i=2, missed=False, speaker_accept=False)
    refused_own = turn(total=None, i=3, missed=True, speaker_accept=False)
    s = bv.summarize([turn(total=600), refused_tts])
    assert s["missed_turn_rate"] == 0.0 and s["pass"] is True
    assert bv.summarize([turn(total=600), refused_own])["missed_turn_rate"] == 0.5


# ---------------------------------------------------------------- the schema

def test_a_result_has_exactly_the_agreed_fields():
    d = doc([turn(total=600), turn("backchannel", None, 1, interrupted=False)])
    assert bv.validate(d) == []
    assert set(d) == {"schema", "version", "git_sha", "at", "mode", "machine", "config", "turns", "summary"} and d["schema"] == 1
    assert set(d["machine"]) == {"gpu", "vram_gb", "ram_gb", "cpu"}
    assert set(d["config"]) == {"vad_stop_s", "turn_analyzer", "stt_model", "stt_compute_type", "llm_model", "tts_en", "tts_de"}
    assert set(d["turns"][0]) == {"clip", "lang", "kind", "stages_ms", "total_ms", "interrupted", "missed", "speaker_accept"}
    assert set(d["turns"][0]["stages_ms"]) == {"vad_stop", "turn_decision", "stt_final", "llm_first_token", "tts_first_audio", "network"}
    assert set(d["summary"]) == {"n", "p50_ms", "p90_ms", "p95_ms", "by_stage", "false_interrupt_rate", "missed_turn_rate",
                                 "speaker_false_accept_rate", "target_p95_ms", "pass"}
    assert json.loads(json.dumps(d)) == d                                             # plain JSON
    assert d["at"].endswith("+00:00")


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d.pop("git_sha"), "differ from the schema"),
    (lambda d: d.update(extra=1), "differ from the schema"),
    (lambda d: d.update(mode="fast"), "mode"),
    (lambda d: d["turns"][0]["stages_ms"].update(network="slow"), "stages_ms.network"),
    (lambda d: d["turns"][0].update(missed="no"), "missed"),
    (lambda d: d["turns"][0].update(lang="fr"), "clip/lang/kind"),
    (lambda d: d["summary"].update(pass_=True), "summary"),
    (lambda d: d["summary"].update(n=2.5), "summary.n"),
    (lambda d: d.update(at="yesterday"), "at"),
    (lambda d: d["config"].update(llm_model=3), "llm_model"),
])
def test_validate_names_what_breaks_the_schema(mutate, expect):
    d = doc([turn(total=600)])
    mutate(d)
    problems = bv.validate(d)
    assert problems and any(expect in p for p in problems), problems
    assert bv.validate("nope") and bv.validate(None)


# ---------------------------------------------------------------- the results folder

def test_results_are_saved_by_time_and_the_latest_valid_one_is_found(tmp_path):
    out = tmp_path / "bench" / "results"
    assert bv.latest(out) is None and bv.latest(tmp_path / "nowhere") is None
    t1 = doc([turn(total=600)], at=bv.dt.datetime(2026, 10, 23, 6, 0, 0, tzinfo=bv.dt.timezone.utc))
    t2 = doc([turn(total=700)], mode="live", at=bv.dt.datetime(2026, 10, 23, 7, 0, 0, tzinfo=bv.dt.timezone.utc))
    p1, p2 = bv.save(t1, out), bv.save(t2, out)
    assert p1.name == "voice-20261023T060000Z-mock.json" and p2.name == "voice-20261023T070000Z-live.json"
    assert bv.save(t1, out).name == "voice-20261023T060000Z-mock-2.json"             # the same second: never overwritten
    assert bv.latest(out) == t2 and bv.latest(out, mode="mock") == t1 and bv.latest(out, mode="live") == t2
    (out / "voice-20261023T080000Z-live.json").write_text("{not json")             # the newest is broken: skipped
    (out / "voice-20261023T090000Z-live.json").write_text(json.dumps({"schema": 1}))   # and one against the schema
    assert bv.latest(out) == t2
    bad = doc([turn(total=600)])
    bad.pop("turns")
    with pytest.raises(bv.BenchError, match="schema"):
        bv.save(bad, out)


def test_the_summary_table_names_every_stage_and_the_verdict():
    text = bv.format_table(doc([turn(total=600), turn(total=800, i=2)]))
    for word in (*bv.STAGES, "TOTAL (n=2)", "PASS", "p95 target 1000 ms", "mock run"):
        assert word in text
    assert "FAIL" in bv.format_table(doc([turn(total=1500)]))


# ================================================================ the fake phone (needs the call packages)

def _needs_pipecat():
    pytest.importorskip("pipecat")
    pytest.importorskip("aiortc")
    pytest.importorskip("numpy")


def _frames(phone, loud_at, *, until, step=0.02):
    """Reply frames for a phone that wasn't on a call: loud at the times in loud_at, silent between."""
    t, loud = 0.0, sorted(loud_at)
    while t <= until:
        phone.frames.append((t, 0.1 if any(abs(t - x) < step / 2 for x in loud) else 0.0))
        t += step


def _phone_with(loud_from_to, events, until=10.0):
    """A phone that has heard Kairo's voice at the given (from, to) seconds and the data channel's events (time, type);
    its clock reads `until`."""
    from app.bench_phone import Phone
    p = Phone()
    ticks = []
    for a, b in loud_from_to:
        ticks += [a + i * 0.02 for i in range(int((b - a) / 0.02) + 1)]
    _frames(p, ticks, until=until)
    p.events = [(t, {"type": k}) for t, k in events]
    return p


def test_a_probe_cut_the_reply_only_when_kairo_was_talking_its_vad_heard_it_and_the_voice_then_stopped():
    _needs_pipecat()

    def cut(loud, events, until=10.0):
        return _phone_with(loud, events, until).was_cut(2.0, 2.4, now=until)       # a probe plays from 2.0 to 2.4 s
    speech = [(2.3, "speech")]                                                     # the PC's VAD hears it at 2.3 s
    assert cut([(0.0, 2.45)], speech) is True                                      # and Pipecat stops the voice a moment later
    assert cut([(0.0, 2.45), (6.0, 7.0)], speech) is True                          # it spoke again later: answering the probe
    assert cut([(0.0, 2.9)], speech, until=4.0) is True                            # the voice took a moment to stop
    assert cut([(0.0, 2.45)], []) is False                                         # the VAD never heard it (noise): the answer ended by itself
    assert cut([(0.0, 8.0)], speech) is False                                      # heard, but Kairo talked on
    assert cut([(0.0, 1.0)], speech) is False                                      # it was quiet already: nothing to cut
    assert cut([(0.0, 2.9), (3.2, 8.0)], speech) is False                          # a 0.3 s pause between sentences
    assert cut([(0.0, 3.6)], speech, until=5.0) is False                           # it stopped long after: not because of the probe


def test_the_line_is_quiet_when_kairo_stopped_talking_and_said_it_is_done():
    _needs_pipecat()
    from app.bench_phone import Phone

    async def go():
        p = Phone(still_s=0.15)
        assert await p.quiet(1.0) is True                                  # nothing heard yet
        p.last_loud = time.monotonic()
        assert await p.quiet(0.2) is False                                 # just spoke
        p.events.append((time.monotonic() + 0.01, {"type": "spoken"}))
        assert await p.quiet(1.0) is True                                  # done, and silent for still_s
        p.events.clear()
        p.last_loud = time.monotonic() - 0.1
        t = time.monotonic()
        assert await p.quiet(2.0) is True and time.monotonic() - t > 0.4   # no "spoken" event: four times as long
    asyncio.run(go())


def test_the_fake_phone_asks_no_stun_server_and_the_loopback_rule_is_put_back():
    src = (Path(bv.__file__).parent / "bench_phone.py").read_text(encoding="utf-8")
    assert "iceServers=[]" in src and "stun:" not in src                # the default would ask a public STUN server
    _needs_pipecat()
    import aioice.ice

    from app import voicertc
    from app.bench_phone import ice_rules
    before, flag = aioice.ice.get_host_addresses, voicertc._allow_loopback
    with ice_rules(loopback=True):
        assert "127.0.0.1" in aioice.ice.get_host_addresses(True, False) and voicertc._allow_loopback is True
    assert aioice.ice.get_host_addresses is before and voicertc._allow_loopback is flag
