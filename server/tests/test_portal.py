"""School portal (OLAT): read-only by construction.

Hard rules (owner, 2026-10-14): never open, start, answer or submit tests, quizzes or
assignments; never click submit buttons; never post anything. The password is stored
encrypted on the PC only: never in the repo, chat, logs, the database or an API answer.
"""

from __future__ import annotations

import json
import logging
import sys

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import secretbox
from app.db import AuditLog, PortalItem, SettingKV, make_engine
from app.main import create_app
from app.portal import guard
from app.portal.olat import Link, OlatReader, PageView, PortalError, SafeDriver

BASE = "https://olat.school.example"
PASSWORD = "Sup3r-Secret-pw!"


# ---------------------------------------------------------------- guard: what may ever be loaded

@pytest.mark.parametrize("url", [
    f"{BASE}/auth/RepositoryEntry/12/CourseNode/34/test/start",
    f"{BASE}/auth/RepositoryEntry/12/iqtest/34",
    f"{BASE}/auth/RepositoryEntry/12/CourseNode/34/QTI21/run",
    f"{BASE}/auth/RepositoryEntry/12/Assessment/1",
    f"{BASE}/auth/1%3A1%3A0%3A0%3A0/submit",
    f"{BASE}/auth/RepositoryEntry/12/CourseNode/34/quiz",
    f"{BASE}/auth/RepositoryEntry/12/CourseNode/34/survey",
    f"{BASE}/auth/RepositoryEntry/12/CourseNode/34/upload",
    f"{BASE}/auth/RepositoryEntry/12/CourseNode/34/forum/new",
    f"{BASE}/auth/RepositoryEntry/12/CourseNode/34/edit",
    "https://evil.example/steal",                                   # other sites: never
])
def test_tests_quizzes_assignments_and_other_sites_are_never_loaded(url):
    assert not guard.url_allowed(url, BASE)
    assert not guard.request_allowed("GET", url, BASE, phase="read")


@pytest.mark.parametrize("url", [
    f"{BASE}/url/MyCoursesSite/0",
    f"{BASE}/auth/RepositoryEntry/12/CourseNode/34",
    f"{BASE}/m/abc123/Skript_Kapitel_3.pdf",
    f"{BASE}/raw/static/js/jquery.js",
])
def test_course_pages_and_files_are_allowed(url):
    assert guard.url_allowed(url, BASE)
    assert guard.request_allowed("GET", url, BASE, phase="read")


def test_nothing_is_ever_sent_after_login():
    """After login only page loads: no form posts, no uploads, no AJAX writes."""
    page = f"{BASE}/auth/RepositoryEntry/12/CourseNode/34"
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert not guard.request_allowed(method, page, BASE, phase="read")
    assert guard.request_allowed("POST", f"{BASE}/dmz/", BASE, phase="login")             # the login form only
    assert not guard.request_allowed("POST", "https://evil.example/", BASE, phase="login")
    assert not guard.request_allowed("POST", f"{BASE}/auth/RepositoryEntry/12/test/1", BASE, phase="login")


@pytest.mark.parametrize("classes,ok", [
    ("o_icon o_bc_icon", True),          # folder
    ("o_icon o_sp_icon", True),          # single page
    ("o_icon o_st_icon", True),          # structure
    ("o_icon o_cal_icon", True),         # calendar
    ("o_icon o_info_icon", True),        # announcements
    ("o_icon o_ll_icon", True),          # link list
    ("o_icon o_iqtest_icon", False),     # test
    ("o_icon o_iqself_icon", False),     # self test
    ("o_icon o_iqsurv_icon", False),     # survey
    ("o_icon o_gta_icon", False),        # group task / assignment
    ("o_icon o_ita_icon", False),        # task
    ("o_icon o_ms_icon", False),         # assessment
    ("o_icon o_fo_icon", False),         # forum (posting)
    ("o_icon o_en_icon", False),         # enrollment (would sign you up)
    ("o_icon o_scorm_icon", False),      # SCORM counts attempts
    ("o_icon o_newthing_icon", False),   # unknown: skipped, not guessed
    ("", False),
])
def test_only_reading_course_parts_are_opened(classes, ok):
    assert guard.node_allowed(guard.node_type(classes)) is ok


