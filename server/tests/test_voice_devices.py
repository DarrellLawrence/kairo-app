"""Owner's request 2026-10-17: Voice speed shows where each part of the voice runs; a part off the GPU is
red with its reason, never a silent CPU fallback. And which line the calls use."""

from __future__ import annotations

from tests.test_voice_local import local  # noqa: F401  (the app with the fake voice model)


class DeviceVoice:
    def __init__(self, inner, devices):
        self.inner, self._devices = inner, devices

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def devices(self):
        return self._devices


def test_each_part_says_where_it_runs_and_why_not_the_gpu(local, monkeypatch):
    from app import gpu
    monkeypatch.setattr(gpu, "summary", lambda: {"name": "NVIDIA GeForce RTX 3060", "driver": "580.1", "used_mb": 7000,
                                                 "total_mb": 12288, "processes": [{"pid": 4242, "name": "llama-server.exe",
                                                                                   "used_mb": 3100}]})
    local.app.state.voice = DeviceVoice(local.voice, {
        "whisper": {"device": "cpu", "model": "small", "loaded": True, "ok": False,
                    "reason": "on the CPU: RuntimeError: Library cublas64_12.dll is not found"},
        "kokoro": {"device": "cuda", "model": "kokoro-v1.0.fp16.onnx", "loaded": True, "ok": True, "reason": None},
        "piper": {"device": "cpu", "loaded": False, "ok": True, "reason": None}})
    d = local.get("/api/voice/devices").json()
    c = d["components"]
    assert c["whisper"]["ok"] is False and "cublas64_12.dll" in c["whisper"]["reason"]
    assert c["kokoro"]["ok"] is True
    assert c["voice_model"]["state"] == "ready" and c["voice_model"]["model"] == "lfm2.5-2.6b"
    assert d["all_on_gpu"] is False
    assert d["gpu"]["name"].endswith("3060") and d["gpu"]["processes"][0]["used_mb"] == 3100
    assert set(d["line"]) >= {"webrtc_ready", "reason", "last_turn"}
    text = local.post("/api/diagnosis").json()["text"]
    assert "RED whisper" in text and "cublas64_12.dll" in text and "ok kokoro" in text


def test_the_line_of_the_last_spoken_turn_is_shown(local):
    body = {"turn_id": "x1", "path": "call", "marks": {"playing": 900}, "outcome": "played",
            "server": {"first_audio_at_ms": 700, "turn_at_ms": 300, "first_token_at_ms": 500},
            "device": {"line": "webrtc", "net": "direct"}}
    assert local.post("/api/voice/turns", json=body).status_code == 200
    assert local.get("/api/voice/devices").json()["line"]["last_turn"] == "webrtc"
