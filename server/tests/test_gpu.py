"""Owner's request 2026-10-17: where the voice runs. llama.cpp's own log says whether the voice model
found CUDA and how many layers it put on the GPU; Voice speed shows it red when not all."""

from __future__ import annotations

from app import gpu

FULL = """build: 6900 (abc123) with MSVC for x64
ggml_cuda_init: found 1 CUDA devices:
  Device 0: NVIDIA GeForce RTX 3060, compute capability 8.6, VMM: yes
llama_model_load_from_file_impl: using device CUDA0 (NVIDIA GeForce RTX 3060) - 11250 MiB free
load_tensors: offloaded 31/31 layers to GPU
main: server is listening on http://127.0.0.1:8081
"""


def test_all_layers_on_the_gpu_is_green(tmp_path):
    log = tmp_path / "voice-llm.log"
    log.write_text(FULL, encoding="utf-8")
    d = gpu.llama_device(log)
    assert d == {"device": "gpu", "layers": "31/31", "gpu": "NVIDIA GeForce RTX 3060", "ok": True, "reason": None}


def test_only_the_newest_start_counts_and_cpu_is_red_with_the_reason(tmp_path):
    log = tmp_path / "voice-llm.log"
    log.write_text(FULL + "build: 6900 (abc123)\nggml_cuda_init: failed to initialize CUDA: no CUDA-capable device\n"
                   "load_tensors: offloaded 0/31 layers to GPU\n", encoding="utf-8")
    d = gpu.llama_device(log)
    assert d["device"] == "cpu" and d["ok"] is False and "0/31" in d["reason"] and "failed to initialize CUDA" in d["reason"]


def test_partial_offload_is_red(tmp_path):
    log = tmp_path / "voice-llm.log"
    log.write_text(FULL.replace("31/31", "20/31"), encoding="utf-8")
    d = gpu.llama_device(log)
    assert d["device"] == "gpu" and d["ok"] is False and "20/31" in d["reason"]


def test_no_log_says_so(tmp_path):
    assert gpu.llama_device(tmp_path / "none.log")["ok"] is False


def test_no_nvidia_driver_is_none(monkeypatch):
    monkeypatch.setattr(gpu.shutil, "which", lambda _: None)
    assert gpu.summary() is None
