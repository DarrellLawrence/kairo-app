"""Owner's request 2026-10-17: where the voice runs. llama.cpp's own log says whether the voice model
found CUDA and how many layers it put on the GPU; Voice speed shows it red when not all."""

from __future__ import annotations

import pytest

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


# ---------------------------------------------------------------- what llama.cpp put on the card (voice recipe 9, 12)

GEMMA = """build: 11292 (90c908d) with MSVC for x64
ggml_cuda_init: found 1 CUDA devices:
llama_model_load_from_file_impl: using device CUDA0 (NVIDIA GeForce RTX 3060) - 11250 MiB free
load_tensors: offloaded 43/43 layers to GPU
load_tensors:   CPU_Mapped model buffer size =   525.00 MiB
load_tensors:        CUDA0 model buffer size =  2831.40 MiB
llama_kv_cache:      CUDA0 KV buffer size =   384.00 MiB
llama_kv_cache:      CUDA0 KV buffer size =    64.00 MiB
sched_reserve:      CUDA0 compute buffer size =   121.50 MiB
sched_reserve:  CUDA_Host compute buffer size =    12.01 MiB
sched_reserve:      CUDA0 compute buffer size =   363.25 MiB
"""


def test_llama_vram_adds_up_what_llama_cpp_put_on_the_card(tmp_path):
    log = tmp_path / "voice-llm.log"
    log.write_text(GEMMA, encoding="utf-8")
    # model 2831, both KV caches 448, the largest compute buffer (each reserve reports the same one again) 363;
    # the host-side (CPU_Mapped, CUDA_Host) buffers are not on the card
    assert gpu.llama_vram(log) == {"model": 2831, "kv": 448, "rs": 0, "compute": 363, "total": 3642, "oom": None}


def test_llama_vram_counts_the_recurrent_state_too():
    got = gpu.llama_vram_text("build: 1 (a)\nCUDA0 model buffer size = 5417.12 MiB\nCUDA0 KV buffer size = 256.00 MiB\n"
                              "CUDA0 RS buffer size = 50.25 MiB\nCUDA0 compute buffer size = 485.50 MiB\n")
    assert got == {"model": 5417, "kv": 256, "rs": 50, "compute": 486, "total": 6209, "oom": None}


def test_llama_vram_reads_only_the_newest_start(tmp_path):
    log = tmp_path / "voice-llm.log"
    log.write_text("build: 1 (a)\nCUDA0 model buffer size = 9000.00 MiB\nbuild: 2 (b)\nCUDA0 model buffer size = 1500.00 MiB\n", encoding="utf-8")
    assert gpu.llama_vram(log)["model"] == 1500


def test_llama_vram_has_no_numbers_before_the_load_and_without_a_log(tmp_path):
    none = {"model": None, "kv": None, "rs": None, "compute": None, "total": None, "oom": None}
    assert gpu.llama_vram(tmp_path / "none.log") == none
    log = tmp_path / "voice-llm.log"
    log.write_text("build: 1 (a)\nggml_cuda_init: found 1 CUDA devices:\n", encoding="utf-8")
    assert gpu.llama_vram(log) == none


OOM = ("build: 11292 (90c908d)\nggml_cuda_init: found 1 CUDA devices:\n"
       "ggml_backend_cuda_buffer_type_alloc_buffer: allocating 5417.12 MiB on device 0: cudaMalloc failed: out of memory\n"
       "llama_model_load: error loading model: unable to allocate CUDA0 buffer\n")


def test_an_out_of_memory_start_is_red_with_the_reason_not_green(tmp_path):
    """With 'prefer no sysmem fallback' a full card is an error, not a slowdown. The log has CUDA found and no 'offloaded' line:
    it used to read as a healthy GPU start."""
    log = tmp_path / "voice-llm.log"
    log.write_text(OOM, encoding="utf-8")
    d = gpu.llama_device(log)
    assert d["ok"] is False and d["device"] == "cpu" and "cudaMalloc failed: out of memory" in d["reason"]
    assert gpu.llama_vram(log)["oom"] == "cudaMalloc failed: out of memory"


@pytest.mark.parametrize("line, said", [
    ("llama_model_load: error loading model: unable to allocate CUDA0 buffer", "unable to allocate CUDA0 buffer"),
    ("llama_init_from_model: failed to allocate compute pp buffers", "failed to allocate compute pp buffers"),
    ("llama_kv_cache: failed to allocate buffer for kv cache", "failed to allocate buffer for kv cache"),
])
def test_the_other_ways_llama_cpp_says_the_card_is_full(tmp_path, line, said):
    log = tmp_path / "voice-llm.log"
    log.write_text("build: 1 (a)\nggml_cuda_init: found 1 CUDA devices:\n" + line + "\n", encoding="utf-8")
    assert gpu.llama_device(log)["ok"] is False and said in gpu.llama_device(log)["reason"]
    assert gpu.llama_vram(log)["oom"] == said


