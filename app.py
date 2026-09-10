import re
import secrets
from datetime import date, datetime, timedelta

from flask import Flask, flash, redirect, render_template, request, session, url_for

import claude_client
import db as dbmod
from config import load_env_file

load_env_file()

RECENT_OPTIONS_LIMIT = 3
RECENT_MEAL_MIN_DAYS = 7
RECENT_MEAL_MIN_COUNT = 50
TRENDS_WINDOW_DAYS = 30
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _resolve_entry_date(raw_offset):
    """Log-target date from a form's entry_date_offset field: '0' for today,
    '-1' for yesterday. Anything else (missing, malformed, out of range)
    defaults to today rather than failing the whole submission."""
    try:
        offset = int(raw_offset)
    except (TypeError, ValueError):
        offset = 0
    if offset not in (0, -1):
        offset = 0
    return date.today() + timedelta(days=offset)


def _recent_meal_options(db, min_days=RECENT_MEAL_MIN_DAYS, min_count=RECENT_MEAL_MIN_COUNT):
    """Recent meals to offer for repeating: a flat, reverse-chronological
    list (no more meal-type grouping -- that concept is gone). Covers at
    least min_days of history, extended further back only if that window
    doesn't already contain min_count meals -- handles gaps in logging
    without capping a genuinely busy recent window. Today's own entries are
    included, same as before: repeating the same meal twice in one day is
    a legitimate case, not an edge case to exclude."""
    window_start = (date.today() - timedelta(days=min_days - 1)).isoformat()
    windowed_count = db.execute(
        "SELECT COUNT(*) FROM log_entries WHERE entry_date >= ?", (window_start,)
    ).fetchone()[0]
    limit = max(windowed_count, min_count)

    rows = db.execute(
        """
        SELECT le.id AS entry_id, le.entry_date, le.entry_time,
               lei.description, lei.calories
        FROM log_entries le
        JOIN log_entry_items lei ON lei.log_entry_id = le.id
        WHERE le.id IN (
            SELECT id FROM log_entries
            ORDER BY entry_date DESC, entry_time DESC, id DESC
            LIMIT ?
        )
        ORDER BY le.entry_date DESC, le.entry_time DESC, le.id DESC, lei.id ASC
        """,
        (limit,),
    ).fetchall()

    options = []
    by_entry_id = {}
    for row in rows:
        entry_id = row["entry_id"]
        option = by_entry_id.get(entry_id)
        if option is None:
            option = {
                "entry_id": entry_id,
                "entry_date": row["entry_date"],
                "entry_time": row["entry_time"],
                "items": [],
                "total_calories": 0,
            }
            by_entry_id[entry_id] = option
            options.append(option)
        option["items"].append(row["description"])
        option["total_calories"] += row["calories"]
    return options


