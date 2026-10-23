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


# The args llama.cpp's server got before G3 (overnight build 2026-10-23), word for word: the flags off must give exactly this.
OLD_ARGS = ["-m", "m.gguf", "--host", "127.0.0.1", "--port", "8091", "-c", "8192", "-ngl", "all", "-fa", "auto", "--jinja",
            "--reasoning", "off", "--reasoning-budget", "0", "-np", "1", "--cache-reuse", "256", "--no-webui"]


def test_the_server_keeps_no_host_copies_of_old_prompts_and_a_handful_of_checkpoints():
    """G3 (measured: the host prompt cache grew 33 MiB a call, up to 8 GiB on a 16 GB PC): --cache-ram 0, and
    --ctx-checkpoints 8 (3 broke the reuse of a call's start; the default 32 is a RAM cost per slot)."""
    args = voice_local.server_args(Path("m.gguf"), port=8091, context=8192)
    assert args == [*OLD_ARGS[:-1], "--cache-ram", "0", "--ctx-checkpoints", "8", "--no-webui"]
    assert "--swa-full" not in args                                   # +280 MiB of GPU memory: its own switch
    assert "--cache-reuse" in args                                    # kept: only Gemma ignores it (see the CHANGELOG)


def test_with_the_ram_flags_off_the_args_are_exactly_the_old_ones():
    assert voice_local.server_args(Path("m.gguf"), port=8091, context=8192, lean_ram=False) == OLD_ARGS


def test_the_gemma_cache_flag_is_added_only_when_asked():
    assert "--swa-full" in voice_local.server_args(Path("m.gguf"), swa_full=True)
    assert "--swa-full" not in voice_local.server_args(Path("m.gguf"))
    both = voice_local.server_args(Path("m.gguf"), lean_ram=False, swa_full=True)
    assert both == [*OLD_ARGS[:-1], "--swa-full", "--no-webui"] and OLD_ARGS[-1] == "--no-webui"


def started_with(tmp_path, vm, options=None, **env):
    """The flags the (fake) server was really started with."""
    argv = tmp_path / "argv.json"
    v = vm(FAKE_LLAMA_ARGV=str(argv), **env)
    if options is not None:
        v.options = options
    assert v.wait_ready(20)
    return v, json.loads(argv.read_text(encoding="utf-8"))


def test_the_manager_starts_the_server_with_what_the_switches_say(vm, tmp_path):
    v, argv = started_with(tmp_path, vm, options=lambda: {"llama_flags": True, "swa_full": True})
    assert "--cache-ram" in argv and "--ctx-checkpoints" in argv and "--swa-full" in argv
    assert v.status()["options"] == {"llama_flags": True, "swa_full": True, "gemma_markup": True, "constant_tools": False}


def test_the_manager_with_the_switches_off_starts_the_old_server(vm, tmp_path):
    v, argv = started_with(tmp_path, vm, options=lambda: {"llama_flags": False, "swa_full": False})
    assert "--cache-ram" not in argv and "--ctx-checkpoints" not in argv and "--swa-full" not in argv
    assert "--cache-reuse" in argv


def test_a_broken_switch_reader_never_stops_the_voice(vm, tmp_path):
    def boom():
        raise RuntimeError("database is locked")
    v, argv = started_with(tmp_path, vm, options=boom)
    assert "--cache-ram" in argv and v.status()["state"] == "ready"        # the defaults


def test_every_flag_g3_passes_exists_in_the_real_llama_server():
    """Set KAIRO_LLAMA_SERVER to a llama-server (b11292, the pinned tag): its own --help must know each flag.
    Skipped where there is none (CI); run on the PC with the installed one (docs/voice-recipe P7)."""
    exe = os.environ.get("KAIRO_LLAMA_SERVER")
    if not exe:
        pytest.skip("no llama-server given (KAIRO_LLAMA_SERVER)")
    out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=60).stdout
    for flag in ("--cache-ram", "--ctx-checkpoints", "--swa-full", "--cache-reuse", "--no-webui", "--reasoning-budget"):
        assert flag in out, flag


# ---------------------------------------------------------------- the start of the prompt, read before the first call (G3)
# Instead of a slot file (deferred: a truncated one aborts llama-server): when the server is ready, one request computes
# the static start (system text + tools), so the first call's own first read finds it cached.

def warm_vm(vm, tmp_path, messages=None, on=True, **env):
    rec = tmp_path / "warm.jsonl"
    v = vm(FAKE_LLAMA_RECORD_WARM=str(rec), **env)
    v.warm_messages = lambda: messages if messages is not None else [{"role": "system", "content": "You are Kairo."}]
    v.take_options({"constant_tools": on})
    v.options = lambda: {"constant_tools": on}
    return v, rec


