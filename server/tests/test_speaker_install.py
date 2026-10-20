"""The speaker check's model comes with the voice models (owner's approval 2026-10-20): one file, pinned in
voicellm.json (revision, size, sha256), downloaded and checked by the installer (voicellm.py); a failure
never stops the install. And a file the previous voice-model folder already has is copied, not downloaded."""

from __future__ import annotations

import hashlib

import pytest

from app import voicellm
from tests.test_voice_llm import fake_hub

MODEL = "wespeaker_en_voxceleb_CAM++.onnx"


def test_the_model_is_pinned_and_installed_with_the_voice_models(monkeypatch, tmp_path):
    pin = voicellm.manifest()["speaker"]
    assert pin["path"] == MODEL and len(pin["sha256"]) == 64 and len(pin["revision"]) == 40
    assert 25_000_000 < pin["size"] < 35_000_000
    fetched = fake_hub(monkeypatch)
    voicellm.download(tmp_path / "site", log_=lambda *_: None, free_bytes=lambda: 10 ** 15)
    assert (f"https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/{pin['revision']}/"
            "wespeaker_en_voxceleb_CAM%2B%2B.onnx") in fetched
    path = voicellm.speaker_path(tmp_path / "site")
    assert path is not None and path.name == MODEL
    assert voicellm.installed(tmp_path / "site")["speaker"]["sha256"] == pin["sha256"]


def test_a_speaker_model_that_fails_to_download_never_stops_the_install(monkeypatch, tmp_path):
    fake_hub(monkeypatch)
    fake = voicellm._fetch

    def flaky(url, dest, sha, size=None):
        if "speaker-embedding" in url:
            raise RuntimeError("checksum does not match")
        return fake(url, dest, sha, size)
    monkeypatch.setattr(voicellm, "_fetch", flaky)
    out = voicellm.download(tmp_path / "site", log_=lambda *_: None, free_bytes=lambda: 10 ** 15)
    assert any(s["model"] == "speaker" and "checksum" in s["why"] for s in out["skipped"])
    assert (tmp_path / "site" / voicellm.COMPLETE).is_file() and voicellm.speaker_path(tmp_path / "site") is None


def test_a_file_an_older_folder_has_is_copied_not_downloaded_again(monkeypatch, tmp_path):
    old = tmp_path / "voicellm-aaaaaaaaaaaa"
    (old / "models" / "m").mkdir(parents=True)
    (old / voicellm.COMPLETE).write_text("ok\n", encoding="ascii")
    body = b"a model file"
    (old / "models" / "m" / "x.gguf").write_bytes(body)
    new = tmp_path / "voicellm-bbbbbbbbbbbb.new"
    new.mkdir()
    assert voicellm._older_sites(new) == [old]

    def no_network(*a, **k):
        raise AssertionError("downloaded although the older folder had it")
    monkeypatch.setattr(voicellm.urllib.request, "urlopen", no_network)
    voicellm._get("https://huggingface.co/x/resolve/r/x.gguf", new, new / "models" / "m" / "x.gguf",
                  hashlib.sha256(body).hexdigest(), len(body), voicellm._older_sites(new))
    assert (new / "models" / "m" / "x.gguf").read_bytes() == body
    (old / "models" / "m" / "y.gguf").write_bytes(b"tampered")              # wrong checksum: never copied
    with pytest.raises(AssertionError, match="downloaded"):
        voicellm._get("https://huggingface.co/x/resolve/r/y.gguf", new, new / "models" / "m" / "y.gguf",
                      hashlib.sha256(b"the real one").hexdigest(), 8, voicellm._older_sites(new))
    assert not (new / "models" / "m" / "y.gguf").exists()


def test_a_reused_model_is_hard_linked_and_survives_the_old_folder(monkeypatch, tmp_path):
    """No second copy of a model of several GB while both folders exist; a copy where links don't work."""
    import shutil as _shutil
    old = tmp_path / "voicellm-aaaaaaaaaaaa"
    (old / "models" / "m").mkdir(parents=True)
    (old / voicellm.COMPLETE).write_text("ok\n", encoding="ascii")
    body = b"a big model file"
    (old / "models" / "m" / "x.gguf").write_bytes(body)
    new = tmp_path / "voicellm-bbbbbbbbbbbb.new"
    new.mkdir()
    monkeypatch.setattr(voicellm.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("downloaded")))
    dest = new / "models" / "m" / "x.gguf"
    voicellm._get("https://huggingface.co/x/resolve/r/x.gguf", new, dest, hashlib.sha256(body).hexdigest(), len(body),
                  voicellm._older_sites(new))
    assert dest.stat().st_ino == (old / "models" / "m" / "x.gguf").stat().st_ino     # one file on the disk
    _shutil.rmtree(old)                                                               # the old folder pruned
    assert dest.read_bytes() == body
    old.mkdir()                                                                       # links refused: a copy
    (old / "models" / "m").mkdir(parents=True)
    (old / voicellm.COMPLETE).write_text("ok\n", encoding="ascii")
    (old / "models" / "m" / "y.gguf").write_bytes(body)
    monkeypatch.setattr(voicellm.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("no links here")))
    voicellm._get("https://huggingface.co/x/resolve/r/y.gguf", new, new / "models" / "m" / "y.gguf",
                  hashlib.sha256(body).hexdigest(), len(body), voicellm._older_sites(new))
    assert (new / "models" / "m" / "y.gguf").read_bytes() == body