# ---------------------------------------------------------------- the reader with a fake browser

class FakeDriver:
    """A small OLAT: two courses, folders, a test, a task, an enrollment and a timetable."""

    def __init__(self, timetable=True, login_ok=True):
        self.opened, self.downloaded, self.logins = [], [], []
        self.timetable, self.login_ok = timetable, login_ok
        c = f"{BASE}/auth/RepositoryEntry"
        self.pages = {
            f"{BASE}/url/MyCoursesSite/0": PageView(url=f"{BASE}/url/MyCoursesSite/0", title="My courses", text="", links=[
                Link("Mathematik class-x", f"{c}/12", ""), Link("ABU class-x", f"{c}/13", ""),
                Link("Logout", f"{BASE}/dmz/logout", "")]),
            f"{c}/12": PageView(url=f"{c}/12", title="Mathematik", text="", links=[
                Link("Unterlagen", f"{c}/12/CourseNode/1", "o_icon o_bc_icon"),
                Link("Test Kapitel 3", f"{c}/12/CourseNode/2", "o_icon o_iqtest_icon"),
                Link("Abgabe Projekt", f"{c}/12/CourseNode/3", "o_icon o_gta_icon"),
                Link("Einschreibung", f"{c}/12/CourseNode/4", "o_icon o_en_icon"),
                Link("Infos", f"{c}/12/CourseNode/5", "o_icon o_info_icon")]),
            f"{c}/12/CourseNode/1": PageView(url=f"{c}/12/CourseNode/1", title="Unterlagen", text="", links=[
                Link("Skript_Kap3.pdf", f"{BASE}/m/x1/Skript_Kap3.pdf", "o_icon o_filetype_pdf")]
                + ([Link("Stundenplan class-x.pdf", f"{BASE}/m/x2/Stundenplan_class-x.pdf", "")] if timetable else [])),
            f"{c}/12/CourseNode/5": PageView(url=f"{c}/12/CourseNode/5", title="Infos",
                                             text="Prüfung Kapitel 3 am 28.10.2026. Bitte Taschenrechner mitnehmen.", links=[]),
            f"{c}/13": PageView(url=f"{c}/13", title="ABU", text="", links=[
                Link("Quiz", f"{c}/13/CourseNode/9", "o_icon o_iqself_icon")]),
        }

    def login(self, base, username, password):
        self.logins.append((base, username, password))
        return self.login_ok

    def open(self, url):
        self.opened.append(url)
        if url not in self.pages:
            raise PortalError(f"no page {url}")
        return self.pages[url]

    def download(self, url):
        self.downloaded.append(url)
        return b"%PDF-1.4 fake", url.rsplit("/", 1)[-1]

    def close(self):
        pass


@pytest.fixture
def env(migrated, tmp_path):
    engine = make_engine(migrated.db_url)
    yield {"engine": engine, "vault": tmp_path / "vault", "settings": migrated}
    engine.dispose()


def reader(env, driver):
    return OlatReader(engine=env["engine"], vault_dir=env["vault"], base=BASE, school_class="class-x",
                      driver_factory=lambda: driver, credentials=lambda: ("student.example", PASSWORD))


def test_reads_material_and_never_touches_tests_tasks_or_enrollment(env):
    d = FakeDriver()
    report = reader(env, d).run()
    opened = " ".join(d.opened)
    for node in ("CourseNode/2", "CourseNode/3", "CourseNode/4", "CourseNode/9"):
        assert node not in opened                                         # test, task, enrollment, quiz: never opened
    assert "CourseNode/1" in opened and "CourseNode/5" in opened
    assert "dmz/logout" not in opened
    assert (env["vault"] / "40 School" / "OLAT" / "Mathematik class-x" / "Skript_Kap3.pdf").read_bytes().startswith(b"%PDF")
    assert report["skipped"] == {"iqtest": 1, "gta": 1, "en": 1, "iqself": 1}
    assert report["new_files"] == 2 and report["timetable"]["found"] is True
    with Session(env["engine"]) as s:
        kinds = sorted(i.kind for i in s.exec(select(PortalItem)).all())
    assert kinds == ["file", "page", "timetable"]


