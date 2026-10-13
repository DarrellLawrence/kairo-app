import shutil

import pytest

from app.profile import load_profile, render_personality
from app.settings import REPO_ROOT


def test_personality_renders_from_profile():
    profile = load_profile(REPO_ROOT / "config")
    text = render_personality(REPO_ROOT / "config", profile)
    assert "{{" not in text
    assert profile.name in text
    assert profile.context in text


def test_unknown_placeholder_fails_loudly(tmp_path):
    shutil.copy(REPO_ROOT / "config" / "profile.yaml", tmp_path / "profile.yaml")
    (tmp_path / "personality.md").write_text("Hi {{nickname}}")
    with pytest.raises(KeyError):
        render_personality(tmp_path, load_profile(tmp_path))
