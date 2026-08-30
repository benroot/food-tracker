import re
import secrets
from datetime import date, datetime

from flask import Flask, flash, redirect, render_template, request, session, url_for

import claude_client
import db as dbmod
from config import load_env_file

load_env_file()

MEAL_TYPES = ("Breakfast", "Lunch", "Dinner", "Snack", "Dessert", "Drink")
REPEATABLE_MEAL_TYPES = ("Breakfast", "Lunch", "Dinner")
RECENT_MEAL_OPTIONS_LIMIT = 3
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _recent_meal_options(db, meal_type, before_date, limit=RECENT_MEAL_OPTIONS_LIMIT):
    entry_rows = db.execute(
        """
        SELECT id, entry_date FROM log_entries
        WHERE meal_type = ? AND entry_date < ?
        ORDER BY entry_date DESC, entry_time DESC, id DESC
        LIMIT ?
        """,
        (meal_type, before_date, limit),
    ).fetchall()

    options = []
    for entry_row in entry_rows:
        items = db.execute(
            "SELECT description, calories FROM log_entry_items WHERE log_entry_id = ?",
            (entry_row["id"],),
        ).fetchall()
        options.append(
            {
                "entry_id": entry_row["id"],
                "entry_date": entry_row["entry_date"],
                "items": [item["description"] for item in items],
                "total_calories": sum(item["calories"] for item in items),
            }
        )
    return options

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

app.teardown_appcontext(dbmod.close_db)
dbmod.init_db()


@app.route("/weight", methods=["GET"])
def index():
    db = dbmod.get_db()
    entries = db.execute(
        "SELECT entry_date, weight_lbs FROM weight_log ORDER BY entry_date DESC"
    ).fetchall()
    return render_template("index.html", entries=entries)


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


@app.route("/", methods=["GET"])
def food_page():
    db = dbmod.get_db()
    today = date.today().isoformat()
    entries = db.execute(
        """
        SELECT lei.id AS item_id, le.meal_type, le.entry_time,
               lei.description, lei.quantity, lei.calories,
               lei.is_estimate, lei.assumption_note
        FROM log_entries le
        JOIN log_entry_items lei ON lei.log_entry_id = le.id
        WHERE le.entry_date = ?
        ORDER BY le.entry_time DESC, le.id DESC, lei.id ASC
        """,
        (today,),
    ).fetchall()
    total_calories = sum(entry["calories"] for entry in entries)
    repeat_options = {
        meal_type: _recent_meal_options(db, meal_type, today)
        for meal_type in REPEATABLE_MEAL_TYPES
    }
    return render_template(
        "food.html",
        entries=entries,
        total_calories=total_calories,
        pending_question=session.get("pending_question"),
        meal_types=MEAL_TYPES,
        repeatable_meal_types=REPEATABLE_MEAL_TYPES,
        repeat_options=repeat_options,
        has_repeat_options=any(repeat_options.values()),
    )


