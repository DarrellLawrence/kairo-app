"""kairo diag and the latency harness (app/bench_voice.py, built by another change): run(mock, url, out_dir) and
latest(results_dir). The harness is a stand-in here; the results are tests/fixtures/bench_results.json, which has the
agreed schema exactly. Without the module, diag says "harness not installed" and carries on."""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import threading
import types
from pathlib import Path

import pytest

from app import diag
from tests.test_diag import pc, run  # noqa: F401  (pc is a fixture)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "bench_results.json").read_text(encoding="utf-8"))
STAGES = ["vad_stop", "turn_decision", "stt_final", "llm_first_token", "tts_first_audio", "network"]


@pytest.fixture(autouse=True)
def only_bench(monkeypatch):
    monkeypatch.setattr(diag, "CHECKS", [diag._bench])


@pytest.fixture
def harness(monkeypatch, pc):  # noqa: F811
    h = types.ModuleType("app.bench_voice")
    h.state = types.SimpleNamespace(latest=copy.deepcopy(FIXTURE), mock={**copy.deepcopy(FIXTURE), "mode": "mock"}, calls=[], asked=[])

    def run_(mock, url, out_dir):
        h.state.calls.append((mock, url, Path(out_dir)))
        assert Path(out_dir).is_dir()
        if isinstance(h.state.mock, Exception):
            raise h.state.mock
        return h.state.mock

    def latest(results_dir):
        h.state.asked.append(Path(results_dir))
        return h.state.latest
    h.run, h.latest = run_, latest
    monkeypatch.setitem(sys.modules, "app.bench_voice", h)
    return h.state


def checks(pc, **kw):  # noqa: F811
    report, by_id = run(pc, **kw)
    return report, by_id


def test_the_fixture_has_exactly_the_agreed_schema():
    assert set(FIXTURE) == {"schema", "version", "git_sha", "at", "mode", "machine", "config", "turns", "summary"} and FIXTURE["schema"] == 1
    assert set(FIXTURE["machine"]) == {"gpu", "vram_gb", "ram_gb", "cpu"}
    assert set(FIXTURE["config"]) == {"vad_stop_s", "turn_analyzer", "stt_model", "stt_compute_type", "llm_model", "tts_en", "tts_de"}
    for t in FIXTURE["turns"]:
        assert set(t) == {"clip", "lang", "kind", "stages_ms", "total_ms", "interrupted", "missed", "speaker_accept"}
        assert list(t["stages_ms"]) == STAGES and t["total_ms"] == sum(t["stages_ms"].values())
    s = FIXTURE["summary"]
    assert set(s) == {"n", "p50_ms", "p90_ms", "p95_ms", "by_stage", "false_interrupt_rate", "missed_turn_rate",
                      "speaker_false_accept_rate", "target_p95_ms", "pass"} and s["target_p95_ms"] == 1000
    assert list(s["by_stage"]) == STAGES and all(set(v) == {"p50", "p95"} for v in s["by_stage"].values())


