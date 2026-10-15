"""The read-only rule in a real browser (Chromium via Playwright) against a fake OLAT.
Runs in the CI browser job (KAIRO_BROWSER_TEST=1): the page's own scripts try to post,
auto-submit a form and start a test; the portal must only ever see page loads and the login."""

from __future__ import annotations

import http.server
import json
import os
import threading
import urllib.parse

import pytest
from sqlmodel import SQLModel

from app.db import make_engine
from app.portal.olat import OlatReader, PortalError

pytestmark = pytest.mark.skipif(os.environ.get("KAIRO_BROWSER_TEST") != "1",
                                reason="real browser: set KAIRO_BROWSER_TEST=1 (CI browser job)")

COURSE = '/auth/RepositoryEntry/12'
PAGES = {
    "/url/MyCoursesSite/0": f'<a href="{COURSE}">Math zz99x</a><a href="/dmz/logout">Logout</a>',
    COURSE: (f'<a href="{COURSE}/CourseNode/1"><i class="o_icon o_bc_icon"></i>Unterlagen</a>'
             f'<a href="{COURSE}/CourseNode/2"><i class="o_icon o_iqtest_icon"></i>Test</a>'
             f'<a href="{COURSE}/CourseNode/3"><i class="o_icon o_gta_icon"></i>Abgabe</a>'
             f'<a href="{COURSE}/CourseNode/5"><i class="o_icon o_info_icon"></i>Infos</a>'),
    f"{COURSE}/CourseNode/1": ('<div id="o_main_center_content">Ordner</div>'
                               '<a href="/m/abc/Skript.pdf">Skript.pdf</a><a href="/m/abc/Stundenplan_zz99x.pdf">Stundenplan_zz99x.pdf</a>'
                               f'<script>fetch("/auth/ajax", {{method: "POST", body: "x=1"}}); fetch("{COURSE}/CourseNode/2/test/start")</script>'),
    f"{COURSE}/CourseNode/5": ('<div id="o_main_center_content">Prüfung am 28.10.2026</div>'
                               '<form method="post" action="/auth/x"><input name="a" value="1"></form>'
                               '<script>document.forms[0].submit()</script>'),
}


class FakeOlat(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.seen.append(("GET", self.path))
        if self.path == "/":
            return self._send('<form method="post" action="/dmz/"><input name="o_fiooolat_login_name">'
                              '<input type="password" name="o_fiooolat_login_pass"><button>Login</button></form>')
        if "s=1" not in (self.headers.get("Cookie") or ""):
            return self._send('<form><input type="password"></form>')
        if self.path.startswith("/m/abc/"):
            return self._send(b"%PDF-1.4 fake", "application/pdf")
        return self._send(PAGES[self.path]) if self.path in PAGES else self._send("not here", code=404)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        self.seen.append(("POST", self.path))
        if self.path == "/dmz/":
            ok = urllib.parse.parse_qs(body).get("o_fiooolat_login_pass") == ["right-pw"]
            self.send_response(303)
            self.send_header("Location", "/url/MyCoursesSite/0" if ok else "/")
            if ok:
                self.send_header("Set-Cookie", "s=1; Path=/")
            self.end_headers()
            return
        self._send("posted!")


@pytest.fixture
def portal():
    FakeOlat.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOlat)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def make_reader(tmp_path, base, password, drivers):
    from app.portal.browser import PlaywrightDriver
    engine = make_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    SQLModel.metadata.create_all(engine)

    def factory():
        d = PlaywrightDriver(base)
        drivers.append(d)
        return d
    return OlatReader(engine=engine, vault_dir=tmp_path / "vault", base=base, school_class="zz99x",
                      driver_factory=factory, credentials=lambda: ("student", password))


def test_wrong_password(tmp_path, portal):
    with pytest.raises(PortalError, match="login failed"):
        make_reader(tmp_path, portal, "wrong", []).run()


def test_real_browser_only_reads(tmp_path, portal):
    drivers = []
    report = make_reader(tmp_path, portal, "right-pw", drivers).run()
    print("\nREPORT", json.dumps(report), "\nBLOCKED", drivers[0].blocked)
    posts = [p for m, p in FakeOlat.seen if m == "POST"]
    assert posts == ["/dmz/"]                                           # the login, nothing else ever
    paths = [p for _, p in FakeOlat.seen]
    assert not any("CourseNode/2" in p or "CourseNode/3" in p or "test" in p or "logout" in p for p in paths)
    assert any("/auth/ajax" in b for b in drivers[0].blocked) and any("/test/start" in b for b in drivers[0].blocked)
    assert report["new_files"] == 2 and report["timetable"]["found"] is True
    assert report["skipped"] == {"iqtest": 1, "gta": 1}
    assert (tmp_path / "vault" / "40 School" / "OLAT" / "Math zz99x" / "Skript.pdf").read_bytes().startswith(b"%PDF")
