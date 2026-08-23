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


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA_PATH.read_text())
