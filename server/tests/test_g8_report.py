"""G8: what the voice report says about the graphics card: what llama.cpp put on it, what each staged load took, and NVIDIA's CUDA
sysmem fallback policy (read back from the driver; `kairo diag` has one check for it)."""

from __future__ import annotations

import sys

import pytest

from app import diag, flags, gpu
from tests.test_diag import live, pc, run, vl, voice_parts  # noqa: F401  (pc and vl are fixtures)
from tests.test_diag_system import card, only_system, with_gpu  # noqa: F401  (only_system is a fixture)
from tests.test_voice_local import local  # noqa: F401  (the app with the fake voice model)

OFF = {"available": True, "value": 1, "off": True, "label": "prefer NO sysmem fallback", "why": None}
UNSET = {"available": True, "value": None, "off": False, "label": gpu.nvsettings.UNSET, "why": None}
FALLBACK = {"available": True, "value": 2, "off": False, "label": "prefer sysmem fallback", "why": None}
CANT = {"available": False, "value": None, "off": False, "label": "not available", "why": "no NVIDIA driver found (nvapi64.dll)"}
CARD = {"name": "NVIDIA GeForce RTX 3060", "driver": "580.1", "used_mb": 7000, "total_mb": 12288, "processes": []}


# ---------------------------------------------------------------- /api/voice/devices

def test_the_voice_model_row_says_how_much_graphics_memory_llama_cpp_took_and_the_boot_trail_is_listed(local, monkeypatch):
    monkeypatch.setattr(gpu, "summary", lambda: dict(CARD))
    vram = {"model": 2831, "kv": 448, "rs": 0, "compute": 363, "total": 3642, "oom": None}
    monkeypatch.setattr(gpu, "llama_vram", lambda path, **kw: vram)
    local.app.state.boot_trail[:] = [{"stage": "voice model (llama.cpp)", "ms": 12000, "free_before_mb": 11000, "free_after_mb": 7400,
                                      "used_mb": 3600, "settled": True, "timed_out": False, "error": None}]
    d = local.get("/api/voice/devices").json()
    assert d["components"]["voice_model"]["vram"] == vram
    assert d["boot"][0]["stage"] == "voice model (llama.cpp)" and d["boot"][0]["used_mb"] == 3600


def test_the_policy_is_in_the_gpu_report_only_when_the_readback_is_on(local, monkeypatch):
    on, what = flags.FLAGS["g8.sysmem_readback"]
    assert on is False and "driver" in what                  # unproven on the PC until P17: the owner turns it on
    monkeypatch.setattr(gpu, "summary", lambda: dict(CARD))
    asked = []
    monkeypatch.setattr(gpu, "sysmem_policy", lambda **kw: asked.append(1) or dict(OFF))
    assert "sysmem" not in local.get("/api/voice/devices").json()["gpu"] and asked == []
    assert local.put("/api/flags/g8.sysmem_readback", json={"on": True}).status_code == 200
    got = local.get("/api/voice/devices").json()["gpu"]
    assert got["sysmem"]["value"] == 1 and got["sysmem"]["off"] is True and asked == [1]
    assert local.put("/api/flags/g8.sysmem_readback", json={"on": None}).status_code == 200
    assert "sysmem" not in local.get("/api/voice/devices").json()["gpu"]


def test_no_graphics_card_means_no_policy_to_read(local, monkeypatch):
    monkeypatch.setattr(gpu, "summary", lambda: None)
    monkeypatch.setattr(gpu, "sysmem_policy", lambda **kw: pytest.fail("asked the driver without a card"))
    local.put("/api/flags/g8.sysmem_readback", json={"on": True})
    assert local.get("/api/voice/devices").json()["gpu"] is None


# ---------------------------------------------------------------- kairo diag: system.sysmem

def sysmem(pc, policy, used=7000, total=12288, monkeypatch=None, server=True, **kw):
    monkeypatch.setattr(gpu, "sysmem_policy", lambda **k: dict(policy))
    g = card(used, total)
    if server:
        g["sysmem"] = dict(policy)
    return run(pc, live=with_gpu(g), **kw)[1]["system.sysmem"]


def test_policy_one_is_a_pass_that_says_a_full_card_is_an_error_not_a_slowdown(pc, only_system, monkeypatch):
    c = sysmem(pc, OFF, monkeypatch=monkeypatch)
    assert c["status"] == "PASS" and c["value"] == 1 and "out-of-memory error" in c["detail"] and "Kairo's server" in c["detail"]


