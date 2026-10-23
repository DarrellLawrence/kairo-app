"""G11 in the running app: the router is wired into the runner and the jobs, the people list feeds the scanner, and
GET /api/router (More -> Usage -> Routes) is owner only and never carries a prompt. Made-up data only."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlmodel import Session

from app import flags, router
from app.brain import local_llm
from app.db import Person, make_engine
from app.main import create_app


def set_flag(settings, name: str, on: bool = True) -> None:
    with Session(make_engine(settings.db_url)) as s:
        flags.set_flag(s, name, on)


def test_the_app_wires_the_router_into_the_runner_and_the_jobs(migrated):
    app = create_app(migrated)
    with TestClient(app):
        assert isinstance(app.state.router, router.Router)
        assert app.state.runner.router is app.state.router and app.state.jobs.router is app.state.router
        assert app.state.jobs.local is not None and app.state.jobs.local.ready() is False     # no voice model in tests
        assert app.state.jobs.local.fingerprint() == ""                                       # none running: not any model
        assert app.state.router.rules is not None and app.state.router.rules.providers["claude"].enabled
        assert app.state.jobs.call_open() is False
        app.state.runner.line_open()
        assert app.state.jobs.call_open() is True


def test_the_local_route_is_off_in_the_shipped_app_for_every_purpose(migrated):
    """Both flags on, the model 'ready': still the subscription, because no purpose has passed the lab."""
    app = create_app(migrated)
    with TestClient(app):
        set_flag(migrated, "g11.router")
        set_flag(migrated, "g11.local_route")
        for purpose in ("triage", "reply_check", "summary", "chat"):
            d = app.state.router.route(purpose, "a mail", local_ready=True, local_model="anything")
            assert d.route == "claude", purpose


def test_people_in_the_database_are_names_the_scanner_looks_for(migrated):
    app = create_app(migrated)
    with TestClient(app):
        with Session(make_engine(migrated.db_url)) as s:
            s.add(Person(name="Maja Beispiel"))
            s.commit()
        set_flag(migrated, "g11.router")
        d = app.state.router.route("selftest", "ask Maja about it")
        assert d.content == "personal" and d.hits == ("name",)
        d = app.state.router.route("selftest", "what time is it")
        assert d.content == "public"


def test_the_routes_screen_is_empty_and_says_the_router_is_off_by_default(migrated):
    with TestClient(create_app(migrated)) as c:
        v = c.get("/api/router").json()
        assert v["router_on"] is False and v["local_on"] is False and v["rules_loaded"] is True
        assert v["total"] == 0 and v["recent"] == [] and v["problems"] == []
        assert len(v["ceilings"]) == 33 and v["ceilings"]["selftest"] == "public"
        names = {p["name"]: p["status"] for p in v["providers"]}
        assert names["claude"] == "on" and names["groq"].startswith("off") and names["local"].startswith("off")
        assert v["local"]["running_model"] == "" and v["local"]["purposes"] == []


def test_the_routes_screen_lists_the_routes_taken_with_their_reasons_and_never_the_prompt(migrated):
    app = create_app(migrated)
    with TestClient(app) as c:
        set_flag(migrated, "g11.router")
        app.state.router.route("triage", "mail from anna.example@mail.example, IBAN CH93 0076 2011 6238 5295 7", mode="job", model="haiku")
        app.state.router.route("chat", "hello", mode="owner", model="haiku", tools=True)
        v = c.get("/api/router").json()
        assert v["router_on"] is True and v["total"] == 2 and v["by_route"] == {"claude": 2}
        first, second = v["recent"]
        assert (first["purpose"], second["purpose"]) == ("chat", "triage")              # newest first
        assert second["content"] == "secret" and {"email", "iban"} <= set(second["hits"]) and second["route"] == "claude"
        assert "default" in second["reason"] and second["tier"] == "R1"
        body = json.dumps(v)
        assert "anna.example" not in body and "CH93" not in body and "mail from" not in body
        assert c.get("/api/router?days=0").status_code == 200 and c.get("/api/router?days=100000").json()["days"] == 90
        assert c.get("/api/router?days=1").json()["days"] == 1


def test_the_routes_screen_shows_the_model_that_is_running_so_it_can_be_copied_into_the_file(migrated, tmp_path):
    app = create_app(migrated)
    model_file = tmp_path / "m-Q4_K_M.gguf"
    model_file.write_bytes(b"x" * 64)
    with TestClient(app) as c:
        app.state.jobs.local._identify = lambda: local_llm.fingerprint("lfm2.5-2.6b", model_file)
        v = c.get("/api/router").json()
        assert len(v["local"]["running_model"]) == 12 and v["local"]["running_model_is_lab_model"] is False