def wait_warm(v, timeout=10):
    until = time.monotonic() + timeout
    while time.monotonic() < until and not v.warm:
        time.sleep(0.05)
    return v.warm


def sent_warm(rec):
    return [json.loads(x) for x in rec.read_text(encoding="utf-8").splitlines()] if rec.is_file() else []


def test_the_start_of_the_prompt_is_read_once_when_the_server_is_ready(vm, tmp_path):
    v, rec = warm_vm(vm, tmp_path)
    assert v.wait_ready(20)
    warm = wait_warm(v)
    assert warm["how"] == "computed" and warm["tokens"] > 0
    assert v.status()["warm"] == warm and "ms" in warm
    steps = sent_warm(rec)
    assert [x["path"] for x in steps] == ["/apply-template", "/tokenize", "/completion"]
    tmpl, tok, comp = (x["body"] for x in steps)
    assert tmpl["messages"] == [{"role": "system", "content": "You are Kairo."}] and tmpl["tools"] == [ASK_KAIRO]
    assert tmpl["add_generation_prompt"] is False and tmpl["chat_template_kwargs"] == {"enable_thinking": False}
    assert tok["add_special"] is True and tok["parse_special"] is True               # BOS in; special tokens parsed
    assert comp["n_predict"] == 1 and comp["cache_prompt"] is True and comp["prompt"] and isinstance(comp["prompt"], list)
    time.sleep(0.5)
    assert len(sent_warm(rec)) == 3                                                  # once, not every health check


def test_without_the_switch_nothing_is_read_in_advance(vm, tmp_path):
    v, rec = warm_vm(vm, tmp_path, on=False)
    assert v.wait_ready(20)
    time.sleep(0.5)
    assert v.status()["warm"] == {} and sent_warm(rec) == []


def test_a_server_that_cannot_warm_up_still_talks(vm, tmp_path):
    v, rec = warm_vm(vm, tmp_path, FAKE_LLAMA_WARM_FAIL="1")
    assert v.wait_ready(20)
    assert wait_warm(v)["how"] == "failed" and "500" in v.status()["warm"]["why"]
    assert v.state == "ready" and "".join(e["text"] for e in collect(v, [{"role": "user", "content": "hey"}]) if e["kind"] == "text")


def test_a_broken_message_builder_never_stops_the_voice(vm, tmp_path):
    v, rec = warm_vm(vm, tmp_path)

    def boom():
        raise RuntimeError("no profile yet")
    v.warm_messages = boom
    assert v.wait_ready(20)
    assert wait_warm(v)["how"] == "failed" and v.state == "ready"


def test_the_start_is_read_again_when_the_server_starts_again(vm, tmp_path):
    v, rec = warm_vm(vm, tmp_path)
    assert v.wait_ready(20)
    wait_warm(v)
    v.server.proc.kill()
    v.server.proc.wait(5)
    assert v.ensure() in ("starting", "ready") and v.warm == {}                      # forgotten with the server
    assert v.wait_ready(20)
    assert wait_warm(v)["how"] == "computed" and len(sent_warm(rec)) == 6


def test_the_first_read_of_a_call_says_when_it_found_less_cached_than_the_warm_start(vm, tmp_path, caplog):
    import logging
    v, _ = warm_vm(vm, tmp_path)
    assert v.wait_ready(20)
    wait_warm(v)
    v.warm = {"how": "computed", "tokens": 625, "ms": 5}                              # the fake server cached 80 of the prompt
    with caplog.at_level(logging.WARNING, logger="kairo.voice_llm"):
        t = asyncio.run(v.prime([{"role": "user", "content": "Hi"}], tools=[ASK_KAIRO], tool_choice="none"))
    assert t["cache_n"] == 80 and v.status()["last_cache_n"] == 80 and v.status()["last_prompt_n"] == 20
    assert any("80" in r.getMessage() and "625" in r.getMessage() for r in caplog.records)
    caplog.clear()
    v.warm = {"how": "computed", "tokens": 70, "ms": 5}
    with caplog.at_level(logging.WARNING, logger="kairo.voice_llm"):
        asyncio.run(v.prime([{"role": "user", "content": "Hi"}]))
    assert not caplog.records
    v.warm = {}
    with caplog.at_level(logging.WARNING, logger="kairo.voice_llm"):
        asyncio.run(v.prime([{"role": "user", "content": "Hi"}]))
    assert not caplog.records                                                         # nothing was warmed: nothing to compare


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


def test_switched_off_when_no_model_passed_the_lab(tmp_path):
    v = VoiceModel(lambda: "no model passed the voice lab", log_dir=tmp_path / "logs", port=free_port())
    assert v.ensure() == "off" and v.status()["error"] == "no model passed the voice lab" and v.server is None
    assert not v.wait_ready(1)


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
