"""Board API: goals > projects > tasks; Kanban statuses; validation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(migrated):
    with TestClient(create_app(migrated)) as c:
        yield c


def test_goal_project_task_flow(client):
    g = client.post("/api/goals", json={"title": "Pass the final exam", "horizon": "year", "target_date": "2027-06-30"}).json()
    p = client.post("/api/projects", json={"title": "Maths revision", "goal_id": g["id"]}).json()
    t = client.post("/api/tasks", json={"title": "Chapter 4", "project_id": p["id"], "priority": 2, "place": "school"}).json()
    assert t["place"] == "School" and t["status"] == "open"
    t = client.patch(f"/api/tasks/{t['id']}", json={"status": "doing"}).json()
    assert t["status"] == "doing" and t["done_at"] is None
    t = client.patch(f"/api/tasks/{t['id']}", json={"status": "done"}).json()
    assert t["done_at"] is not None
    t = client.patch(f"/api/tasks/{t['id']}", json={"status": "open", "clear": ["place", "project_id"]}).json()
    assert t["done_at"] is None and t["place"] is None and t["project_id"] is None
    b = client.get("/api/board").json()
    assert [x["title"] for x in b["goals"]] == ["Pass the final exam"]
    assert b["projects"][0]["goal_id"] == g["id"] and b["tasks"][0]["title"] == "Chapter 4"
    client.patch(f"/api/goals/{g['id']}", json={"status": "done"})
    assert client.get("/api/board").json()["goals"] == []


def test_board_validation(client):
    assert client.post("/api/tasks", json={"title": ""}).status_code == 400
    assert client.post("/api/tasks", json={"title": "x", "evil": 1}).status_code == 400
    assert client.post("/api/tasks", json={"title": "x", "project_id": 999}).status_code == 400
    assert client.post("/api/projects", json={"title": "x", "goal_id": 999}).status_code == 400
    assert client.patch("/api/tasks/999", json={"title": "y"}).status_code == 404
    assert client.post("/api/tasks", json={"title": "x", "status": "deleted"}).status_code == 400
    assert client.post("/api/goals", json={"title": "x", "horizon": "decade"}).status_code == 400
