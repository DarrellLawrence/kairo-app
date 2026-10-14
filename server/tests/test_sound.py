"""A1: Kairo's voice must be audible on the iPhone. The PC only sends a valid, non-silent WAV
(checked by the voice self-test), and the phone reports every playback step to the PC log
(/api/client-log), which Run diagnosis shows."""

from __future__ import annotations

import io
import logging
import math
import wave

from fastapi.testclient import TestClient

from app import diagnosis
from app.main import create_app
from app.voice import wav_problems


def tone(seconds: float, rate: int = 24000, amp: float = 0.3) -> list[float]:
    return [amp * math.sin(2 * math.pi * 220 * i / rate) for i in range(int(seconds * rate))]


def to_wav(samples: list[float], rate: int) -> bytes:
    """Like app.voice.to_wav, without numpy (the server venv has no voice libraries)."""
    import array
    pcm = array.array("h", (int(max(-1.0, min(1.0, x)) * 32767) for x in samples))
    return raw_wav(2, rate, pcm.tobytes())


def raw_wav(width: int, rate: int, frames: bytes, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def test_kairos_voice_is_a_playable_wav():
    assert wav_problems(to_wav(tone(1.0), 24000)) == []
    assert wav_problems(to_wav(tone(1.0, 22050), 22050)) == []


def test_the_real_wav_writer_passes():
    import pytest
    pytest.importorskip("numpy")             # comes with the voice libraries
    from app.voice import to_wav as real
    assert wav_problems(real(tone(1.0), 24000)) == []


def test_bad_voice_is_caught():
    assert any("silent" in p for p in wav_problems(to_wav([0.0] * 24000, 24000)))
    assert any("long" in p for p in wav_problems(to_wav(tone(0.1), 24000)))
    assert any("8-bit" in p for p in wav_problems(raw_wav(1, 24000, bytes([200, 50]) * 12000)))
    assert any("sample rate" in p for p in wav_problems(raw_wav(2, 96000, b"\x10\x27\xf0\xd8" * 48000)))
    assert any("not a WAV" in p for p in wav_problems(b"ID3\x04garbage mp3"))
    assert any("not a WAV" in p for p in wav_problems(b""))


def test_the_phone_reports_to_the_pc(migrated, caplog):
    caplog.set_level(logging.INFO, logger="kairo.client")
    with TestClient(create_app(migrated)) as c:
        r = c.post("/api/client-log", json={"events": [
            {"t": "x", "event": "audio.play", "data": {"path": "webaudio", "micOpen": False, "ios": "18.1"}},
            {"event": "audio.blocked\nFAKE 2026 ERROR injected", "data": {"why": "NotAllowedError: line1\nline2"}},
            "not an event",
        ]})
        assert r.status_code == 200 and r.json() == {"kept": 2}
        lines = [rec.getMessage() for rec in caplog.records if rec.name == "kairo.client"]
        assert lines[0] == 'audio.play {"path":"webaudio","micOpen":false,"ios":"18.1"}'
        assert "\n" not in lines[1] and lines[1].startswith("audio.blockedFAKE2026ERRORinjected ")
        assert c.post("/api/client-log", json={"events": "x"}).status_code == 400
        many = {"events": [{"event": "audio.play", "data": {}}] * 50}
        assert c.post("/api/client-log", json=many).json() == {"kept": 20}           # 20 per call
        for _ in range(10):
            c.post("/api/client-log", json=many)
        assert c.post("/api/client-log", json=many).json() == {"kept": 0}             # 120 a minute


def test_diagnosis_shows_what_the_phone_saw(settings):
    d = settings.data_dir / "logs"
    d.mkdir(parents=True)
    (d / "kairo.log").write_text(
        '2026-10-14 09:00:00,001 INFO kairo.client: audio.play {"path":"webaudio","micOpen":true,"call":true}\n'
        '2026-10-14 09:00:05,001 INFO kairo.client: audio.tap_to_play {"call":true}\n'
        '2026-10-14 09:01:00,001 INFO kairo.client: audio.check {"step":"call","heard":false}\n', encoding="utf-8")
    text = diagnosis.run(db_url=settings.db_url, data_dir=settings.data_dir, app_root=settings.data_dir)["text"]
    assert "== Phone events (sound, from the iPhone)" in text
    assert "audio.check 1, audio.play 1, audio.tap_to_play 1" in text
    assert '2026-10-14 09:01:00 audio.check {"step":"call","heard":false}' in text
