import sqlite3
from datetime import date
from pathlib import Path

from flask import Flask, g, redirect, render_template, request, url_for

DB_PATH = Path(__file__).parent / "food_log.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

app = Flask(__name__)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA_PATH.read_text())


@app.route("/", methods=["GET"])
def index():
    db = get_db()
    latest = db.execute(
        "SELECT entry_date, weight_lbs FROM weight_log ORDER BY entry_date DESC LIMIT 1"
    ).fetchone()
    return render_template("index.html", latest=latest)


@app.route("/log", methods=["POST"])
def log_weight():
    weight_lbs = request.form.get("weight_lbs", "").strip()
    if weight_lbs:
        db = get_db()
        db.execute(
            """
            INSERT INTO weight_log (entry_date, weight_lbs)
            VALUES (?, ?)
            ON CONFLICT(entry_date) DO UPDATE SET weight_lbs = excluded.weight_lbs
            """,
            (date.today().isoformat(), float(weight_lbs)),
        )
        db.commit()
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