# The real order in llama.cpp (llama-model.cpp: weights, then the "offloaded n/n" line; llama-kv-cache.cpp: the context memory;
# llama-context.cpp: the work space): a card that fills up AFTER the weights went on it prints the "offloaded" line first.
WEIGHTS_IN = ("build: 11292 (90c908d) with MSVC for x64\nggml_cuda_init: found 1 CUDA devices:\n"
              "llama_model_load_from_file_impl: using device CUDA0 (NVIDIA GeForce RTX 3060) - 11250 MiB free\n"
              "load_tensors: offloaded 43/43 layers to GPU\nload_tensors:        CUDA0 model buffer size =  2831.40 MiB\n")
CUDA_FULL = "ggml_backend_cuda_buffer_type_alloc_buffer: allocating {mib} MiB on device 0: cudaMalloc failed: out of memory\n"


@pytest.mark.parametrize("after", [
    CUDA_FULL.format(mib="448.00") + "llama_init_from_model: failed to allocate buffer for kv cache\n",
    "llama_kv_cache:      CUDA0 KV buffer size =   448.00 MiB\n" + CUDA_FULL.format(mib="363.00")
    + "llama_init_from_model: failed to allocate compute pp buffers\n",
    CUDA_FULL.format(mib="363.00") + "llama_init_from_model: failed to allocate compute tg buffers\n",
    CUDA_FULL.format(mib="448.00"),
], ids=["kv cache", "compute pp buffers", "compute tg buffers", "only the cudaMalloc line"])
def test_a_card_that_fills_up_after_the_weights_went_on_it_is_red_not_green(tmp_path, after):
    """The weights fit (the 'offloaded 43/43' line is printed), then the context memory or the work space does not: the start is dead.
    The first version looked at the layer count first and called this a healthy GPU start."""
    log = tmp_path / "voice-llm.log"
    log.write_text(WEIGHTS_IN + after, encoding="utf-8")
    d = gpu.llama_device(log)
    assert d["ok"] is False and d["device"] == "gpu" and d["layers"] == "43/43"
    assert "cudaMalloc failed: out of memory" in d["reason"]            # the cause, not only the line that says what was not allocated
    assert "43/43" in d["reason"] and "ran out of memory" in d["reason"]
    assert gpu.llama_vram(log)["oom"] == "cudaMalloc failed: out of memory"


def test_a_partial_offload_that_also_ran_out_of_memory_says_the_memory_first(tmp_path):
    log = tmp_path / "voice-llm.log"
    log.write_text(WEIGHTS_IN.replace("43/43", "30/43") + CUDA_FULL.format(mib="448.00") + "llama_init_from_model: failed to allocate buffer for kv cache\n",
                   encoding="utf-8")
    d = gpu.llama_device(log)
    assert d["ok"] is False and "30/43" in d["reason"] and "cudaMalloc failed: out of memory" in d["reason"]


def test_an_allocation_that_llama_cpp_got_past_is_not_a_failure(tmp_path):
    """llama.cpp retries a failed work-space allocation without pipeline parallelism (llama-context.cpp) and then starts: the server
    saying it is listening after the message means the start worked, so the message must not turn a healthy start red."""
    log = tmp_path / "voice-llm.log"
    log.write_text(WEIGHTS_IN + CUDA_FULL.format(mib="363.00")
                   + "llama_context: compute buffer allocation failed, retrying without pipeline parallelism\n"
                   "sched_reserve:      CUDA0 compute buffer size =   363.25 MiB\nsrv          main: listening on http://127.0.0.1:8081\n", encoding="utf-8")
    assert gpu.llama_device(log)["ok"] is True and gpu.llama_vram(log)["oom"] is None
    log.write_text(WEIGHTS_IN + CUDA_FULL.format(mib="363.00") + "main: server is listening on http://127.0.0.1:8081 - starting the main loop\n",
                   encoding="utf-8")
    assert gpu.llama_device(log)["ok"] is True and gpu.llama_vram(log)["oom"] is None


def test_an_out_of_memory_after_the_server_was_listening_still_counts(tmp_path):
    """Only a message BEFORE 'listening' is forgiven; the order matters (a later allocation failing is a real failure)."""
    log = tmp_path / "voice-llm.log"
    log.write_text(WEIGHTS_IN + "srv          main: listening on http://127.0.0.1:8081\n" + CUDA_FULL.format(mib="64.00"), encoding="utf-8")
    assert gpu.llama_device(log)["ok"] is False and gpu.llama_vram(log)["oom"] == "cudaMalloc failed: out of memory"


