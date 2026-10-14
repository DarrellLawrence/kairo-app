"""Document gauntlet (v7 item 11): create -> render every page/slide to an image -> Claude critiques
the images -> fix, max 3 rounds on Sonnet. No generic AI look. Final PDF to the vault and the
owner's own Gmail."""

from __future__ import annotations

import json
import os

import pytest
from sqlmodel import Session

from app import gauntlet
from app.db import Document, make_engine
from app.executor import Executor
from app.gate import load_gate
from app.settings import REPO_ROOT
from tests.fakes.fake_google import FakeGoogle, connect, decode_raw
from app.chat import AskResult

PAGE1 = '<section class="page"><h1>Thermo — summary</h1><p>Energy is conserved.</p></section>'
PAGE2 = '<section class="page"><h2>Formulas</h2><ul><li>Q = m c ΔT</li></ul></section>'


class Ctx:
    """Fake model: answers by purpose, records prompts and how many images each call saw."""

    def __init__(self, s, critiques):
        self.session, self.critiques, self.calls = s, list(critiques), []

    def ask(self, prompt, system, images=None, purpose=None):
        self.calls.append((purpose, prompt, len(images or [])))
        if purpose == "gauntlet":
            return AskResult(json.dumps(self.critiques.pop(0)))
        if "Fix these problems" in prompt:
            return AskResult(json.dumps({"title": "Thermo summary", "html": PAGE1 + PAGE2 + f"<!-- fix {len(self.calls)} -->"}))
        return AskResult(json.dumps({"title": "Thermo summary", "html": PAGE1 + PAGE2}))


class FakeRenderer:
    def __init__(self, overflow=()):
        self.overflow, self.seen = list(overflow), []

    def render(self, html, kind):
        self.seen.append(html)
        n = html.count('class="page"') or html.count('class="slide"')
        return {"pages": [b"\x89PNG page"] * n, "pdf": b"%PDF-1.7 fake", "overflow": self.overflow}


@pytest.fixture
def env(migrated):
    fake = FakeGoogle(owner="owner@example.com")
    secrets = migrated.data_dir / "secrets"
    connect(secrets, fake)
    engine = make_engine(migrated.db_url)
    vault = migrated.data_dir / "vault"
    (vault / "40 School" / "Thermo").mkdir(parents=True)
    (vault / "40 School" / "Thermo" / "notes.md").write_text("First law: energy is conserved.", encoding="utf-8")
    pushes = []
    yield {"engine": engine, "fake": fake, "vault": vault, "pushes": pushes,
           "executor": lambda: Executor(secrets_dir=secrets, transport=fake.transport(), owner_email="owner@example.com",
                                        vault_dir=vault)}
    engine.dispose()


def make(env, renderer):
    return gauntlet.Gauntlet(engine=env["engine"], gate=load_gate(REPO_ROOT / "config"), executor=env["executor"],
                             vault_dir=env["vault"], renderer=renderer, language="English",
                             notify=lambda body: env["pushes"].append(body))


def queue(s, **kw):
    d = Document(kind="document", title="Thermo summary", brief="One page, key formulas",
                 sources_json=json.dumps(["40 School/Thermo/notes.md"]), label="Kairo/School", **kw)
    s.add(d)
    s.commit()
    s.refresh(d)
    return d.id


def test_sanitize_keeps_structure_drops_anything_active():
    dirty = ('<section class="page" onclick="x()"><h1 style="color:red">Hi</h1><script>alert(1)</script>'
             '<img src="http://evil.example/t.png"><a href="http://evil.example">link text</a>'
             '<iframe src="x"></iframe><p class="lead">Text</p></section>')
    clean = gauntlet.sanitize(dirty)
    assert clean == '<section class="page"><h1>Hi</h1>link text<p class="lead">Text</p></section>'


def test_good_after_one_fix(env):
    r = FakeRenderer()
    with Session(env["engine"]) as s:
        did = queue(s)
        ctx = Ctx(s, [{"verdict": "fix", "issues": ["Page 2 is half empty", "Title looks generic"]},
                      {"verdict": "good", "issues": []}])
        out = make(env, r).run(ctx, did)
        doc = s.get(Document, did)
    assert out["rounds"] == 2 and doc.status == "done"
    purposes = [p for p, _, _ in ctx.calls]
    assert purposes == ["document", "gauntlet", "document", "gauntlet"]
    assert all(n == 2 for p, _, n in ctx.calls if p == "gauntlet")                 # the critic sees every page
    assert "energy is conserved" in ctx.calls[0][1].lower()                        # sources read from the vault
    assert "Page 2 is half empty" in ctx.calls[2][1]                               # the fix gets the critique
    pdf = env["vault"] / doc.vault_path
    assert pdf.read_bytes().startswith(b"%PDF") and doc.vault_path.startswith("Kairo/Documents/")
    msg = decode_raw(env["fake"].sent[-1]["raw"])
    assert msg["To"] == "owner@example.com"
    assert [a.get_filename() for a in msg.iter_attachments()] == [pdf.name]
    assert env["pushes"] and "Thermo summary" in env["pushes"][-1]
    assert "—" not in r.seen[-1]                                                    # no em dashes in the final


def test_three_rounds_at_most(env):
    r = FakeRenderer()
    with Session(env["engine"]) as s:
        did = queue(s)
        ctx = Ctx(s, [{"verdict": "fix", "issues": [f"problem {i}"]} for i in range(5)])
        out = make(env, r).run(ctx, did)
        doc = s.get(Document, did)
    assert out["rounds"] == 3
    assert [p for p, _, _ in ctx.calls].count("gauntlet") == 3
    assert json.loads(doc.issues_left_json) == ["problem 2"] and doc.status == "done"


def test_overflow_is_told_to_the_critic(env):
    with Session(env["engine"]) as s:
        did = queue(s)
        ctx = Ctx(s, [{"verdict": "good", "issues": []}] * 3)
        out = make(env, FakeRenderer(overflow=[2])).run(ctx, did)
    critique = [p for purpose, p, _ in ctx.calls if purpose == "gauntlet"][0]
    assert "Page 2 overflows" in critique
    assert out["rounds"] == 3          # "good" doesn't count while text is still cut off


def test_model_failure_marks_the_document_failed(env):
    class Down(Ctx):
        def ask(self, prompt, system, images=None, purpose=None):
            return AskResult("", status="error", error="usage limit")
    with Session(env["engine"]) as s:
        did = queue(s)
        with pytest.raises(gauntlet.GauntletError):
            make(env, FakeRenderer()).run(Down(s, []), did)
        assert s.get(Document, did).status == "error"
    assert env["fake"].sent == []


@pytest.mark.skipif(os.environ.get("KAIRO_BROWSER_TEST") != "1", reason="real browser: CI browser job")
def test_chromium_renders_pages_offline(tmp_path):
    html = gauntlet.page_html(PAGE1 + PAGE2 + '<section class="page">' + "<p>long</p>" * 400 + "</section>", "document", "T")
    out = gauntlet.ChromiumRenderer().render(html, "document")
    assert len(out["pages"]) == 3 and all(p[:4] == b"\x89PNG" for p in out["pages"])
    assert out["pdf"][:4] == b"%PDF" and out["overflow"] == [3]