def _recent_exercise_options(db, limit=RECENT_OPTIONS_LIMIT):
    # Today's own entries are included -- repeating the same activity twice
    # in one day (e.g. cycling to work, then back) is a real, common case.
    rows = db.execute(
        """
        SELECT id, entry_date, activity, calories_burned
        FROM exercise_log
        ORDER BY entry_date DESC, entry_time DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "entry_id": row["id"],
            "entry_date": row["entry_date"],
            "activity": row["activity"],
            "calories_burned": row["calories_burned"],
        }
        for row in rows
    ]


def _food_day_summary(db, day, label):
    entries = db.execute(
        """
        SELECT lei.id AS item_id, le.entry_time,
               lei.description, lei.quantity, lei.calories,
               lei.is_estimate, lei.assumption_note
        FROM log_entries le
        JOIN log_entry_items lei ON lei.log_entry_id = le.id
        WHERE le.entry_date = ?
        ORDER BY le.entry_time DESC, le.id DESC, lei.id ASC
        """,
        (day.isoformat(),),
    ).fetchall()
    total_calories = sum(entry["calories"] for entry in entries)
    calories_burned = db.execute(
        "SELECT COALESCE(SUM(calories_burned), 0) FROM exercise_log WHERE entry_date = ?",
        (day.isoformat(),),
    ).fetchone()[0]
    return {
        "label": label,
        "entries": entries,
        "total_calories": total_calories,
        "calories_burned": calories_burned,
        "net_calories": total_calories - calories_burned,
    }


def _exercise_day_summary(db, day, label):
    entries = db.execute(
        """
        SELECT id, entry_time, activity, calories_burned, is_estimate, assumption_note
        FROM exercise_log
        WHERE entry_date = ?
        ORDER BY entry_time DESC, id DESC
        """,
        (day.isoformat(),),
    ).fetchall()
    return {
        "label": label,
        "entries": entries,
        "total_calories_burned": sum(entry["calories_burned"] for entry in entries),
    }


def _daily_food_trends(db, window_days=TRENDS_WINDOW_DAYS):
    """Per-day entry count and total calories for the last window_days days,
    reverse chronological, zero-filled for days with no logging -- gaps are
    kept visible rather than silently skipped, per Phase 9. "Entry count" is
    meal events (log_entries rows), not individual food items: a 3-item
    breakfast counts as 1."""
    window_start = (date.today() - timedelta(days=window_days - 1)).isoformat()
    rows = db.execute(
        """
        SELECT le.entry_date AS entry_date,
               COUNT(DISTINCT le.id) AS entry_count,
               COALESCE(SUM(lei.calories), 0) AS total_calories
        FROM log_entries le
        LEFT JOIN log_entry_items lei ON lei.log_entry_id = le.id
        WHERE le.entry_date >= ?
        GROUP BY le.entry_date
        """,
        (window_start,),
    ).fetchall()
    by_date = {row["entry_date"]: row for row in rows}

    days = []
    for offset in range(window_days):
        day = (date.today() - timedelta(days=offset)).isoformat()
        row = by_date.get(day)
        days.append(
            {
                "entry_date": day,
                "entry_count": row["entry_count"] if row else 0,
                "total_calories": row["total_calories"] if row else 0,
            }
        )
    return days


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


@app.route("/exercise", methods=["GET"])
def exercise_page():
    db = dbmod.get_db()
    today_summary = _exercise_day_summary(db, date.today(), "Today")
    yesterday_summary = _exercise_day_summary(db, date.today() - timedelta(days=1), "Yesterday")
    repeat_options = _recent_exercise_options(db)
    return render_template(
        "exercise.html",
        today=today_summary,
        yesterday=yesterday_summary,
        pending_question=session.get("pending_exercise_question"),
        repeat_options=repeat_options,
    )


def _describe_logged_exercise(tool_input):
    description = f'{tool_input["activity"]} ({tool_input["estimated_calories_burned"]} cal)'
    if tool_input.get("assumption_note"):
        description += f' -- {tool_input["assumption_note"]}'
    return description


@app.route("/exercise/log", methods=["POST"])
def exercise_log():
    user_message = request.form.get("message", "").strip()
    if not user_message:
        return redirect(url_for("exercise_page"))

    history = session.get("pending_exercise_conversation", [])
    messages = history + [claude_client.build_user_turn(history, user_message)]

    try:
        response = claude_client.call_claude(
            messages,
            tool=claude_client.LOG_EXERCISE_ENTRY_TOOL,
            system_prompt=claude_client.EXERCISE_SYSTEM_PROMPT,
        )
    except Exception:
        flash("Couldn't reach the Claude API. Check your ANTHROPIC_API_KEY and try again.", "error")
        return redirect(url_for("exercise_page"))

    tool_input = claude_client.extract_tool_input(response)
    updated_history = messages + [claude_client.response_as_message(response)]

    if tool_input.get("needs_clarification"):
        session["pending_exercise_conversation"] = updated_history
        session["pending_exercise_question"] = tool_input.get(
            "clarification_question", "Could you clarify that?"
        )
        return redirect(url_for("exercise_page"))

    db = dbmod.get_db()
    now = datetime.now()
    entry_date = _resolve_entry_date(request.form.get("entry_date_offset"))
    parsed_time = tool_input.get("entry_time")
    entry_time = parsed_time if parsed_time and TIME_PATTERN.match(parsed_time) else now.strftime("%H:%M")
    db.execute(
        """
        INSERT INTO exercise_log
            (entry_date, entry_time, activity, calories_burned, is_estimate, assumption_note)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            entry_date.isoformat(),
            entry_time,
            tool_input["activity"],
            tool_input["estimated_calories_burned"],
            1 if tool_input["is_estimate"] else 0,
            tool_input.get("assumption_note"),
        ),
    )
    db.commit()

    session.pop("pending_exercise_conversation", None)
    session.pop("pending_exercise_question", None)
    flash(f"Logged: {_describe_logged_exercise(tool_input)}", "success")
    return redirect(url_for("exercise_page"))