def test_not_set_is_fine_while_the_card_has_room_and_a_warning_when_it_is_nearly_full(pc, only_system, monkeypatch):
    fine = sysmem(pc, UNSET, used=7000, monkeypatch=monkeypatch)
    assert fine["status"] == "PASS" and "may spill" in fine["detail"] and "Your choice" in fine["detail"]
    full = sysmem(pc, UNSET, used=11000, monkeypatch=monkeypatch)
    assert full["status"] == "WARN" and "nearly full" in full["detail"] and "-CudaSysmemPolicy" in full["detail"]
    assert sysmem(pc, FALLBACK, used=12000, monkeypatch=monkeypatch)["status"] == "WARN"


def test_the_policy_unread_on_the_server_is_read_by_the_command_itself_and_says_so(pc, only_system, monkeypatch):
    c = sysmem(pc, OFF, monkeypatch=monkeypatch, server=False)
    assert c["status"] == "PASS" and "this command" in c["detail"]
    # a report built inside Kairo's server (the page, and what `kairo diag` fetches) is read by Kairo's own account
    c = sysmem(pc, OFF, monkeypatch=monkeypatch, server=False, source="server")
    assert c["status"] == "PASS" and "Kairo's server" in c["detail"] and "this command" not in c["detail"]
    monkeypatch.setattr(gpu, "sysmem_policy", lambda **k: dict(OFF))
    assert run(pc, live=None)[1]["system.sysmem"]["status"] == "PASS"       # Kairo not answering: this process asks itself


def test_a_driver_that_cannot_be_read_is_a_warning_on_windows_and_nothing_on_linux(pc, only_system, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    c = sysmem(pc, CANT, monkeypatch=monkeypatch)
    assert c["status"] == "WARN" and "can't read" in c["detail"] and "nvapi64.dll" in c["detail"]
    monkeypatch.setattr(sys, "platform", "linux")
    c = sysmem(pc, {**CANT, "why": "Windows only"}, monkeypatch=monkeypatch)
    assert c["status"] == "PASS" and "not applicable" in c["detail"]


def test_without_a_graphics_card_there_is_nothing_to_judge(pc, only_system, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(gpu, "summary", lambda: None)
    monkeypatch.setattr(gpu, "sysmem_policy", lambda **k: dict(CANT))
    c = run(pc, live=with_gpu(None))[1]["system.sysmem"]
    assert c["status"] == "PASS" and "no NVIDIA graphics card" in c["detail"]


def test_the_check_is_one_of_this_pcs(pc, only_system, monkeypatch):
    c = sysmem(pc, OFF, monkeypatch=monkeypatch)
    assert c["group"] == diag.GROUPS["system"] and c["name"] == "CUDA sysmem fallback"


# ---------------------------------------------------------------- kairo diag: what the voice model took

def test_the_voice_model_check_says_how_much_graphics_memory_it_holds(pc, vl):
    (pc / "logs" / "voice-llm.log").write_text(
        "build: 11292 (90c908d)\nggml_cuda_init: found 1 CUDA devices:\nllama_model_load_from_file_impl: using device CUDA0 (NVIDIA GeForce RTX 3060) - 11250 MiB free\n"
        "load_tensors: offloaded 33/33 layers to GPU\nload_tensors: CUDA0 model buffer size = 2831.40 MiB\nllama_kv_cache: CUDA0 KV buffer size = 448.00 MiB\n"
        "sched_reserve: CUDA0 compute buffer size = 363.25 MiB\n", encoding="utf-8")
    c = run(pc, live=voice_parts())[1]["llama.device"]
    assert c["status"] == "PASS" and "33/33 layers on the GPU" in c["detail"]
    assert "3.6 GB of graphics memory" in c["detail"] and "model 2.8 GB" in c["detail"] and "context 448 MB" in c["detail"]


def test_an_out_of_memory_start_is_a_fail_with_llama_cpps_words(pc, vl):
    (pc / "logs" / "voice-llm.log").write_text(
        "build: 11292 (90c908d)\nggml_cuda_init: found 1 CUDA devices:\n"
        "ggml_backend_cuda_buffer_type_alloc_buffer: allocating 5417.12 MiB on device 0: cudaMalloc failed: out of memory\n", encoding="utf-8")
    c = run(pc, live=voice_parts())[1]["llama.device"]
    assert c["status"] == "FAIL" and "cudaMalloc failed: out of memory" in c["detail"]
