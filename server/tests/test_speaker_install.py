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


# ---------------------------------------------------------------- the turn detector, pinned like the speaker model (recipe 2)

TURN_PIN = {"repo": "pipecat-ai/smart-turn-v3", "revision": "f" * 40, "path": "smart-turn-v3.2-cpu.onnx", "size": 8_679_182,
            "sha256": "2" * 64}


def test_the_turn_detector_is_fetched_by_its_pin_and_the_hub_is_never_asked_which_is_newest(monkeypatch, tmp_path):
    asked, fetched = [], []
    monkeypatch.setattr(voicellm, "_get_json", lambda url: asked.append(url) or (_ for _ in ()).throw(AssertionError(url)))
    monkeypatch.setattr(voicellm, "_fetch", lambda url, dest, sha, size=None: (fetched.append((url, sha, size)),
                                                                                dest.parent.mkdir(parents=True, exist_ok=True),
                                                                                dest.write_bytes(b"x")))
    got = voicellm._smart_turn(tmp_path / "site", TURN_PIN, [])
    assert asked == []                                                       # no /api/models, no /tree, no regex pick
    assert fetched == [(f"https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/{'f' * 40}/smart-turn-v3.2-cpu.onnx",
                        "2" * 64, 8_679_182)]
    assert got == {"repo": "pipecat-ai/smart-turn-v3", "revision": "f" * 40, "file": "smart-turn/smart-turn-v3.2-cpu.onnx",
                   "sha256": "2" * 64, "size": 8_679_182}


def test_the_speaker_and_the_turn_detector_share_one_pinned_download(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(voicellm, "_get", lambda url, to, dest, sha, size, older: seen.append((url, dest.relative_to(to).as_posix(), sha, size)))
    voicellm._pinned(tmp_path, {**TURN_PIN, "path": "a b/x.onnx"}, "somewhere", [])
    assert seen == [(f"https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/{'f' * 40}/a%20b/x.onnx", "somewhere/x.onnx",
                     "2" * 64, 8_679_182)]
    pin = voicellm.manifest()["speaker"]
    assert voicellm._speaker(tmp_path, pin, [])["file"] == "speaker/" + pin["path"]


def test_a_manifest_without_a_turn_pin_still_downloads_the_newest_cpu_build(monkeypatch, tmp_path):
    """Until voicellm.json carries the pin, the installer behaves as before (the pin is a separate, owner-gated step)."""
    fake_hub(monkeypatch)
    old_shape = {"repo": "pipecat-ai/smart-turn-v3", "pattern": "(?i)cpu.*\\.onnx$"}
    assert voicellm._smart_turn(tmp_path / "site", old_shape, [])["file"] == "smart-turn/smart-turn-v3.2-cpu.onnx"


def test_a_turn_detector_that_cannot_be_fetched_is_skipped_not_fatal(monkeypatch, tmp_path):
    fake_hub(monkeypatch)
    monkeypatch.setattr(voicellm, "manifest", lambda: {**voicellm_manifest(), "smart_turn": TURN_PIN})
    fake = voicellm._fetch

    def flaky(url, dest, sha, size=None):
        if "smart-turn" in url:
            raise RuntimeError("checksum does not match")
        return fake(url, dest, sha, size)
    monkeypatch.setattr(voicellm, "_fetch", flaky)
    out = voicellm.download(tmp_path / "site", log_=lambda *_: None, free_bytes=lambda: 10 ** 15)
    assert any(s["model"] == "smart-turn" and "checksum" in s["why"] for s in out["skipped"])
    assert out["smart_turn"] is None and (tmp_path / "site" / voicellm.COMPLETE).is_file()


def voicellm_manifest():
    import json
    return json.loads(voicellm.MANIFEST.read_text(encoding="utf-8"))


def installed_turn(tmp_path, monkeypatch, *, sha=TURN_PIN["sha256"], revision=TURN_PIN["revision"], pinned=True):
    """A complete folder whose installed.json says it holds an 8-byte turn-detector file; voicellm.json pins an 8-byte one."""
    import json
    site = tmp_path / "turn-site"
    f = site / "smart-turn" / "smart-turn-v3.2-cpu.onnx"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x" * 8)
    (site / voicellm.INSTALLED).write_text(json.dumps({"smart_turn": {"file": "smart-turn/smart-turn-v3.2-cpu.onnx", "revision": revision,
                                                                       "sha256": sha, "size": 8}}), encoding="utf-8")
    (site / voicellm.COMPLETE).write_text("ok\n", encoding="ascii")
    cfg = {**TURN_PIN, "size": 8} if pinned else {"repo": "x", "pattern": "y"}
    monkeypatch.setattr(voicellm, "manifest", lambda: {**voicellm_manifest(), "smart_turn": cfg})
    return site


def test_calls_may_load_the_downloaded_turn_detector_only_when_it_is_the_pinned_file(monkeypatch, tmp_path):
    site = installed_turn(tmp_path / "a", monkeypatch)
    assert voicellm.smart_turn_pinned_path(site) == site / "smart-turn" / "smart-turn-v3.2-cpu.onnx"
    other_file = installed_turn(tmp_path / "b", monkeypatch, sha="3" * 64)
    assert voicellm.smart_turn_pinned_path(other_file) is None
    other_revision = installed_turn(tmp_path / "c", monkeypatch, revision="e" * 40)
    assert voicellm.smart_turn_pinned_path(other_revision) is None
    cut = installed_turn(tmp_path / "d", monkeypatch)
    (cut / "smart-turn" / "smart-turn-v3.2-cpu.onnx").write_bytes(b"cut")               # cut short since it was checked
    assert voicellm.smart_turn_pinned_path(cut) is None
    no_pin_yet = installed_turn(tmp_path / "e", monkeypatch, pinned=False)               # voicellm.json has no pin yet
    assert voicellm.smart_turn_pinned_path(no_pin_yet) is None
    assert voicellm.smart_turn_pinned_path(tmp_path / "nothing") is None                 # not installed


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