@app.route("/exercise/log-direct", methods=["POST"])
def exercise_log_direct():
    activity = request.form.get("activity", "").strip()
    calories_raw = request.form.get("calories", "").strip()

    if not activity:
        flash("Enter an activity.", "error")
        return redirect(url_for("exercise_page"))

    try:
        calories = int(calories_raw)
        if calories <= 0:
            raise ValueError
    except ValueError:
        flash("Calories must be a whole number greater than 0.", "error")
        return redirect(url_for("exercise_page"))

    db = dbmod.get_db()
    now = datetime.now()
    entry_date = _resolve_entry_date(request.form.get("entry_date_offset"))
    db.execute(
        """
        INSERT INTO exercise_log
            (entry_date, entry_time, activity, calories_burned, is_estimate)
        VALUES (?, ?, ?, ?, 0)
        """,
        (entry_date.isoformat(), now.strftime("%H:%M"), activity, calories),
    )
    db.commit()

    flash(f"Logged: {activity} ({calories} cal)", "success")
    return redirect(url_for("exercise_page"))


@app.route("/exercise/entries/<int:entry_id>/edit", methods=["GET"])
def exercise_entry_edit_form(entry_id):
    db = dbmod.get_db()
    entry = db.execute(
        "SELECT id, activity, calories_burned FROM exercise_log WHERE id = ?", (entry_id,)
    ).fetchone()
    if entry is None:
        flash("That entry no longer exists.", "error")
        return redirect(url_for("exercise_page"))
    return render_template("edit_exercise_entry.html", entry=entry)


@app.route("/exercise/entries/<int:entry_id>/edit", methods=["POST"])
def exercise_entry_edit(entry_id):
    db = dbmod.get_db()
    existing = db.execute(
        "SELECT id FROM exercise_log WHERE id = ?", (entry_id,)
    ).fetchone()
    if existing is None:
        flash("That entry no longer exists.", "error")
        return redirect(url_for("exercise_page"))

    activity = request.form.get("activity", "").strip()
    calories_raw = request.form.get("calories", "").strip()

    if not activity:
        flash("Enter an activity.", "error")
        return redirect(url_for("exercise_entry_edit_form", entry_id=entry_id))

    try:
        calories = int(calories_raw)
        if calories <= 0:
            raise ValueError
    except ValueError:
        flash("Calories must be a whole number greater than 0.", "error")
        return redirect(url_for("exercise_entry_edit_form", entry_id=entry_id))

    db.execute(
        "UPDATE exercise_log SET activity = ?, calories_burned = ? WHERE id = ?",
        (activity, calories, entry_id),
    )
    db.commit()

    flash(f"Updated: {activity} ({calories} cal)", "success")
    return redirect(url_for("exercise_page"))