def test_without_the_harness_it_says_so_and_nothing_else_breaks(pc, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.bench_voice", None)               # as if the file were not there
    report, by_id = checks(pc)
    assert [by_id[i]["status"] for i in ("bench.mock", "bench.latest")] == ["WARN", "WARN"]
    assert by_id["bench.mock"]["detail"] == "harness not installed"
    assert report["exit_code"] == 0 and report["bench"] == {"installed": False, "mock": None, "latest": None}


def test_a_harness_that_cannot_be_imported_is_a_fail_not_a_missing_one(pc, monkeypatch):
    def broken(name):
        raise ModuleNotFoundError("No module named 'torch'", name="torch")
    monkeypatch.setattr(diag.importlib, "import_module", broken)
    c = checks(pc)[1]["bench.mock"]
    assert c["status"] == "FAIL" and "No module named 'torch'" in c["detail"]


def test_the_mock_run_is_a_self_check_that_writes_nothing_into_the_data_folder(pc, harness):
    report, by_id = checks(pc)
    (mock, url, out_dir), = harness.calls
    assert mock is True and url is None and not out_dir.exists()            # a temporary folder, gone again
    assert pc not in out_dir.parents and not (pc / "bench").exists()
    c = by_id["bench.mock"]
    assert c["status"] == "PASS" and c["value"] == 3 and "not a measurement" in c["detail"]
    assert report["bench"]["mock"] == {"ok": True, "turns": 3}


@pytest.mark.parametrize("mock, words", [
    (ValueError("clip folder missing"), "ValueError: clip folder missing"),
    ({"schema": 2, "turns": [1], "summary": {}}, "not in the agreed shape"),
    ({"schema": 1, "turns": [], "summary": {"n": 0}}, "not in the agreed shape"),
    ("nope", "not in the agreed shape"),
])
def test_a_mock_run_that_fails_or_answers_in_the_wrong_shape_is_a_fail(pc, harness, mock, words):
    harness.mock = mock
    c = checks(pc)[1]["bench.mock"]
    assert c["status"] == "FAIL" and words in c["detail"]


def test_a_mock_run_that_hangs_is_given_up_on(pc, harness, monkeypatch):
    stop = threading.Event()
    sys.modules["app.bench_voice"].run = lambda mock, url, out_dir: stop.wait(5)
    monkeypatch.setattr(diag, "MOCK_TIMEOUT_S", 0.2)
    c = checks(pc)[1]["bench.mock"]
    stop.set()
    assert c["status"] == "FAIL" and "no answer after 0.2 s" in c["detail"]


def test_the_latest_live_run_is_reported_with_its_numbers_and_handed_on_without_the_turns(pc, harness):
    harness.latest["at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    report, by_id = checks(pc)
    c = by_id["bench.latest"]
    assert c["status"] == "PASS" and c["value"] == 876 and c["unit"] == "ms"
    assert "p50 875 ms, p90 876 ms, p95 876 ms over 3 turns" in c["detail"] and "within the 1000 ms target" in c["detail"]
    assert harness.asked == [pc / "bench" / "results"]
    latest = report["bench"]["latest"]
    assert "turns" not in latest and latest["summary"] == FIXTURE["summary"] and latest["machine"] == FIXTURE["machine"]
    assert latest["git_sha"] == FIXTURE["git_sha"]                          # a sha is long and opaque, not a secret
    assert json.dumps(report)


def test_a_live_run_over_the_target_is_a_fail(pc, harness):
    harness.latest["summary"].update(p95_ms=1240, p90_ms=1100)
    harness.latest["summary"]["pass"] = False
    c = checks(pc)[1]["bench.latest"]
    assert c["status"] == "FAIL" and c["value"] == 1240 and "p95 is over the 1000 ms target" in c["detail"]


def test_nothing_measured_yet_or_only_a_mock_is_a_warning(pc, harness):
    harness.latest = None
    c = checks(pc)[1]["bench.latest"]
    assert c["status"] == "WARN" and "no run yet" in c["detail"]
    harness.latest = {**FIXTURE, "mode": "mock"}
    c = checks(pc)[1]["bench.latest"]
    assert c["status"] == "WARN" and "mock run" in c["detail"]


def test_an_old_run_or_one_from_another_version_is_a_warning(pc, harness, tmp_path):
    now = dt.datetime.now(dt.timezone.utc)
    harness.latest["at"] = (now - dt.timedelta(days=30)).isoformat()
    c = checks(pc)[1]["bench.latest"]
    assert c["status"] == "WARN" and "30 days old" in c["detail"]
    harness.latest["at"] = now.isoformat()
    app_root = tmp_path / "release"
    app_root.mkdir()
    (app_root / "release.json").write_text(json.dumps({"version": "2026.10.23.0900-fffffff"}), encoding="utf-8")
    c = checks(pc, app_root=app_root)[1]["bench.latest"]
    assert c["status"] == "WARN" and "made on 2026.10.22.1830-abc1234, this is 2026.10.23.0900-fffffff" in c["detail"]


def test_a_results_file_without_a_summary_or_a_reader_that_breaks_is_a_fail(pc, harness):
    harness.latest = {**FIXTURE, "summary": None}
    assert checks(pc)[1]["bench.latest"]["status"] == "FAIL"
    sys.modules["app.bench_voice"].latest = lambda results_dir: (_ for _ in ()).throw(OSError("disk"))
    c = checks(pc)[1]["bench.latest"]
    assert c["status"] == "FAIL" and "OSError: disk" in c["detail"]


def test_what_the_harness_writes_is_redacted_like_everything_else(pc, harness):
    harness.latest["config"]["llm_model"] = "lfm token=hunter2xyz mail alex.rivera@example.com"
    harness.latest["at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    out = json.dumps(checks(pc)[0])
    assert "hunter2xyz" not in out and "alex.rivera@example.com" not in out and "a***@example.com" in out
