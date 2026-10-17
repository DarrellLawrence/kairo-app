"""CI (voice-install-windows): the voice folder exactly as the installer builds it on kairo-home
(packages in their own folder, appended after Kairo's own venv), then what the release self-test does
with it: Kokoro speaks, Whisper hears it, Piper speaks German; the WebRTC packages import from there;
and where each part ended up (on a runner without an NVIDIA card: the CPU, with the reason).

python tools/voice_site_check.py <site> <models>   (exit 1 on a failure)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # server\: the app package, like Kairo's own start


def main(site: str, models: str) -> int:
    from app.voice import Voice
    v = Voice(Path(models), site_dir=Path(site))
    report = v.selftest()
    devices = v.devices()
    import onnxruntime as ort
    from app import voicertc
    out = {"selftest": report, "devices": devices, "onnxruntime": ort.__version__,
           "providers": ort.get_available_providers(), "webrtc": voicertc.available()}
    print(json.dumps(out, indent=1, default=str))
    problems = []
    if not report.get("ok"):
        problems.append(f"voice self-test: {report.get('reason')}")
    if not out["webrtc"]:
        problems.append("pipecat/aiortc don't import from the voice folder")
    if sys.platform == "win32" and "CUDAExecutionProvider" not in out["providers"]:
        problems.append("the voice folder has the CPU build of ONNX Runtime, not onnxruntime-gpu")
    for name in ("whisper", "kokoro"):
        if not devices[name]["loaded"]:
            problems.append(f"{name} did not load: {devices[name].get('reason')}")
        elif not devices[name]["ok"] and not devices[name].get("reason"):
            problems.append(f"{name} is off the GPU without a reason")   # never a silent CPU fallback
    for p in problems:
        print("FAIL:", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
