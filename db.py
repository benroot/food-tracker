import os
import sqlite3
from pathlib import Path

from flask import g

DB_PATH = Path(os.environ.get("FOOD_LOG_DB_PATH", Path(__file__).parent / "food_log.db"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _migrate_schema(db):
    """One-off, idempotent migrations for schema changes made after initial
    deploy (schema.sql's CREATE TABLE IF NOT EXISTS never retroactively
    alters an existing table). Each check only fires once per real database,
    and no-ops instantly afterward -- safe to leave in permanently."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(log_entries)")}
    if "meal_type" in columns:
        db.execute("ALTER TABLE log_entries DROP COLUMN meal_type")


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA_PATH.read_text())
        _migrate_schema(db)