def _describe_logged_item(item):
    description = f'{item["food"]} ({item["estimated_calories"]} cal)'
    if item.get("assumption_note"):
        description += f' -- {item["assumption_note"]}'
    return description


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
    parsed_time = tool_input.get("entry_time")
    entry_time = parsed_time if parsed_time and TIME_PATTERN.match(parsed_time) else now.strftime("%H:%M")
    cursor = db.execute(
        "INSERT INTO log_entries (entry_date, entry_time, meal_type) VALUES (?, ?, ?)",
        (now.date().isoformat(), entry_time, tool_input["meal_type"]),
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
    summary = ", ".join(_describe_logged_item(item) for item in tool_input["items"])
    flash(f'Logged to {tool_input["meal_type"]}: {summary}', "success")
    return redirect(url_for("food_page"))


@app.route("/food/log-direct", methods=["POST"])
def food_log_direct():
    food = request.form.get("food", "").strip()
    calories_raw = request.form.get("calories", "").strip()
    meal_type = request.form.get("meal_type", "").strip()

    if not food or meal_type not in MEAL_TYPES:
        flash("Enter a food label and pick a meal.", "error")
        return redirect(url_for("food_page"))

    try:
        calories = int(calories_raw)
        if calories <= 0:
            raise ValueError
    except ValueError:
        flash("Calories must be a whole number greater than 0.", "error")
        return redirect(url_for("food_page"))

    db = dbmod.get_db()
    now = datetime.now()
    cursor = db.execute(
        "INSERT INTO log_entries (entry_date, entry_time, meal_type) VALUES (?, ?, ?)",
        (now.date().isoformat(), now.strftime("%H:%M"), meal_type),
    )
    db.execute(
        """
        INSERT INTO log_entry_items
            (log_entry_id, description, calories, is_estimate)
        VALUES (?, ?, ?, 0)
        """,
        (cursor.lastrowid, food, calories),
    )
    db.commit()

    flash(f"Logged to {meal_type}: {food} ({calories} cal)", "success")
    return redirect(url_for("food_page"))


@app.route("/food/repeat", methods=["POST"])
def food_repeat():
    raw_entry_id = request.form.get("entry_id", "")
    if not raw_entry_id.isdigit():
        flash("That meal no longer exists.", "error")
        return redirect(url_for("food_page"))
    entry_id = int(raw_entry_id)

    db = dbmod.get_db()
    source_entry = db.execute(
        "SELECT meal_type FROM log_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    source_items = db.execute(
        """
        SELECT description, quantity, calories, is_estimate, assumption_note
        FROM log_entry_items WHERE log_entry_id = ?
        """,
        (entry_id,),
    ).fetchall()
    if source_entry is None or not source_items:
        flash("That meal no longer exists.", "error")
        return redirect(url_for("food_page"))

    now = datetime.now()
    requested_time = request.form.get("entry_time", "").strip()
    entry_time = requested_time if requested_time and TIME_PATTERN.match(requested_time) else now.strftime("%H:%M")
    cursor = db.execute(
        "INSERT INTO log_entries (entry_date, entry_time, meal_type) VALUES (?, ?, ?)",
        (now.date().isoformat(), entry_time, source_entry["meal_type"]),
    )
    new_entry_id = cursor.lastrowid
    for item in source_items:
        db.execute(
            """
            INSERT INTO log_entry_items
                (log_entry_id, description, quantity, calories, is_estimate, assumption_note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_entry_id,
                item["description"],
                item["quantity"],
                item["calories"],
                item["is_estimate"],
                item["assumption_note"],
            ),
        )
    db.commit()

    summary = ", ".join(f'{item["description"]} ({item["calories"]} cal)' for item in source_items)
    flash(f'Logged to {source_entry["meal_type"]}: {summary}', "success")
    return redirect(url_for("food_page"))


@app.route("/food/entries/<int:item_id>/edit", methods=["GET"])
def food_entry_edit_form(item_id):
    db = dbmod.get_db()
    entry = db.execute(
        """
        SELECT lei.id AS item_id, lei.description, lei.calories, le.meal_type,
               (SELECT COUNT(*) FROM log_entry_items WHERE log_entry_id = le.id) AS sibling_count
        FROM log_entry_items lei
        JOIN log_entries le ON le.id = lei.log_entry_id
        WHERE lei.id = ?
        """,
        (item_id,),
    ).fetchone()
    if entry is None:
        flash("That entry no longer exists.", "error")
        return redirect(url_for("food_page"))
    return render_template("edit_entry.html", entry=entry, meal_types=MEAL_TYPES)


@app.route("/food/entries/<int:item_id>/edit", methods=["POST"])
def food_entry_edit(item_id):
    db = dbmod.get_db()
    row = db.execute(
        "SELECT log_entry_id FROM log_entry_items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        flash("That entry no longer exists.", "error")
        return redirect(url_for("food_page"))

    food = request.form.get("food", "").strip()
    calories_raw = request.form.get("calories", "").strip()
    meal_type = request.form.get("meal_type", "").strip()

    if not food or meal_type not in MEAL_TYPES:
        flash("Enter a food label and pick a meal.", "error")
        return redirect(url_for("food_entry_edit_form", item_id=item_id))

    try:
        calories = int(calories_raw)
        if calories <= 0:
            raise ValueError
    except ValueError:
        flash("Calories must be a whole number greater than 0.", "error")
        return redirect(url_for("food_entry_edit_form", item_id=item_id))

    db.execute(
        "UPDATE log_entry_items SET description = ?, calories = ? WHERE id = ?",
        (food, calories, item_id),
    )
    db.execute(
        "UPDATE log_entries SET meal_type = ? WHERE id = ?",
        (meal_type, row["log_entry_id"]),
    )
    db.commit()

    flash(f"Updated: {food} ({calories} cal)", "success")
    return redirect(url_for("food_page"))


@app.route("/food/entries/<int:item_id>/delete", methods=["POST"])
def food_entry_delete(item_id):
    db = dbmod.get_db()
    row = db.execute(
        "SELECT log_entry_id, description FROM log_entry_items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        flash("That entry no longer exists.", "error")
        return redirect(url_for("food_page"))

    db.execute("DELETE FROM log_entry_items WHERE id = ?", (item_id,))
    remaining = db.execute(
        "SELECT COUNT(*) FROM log_entry_items WHERE log_entry_id = ?", (row["log_entry_id"],)
    ).fetchone()[0]
    if remaining == 0:
        db.execute("DELETE FROM log_entries WHERE id = ?", (row["log_entry_id"],))
    db.commit()

    flash(f'Deleted: {row["description"]}', "success")
    return redirect(url_for("food_page"))


if __name__ == "__main__":
    app.run(debug=True)
