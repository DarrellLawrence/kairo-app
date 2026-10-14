"""OLAT reader: logs in read-only, reads course material, announcements and dates,
finds the timetable, downloads files into the vault (40 School/OLAT/<course>/).

Runs as a backend job, never as a model tool. Page content is untrusted data:
it only lands in the vault and, as short titles, in the brief.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlmodel import Session, select

from ..db import PortalItem, SettingKV, utcnow
from . import guard

log = logging.getLogger("kairo.portal")

SITE = "olat"
MAX_COURSES = 30
MAX_NODES = 40
MAX_FILE = 50 * 1024 * 1024
FILE_EXT = re.compile(r"\.(pdf|docx?|pptx?|xlsx?|odt|ods|odp|txt|md|zip|png|jpe?g)$", re.I)
COURSE = re.compile(r"/RepositoryEntry/(\d+)/?$")
NODE = re.compile(r"/RepositoryEntry/(\d+)/CourseNode/(\d+)/?$")
TIMETABLE = re.compile(r"stundenplan|timetable", re.I)
ASKED_KEY = "portal.olat.timetable_asked"
REPORT_KEY = "portal.olat.report"


class PortalError(Exception):
    pass


@dataclass
class Link:
    text: str
    href: str
    classes: str = ""


@dataclass
class PageView:
    url: str
    title: str
    text: str
    links: list[Link] = field(default_factory=list)


class SafeDriver:
    """The only door to the portal: open a page by address, download a file. Every address is
    checked by the guard first. There is deliberately no click, fill or post here."""

    def __init__(self, driver, base: str):
        self._d, self._base = driver, base

    def login(self, username: str, password: str) -> bool:
        return self._d.login(self._base, username, password)

    def open(self, url: str) -> PageView:
        if not guard.url_allowed(url, self._base):
            raise PortalError(f"refused (read-only rule): {url}")
        return self._d.open(url)

    def download(self, url: str) -> tuple[bytes, str]:
        if not guard.url_allowed(url, self._base):
            raise PortalError(f"refused (read-only rule): {url}")
        return self._d.download(url)

    def close(self) -> None:
        self._d.close()


def _safe_name(text: str, limit: int = 80) -> str:
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", text or "").strip(" .")
    return (name[:limit].strip(" .") or "untitled")


class OlatReader:
    def __init__(self, *, engine, vault_dir: Path, base: str, school_class: str | None,
                 driver_factory: Callable, credentials: Callable[[], tuple[str, str] | None]):
        self.engine, self.vault_dir, self.base = engine, Path(vault_dir), base.rstrip("/")
        self.school_class = (school_class or "").lower()
        self.driver_factory, self.credentials = driver_factory, credentials
        self.on_need_timetable: Callable[[], None] | None = None

    def run(self) -> dict:
        creds = self.credentials()
        if not creds:
            raise PortalError("no OLAT login yet: add it in More -> Logins")
        drv = SafeDriver(self.driver_factory(), self.base)
        report = {"at": utcnow().isoformat(), "courses": 0, "nodes": {}, "skipped": {}, "new_files": 0, "new_pages": 0,
                  "timetable": {"found": False}, "errors": []}
        try:
            if not drv.login(*creds):
                raise PortalError("OLAT login failed: check username and password in More -> Logins")
            home = drv.open(f"{self.base}/url/MyCoursesSite/0")
            courses = {}
            for l in home.links:
                if COURSE.search(l.href) and guard.url_allowed(l.href, self.base):
                    courses.setdefault(l.href.rstrip("/"), l.text.strip() or "Course")
            report["courses"] = len(courses)
            with Session(self.engine) as s:
                for url, name in list(courses.items())[:MAX_COURSES]:
                    try:
                        self._course(s, drv, url, name, report)
                    except PortalError as exc:
                        report["errors"].append(str(exc)[:200])
                self._timetable(s, report)
                row = s.get(SettingKV, REPORT_KEY) or SettingKV(key=REPORT_KEY)
                import json
                row.value = json.dumps(report)
                s.add(row)
                s.commit()
        finally:
            drv.close()
        return report

    def _course(self, s: Session, drv: SafeDriver, url: str, name: str, report: dict) -> None:
        page = drv.open(url)
        nodes = []
        for l in page.links:
            if not NODE.search(l.href):
                continue
            kind = guard.node_type(l.classes)
            if not guard.node_allowed(kind):
                report["skipped"][kind or "unknown"] = report["skipped"].get(kind or "unknown", 0) + 1
                continue
            report["nodes"][kind] = report["nodes"].get(kind, 0) + 1
            nodes.append(l)
        for l in nodes[:MAX_NODES]:
            try:
                view = drv.open(l.href)
            except PortalError as exc:
                report["errors"].append(str(exc)[:200])
                continue
            self._page(s, name, l.text or view.title, view, report)
            for f in view.links:
                if FILE_EXT.search(f.href.split("?")[0]) or FILE_EXT.search(f.text or ""):
                    self._file(s, drv, name, f, report)

    def _page(self, s: Session, course: str, title: str, view: PageView, report: dict) -> None:
        text = (view.text or "").strip()
        if not text:
            return
        key = f"page:{view.url}"
        digest = hashlib.sha256(text.encode()).hexdigest()
        item = s.exec(select(PortalItem).where(PortalItem.site == SITE).where(PortalItem.key == key)).first()
        if item and item.digest == digest:
            return
        item = item or PortalItem(site=SITE, key=key, kind="page")
        item.course, item.title, item.digest = course, title[:200], digest
        item.snippet = text[:600]
        item.found_at, item.briefed = utcnow(), False
        s.add(item)
        s.commit()
        report["new_pages"] += 1

    def _file(self, s: Session, drv: SafeDriver, course: str, link: Link, report: dict) -> None:
        name = _safe_name(link.text if FILE_EXT.search(link.text or "") else link.href.split("?")[0].rsplit("/", 1)[-1])
        key = f"file:{course}/{name}"   # OLAT download addresses change per session: course + name is stable
        if s.exec(select(PortalItem).where(PortalItem.site == SITE).where(PortalItem.key == key)).first():
            return
        try:
            data, _ = drv.download(link.href)
        except PortalError as exc:
            report["errors"].append(str(exc)[:200])
            return
        if len(data) > MAX_FILE:
            report["errors"].append(f"too big, skipped: {name}")
            return
        rel = Path("40 School") / "OLAT" / _safe_name(course) / name
        target = (self.vault_dir / rel).resolve()
        if self.vault_dir.resolve() not in target.parents:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        kind = "timetable" if TIMETABLE.search(name) else "file"
        s.add(PortalItem(site=SITE, key=key, kind=kind, course=course, title=name, vault_path=rel.as_posix(),
                         digest=hashlib.sha256(data).hexdigest(), found_at=utcnow()))
        s.commit()
        report["new_files"] += 1

    def _timetable(self, s: Session, report: dict) -> None:
        items = s.exec(select(PortalItem).where(PortalItem.site == SITE).where(PortalItem.kind == "timetable")).all()
        mine = [i for i in items if self.school_class and self.school_class in (i.title or "").lower()] or list(items)
        if mine:
            report["timetable"] = {"found": True, "file": mine[0].vault_path}
            return
        asked = s.get(SettingKV, ASKED_KEY)
        if asked is None and self.on_need_timetable:
            self.on_need_timetable()
            s.add(SettingKV(key=ASKED_KEY, value=dt.date.today().isoformat()))
            s.commit()


def news(s: Session, limit: int = 8) -> list[dict]:
    """New OLAT material and changed pages for the brief (marked as briefed by the caller)."""
    rows = s.exec(select(PortalItem).where(PortalItem.briefed == False).order_by(PortalItem.found_at.desc())  # noqa: E712
                  .limit(limit)).all()
    return [{"id": r.id, "kind": r.kind, "course": r.course, "title": r.title, "snippet": (r.snippet or "")[:300]} for r in rows]


def mark_briefed(s: Session, ids: list[int]) -> None:
    for i in ids:
        r = s.get(PortalItem, i)
        if r:
            r.briefed = True
            s.add(r)
    s.commit()
