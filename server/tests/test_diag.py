"""kairo diag (owner's spec: "`kairo diag` exits 0" when the voice stack is healthy). The checks are judged
here against a made-up Kairo folder and made-up answers from the running server; nothing needs a GPU or a model."""

from __future__ import annotations

import json
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app import diag, tool_sites
from app import voice as voice_mod
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


def test_a_healthy_voice_passes_and_exits_zero(pc):
    report, by_id = run(pc, live=live(), source="server")
    assert [c["status"] for c in report["checks"]] == ["PASS"] * len(report["checks"])
    assert report["ok"] is True and report["exit_code"] == 0 and report["counts"] == {"pass": len(report["checks"]), "warn": 0, "fail": 0}
    assert by_id["whisper.loaded"]["value"] == 140 and by_id["whisper.loaded"]["unit"] == "ms"
    assert "2300 ms" in by_id["whisper.loaded"]["detail"] and by_id["whisper.installed"]["unit"] == "MB"
    assert by_id["piper.device"]["detail"].startswith("on the CPU by design")


def test_a_part_on_the_cpu_that_should_be_on_the_gpu_is_a_fail_with_its_reason(pc):
    report, by_id = run(pc, live=live(whisper=comp("cpu")))
    c = by_id["whisper.device"]
    assert c["status"] == "FAIL" and "should be on the GPU" in c["detail"] and "cublas64_12.dll" in c["detail"]
    assert by_id["kokoro.device"]["status"] == "PASS"
    assert report["exit_code"] == 1 and report["ok"] is False and report["counts"]["fail"] == 1


def test_loaded_but_never_warmed_up_is_a_warning(pc):
    _, by_id = run(pc, live=live(kokoro=comp(warm_ms=None)))
    assert by_id["kokoro.loaded"]["status"] == "WARN" and "not warmed up" in by_id["kokoro.loaded"]["detail"]


def test_not_loaded_is_a_fail_except_for_the_german_voice_which_loads_on_first_use(pc):
    _, by_id = run(pc, live=live(whisper={"device": None, "loaded": False, "ok": False, "reason": "not loaded yet (it loads when Kairo starts)"},
                                 piper={"device": "cpu", "loaded": False, "ok": True}))
    assert by_id["whisper.loaded"]["status"] == "FAIL" and "not loaded yet" in by_id["whisper.loaded"]["detail"]
    assert by_id["whisper.device"]["status"] == "FAIL"
    assert by_id["piper.loaded"]["status"] == "WARN" and by_id["piper.device"]["status"] == "WARN"


def test_a_package_that_cannot_be_used_is_a_fail(pc, monkeypatch):
    monkeypatch.setattr(tool_sites, "is_real_package",
                        lambda name: (False, "faster_whisper: folder found but not readable by this account") if name == "faster_whisper" else (True, ""))
    _, by_id = run(pc, live=live())
    assert by_id["whisper.installed"]["status"] == "FAIL" and "not readable by this account" in by_id["whisper.installed"]["detail"]
    assert by_id["kokoro.installed"]["status"] == "PASS"


def test_a_missing_model_file_is_a_fail(pc):
    next((pc.parent / "tools" / "voice" / "models" / "whisper").rglob("model.bin")).unlink()
    (pc.parent / "tools" / "voice" / "models" / "kokoro" / "voices-v1.0.bin").unlink()
    _, by_id = run(pc, live=live())
    assert by_id["whisper.installed"]["status"] == "FAIL" and "isn't downloaded" in by_id["whisper.installed"]["detail"]
    assert by_id["kokoro.installed"]["status"] == "FAIL" and "voices-v1.0.bin" in by_id["kokoro.installed"]["detail"]


def test_without_the_running_server_nothing_can_be_called_loaded(pc):
    report, by_id = run(pc, live=None)
    assert by_id["kairo.running"]["status"] == "FAIL"
    for cid in ("whisper.loaded", "whisper.device", "kokoro.loaded", "piper.device"):
        assert by_id[cid]["status"] == "FAIL" and "server didn't answer" in by_id[cid]["detail"]
    assert by_id["whisper.installed"]["status"] == "PASS"            # what is on the disk is still judged
    assert report["exit_code"] == 1


def test_strict_turns_a_warning_into_exit_one(pc):
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


def test_the_json_has_a_fixed_shape(pc):
    report, _ = run(pc, live=live(), source="server")
    assert set(report) == {"schema", "at", "source", "ok", "exit_code", "counts", "host", "checks"}
    assert report["schema"] == 1 and set(report["host"]) == {"version", "python", "platform"}
    for c in report["checks"]:
        assert set(c) == {"id", "group", "name", "status", "detail", "value", "unit"} and c["status"] in ("PASS", "WARN", "FAIL")
    json.dumps(report)


def test_the_text_is_one_ascii_line_per_check_with_a_verdict(pc):
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


def test_main_prints_the_running_servers_report_and_applies_strict_itself(pc, monkeypatch, capsys):
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
    r = local.get("/api/diag/voice")
    assert r.status_code == 200
    report = r.json()
    by_id = {c["id"]: c for c in report["checks"]}
    assert report["source"] == "server" and report["schema"] == 1 and report["ok"] is False
    assert by_id["kairo.running"]["status"] == "PASS"
    assert by_id["whisper.device"]["status"] == "FAIL" and "cublas64_12.dll" in by_id["whisper.device"]["detail"]
    assert by_id["kokoro.device"]["status"] == "PASS" and by_id["piper.loaded"]["status"] == "WARN"
    assert local.get("/api/diag/voice?deep=true").status_code == 200


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
