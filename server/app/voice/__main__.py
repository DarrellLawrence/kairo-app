"""python -m app.voice download [--to <models folder>] | selftest   (run by the installer on kairo-home)

Fix F24: the installer passes --to <tools\\voice-<hash>\\models> (admins only), never data\\."""

from __future__ import annotations

import sys
from pathlib import Path

from ..settings import get_settings
from . import Voice, models_dir


def main(argv: list[str]) -> int:
    data = get_settings().data_dir
    site = data.parent / "tools" / "voice"
    to = None
    if "--to" in argv:
        i = argv.index("--to")
        to, argv = Path(argv[i + 1]), argv[:i] + argv[i + 2:]
    v = Voice(to or models_dir(data, site), site_dir=site)
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
