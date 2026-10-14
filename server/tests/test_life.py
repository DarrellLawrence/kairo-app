"""Phase 3 life data: shopping, chores, people, money (read only), health; model tools; brief."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import life
from app.db import make_engine
from app.main import create_app
from tests.test_internal_tools import setup  # noqa: F401  (fixture)

CAMT = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt><Stmt>
<Ntry><Amt Ccy="CHF">54.30</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-10-10</Dt></BookgDt><AcctSvcrRef>R1</AcctSvcrRef>
 <NtryDtls><TxDtls><RltdPties><Cdtr><Pty><Nm>MIGROS ZUERICH</Nm></Pty></Cdtr></RltdPties></TxDtls></NtryDtls></Ntry>
<Ntry><Amt Ccy="CHF">1200.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-10-01</Dt></BookgDt><AcctSvcrRef>R2</AcctSvcrRef>
 <AddtlNtryInf>Lohn Oktober</AddtlNtryInf></Ntry>
<Ntry><Amt Ccy="CHF">9.90</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-10-11</Dt></BookgDt><AcctSvcrRef>R3</AcctSvcrRef>
 <NtryDtls><TxDtls><RmtInf><Ustrd>SPOTIFY P12345</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
</Stmt></BkToCstmrStmt></Document>"""


@pytest.fixture
def client(migrated):
    with TestClient(create_app(migrated)) as c:
        c.key = {"Authorization": f"Bearer {c.get('/api/capture/setup').json()['token']}"}
        yield c


def test_amounts():
    assert life.parse_amount("CHF 12.50") == 12.5 and life.parse_amount("1'234.50") == 1234.5
    assert life.parse_amount("12,50 €") == 12.5 and life.parse_amount(7) == 7.0
    with pytest.raises(ValueError):
        life.parse_amount("free")


def test_camt_import_is_idempotent_and_safe(client):
    r = client.post("/api/finance/import", content=CAMT, headers={"Content-Type": "application/xml"}).json()
    assert r == {"added": 3, "skipped": 0}
    assert client.post("/api/finance/import", content=CAMT).json() == {"added": 0, "skipped": 3}
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    assert client.post("/api/finance/import", content=bomb).status_code == 400
    m = client.get("/api/finance?days=366").json()
    assert m["by_category"] == {"groceries": 54.3, "subscriptions": 9.9} and m["income_chf"] == 1200.0


def test_wallet_trigger_needs_the_key(client):
    body = {"amount": "CHF 4.80", "merchant": "Starbucks Bahnhof", "card": "UBS Visa"}
    assert client.post("/api/finance/transaction", json=body).status_code == 401
    r = client.post("/api/finance/transaction", json=body, headers=client.key).json()
    assert r == {"ok": True, "stored": True, "category": "eating out"}
    assert client.post("/api/finance/transaction", json={"merchant": "x"}, headers=client.key).status_code == 400
    assert client.get("/api/finance").json()["recent"][0]["amount"] == -4.8


def test_health_shortcut(client):
    assert client.post("/api/health", json={"steps": 9000}).status_code == 401
    client.post("/api/health", json={"date": "2026-10-13", "steps": "9'120", "sleep_h": 6.5, "resting_hr": 58}, headers=client.key)
    assert client.post("/api/health", json={"date": "2026-10-13", "sleep_h": 30}, headers=client.key).status_code == 400
    h = client.get("/api/health/summary?days=366").json()
    assert h["daily"][0]["steps"] == 9120 and h["avg"]["sleep_h"] == 6.5


def test_shopping_chores_people(client):
    client.post("/api/shopping", json={"items": ["Milch", "milch", "Brot"]})
    items = client.get("/api/shopping").json()
    assert [i["text"] for i in items] == ["Milch", "Brot"]
    client.post(f"/api/shopping/{items[0]['id']}/toggle")
    assert client.post("/api/shopping/clear-done").json() == {"removed": 1}
    c = client.post("/api/chores", json={"title": "Bad putzen", "every_days": 7}).json()
    assert c["due"] is True
    assert client.post(f"/api/chores/{c['id']}/done").json()["due"] is False
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).replace(year=2000)
    client.post("/api/people", json={"name": "Anna", "relation": "friend", "birthday": tomorrow.isoformat(), "contact_every_days": 14})
    p = client.get("/api/people").json()
    assert p["birthdays"] == [{"name": "Anna", "in_days": 1}] and p["reach_out"][0]["name"] == "Anna"
    client.post("/api/people", json={"name": "anna", "contacted": True})
    assert client.get("/api/people").json()["reach_out"] == []


def test_model_tools_work_through_the_gate(setup):
    call = lambda n, a: setup["call"](n, a).json()  # noqa: E731
    assert "Milch" in call("shopping_add", {"items": ["Milch"]})["text"]
    assert json.loads(call("shopping_list", {})["text"]) == ["Milch"]
    r = json.loads(call("people_note", {"name": "Lea", "note": "New job in November", "contacted": True})["text"])
    assert r["name"] == "Lea" and "New job" in r["notes"]
    assert json.loads(call("finance_list", {"days": 30})["text"])["spent_chf"] == 0
    assert call("finance_list", {"days": 30, "pay": "x"})["is_error"]
    assert json.loads(call("health_list", {"days": 7})["text"])["daily"] == []


def test_brief_and_capture_use_life_data(migrated):
    from app.chat import AskResult
    app = create_app(migrated)
    with TestClient(app) as c:
        c.post("/api/chores", json={"title": "Müll raus", "every_days": 3})
        c.post("/api/people", json={"name": "Ben", "birthday": dt.date.today().replace(year=2001).isoformat()})
        app.state.jobs._ask = lambda p, s, images=None: AskResult("not json")
        assert app.state.jobs.run_job("morning_brief", forced=True).status == "ok"
        b = c.get("/api/brief").json()["brief"]
        assert b["chores_due"] == ["Müll raus"] and b["people"]["birthdays"] == [{"name": "Ben", "in_days": 0}]
        # A captured shopping note lands on the list.
        c.post("/api/captures", json={"text": "Einkaufen: Eier, Butter"})
        cap_id = c.get("/api/captures").json()[0]["id"]
        app.state.jobs._ask = lambda p, s, images=None: AskResult(json.dumps([{"id": cap_id, "type": "shopping", "title": "Einkauf", "items": ["Eier", "Butter"]}]))
        assert app.state.jobs.run_job("capture_process", forced=True).status == "ok"
        assert [i["text"] for i in c.get("/api/shopping").json()] == ["Eier", "Butter"]
