"""python -m app.voice download | selftest   (run by the installer on kairo-home)"""

from __future__ import annotations

import sys

from ..settings import get_settings
from . import Voice


def main(argv: list[str]) -> int:
    data = get_settings().data_dir
    v = Voice(data / "models", site_dir=data.parent / "tools" / "voice")
    cmd = argv[0] if argv else "download"
    if cmd == "download":
        for item in v.download():
            print(f"downloaded {item}")
        print("voice models ready")
        return 0
    if cmd == "selftest":
        wav, lang = v.speak("Kairo voice test. One, two, three.")
        t = v.transcribe(wav, language="en")
        print(f"tts {lang} {len(wav)} bytes; stt on {v.status()['stt_device']}: {t.text!r} ({t.took}s)")
        return 0 if "test" in t.text.lower() else 1
    print("usage: python -m app.voice download|selftest", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