@app.route("/exercise/entries/<int:entry_id>/delete", methods=["POST"])
def exercise_entry_delete(entry_id):
    db = dbmod.get_db()
    row = db.execute(
        "SELECT activity FROM exercise_log WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        flash("That entry no longer exists.", "error")
        return redirect(url_for("exercise_page"))

    db.execute("DELETE FROM exercise_log WHERE id = ?", (entry_id,))
    db.commit()

    flash(f'Deleted: {row["activity"]}', "success")
    return redirect(url_for("exercise_page"))


@app.route("/exercise/repeat", methods=["POST"])
def exercise_repeat():
    raw_entry_id = request.form.get("entry_id", "")
    if not raw_entry_id.isdigit():
        flash("That activity no longer exists.", "error")
        return redirect(url_for("exercise_page"))
    entry_id = int(raw_entry_id)

    db = dbmod.get_db()
    source = db.execute(
        """
        SELECT activity, calories_burned, is_estimate, assumption_note
        FROM exercise_log WHERE id = ?
        """,
        (entry_id,),
    ).fetchone()
    if source is None:
        flash("That activity no longer exists.", "error")
        return redirect(url_for("exercise_page"))

    now = datetime.now()
    entry_date = _resolve_entry_date(request.form.get("entry_date_offset"))
    requested_time = request.form.get("entry_time", "").strip()
    entry_time = requested_time if requested_time and TIME_PATTERN.match(requested_time) else now.strftime("%H:%M")
    db.execute(
        """
        INSERT INTO exercise_log
            (entry_date, entry_time, activity, calories_burned, is_estimate, assumption_note)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            entry_date.isoformat(),
            entry_time,
            source["activity"],
            source["calories_burned"],
            source["is_estimate"],
            source["assumption_note"],
        ),
    )
    db.commit()

    flash(f'Logged: {source["activity"]} ({source["calories_burned"]} cal)', "success")
    return redirect(url_for("exercise_page"))


@app.route("/", methods=["GET"])
def food_page():
    db = dbmod.get_db()
    today_summary = _food_day_summary(db, date.today(), "Today")
    yesterday_summary = _food_day_summary(db, date.today() - timedelta(days=1), "Yesterday")
    repeat_options = _recent_meal_options(db)
    return render_template(
        "food.html",
        today=today_summary,
        yesterday=yesterday_summary,
        pending_question=session.get("pending_question"),
        repeat_options=repeat_options,
    )


def _describe_logged_item(item):
    description = f'{item["food"]} ({item["estimated_calories"]} cal)'
    if item.get("assumption_note"):
        description += f' -- {item["assumption_note"]}'
    return description


@app.route("/trends", methods=["GET"])
def trends_page():
    db = dbmod.get_db()
    days = _daily_food_trends(db)
    return render_template("trends.html", days=days, window_days=TRENDS_WINDOW_DAYS)


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
    entry_date = _resolve_entry_date(request.form.get("entry_date_offset"))
    parsed_time = tool_input.get("entry_time")
    entry_time = parsed_time if parsed_time and TIME_PATTERN.match(parsed_time) else now.strftime("%H:%M")
    cursor = db.execute(
        "INSERT INTO log_entries (entry_date, entry_time) VALUES (?, ?)",
        (entry_date.isoformat(), entry_time),
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
    flash(f"Logged: {summary}", "success")
    return redirect(url_for("food_page"))


@app.route("/food/log-direct", methods=["POST"])
def food_log_direct():
    food = request.form.get("food", "").strip()
    calories_raw = request.form.get("calories", "").strip()

    if not food:
        flash("Enter a food label.", "error")
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
    entry_date = _resolve_entry_date(request.form.get("entry_date_offset"))
    cursor = db.execute(
        "INSERT INTO log_entries (entry_date, entry_time) VALUES (?, ?)",
        (entry_date.isoformat(), now.strftime("%H:%M")),
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

    flash(f"Logged: {food} ({calories} cal)", "success")
    return redirect(url_for("food_page"))


@app.route("/food/repeat", methods=["POST"])
def food_repeat():
    raw_entry_id = request.form.get("entry_id", "")
    if not raw_entry_id.isdigit():
        flash("That meal no longer exists.", "error")
        return redirect(url_for("food_page"))
    entry_id = int(raw_entry_id)

    db = dbmod.get_db()
    source_items = db.execute(
        """
        SELECT description, quantity, calories, is_estimate, assumption_note
        FROM log_entry_items WHERE log_entry_id = ?
        """,
        (entry_id,),
    ).fetchall()
    if not source_items:
        flash("That meal no longer exists.", "error")
        return redirect(url_for("food_page"))

    now = datetime.now()
    entry_date = _resolve_entry_date(request.form.get("entry_date_offset"))
    requested_time = request.form.get("entry_time", "").strip()
    entry_time = requested_time if requested_time and TIME_PATTERN.match(requested_time) else now.strftime("%H:%M")
    cursor = db.execute(
        "INSERT INTO log_entries (entry_date, entry_time) VALUES (?, ?)",
        (entry_date.isoformat(), entry_time),
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
    flash(f"Logged: {summary}", "success")
    return redirect(url_for("food_page"))


@app.route("/food/entries/<int:item_id>/edit", methods=["GET"])
def food_entry_edit_form(item_id):
    db = dbmod.get_db()
    entry = db.execute(
        "SELECT id AS item_id, description, calories FROM log_entry_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if entry is None:
        flash("That entry no longer exists.", "error")
        return redirect(url_for("food_page"))
    return render_template("edit_entry.html", entry=entry)


@app.route("/food/entries/<int:item_id>/edit", methods=["POST"])
def food_entry_edit(item_id):
    db = dbmod.get_db()
    row = db.execute(
        "SELECT id FROM log_entry_items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        flash("That entry no longer exists.", "error")
        return redirect(url_for("food_page"))

    food = request.form.get("food", "").strip()
    calories_raw = request.form.get("calories", "").strip()

    if not food:
        flash("Enter a food label.", "error")
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
