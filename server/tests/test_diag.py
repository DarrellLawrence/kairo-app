"""kairo diag (owner's spec: "`kairo diag` exits 0" when the voice stack is healthy). The checks are judged
here against a made-up Kairo folder and made-up answers from the running server; nothing needs a GPU or a model."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app import diag, tool_sites, voicellm
from app import voice as voice_mod
from app.brain import voice_local
from app.main import create_app
from tests.conftest import SERVER_DIR
from tests.test_voice_devices import DeviceVoice
from tests.test_voice_local import local  # noqa: F401  (the app with the fake voice model)


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """C:\\Kairo in miniature: data\\ next to tools\\voice\\models with every voice file, all packages importable."""
    root = tmp_path / "Kairo"
    models = root / "tools" / "voice" / "models"
    snap = models / "whisper" / f"models--Systran--faster-whisper-{voice_mod.WHISPER_GPU}" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"w" * 2048)
    (models / "kokoro").mkdir()
    for name in voice_mod.KOKORO_FILES:
        (models / "kokoro" / name).write_bytes(b"k" * 1024)
    (models / "piper").mkdir()
    for name in (f"{voice_mod.PIPER_DE}.onnx", f"{voice_mod.PIPER_DE}.onnx.json"):
        (models / "piper" / name).write_bytes(b"p" * 512)
    (models / ".complete").write_text("ok\n", encoding="ascii")
    (root / "data").mkdir()
    monkeypatch.delenv("KAIRO_VOICE_SITE", raising=False)
    monkeypatch.setattr(tool_sites, "is_real_package", lambda name: (True, ""))
    return root / "data"


@pytest.fixture
def speech_only(monkeypatch):
    """Only the speech parts judged: the tests below are about how one part is read, not about the whole PC."""
    monkeypatch.setattr(diag, "CHECKS", [diag._kairo, diag._whisper, diag._kokoro, diag._piper])


def comp(device="cuda", **kw):
    return {"device": device, "model": "m", "loaded": True, "load_ms": 2300, "warm_ms": 140, "ok": device == "cuda",
            "reason": None if device == "cuda" else "on the CPU: Library cublas64_12.dll is not found", **kw}


def live(**parts):
    comps = {"whisper": comp(), "kokoro": comp(), "piper": {"device": "cpu", "loaded": True, "ok": True, "reason": None}}
    comps.update(parts)
    return {"devices": {"components": comps, "gpu": None, "line": {"webrtc_ready": True, "reason": None}}}


def run(data, strict=False, **kw):
    report = diag.finish(diag.collect(data_dir=data, **kw), strict)
    return report, {c["id"]: c for c in report["checks"]}


def test_a_healthy_voice_passes_and_exits_zero(pc, speech_only):
    report, by_id = run(pc, live=live(), source="server")
    assert [c["status"] for c in report["checks"]] == ["PASS"] * len(report["checks"])
    assert report["ok"] is True and report["exit_code"] == 0 and report["counts"] == {"pass": len(report["checks"]), "warn": 0, "fail": 0}
    assert by_id["whisper.loaded"]["value"] == 140 and by_id["whisper.loaded"]["unit"] == "ms"
    assert "2300 ms" in by_id["whisper.loaded"]["detail"] and by_id["whisper.installed"]["unit"] == "MB"
    assert by_id["piper.device"]["detail"].startswith("on the CPU by design")


def test_a_part_on_the_cpu_that_should_be_on_the_gpu_is_a_fail_with_its_reason(pc, speech_only):
    report, by_id = run(pc, live=live(whisper=comp("cpu")))
    c = by_id["whisper.device"]
    assert c["status"] == "FAIL" and "should be on the GPU" in c["detail"] and "cublas64_12.dll" in c["detail"]
    assert by_id["kokoro.device"]["status"] == "PASS"
    assert report["exit_code"] == 1 and report["ok"] is False and report["counts"]["fail"] == 1


def test_loaded_but_never_warmed_up_is_a_warning(pc, speech_only):
    _, by_id = run(pc, live=live(kokoro=comp(warm_ms=None)))
    assert by_id["kokoro.loaded"]["status"] == "WARN" and "not warmed up" in by_id["kokoro.loaded"]["detail"]


def test_not_loaded_is_a_fail_except_for_the_german_voice_which_loads_on_first_use(pc, speech_only):
    _, by_id = run(pc, live=live(whisper={"device": None, "loaded": False, "ok": False, "reason": "not loaded yet (it loads when Kairo starts)"},
                                 piper={"device": "cpu", "loaded": False, "ok": True}))
    assert by_id["whisper.loaded"]["status"] == "FAIL" and "not loaded yet" in by_id["whisper.loaded"]["detail"]
    assert by_id["whisper.device"]["status"] == "FAIL"
    assert by_id["piper.loaded"]["status"] == "WARN" and by_id["piper.device"]["status"] == "WARN"


def test_a_package_that_cannot_be_used_is_a_fail(pc, speech_only, monkeypatch):
    monkeypatch.setattr(tool_sites, "is_real_package",
                        lambda name: (False, "faster_whisper: folder found but not readable by this account") if name == "faster_whisper" else (True, ""))
    _, by_id = run(pc, live=live())
    assert by_id["whisper.installed"]["status"] == "FAIL" and "not readable by this account" in by_id["whisper.installed"]["detail"]
    assert by_id["kokoro.installed"]["status"] == "PASS"


def test_a_missing_model_file_is_a_fail(pc, speech_only):
    next((pc.parent / "tools" / "voice" / "models" / "whisper").rglob("model.bin")).unlink()
    (pc.parent / "tools" / "voice" / "models" / "kokoro" / "voices-v1.0.bin").unlink()
    _, by_id = run(pc, live=live())
    assert by_id["whisper.installed"]["status"] == "FAIL" and "isn't downloaded" in by_id["whisper.installed"]["detail"]
    assert by_id["kokoro.installed"]["status"] == "FAIL" and "voices-v1.0.bin" in by_id["kokoro.installed"]["detail"]


def test_without_the_running_server_nothing_can_be_called_loaded(pc, speech_only):
    report, by_id = run(pc, live=None)
    assert by_id["kairo.running"]["status"] == "FAIL"
    for cid in ("whisper.loaded", "whisper.device", "kokoro.loaded", "piper.device"):
        assert by_id[cid]["status"] == "FAIL" and "server didn't answer" in by_id[cid]["detail"]
    assert by_id["whisper.installed"]["status"] == "PASS"            # what is on the disk is still judged
    assert report["exit_code"] == 1


def test_strict_turns_a_warning_into_exit_one(pc, speech_only):
    ok_live = live(piper={"device": "cpu", "loaded": False, "ok": True})
    assert run(pc, live=ok_live)[0]["exit_code"] == 0
    assert run(pc, live=ok_live, strict=True)[0]["exit_code"] == 1


def test_a_check_that_crashes_is_a_fail_of_its_own_and_the_rest_still_run(pc, monkeypatch):
    def broken(ctx):
        raise RuntimeError("boom")
    monkeypatch.setattr(diag, "CHECKS", [diag._whisper, broken])
    report, by_id = run(pc, live=live())
    assert by_id["broken.error"]["status"] == "FAIL" and "RuntimeError: boom" in by_id["broken.error"]["detail"]
    assert by_id["whisper.device"]["status"] == "PASS" and report["exit_code"] == 1


def test_the_json_has_a_fixed_shape(pc, speech_only):
    report, _ = run(pc, live=live(), source="server")
    assert set(report) == {"schema", "at", "source", "ok", "exit_code", "counts", "host", "checks"}
    assert report["schema"] == 1 and set(report["host"]) == {"version", "python", "platform"}
    for c in report["checks"]:
        assert set(c) == {"id", "group", "name", "status", "detail", "value", "unit"} and c["status"] in ("PASS", "WARN", "FAIL")
    json.dumps(report)


def test_the_text_is_one_ascii_line_per_check_with_a_verdict(pc, speech_only):
    report, _ = run(pc, live=live(whisper=comp("cpu")))
    text = diag.render(report)
    text.encode("ascii")
    lines = text.splitlines()
    assert sum(1 for ln in lines if ln.startswith(("  PASS", "  WARN", "  FAIL"))) == len(report["checks"])
    assert any(ln.startswith("  FAIL  whisper.device") and "cublas64_12.dll" in ln for ln in lines)
    assert lines[-1].endswith("NOT HEALTHY (exit 1)") and "Speech to text (Whisper)" in lines


def test_nothing_personal_or_secret_leaves_a_report(pc, monkeypatch):
    leaky = diag.ok("x.leak", "leak", "mail alex.rivera@example.com, Authorization: Bearer abcdef0123456789xyz, token=hunter2xyz, "
                    "kairo-home.tail1234.ts.net, +41 79 123 45 67")
    monkeypatch.setattr(diag, "CHECKS", [lambda ctx: [leaky]])
    report, _ = run(pc, live=live())
    out = json.dumps(report) + diag.render(report)
    for secret in ("alex.rivera@example.com", "abcdef0123456789xyz", "hunter2xyz", "tail1234", "79 123 45 67"):
        assert secret not in out
    assert "a***@example.com" in out


def test_main_prints_the_report_and_exits_with_the_verdict(pc, monkeypatch, capsys):
    monkeypatch.setenv("KAIRO_DATA_DIR", str(pc))
    assert diag.main(["--local", "--json"]) == 1                       # no server answers here
    report = json.loads(capsys.readouterr().out)
    assert report["source"] == "local" and report["exit_code"] == 1 and report["counts"]["fail"] >= 1
    assert diag.main(["--local"]) == 1
    assert "NOT HEALTHY" in capsys.readouterr().out


def test_main_exits_2_when_the_tool_itself_breaks(pc, monkeypatch, capsys):
    monkeypatch.setenv("KAIRO_DATA_DIR", str(pc))
    monkeypatch.setattr(diag, "collect", lambda **kw: 1 / 0)
    assert diag.main(["--local"]) == 2
    assert "could not run: ZeroDivisionError" in capsys.readouterr().err


def test_python_dash_m_app_diag_runs_and_fails_cleanly_on_a_bare_machine(tmp_path):
    out = subprocess.run([sys.executable, "-m", "app.diag", "--local", "--json"], cwd=SERVER_DIR, capture_output=True, text=True,
                         env={"PATH": "", "KAIRO_DATA_DIR": str(tmp_path / "data"), "SYSTEMROOT": "C:\\Windows"} | ({} if sys.platform == "win32" else {"HOME": str(tmp_path)}))
    assert out.returncode == 1, out.stderr
    report = json.loads(out.stdout)
    assert report["counts"]["fail"] > 0 and all(c["detail"] for c in report["checks"])


# ---------------------------------------------------------------- asking the running Kairo

def reply(status=200, body=None):
    return lambda url, **kw: httpx.Response(status, json=body, request=httpx.Request("GET", url))


def test_main_prints_the_running_servers_report_and_applies_strict_itself(pc, speech_only, monkeypatch, capsys):
    monkeypatch.setenv("KAIRO_DATA_DIR", str(pc))
    report, _ = run(pc, live=live(piper={"device": "cpu", "loaded": False, "ok": True}), source="server")
    report = {**report, "ok": True, "exit_code": 0, "counts": {"pass": 99, "warn": 0, "fail": 0}}     # a server can't talk us into a green
    monkeypatch.setattr(httpx, "get", reply(200, report))
    assert diag.main([]) == 0
    assert "asked the running Kairo" in capsys.readouterr().out
    assert diag.main(["--strict", "--json"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["source"] == "server" and printed["counts"]["warn"] == 2 and printed["counts"]["pass"] != 99


def test_fetch_only_ever_calls_this_pcs_own_server(monkeypatch):
    seen = []

    def get(url, **kw):
        seen.append((url, kw))
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", get)
    report, why = diag.fetch(8000, deep=True)
    assert report is None and "didn't answer on 127.0.0.1:8000 (ConnectError)" in why
    (url, kw), = seen
    assert url == "http://127.0.0.1:8000/api/diag/voice" and kw["params"] == {"deep": "true"} and kw["trust_env"] is False


@pytest.mark.parametrize("answer, expect", [
    (reply(404), "answered 404"), (reply(403), "answered 403"), (reply(200, {"schema": 2, "checks": []}), "can't read"),
    (lambda url, **kw: httpx.Response(200, text="<html>", request=httpx.Request("GET", url)), "can't read"),
])
def test_a_server_that_cant_give_a_report_is_explained(monkeypatch, answer, expect):
    monkeypatch.setattr(httpx, "get", answer)
    report, why = diag.fetch(8000)
    assert report is None and expect in why


def test_without_a_server_the_files_are_still_judged_and_the_reason_is_in_the_report(pc, monkeypatch, capsys):
    monkeypatch.setenv("KAIRO_DATA_DIR", str(pc))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    assert diag.main(["--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    by_id = {c["id"]: c for c in report["checks"]}
    assert report["source"] == "local" and "didn't answer on 127.0.0.1" in by_id["kairo.running"]["detail"]
    assert by_id["whisper.installed"]["status"] == "PASS"


# ---------------------------------------------------------------- GET /api/diag/voice

def test_the_endpoint_returns_the_report_from_the_running_servers_own_state(local, monkeypatch):
    local.app.state.voice = DeviceVoice(local.voice, {"whisper": comp("cpu"), "kokoro": comp(),
                                                      "piper": {"device": "cpu", "loaded": False, "ok": True}})
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    r = local.get("/api/diag/voice")
    assert r.status_code == 200
    report = r.json()
    by_id = {c["id"]: c for c in report["checks"]}
    assert report["source"] == "server" and report["schema"] == 1 and report["ok"] is False
    assert by_id["kairo.running"]["status"] == "PASS"
    assert by_id["whisper.device"]["status"] == "FAIL" and "cublas64_12.dll" in by_id["whisper.device"]["detail"]
    assert by_id["kokoro.device"]["status"] == "PASS" and by_id["piper.loaded"]["status"] == "WARN"
    assert by_id["system.hf_offline"]["status"] == "PASS"                     # the server's own environment, not the caller's
    monkeypatch.delenv("HF_HUB_OFFLINE")
    assert {c["id"]: c for c in local.get("/api/diag/voice?deep=true").json()["checks"]}["system.hf_offline"]["status"] == "WARN"


TAILNET = "https://kairo-home.tail1234.ts.net"


def test_the_endpoint_is_owner_only(migrated, monkeypatch):
    monkeypatch.setattr(main_mod, "LOCAL_HOSTS", {"testclient", "127.0.0.1"})
    strict = migrated.model_copy(update={"owner_check": True})
    me = {"tailscale-user-login": "Owner@Example.com"}
    with TestClient(create_app(strict), base_url="http://127.0.0.1:8000") as c:
        assert c.get("/api/diag/voice").status_code == 200                                    # on the PC
        assert c.get("/api/diag/voice", headers={"x-forwarded-for": "100.64.0.2"}).status_code == 403
    with TestClient(create_app(strict), base_url="http://kairo.example.org") as c:
        assert c.get("/api/diag/voice", headers=me).status_code == 403                        # not Kairo's tailnet address
    with TestClient(create_app(strict), base_url=TAILNET) as c:
        assert c.get("/api/diag/voice").status_code == 403                                    # no Tailscale person
        assert c.get("/api/diag/voice", headers=me).status_code == 200
        (strict.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        (strict.data_dir / "logs" / "boot-check.json").write_text(json.dumps({"tailnet_login": "owner@example.com"}), encoding="utf-8")
        assert c.get("/api/diag/voice", headers=me).status_code == 200
        assert c.get("/api/diag/voice", headers={"tailscale-user-login": "guest@example.com"}).status_code == 403


# ---------------------------------------------------------------- the voice model, the turn parts, the speaker, the memory

LLAMA_LOG = ("build: 11292\nggml_cuda_init: found 1 CUDA devices\nusing device CUDA0 (NVIDIA GeForce RTX 3060)\n"
             "load_tensors: offloaded {n}/33 layers to GPU\n")
PROPS = {"default_generation_settings": {"n_ctx": 8192}, "total_slots": 1, "chat_template": "{{ messages }}"}


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def vl(pc, monkeypatch):
    """tools\\voicellm-<hash> in miniature: what the installer recorded, and a running llama-server that is on the GPU."""
    site = pc.parent / "tools" / "voicellm"
    files = {"exe": site / "llama" / "llama-server", "model": site / "models" / "lfm2.5-2.6b" / "m.gguf",
             "speaker": site / "speaker" / "spk.onnx", "turn": site / "smart-turn" / "smart-turn-v3.2-cpu.onnx"}
    for name, f in files.items():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes((name[0].encode() * 300))
    rel = lambda f: f.relative_to(site).as_posix()
    real = voicellm.manifest()
    (site / "installed.json").write_text(json.dumps({
        "llama": {"tag": real["llama_cpp"]["tag"], "server": rel(files["exe"]), "files": []},
        "models": {"lfm2.5-2.6b": {"file": rel(files["model"]), "sha256": sha(files["model"]), "size": 300, "quant": "Q4_K_M"}},
        "smart_turn": {"file": rel(files["turn"]), "sha256": sha(files["turn"])}, "speaker": {"file": rel(files["speaker"])},
        "skipped": []}), encoding="utf-8")
    (site / ".complete").write_text("ok\n", encoding="ascii")
    pinned = {**real, "speaker": {**real["speaker"], "size": 300, "sha256": sha(files["speaker"])}}
    monkeypatch.setattr(voicellm, "manifest", lambda: pinned)
    (pc / "logs").mkdir()
    (pc / "logs" / "voice-llm.log").write_text(LLAMA_LOG.format(n=33), encoding="utf-8")
    monkeypatch.setattr(voice_local, "health", lambda port=8091, timeout=2.0: True)
    monkeypatch.setattr(voice_local, "props", lambda port=8091, timeout=2.0: dict(PROPS))
    monkeypatch.setattr(diag, "pipecat_model", lambda ctx, sub, pat: files["turn"] if "turn" in sub else files["speaker"])
    monkeypatch.setattr(diag, "probe_smart_turn", lambda path: (14, None))
    monkeypatch.setattr(diag, "probe_silero", lambda path: (6, None))
    return types.SimpleNamespace(site=site, **files)


def voice_parts(**over):
    return live(**{"voice_model": {"model": "lfm2.5-2.6b", "state": "ready", "loaded": True}, **over})


def test_a_healthy_voice_model_passes_all_four_checks(pc, vl):
    _, by_id = run(pc, live=voice_parts())
    got = {k: (c["status"], c["value"]) for k, c in by_id.items() if k.startswith("llama.")}
    assert got == {"llama.installed": ("PASS", 0), "llama.running": ("PASS", None), "llama.flags": ("PASS", 8192), "llama.device": ("PASS", None)}
    assert "size matches" in by_id["llama.installed"]["detail"] and "33/33 layers on the GPU" in by_id["llama.device"]["detail"]
    assert "RTX 3060" in by_id["llama.device"]["detail"] and "state: ready" in by_id["llama.running"]["detail"]


def test_a_whole_healthy_stack_exits_zero_with_only_the_expected_warnings(pc, vl, monkeypatch):
    from app import tailnet
    idle = {"device": None, "loaded": False, "on": False, "ok": True, "reason": "until you teach Kairo your voice"}
    monkeypatch.setattr(diag, "ram", lambda: (8000, 16384))
    monkeypatch.setattr(tailnet, "status", lambda data_dir: {"peers": [{"os": "iOS", "online": True, "direct": True}], "source": "live"})
    state = {**voice_parts(speaker=idle), "env": {"HF_HUB_OFFLINE": "1"}}
    state["devices"]["gpu"] = {"name": "NVIDIA GeForce RTX 3060", "used_mb": 7000, "total_mb": 12288, "processes": []}
    report, by_id = run(pc, live=state, source="server")
    assert report["counts"]["fail"] == 0 and report["exit_code"] == 0 and report["ok"] is True
    assert sorted(i for i, c in by_id.items() if c["status"] == "WARN") == ["embeddings.installed", "speaker.enrolled"]
    assert run(pc, live=state, strict=True)[0]["exit_code"] == 1


def test_a_model_file_of_the_wrong_size_is_a_fail_and_the_checksum_is_only_read_with_deep(pc, vl):
    vl.model.write_bytes(b"x" * 300)                         # same size, other content: the checksum is the only thing that knows
    assert run(pc, live=voice_parts())[1]["llama.installed"]["status"] == "PASS"
    deep = run(pc, live=voice_parts(), deep=True)[1]["llama.installed"]
    assert deep["status"] == "FAIL" and "damaged or replaced" in deep["detail"]
    vl.model.write_bytes(b"m" * 10)
    assert "it is 10 bytes, the pin says 300" in run(pc, live=voice_parts())[1]["llama.installed"]["detail"]


def test_llama_cpp_of_another_tag_than_the_release_pins_is_a_fail(pc, vl):
    rec = json.loads((vl.site / "installed.json").read_text())
    rec["llama"]["tag"] = "b1"
    (vl.site / "installed.json").write_text(json.dumps(rec), encoding="utf-8")
    c = run(pc, live=voice_parts())[1]["llama.installed"]
    assert c["status"] == "FAIL" and "b1 is installed" in c["detail"]


def test_a_voice_model_that_does_not_answer_is_a_fail_with_what_kairo_says(pc, vl, monkeypatch):
    monkeypatch.setattr(voice_local, "health", lambda port=8091, timeout=2.0: False)
    _, by_id = run(pc, live=voice_parts(voice_model={"model": "lfm2.5-2.6b", "state": "failed", "reason": "llama-server did not start (no log)"}))
    assert by_id["llama.running"]["status"] == "FAIL" and "127.0.0.1:8091" in by_id["llama.running"]["detail"]
    assert "failed: llama-server did not start" in by_id["llama.running"]["detail"]
    assert by_id["llama.flags"]["status"] == "FAIL" and "not running" in by_id["llama.flags"]["detail"]


def test_context_slots_and_template_are_judged_against_what_kairo_starts_it_with(pc, vl, monkeypatch):
    def props(n_ctx=8192, slots=1, template="x"):
        monkeypatch.setattr(voice_local, "props", lambda port=8091, timeout=2.0: {
            "default_generation_settings": {"n_ctx": n_ctx}, "total_slots": slots, "chat_template": template})
        return run(pc, live=voice_parts())[1]["llama.flags"]
    assert props()["status"] == "PASS"
    small = props(n_ctx=4096)
    assert small["status"] == "FAIL" and "context 4096 tokens, Kairo starts it with 8192" in small["detail"] and small["value"] == 4096
    no_template = props(template="")
    assert no_template["status"] == "FAIL" and "--jinja" in no_template["detail"]
    assert props(slots=4)["status"] == "WARN"
    monkeypatch.setattr(voice_local, "props", lambda port=8091, timeout=2.0: None)
    assert run(pc, live=voice_parts())[1]["llama.flags"]["status"] == "WARN"


def test_layers_off_the_gpu_are_a_fail_with_llama_cpps_own_reason(pc, vl):
    (pc / "logs" / "voice-llm.log").write_text(LLAMA_LOG.format(n=20), encoding="utf-8")
    c = run(pc, live=voice_parts())[1]["llama.device"]
    assert c["status"] == "FAIL" and "only 20/33 layers on the GPU" in c["detail"]


def test_smart_turn_and_silero_must_be_there_load_and_answer(pc, vl, monkeypatch):
    _, by_id = run(pc, live=voice_parts())
    assert by_id["smart_turn.installed"]["status"] == "PASS" and "checksum ok (old line)" in by_id["smart_turn.installed"]["detail"]
    assert (by_id["smart_turn.loaded"]["value"], by_id["silero.loaded"]["value"]) == (14, 6)
    assert by_id["smart_turn.device"]["status"] == by_id["silero.device"]["status"] == "PASS"
    vl.turn.write_bytes(b"t" * 299 + b"x")
    c = run(pc, live=voice_parts())[1]["smart_turn.installed"]
    assert c["status"] == "FAIL" and "damaged or replaced" in c["detail"]
    monkeypatch.setattr(diag, "probe_smart_turn", lambda path: (None, "InvalidProtobuf: bad model"))
    monkeypatch.setattr(diag, "probe_silero", lambda path: (None, "NoSuchFile"))
    _, by_id = run(pc, live=voice_parts())
    assert "InvalidProtobuf: bad model" in by_id["smart_turn.loaded"]["detail"] and by_id["silero.loaded"]["status"] == "FAIL"
    monkeypatch.setattr(diag, "pipecat_model", lambda ctx, sub, pat: None)
    _, by_id = run(pc, live=voice_parts())
    assert by_id["smart_turn.installed"]["status"] == by_id["silero.installed"]["status"] == "FAIL"
    assert "pipecat" in by_id["silero.installed"]["detail"]


def test_a_model_that_cannot_be_loaded_is_reported_not_raised(tmp_path):
    junk = tmp_path / "junk.onnx"
    junk.write_bytes(b"not a model")
    ms, why = diag.probe_smart_turn(junk)
    assert ms is None and why
    ms, why = diag.probe_silero(junk)
    assert ms is None and why


def test_the_speaker_model_must_match_the_pin_in_voicellm_json(pc, vl):
    c = run(pc, live=voice_parts())[1]["speaker.installed"]
    assert c["status"] == "PASS" and "match the pin" in c["detail"]
    vl.speaker.write_bytes(b"s" * 299 + b"x")                 # right size, wrong bytes
    c = run(pc, live=voice_parts())[1]["speaker.installed"]
    assert c["status"] == "FAIL" and "not the pinned one" in c["detail"] and "voicellm.json" in c["detail"]
    vl.speaker.unlink()
    assert "isn't downloaded" in run(pc, live=voice_parts())[1]["speaker.installed"]["detail"]


def test_the_real_manifest_does_pin_the_speaker_model():
    pin = json.loads(Path(voicellm.MANIFEST).read_text(encoding="utf-8"))["speaker"]
    assert pin["size"] > 1_000_000 and len(pin["sha256"]) == 64 and len(pin["revision"]) == 40


def test_the_speaker_is_a_warning_until_it_is_enrolled_and_a_fail_on_the_cpu_once_it_is(pc, vl):
    idle = {"device": None, "loaded": False, "on": False, "ok": True, "reason": "until you teach Kairo your voice"}
    _, by_id = run(pc, live=voice_parts(speaker=idle))
    assert by_id["speaker.enrolled"]["status"] == "WARN" and "speaker.loaded" not in by_id          # off: said once
    (pc / "voice").mkdir()
    (pc / "voice" / "voiceprint.bin").write_bytes(b"VOICEPRINT-BYTES")
    _, by_id = run(pc, live=voice_parts(speaker={**comp("cpu"), "on": True}))
    assert by_id["speaker.enrolled"]["status"] == "PASS" and "never shown" in by_id["speaker.enrolled"]["detail"]
    assert by_id["speaker.device"]["status"] == "FAIL" and "should be on the GPU" in by_id["speaker.device"]["detail"]
    assert "VOICEPRINT-BYTES" not in json.dumps(by_id)


def test_the_embeddings_start_with_the_first_search_but_must_not_run_on_the_cpu(pc, vl, monkeypatch):
    from app import semantic
    _, by_id = run(pc, live=voice_parts())
    assert by_id["embeddings.installed"]["status"] == "WARN" and "isn't set up" in by_id["embeddings.installed"]["detail"]
    monkeypatch.setattr(tool_sites, "configured", lambda kind: True)
    monkeypatch.setattr(semantic.Embedder, "installed", lambda self: True)
    idle = {"device": None, "loaded": False, "ok": False, "reason": "not started yet (it starts with the first memory search)"}
    _, by_id = run(pc, live=voice_parts(embeddings=idle))
    assert by_id["embeddings.installed"]["status"] == "PASS" and by_id["embeddings.loaded"]["status"] == "WARN"
    _, by_id = run(pc, live=voice_parts(embeddings={"device": "cpu", "loaded": True, "ok": False, "reason": "on the CPU: only 900 MB free on the GPU"}))
    assert by_id["embeddings.device"]["status"] == "FAIL" and "only 900 MB free" in by_id["embeddings.device"]["detail"]
    _, by_id = run(pc, live=voice_parts(embeddings={"device": "cuda", "loaded": True, "ok": True}))
    assert by_id["embeddings.device"]["status"] == "PASS"
    monkeypatch.setattr(semantic.Embedder, "installed", lambda self: False)
    assert run(pc, live=voice_parts())[1]["embeddings.installed"]["status"] == "FAIL"
