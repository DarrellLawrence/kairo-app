"""Kairo's headless browser for the school portal (Playwright + Chromium, KICKOFF: web browsing).

Installed by the installer into C:\\Kairo\\tools\\browser (requirements-browser.txt), like voice.
Every request goes through the guard: after login only page loads on the portal's own site,
never a test/quiz/assignment address. The only typing is the login form.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from . import guard
from .olat import Link, PageView, PortalError

log = logging.getLogger("kairo.portal")

LINKS_JS = """els => els.map(e => ({
  text: (e.innerText || e.title || '').trim().slice(0, 200),
  href: e.href || '',
  classes: [e.className || '', ...Array.from(e.querySelectorAll('i,span')).map(i => i.className || '')].join(' ')
}))"""
USER_FIELDS = ['input[name="o_fiooolat_login_name"]', 'input[autocomplete="username"]', 'input[name*="user" i]',
               'input[type="email"]', 'input[type="text"]']
PASS_FIELDS = ['input[name="o_fiooolat_login_pass"]', 'input[type="password"]']
TIMEOUT_MS = 30_000


def available(site_dir: Path | None = None) -> bool:
    _use_site(site_dir)
    from ..tool_sites import is_real_package
    return is_real_package("playwright")[0]


def selftest(site_dir: Path | None = None) -> dict:
    """For the installer and the boot check: import Playwright as Kairo's account and start Chromium once."""
    _use_site(site_dir)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, executable_path=os.environ.get("KAIRO_BROWSER_EXECUTABLE") or None)
            page = b.new_page()
            page.set_content("<p>ok</p>")
            ok = page.inner_text("p") == "ok"
            b.close()
        return {"ok": ok, "reason": "" if ok else "Chromium did not render"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:240]}


def _use_site(site_dir: Path | None) -> None:
    if site_dir and Path(site_dir).is_dir():
        if str(site_dir) not in sys.path:
            sys.path.append(str(site_dir))
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(site_dir) / "ms-playwright"))


class PlaywrightDriver:
    def __init__(self, base: str, site_dir: Path | None = None):
        _use_site(site_dir)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PortalError("Kairo's browser is not installed on the PC (run Update Kairo)") from exc
        self.base = base
        self.phase = "login"
        self.blocked: list[str] = []
        self._pw = sync_playwright().start()
        exe = os.environ.get("KAIRO_BROWSER_EXECUTABLE") or None
        self._browser = self._pw.chromium.launch(headless=True, executable_path=exe)
        self._ctx = self._browser.new_context(accept_downloads=False, locale="de-CH")
        self._ctx.set_default_timeout(TIMEOUT_MS)
        self._ctx.route("**/*", self._route)
        self.page = self._ctx.new_page()

    def _route(self, route) -> None:
        req = route.request
        if guard.request_allowed(req.method, req.url, self.base, phase=self.phase):
            route.continue_()
        else:
            self.blocked.append(f"{req.method} {req.url[:120]}")
            route.abort()

    def login(self, base: str, username: str, password: str) -> bool:
        self.phase = "login"
        p = self.page
        p.goto(base, wait_until="domcontentloaded")
        user = next((p.locator(s).first for s in USER_FIELDS if p.locator(s).count()), None)
        pw = next((p.locator(s).first for s in PASS_FIELDS if p.locator(s).count()), None)
        if user is None or pw is None:
            raise PortalError("OLAT login form not found (maybe the school uses a single sign-on page now)")
        user.fill(username)
        pw.fill(password)
        pw.press("Enter")   # the login form is the only form Kairo ever sends
        try:
            p.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
        except Exception:
            pass
        ok = p.locator('input[type="password"]').count() == 0
        self.phase = "read"   # from here on: page loads only
        return ok

    def open(self, url: str) -> PageView:
        if self.phase != "read":
            raise PortalError("not logged in")
        p = self.page
        p.goto(url, wait_until="domcontentloaded")
        try:
            p.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        main = p.locator("#o_main_center_content_inner, #o_main_center_content, main, body").first
        text = main.inner_text() if main.count() else ""
        links = [Link(**l) for l in p.eval_on_selector_all("a[href]", LINKS_JS)]
        return PageView(url=p.url, title=p.title(), text=text[:20_000], links=links)

    def download(self, url: str) -> tuple[bytes, str]:
        if self.phase != "read" or not guard.request_allowed("GET", url, self.base, phase="read"):
            raise PortalError(f"refused: {url}")
        r = self._ctx.request.get(url, max_redirects=5)   # not routed: checked here, before and after redirects
        if not guard.url_allowed(r.url, self.base):
            raise PortalError(f"refused after redirect: {r.url[:120]}")
        if not r.ok:
            raise PortalError(f"download failed ({r.status}): {url.rsplit('/', 1)[-1]}")
        return r.body(), url.split("?")[0].rsplit("/", 1)[-1]

    def close(self) -> None:
        for fn in (self._ctx.close, self._browser.close, self._pw.stop):
            try:
                fn()
            except Exception:
                pass
