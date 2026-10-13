from fastapi.testclient import TestClient

from app import onboarding, vault
from app.main import create_app


def test_questions_cover_every_area():
    sections = {q.section for q in onboarding.QUESTIONS}
    assert {"You", "Goals", "School", "Apprenticeship", "Training", "People", "Routines", "Working with Kairo"} <= sections
    assert 20 <= len(onboarding.QUESTIONS) <= 30
    for q in onboarding.QUESTIONS:
        assert q.file.split("/")[0] in vault.FOLDERS


def test_answer_edit_and_reset_through_the_api(migrated):
    app = create_app(migrated)
    root = migrated.vault_dir
    with TestClient(app) as c:
        st = c.get("/api/onboarding").json()
        assert st["answered"] == 0 and len(st["questions"]) == len(onboarding.QUESTIONS)
        c.put("/api/onboarding/goals_school", json={"value": "Grade 5 in math\nPass the exam"})
        goals = (root / "20 Goals" / "Goals.md").read_text()
        assert "- Grade 5 in math" in goals and "- Pass the exam" in goals
        # the owner's own text in the same file survives an edit
        (root / "20 Goals" / "Goals.md").write_text("My note.\n" + goals)
        c.put("/api/onboarding/goals_school", json={"value": "Grade 6 in math"})
        goals = (root / "20 Goals" / "Goals.md").read_text()
        assert "My note." in goals and "Grade 6" in goals and "Grade 5" not in goals
        # and it reaches the chat
        assert "Grade 6 in math" in app.state.system_prompt()
        assert c.put("/api/onboarding/nope", json={"value": "x"}).status_code == 400
        assert c.put("/api/onboarding/about", json={"value": "x" * 5000}).status_code == 400
        st = c.post("/api/onboarding/reset").json()
        assert st["answered"] == 0
        assert "Grade 6" not in (root / "20 Goals" / "Goals.md").read_text()
        assert "My note." in (root / "20 Goals" / "Goals.md").read_text()