def test_second_run_brings_only_what_is_new(env):
    d = FakeDriver()
    reader(env, d).run()
    d2 = FakeDriver()
    d2.pages[f"{BASE}/auth/RepositoryEntry/12/CourseNode/5"].text += " Neu: Abgabe am 4.11.2026."
    report = reader(env, d2).run()
    assert report["new_files"] == 0 and report["new_pages"] == 1


def test_the_safe_driver_refuses_what_the_guard_refuses(env):
    d = FakeDriver()
    safe = SafeDriver(d, BASE)
    with pytest.raises(PortalError):
        safe.open(f"{BASE}/auth/RepositoryEntry/12/CourseNode/2/test/start")
    with pytest.raises(PortalError):
        safe.download("https://evil.example/x.pdf")
    assert d.opened == [] and d.downloaded == []
    assert not hasattr(safe, "click") and not hasattr(safe, "fill") and not hasattr(safe, "post")


def test_no_timetable_asks_once(env):
    asked = []
    r = reader(env, FakeDriver(timetable=False))
    r.on_need_timetable = lambda: asked.append(1)
    assert r.run()["timetable"]["found"] is False
    reader_again = reader(env, FakeDriver(timetable=False))
    reader_again.on_need_timetable = lambda: asked.append(1)
    reader_again.run()
    assert asked == [1]


def test_wrong_password_is_a_clear_error(env):
    with pytest.raises(PortalError, match="login"):
        reader(env, FakeDriver(login_ok=False)).run()


# ---------------------------------------------------------------- the password

def test_secretbox_round_trip(tmp_path):
    path = tmp_path / "x.bin"
    secretbox.save(path, {"username": "u", "password": PASSWORD})
    assert secretbox.load(path) == {"username": "u", "password": PASSWORD}


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI exists on Windows (kairo-home and the Windows CI job)")
def test_password_is_encrypted_on_windows(tmp_path):
    path = tmp_path / "x.bin"
    secretbox.save(path, {"username": "u", "password": PASSWORD})
    raw = path.read_bytes()
    assert PASSWORD.encode() not in raw and b"Sup3r" not in raw and secretbox.encrypted(path)


@pytest.fixture
def client(migrated):
    app = create_app(migrated)
    with TestClient(app) as c:
        c.app_ = app
        yield c


def test_password_never_leaves_the_pc_or_lands_in_logs(client, migrated, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.put("/api/logins/olat", json={"username": "student.example", "password": PASSWORD})
    assert r.status_code == 200 and PASSWORD not in r.text
    listing = client.get("/api/logins").text
    assert "student.example" in listing and PASSWORD not in listing and '"password_set": true' in listing.replace(" ", "").replace(":true", ": true")
    assert PASSWORD not in caplog.text
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        dump = json.dumps([r.model_dump(mode="json") for r in s.exec(select(SettingKV)).all()]
                          + [a.model_dump(mode="json") for a in s.exec(select(AuditLog)).all()])
    engine.dispose()
    assert PASSWORD not in dump
    assert (migrated.secrets_dir / "logins" / "olat.bin").exists()
    assert client.put("/api/logins/olat", json={"username": "x"}).status_code == 400      # password required
    assert client.put("/api/logins/unknown", json={"username": "x", "password": "y"}).status_code == 404
    assert client.delete("/api/logins/olat").status_code == 200
    assert not (migrated.secrets_dir / "logins" / "olat.bin").exists()


def test_the_model_has_no_portal_or_login_tool(client):
    from app.actions import DIRECT_TOOLS, MIN_TIER
    names = " ".join(list(DIRECT_TOOLS) + list(MIN_TIER)).lower()
    assert "olat" not in names and "login" not in names and "portal" not in names and "browser" not in names
