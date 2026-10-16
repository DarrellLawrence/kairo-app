"""The local voice model (owner's decision 2026-10-16): installed by the installer, kept loaded by Kairo,
streamed turn by turn. Here llama.cpp's server is tests/fakes/fake_llama.py (same flags, same streaming);
the real one was checked by hand against llama.cpp built from its Oct 2026 source."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlmodel import Session

from app import outbound, voicellm
from app.brain import voice_local
from app.brain.voice_local import ASK_KAIRO, VoiceModel
from app.db import make_engine

FAKE = Path(__file__).parent / "fakes" / "fake_llama.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def vm(tmp_path, monkeypatch):
    made = []

    def build(model_id="lfm2.5-2.6b", installed=True, **env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        model = tmp_path / f"{model_id}.gguf"
        model.write_bytes(b"GGUF")
        state = {"model": model_id}
        v = VoiceModel(lambda: ([sys.executable, str(FAKE)], model, state["model"], 4096) if installed else None,
                       log_dir=tmp_path / "logs", port=free_port())
        v.state_ = state
        made.append(v)
        return v
    yield build
    for v in made:
        v.shutdown()


def collect(v, messages, tools=None):
    async def run():
        return [ev async for ev in v.stream(messages, tools=tools)]
    return asyncio.run(run())


def test_the_server_gets_the_flags_for_a_fast_talker():
    args = voice_local.server_args(Path("m.gguf"), port=8091, context=8192)
    flat = " ".join(args)
    for flag in ("--host 127.0.0.1", "-ngl all", "--jinja", "--reasoning off", "--reasoning-budget 0", "-np 1",
                 "--cache-reuse 256", "-c 8192", "--no-webui"):
        assert flag in flat, flag
    assert "0.0.0.0" not in flat


def test_nothing_installed_means_no_server(vm):
    v = vm(installed=False)
    assert v.ensure() == "not installed" and v.server is None and not v.ready()


def test_the_model_is_loaded_and_answers_in_pieces(vm):
    v = vm()
    assert v.wait_ready(20) and v.status()["model"] == "lfm2.5-2.6b"
    evs = collect(v, [{"role": "user", "content": "hey there"}], tools=[ASK_KAIRO])
    text = "".join(e["text"] for e in evs if e["kind"] == "text")
    done = evs[-1]
    assert text == "Sure, happy to help with that." and done["kind"] == "done" and done["finish"] == "stop"
    assert done["first_token_ms"] is not None and done["timings"]["cache_n"] == 80


def test_a_question_about_the_owners_data_becomes_one_ask_kairo_call(vm):
    v = vm()
    assert v.wait_ready(20)
    evs = collect(v, [{"role": "user", "content": "What's on my calendar tomorrow?"}], tools=[ASK_KAIRO])
    kinds = [e["kind"] for e in evs]
    assert "tool_start" in kinds
    call = next(e for e in evs if e["kind"] == "tool_call")
    assert call["name"] == "ask_kairo" and json.loads(call["arguments"]) == {"request": "What's on my calendar tomorrow?"}
    assert evs[-1]["finish"] == "tool_calls"


def test_a_server_that_dies_is_started_again(vm):
    v = vm()
    assert v.wait_ready(20)
    v.server.proc.kill()
    v.server.proc.wait(5)
    assert v.ensure() in ("starting", "ready")
    assert v.wait_ready(20)


def test_the_lab_can_take_the_gpu_and_give_it_back(vm):
    v = vm()
    assert v.wait_ready(20)
    v.pause()
    assert v.status()["state"] == "paused" and v.server is None and v.ensure() == "paused"
    v.resume()
    assert v.wait_ready(20)


def test_switching_models_restarts_the_server(vm):
    v = vm()
    assert v.wait_ready(20)
    v.state_["model"] = "qwen3.5-9b"
    v.ensure()
    assert v.wait_ready(20) and v.status()["model"] == "qwen3.5-9b"


def test_a_server_left_behind_by_a_crash_is_stopped(vm, tmp_path):
    v = vm()
    assert v.wait_ready(20)
    old = v.server.proc
    v.server.proc = None                       # Kairo "crashed": it forgot its server, the pid file stays
    v.server = None
    v.state = "not installed"
    assert v.wait_ready(20)
    assert old.wait(10) is not None            # the old one was stopped, not left holding the GPU


def test_a_pid_file_from_before_a_reboot_never_kills_another_program(tmp_path):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])   # got the old pid by chance
    try:
        pid_file = tmp_path / "llama.pid"
        pid_file.write_text(str(other.pid), encoding="ascii")
        voice_local._kill_stale(pid_file, free_port())     # nothing answers on the voice model's port
        assert other.poll() is None and not pid_file.exists()
        pid_file.write_text(str(os.getpid()), encoding="ascii")
        voice_local._kill_stale(pid_file, free_port())     # and never Kairo itself
    finally:
        other.kill()
        other.wait(5)


def test_a_server_that_never_comes_up_is_reported(vm, monkeypatch):
    monkeypatch.setattr(voice_local, "START_TIMEOUT_S", 1)
    v = vm(FAKE_LLAMA_LOAD_S="30")
    assert not v.wait_ready(5)
    assert v.status()["state"] == "failed" and "did not start" in v.status()["error"]
    assert v.ensure() == "failed"              # not hammered: tried again after RETRY_AFTER_S


# ---------------------------------------------------------------- what the installer downloads

ASSETS = [
    {"name": "llama-b11292-bin-win-cuda-12.4-x64.zip", "browser_download_url": "https://github.com/x/a.zip", "digest": "sha256:aa", "size": 10},
    {"name": "llama-b11292-bin-win-cuda-13.1-x64.zip", "browser_download_url": "https://github.com/x/b.zip", "digest": "sha256:bb", "size": 10},
    {"name": "cudart-llama-bin-win-cuda-12.4-x64.zip", "browser_download_url": "https://github.com/x/c.zip", "digest": "sha256:cc", "size": 10},
    {"name": "llama-b11292-bin-ubuntu-x64.zip", "browser_download_url": "https://github.com/x/d.zip", "digest": "sha256:dd", "size": 10},
]


def test_llama_cpp_comes_from_the_pinned_tag_with_a_checksum():
    m = voicellm.manifest()["llama_cpp"]
    picked = voicellm.pick_assets(ASSETS, m["windows"], m["tag"])
    assert [a["name"] for a in picked] == ["llama-b11292-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"]
    assert picked[0]["sha256"] == "aa"
    no_digest = [{**a, "digest": None} for a in ASSETS]
    with pytest.raises(RuntimeError, match="no checksum"):
        voicellm.pick_assets(no_digest, m["windows"], m["tag"])
    with pytest.raises(RuntimeError, match="no release file"):
        voicellm.pick_assets(ASSETS[3:], m["windows"], m["tag"])


TREE = [
    {"type": "file", "path": "LFM2.5-2.6B-Q4_K_M.gguf", "size": 1600, "lfs": {"oid": "f" * 64, "size": 1600}},
    {"type": "file", "path": "LFM2.5-2.6B-Q8_0.gguf", "size": 2800, "lfs": {"oid": "e" * 64, "size": 2800}},
    {"type": "file", "path": "mmproj-Q4_K_M.gguf", "size": 300, "lfs": {"oid": "d" * 64, "size": 300}},
    {"type": "file", "path": "README.md", "size": 10},
]


def fake_hub(monkeypatch, tree=TREE, sizes=None):
    fetched = []

    def get_json(url):
        if "api.github.com" in url:
            return {"assets": ASSETS}
        if url.endswith("/tree/rev1?recursive=true"):
            if "smart-turn" in url:
                return [{"type": "file", "path": "smart-turn-v3.2-cpu.onnx", "lfs": {"oid": "c" * 64, "size": 8}}]
            return [{**f, "size": (sizes or {}).get(url, f.get("size"))} for f in tree]
        return {"sha": "rev1"}

    def fetch(url, dest, sha, size=None):
        fetched.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix == ".zip":
            import zipfile
            with zipfile.ZipFile(dest, "w") as z:
                z.writestr("llama-server.exe" if sys.platform == "win32" else "llama-server", "bin")
        else:
            dest.write_bytes(b"x")
    monkeypatch.setattr(voicellm, "_get_json", get_json)
    monkeypatch.setattr(voicellm, "_fetch", fetch)
    return fetched


def test_the_quantization_picked_is_the_first_the_repo_has(monkeypatch):
    fake_hub(monkeypatch)
    p = voicellm.hf_pick("LiquidAI/LFM2.5-2.6B-GGUF", ["Q4_K_M", "Q6_K"])
    assert p == {"revision": "rev1", "path": "LFM2.5-2.6B-Q4_K_M.gguf", "sha256": "f" * 64, "size": 1600, "quant": "Q4_K_M"}
    with pytest.raises(RuntimeError, match="no GGUF"):
        voicellm.hf_pick("x/y", ["Q2_K"])


def test_download_installs_what_fits_and_records_it(monkeypatch, tmp_path):
    fetched = fake_hub(monkeypatch)
    gguf = lambda: sum(1 for u in fetched if u.endswith(".gguf"))   # noqa: E731
    out = voicellm.download(tmp_path / "site", log_=lambda *_: None,
                            free_bytes=lambda: voicellm.RESERVE_BYTES + 2000 - 1600 * gguf())
    site = tmp_path / "site"
    assert (site / voicellm.COMPLETE).is_file()
    assert voicellm.server_binary(site) is not None and voicellm.smart_turn_path(site) is not None
    assert set(out["models"]) == {"lfm2.5-2.6b"}                       # 1600 bytes fit, then the disk is "full"
    assert all("not enough disk space" in s["why"] for s in out["skipped"] if s["model"] != "smart-turn")
    assert all(u.startswith(("https://huggingface.co/", "https://github.com/")) for u in fetched)
    assert voicellm.available(site) == ["lfm2.5-2.6b"]


def test_the_lab_pick_decides_and_falls_back_to_what_is_installed(monkeypatch, tmp_path, migrated):
    fake_hub(monkeypatch)
    voicellm.download(tmp_path / "site", log_=lambda *_: None, free_bytes=lambda: 10 ** 15)
    site = tmp_path / "site"
    with Session(make_engine(migrated.db_url)) as s:
        assert voicellm.active(s, site) == "lfm2.5-2.6b"               # no lab run yet: the first one
        voicellm.set_choice(s, "qwen3.5-9b", "lab")
        assert voicellm.active(s, site) == "qwen3.5-9b"
        with pytest.raises(ValueError):
            voicellm.set_choice(s, "gpt-5", "owner")
        (site / voicellm.installed(site)["models"]["qwen3.5-9b"]["file"]).unlink()
        assert voicellm.active(s, site) == "lfm2.5-2.6b"               # picked but gone: the next one


def test_the_kyutai_copy_has_one_folder_name_for_the_installer_and_kairo(tmp_path):
    a = voicellm.kyutai_dir(tmp_path)
    assert a == voicellm.kyutai_dir(tmp_path) and a.name.startswith("kyutai-") and not voicellm.kyutai_ready(tmp_path)


def test_only_listed_modules_reach_the_network_and_the_voice_model_is_local():
    assert "brain/voice_local.py" in outbound.MODULES and "voicellm.py" in outbound.MODULES
    assert voice_local.HOST == "127.0.0.1"


def test_the_memory_model_moves_to_the_cpu_when_a_voice_model_is_installed(tmp_path, monkeypatch):
    """VRAM plan (owner's request 2026-10-16): voice always wins the 12 GB; embeddings go to the CPU."""
    from app import semantic
    seen = []

    class Stop(Exception):
        pass

    def popen(cmd, **kw):
        seen.append(cmd)
        raise Stop()
    (tmp_path / "tokenizer.json").write_text("{}")
    monkeypatch.setattr(semantic.subprocess, "Popen", popen)
    for cpu in (True, False):
        e = semantic.Embedder(tmp_path, cpu_only=lambda c=cpu: c)
        try:
            e._start()
        except Stop:
            pass
    assert "--cpu" in seen[0] and "--cpu" not in seen[1]
