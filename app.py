import secrets
from datetime import date, datetime

from flask import Flask, flash, redirect, render_template, request, session, url_for

import claude_client
import db as dbmod
from config import load_env_file

load_env_file()

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

app.teardown_appcontext(dbmod.close_db)
dbmod.init_db()


@app.route("/", methods=["GET"])
def index():
    db = dbmod.get_db()
    latest = db.execute(
        "SELECT entry_date, weight_lbs FROM weight_log ORDER BY entry_date DESC LIMIT 1"
    ).fetchone()
    return render_template("index.html", latest=latest)


@app.route("/log", methods=["POST"])
def log_weight():
    weight_lbs = request.form.get("weight_lbs", "").strip()
    if weight_lbs:
        db = dbmod.get_db()
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


@app.route("/food", methods=["GET"])
def food_page():
    db = dbmod.get_db()
    today = date.today().isoformat()
    entries = db.execute(
        """
        SELECT le.id, le.meal_type, le.entry_time,
               lei.description, lei.quantity, lei.calories,
               lei.is_estimate, lei.assumption_note
        FROM log_entries le
        JOIN log_entry_items lei ON lei.log_entry_id = le.id
        WHERE le.entry_date = ?
        ORDER BY le.entry_time, le.id
        """,
        (today,),
    ).fetchall()
    total_calories = sum(entry["calories"] for entry in entries)
    return render_template(
        "food.html",
        entries=entries,
        total_calories=total_calories,
        pending_question=session.get("pending_question"),
    )


@app.route("/food/log", methods=["POST"])
def food_log():
    user_message = request.form.get("message", "").strip()
    if not user_message:
        return redirect(url_for("food_page"))

    history = session.get("pending_conversation", [])
    messages = history + [claude_client.build_user_turn(history, user_message)]

    try:
        response = claude_client.call_claude(messages)
    except Exception:
        flash("Couldn't reach the Claude API. Check your ANTHROPIC_API_KEY and try again.", "error")
        return redirect(url_for("food_page"))

    tool_input = claude_client.extract_tool_input(response)
    updated_history = messages + [claude_client.response_as_message(response)]

    if tool_input.get("needs_clarification"):
        session["pending_conversation"] = updated_history
        session["pending_question"] = tool_input.get(
            "clarification_question", "Could you clarify that?"
        )
        return redirect(url_for("food_page"))

    db = dbmod.get_db()
    now = datetime.now()
    cursor = db.execute(
        "INSERT INTO log_entries (entry_date, entry_time, meal_type) VALUES (?, ?, ?)",
        (now.date().isoformat(), now.strftime("%H:%M"), tool_input["meal_type"]),
    )
    log_entry_id = cursor.lastrowid
    for item in tool_input["items"]:
        db.execute(
            """
            INSERT INTO log_entry_items
                (log_entry_id, description, quantity, calories, is_estimate, assumption_note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                log_entry_id,
                item["food"],
                item.get("quantity"),
                item["estimated_calories"],
                1 if item["is_estimate"] else 0,
                item.get("assumption_note"),
            ),
        )
    db.commit()

    session.pop("pending_conversation", None)
    session.pop("pending_question", None)
    summary = ", ".join(
        f'{item["food"]} ({item["estimated_calories"]} cal)' for item in tool_input["items"]
    )
    flash(f'Logged to {tool_input["meal_type"]}: {summary}', "success")
    return redirect(url_for("food_page"))


if __name__ == "__main__":
    app.run(debug=True)
