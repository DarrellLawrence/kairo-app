from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlmodel import SQLModel

import app.db  # noqa: F401
from tests.conftest import alembic_config

SPEC_TABLES = {  # KICKOFF section 9 (+ chat_messages for the Chat screen)
    "goals", "projects", "tasks", "captures", "approvals", "audit_log", "job_runs",
    "checkins", "reviews", "school_events", "grades", "lerndoku", "chat_threads",
    "chat_messages", "push_subscriptions", "settings",
}


def test_fresh_db_migrates_to_all_tables(migrated):
    engine = create_engine(migrated.db_url)
    tables = set(inspect(engine).get_table_names())
    assert SPEC_TABLES <= tables
    assert "alembic_version" in tables


def test_models_and_migrations_match(migrated):
    """A model change without a migration fails here."""
    engine = create_engine(migrated.db_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    assert diff == []


def test_model_runs_say_why_they_were_not_on_haiku(migrated):
    """0025: the reason column (owner's rule 2026-10-19), and it comes off again."""
    cfg = alembic_config(migrated.db_url)
    cols = lambda: {c["name"] for c in inspect(create_engine(migrated.db_url)).get_columns("model_runs")}  # noqa: E731
    assert "reason" in cols()
    command.downgrade(cfg, "0024")
    assert "reason" not in cols() and "warm" in cols()
    command.upgrade(cfg, "head")
    assert "reason" in cols()


def test_downgrade_to_base_and_back(migrated):
    cfg = alembic_config(migrated.db_url)
    command.downgrade(cfg, "base")
    engine = create_engine(migrated.db_url)
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")
    assert SPEC_TABLES <= set(inspect(create_engine(migrated.db_url)).get_table_names())


def test_route_log_table_comes_with_0026_and_goes_again(migrated):
    """0026: G11's decision log (which route each model call took and why), no prompt text in it."""
    cfg = alembic_config(migrated.db_url)
    tables = lambda: set(inspect(create_engine(migrated.db_url)).get_table_names())  # noqa: E731
    assert "route_log" in tables()
    cols = {c["name"] for c in inspect(create_engine(migrated.db_url)).get_columns("route_log")}
    assert cols == {"id", "ts", "purpose", "mode", "content", "hits", "route", "tier", "model", "reason", "fallback_from"}
    command.downgrade(cfg, "0025")
    assert "route_log" not in tables() and "model_runs" in tables()
    command.upgrade(cfg, "head")
    assert "route_log" in tables()
