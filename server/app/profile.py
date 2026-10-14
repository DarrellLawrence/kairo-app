"""Owner profile (config/profile.yaml) and the personality prompt built from it."""

from __future__ import annotations

import re
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel


class QuietHours(BaseModel):
    start: str
    end: str


class Profile(BaseModel):
    name: str
    default_language: str
    languages: list[str]
    timezone: str
    context: str
    gmail: str | None = None
    quiet_hours: QuietHours
    push_devices: list[str] = []
    places: list[dict] = []   # seeds Settings -> Places on first start
    school_portal: dict | None = None   # {kind: olat, url, class}: read-only, login in More -> Logins

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_profile(config_dir: Path) -> Profile:
    data = yaml.safe_load((config_dir / "profile.yaml").read_text(encoding="utf-8"))
    return Profile.model_validate(data)


_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_personality(config_dir: Path, profile: Profile) -> str:
    """Fill {{placeholders}} in config/personality.md from the profile.

    Unknown placeholders are an error: a silently empty prompt field is worse
    than a crash at startup.
    """
    template = (config_dir / "personality.md").read_text(encoding="utf-8")
    template = re.sub(r"^<!--\s*kairo-personality v\d+\s*-->\n?", "", template)   # its version (self-test), not prompt text
    values = profile.model_dump()

    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values or values[key] is None:
            raise KeyError(f"personality.md uses {{{{{key}}}}} but profile.yaml has no value for it")
        return str(values[key])

    return _PLACEHOLDER.sub(sub, template)