def test_an_old_out_of_memory_does_not_count_after_a_good_restart(tmp_path):
    log = tmp_path / "voice-llm.log"
    log.write_text(OOM + FULL, encoding="utf-8")
    assert gpu.llama_device(log)["ok"] is True and gpu.llama_vram(log)["oom"] is None


# ---------------------------------------------------------------- free graphics memory

def test_free_mb_is_total_minus_used_from_one_query(monkeypatch):
    asked = []
    monkeypatch.setattr(gpu, "_smi", lambda args: asked.append(args) or [["4096", "12288"]])
    assert gpu.free_mb() == 8192
    assert asked == [["--query-gpu=memory.used,memory.total"]]


@pytest.mark.parametrize("rows", [None, [], [["N/A", "12288"]], [["4096"]]])
def test_free_mb_is_none_when_the_driver_does_not_say(monkeypatch, rows):
    monkeypatch.setattr(gpu, "_smi", lambda args: rows)
    assert gpu.free_mb() is None


# ---------------------------------------------------------------- the driver's sysmem fallback policy

class Ran:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(gpu.sys, "platform", "win32")
    monkeypatch.setattr(gpu, "_sysmem_cache", None)


def test_off_windows_it_is_not_available_and_no_process_starts(monkeypatch):
    monkeypatch.setattr(gpu, "_sysmem_cache", None)
    monkeypatch.setattr(gpu.sys, "platform", "linux")
    monkeypatch.setattr(gpu.subprocess, "run", lambda *a, **k: pytest.fail("a process was started"))
    got = gpu.sysmem_policy()
    assert got["available"] is False and got["value"] is None and "Windows" in got["why"]


def test_the_policy_is_read_by_a_child_python_never_inside_kairo(windows, monkeypatch):
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw))
        return Ran('{"available": true, "value": 1, "off": true, "label": "prefer NO sysmem fallback", "why": null}\n')
    monkeypatch.setattr(gpu.subprocess, "run", run)
    got = gpu.sysmem_policy()
    assert got["value"] == 1 and got["off"] is True
    cmd, kw = seen[0]
    assert cmd[0] == gpu.sys.executable and cmd[1] == "-I" and cmd[2].endswith("nvsettings.py") and cmd[3:] == ["get", "--json"]
    assert kw["timeout"] <= 30 and kw["capture_output"] is True


def test_a_child_that_could_not_read_is_not_available_with_its_reason(windows, monkeypatch):
    monkeypatch.setattr(gpu.subprocess, "run", lambda cmd, **kw: Ran(
        '{"available": false, "value": null, "off": false, "label": "not available", "why": "no NVIDIA driver found"}', 3))
    got = gpu.sysmem_policy()
    assert got["available"] is False and got["why"] == "no NVIDIA driver found"


@pytest.mark.parametrize("ran", [Ran("", 1, "Traceback ..."), Ran("garbage", 0), Ran("[1]", 0), Ran("", -1073741819)])
def test_a_child_that_crashed_or_said_nonsense_is_not_available(windows, monkeypatch, ran):
    monkeypatch.setattr(gpu.subprocess, "run", lambda cmd, **kw: ran)
    got = gpu.sysmem_policy()
    assert got["available"] is False and got["value"] is None and got["why"]


def test_a_child_that_hangs_or_cannot_start_is_not_available(windows, monkeypatch):
    def hang(cmd, **kw):
        raise gpu.subprocess.TimeoutExpired(cmd, 20)
    monkeypatch.setattr(gpu.subprocess, "run", hang)
    assert "did not answer" in gpu.sysmem_policy()["why"]
    monkeypatch.setattr(gpu, "_sysmem_cache", None)
    monkeypatch.setattr(gpu.subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(OSError("no python")))
    assert gpu.sysmem_policy()["available"] is False


def test_the_answer_is_kept_for_two_minutes_and_force_asks_again(windows, monkeypatch):
    calls, now = [], [1000.0]
    monkeypatch.setattr(gpu.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(gpu.subprocess, "run", lambda cmd, **kw: calls.append(1) or Ran(
        '{"available": true, "value": %d, "off": false, "label": "x", "why": null}' % len(calls)))
    assert gpu.sysmem_policy()["value"] == 1 and gpu.sysmem_policy()["value"] == 1 and len(calls) == 1
    now[0] += gpu.SYSMEM_TTL_S - 1
    assert gpu.sysmem_policy()["value"] == 1 and len(calls) == 1
    now[0] += 2
    assert gpu.sysmem_policy()["value"] == 2
    assert gpu.sysmem_policy(force=True)["value"] == 3
