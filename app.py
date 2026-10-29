import base64
import os
import re
import secrets
from datetime import date, datetime, timedelta

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

import claude_client
import db as dbmod
from config import load_env_file

load_env_file()

# AUTH_PASSWORD_HASH is stored base64-encoded, not as the raw
# scrypt:.../pbkdf2:... string. cPanel's Setup Python App env-var pipeline
# was found to corrupt the raw form -- something in it shell-interpolates
# $salt$hash as variable references, silently dropping everything between
# the $ delimiters. Base64's alphabet has no $, so nothing to misinterpret.
# Decoding here means everything downstream still sees the real hash string.
_auth_hash_b64 = os.environ.get("AUTH_PASSWORD_HASH")
AUTH_PASSWORD_HASH = (
    base64.urlsafe_b64decode(_auth_hash_b64).decode() if _auth_hash_b64 else None
)

# SECRET_KEY signs the session cookie that carries the login. It must be a
# stable, real secret whenever auth is on -- a per-boot random fallback would
# log you out on every restart (and disagree across Passenger workers), and
# silently falling back to one in production would hide a misconfiguration,
# so refuse to boot instead. With auth off (local dev, tests) a random key is
# fine: the session only holds transient pending-clarification state.
SECRET_KEY = os.environ.get("SECRET_KEY")
if AUTH_PASSWORD_HASH and not SECRET_KEY:
    raise RuntimeError("AUTH_PASSWORD_HASH is set but SECRET_KEY isn't -- set both.")

SESSION_LIFETIME = timedelta(days=30)
LOGIN_MAX_FAILURES = 5
LOGIN_FAILURE_WINDOW_MINUTES = 15

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


def _format_fiber(grams):
    """'4 g fiber', or '— fiber' when unknown (NULL: logged before fiber
    tracking existed, or left blank on direct entry). 0 g is a real value
    and is shown as such."""
    return "— fiber" if grams is None else f"{grams} g fiber"


def _parse_optional_fiber(raw):
    """Fiber form field: blank means unknown (None). Returns (value, ok)."""
    raw = (raw or "").strip()
    if not raw:
        return None, True
    try:
        value = int(raw)
    except ValueError:
        return None, False
    return (value, True) if value >= 0 else (None, False)


def _describe_item(description, calories, fiber_grams):
    if fiber_grams is None:
        return f"{description} ({calories} cal)"
    return f"{description} ({calories} cal, {fiber_grams} g fiber)"


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
               lei.description, lei.calories, lei.fiber_grams
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
                # Stays None (not shown) unless at least one item's fiber is known.
                "total_fiber": None,
            }
            by_entry_id[entry_id] = option
            options.append(option)
        option["items"].append(row["description"])
        option["total_calories"] += row["calories"]
        if row["fiber_grams"] is not None:
            option["total_fiber"] = (option["total_fiber"] or 0) + row["fiber_grams"]
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
               lei.description, lei.quantity, lei.calories, lei.fiber_grams,
               lei.is_estimate, lei.assumption_note
        FROM log_entries le
        JOIN log_entry_items lei ON lei.log_entry_id = le.id
        WHERE le.entry_date = ?
        ORDER BY le.entry_time DESC, le.id DESC, lei.id ASC
        """,
        (day.isoformat(),),
    ).fetchall()
    total_calories = sum(entry["calories"] for entry in entries)
    # Unknown-fiber items count as 0 toward the day's total.
    total_fiber = sum(entry["fiber_grams"] or 0 for entry in entries)
    calories_burned = db.execute(
        "SELECT COALESCE(SUM(calories_burned), 0) FROM exercise_log WHERE entry_date = ?",
        (day.isoformat(),),
    ).fetchone()[0]
    return {
        "label": label,
        "entries": entries,
        "total_calories": total_calories,
        "total_fiber": total_fiber,
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


SPARKLINE_WIDTH = 280
SPARKLINE_HEIGHT = 90
SPARKLINE_MARGIN_LEFT = 32
SPARKLINE_MARGIN_RIGHT = 4
SPARKLINE_MARGIN_TOP = 8
SPARKLINE_MARGIN_BOTTOM = 16
WEIGHT_MIN_AXIS_RANGE = 15


def _weight_trend(db, window_days=TRENDS_WINDOW_DAYS):
    """Actual logged weight entries within the window, chronological, as
    (day_offset, weight_lbs) pairs -- NOT zero-filled like food's daily trend.
    Weight logging is inherently sparse (you don't necessarily weigh in every
    day), and a missing day isn't a meaningful "0 lbs" the way a missing food
    day is legitimately "0 cal". day_offset is days elapsed from the window
    start (0..window_days-1), not list position, so a real gap between logs
    renders as a stretched-out flat segment on the sparkline rather than being
    visually compressed away."""
    window_start = date.today() - timedelta(days=window_days - 1)
    rows = db.execute(
        "SELECT entry_date, weight_lbs FROM weight_log WHERE entry_date >= ? ORDER BY entry_date ASC",
        (window_start.isoformat(),),
    ).fetchall()
    return [
        ((date.fromisoformat(row["entry_date"]) - window_start).days, row["weight_lbs"])
        for row in rows
    ]


def _calorie_trend(db, window_days=TRENDS_WINDOW_DAYS):
    """Total calories per day as (day_offset, total_calories) pairs, oldest
    first -- built from _daily_food_trends' already-zero-filled, today-first
    list, just reordered and re-indexed for sparkline plotting."""
    chronological = list(reversed(_daily_food_trends(db, window_days=window_days)))
    return [(offset, day["total_calories"]) for offset, day in enumerate(chronological)]


def _fiber_trend(db, window_days=TRENDS_WINDOW_DAYS):
    """Total fiber per day as (day_offset, fiber_grams) pairs, oldest first.
    Unlike calories, NOT zero-filled: only days with at least one known
    fiber value are plotted (same sparse treatment as weight). Otherwise every
    day logged before fiber tracking existed -- and any day with nothing
    logged -- would plot as a misleading 0 g. Unknown items on an otherwise
    plotted day still count as 0, matching the food page's daily total."""
    window_start = date.today() - timedelta(days=window_days - 1)
    rows = db.execute(
        """
        SELECT le.entry_date, SUM(lei.fiber_grams) AS total_fiber
        FROM log_entries le
        JOIN log_entry_items lei ON lei.log_entry_id = le.id
        WHERE le.entry_date >= ?
        GROUP BY le.entry_date
        HAVING COUNT(lei.fiber_grams) > 0
        ORDER BY le.entry_date ASC
        """,
        (window_start.isoformat(),),
    ).fetchall()
    return [
        ((date.fromisoformat(row["entry_date"]) - window_start).days, row["total_fiber"])
        for row in rows
    ]


def _sparkline_svg(
    points,
    window_days,
    min_range=None,
    width=SPARKLINE_WIDTH,
    height=SPARKLINE_HEIGHT,
    margin_left=SPARKLINE_MARGIN_LEFT,
    margin_right=SPARKLINE_MARGIN_RIGHT,
    margin_top=SPARKLINE_MARGIN_TOP,
    margin_bottom=SPARKLINE_MARGIN_BOTTOM,
):
    """Turn (day_offset, value) points into SVG polyline coordinates plus a
    simple axis (a vertical + horizontal line; labels are the caller's job).
    Y is tight-fit to the data's own min/max by default, not zero-anchored --
    standard sparkline convention, needed so a few lbs or a few hundred
    calories of day-to-day variation still reads as visible movement instead
    of a flat line. min_range optionally floors that span, padded
    symmetrically around the data's own midpoint (e.g. weight: a 230-235 lb
    cluster gets padded to 225-240 rather than being stretched to fill the
    full height and look more dramatic than it is). X is proportional to
    day_offset (not list position), so real time gaps in sparse data (e.g.
    weight) show up as stretched flat segments rather than being compressed
    away. Returns None if there's nothing to plot at all.

    "min"/"max" in the result are the actual data's extremes (for a caption
    describing what really happened); "axis_min"/"axis_max" are the
    (possibly min_range-padded) bounds the y-axis and its labels actually
    use -- these two can differ when min_range pads a tight cluster."""
    if not points:
        return None
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    axis_lo, axis_hi = lo, hi
    if min_range is not None and (hi - lo) < min_range:
        mid = (hi + lo) / 2
        axis_lo, axis_hi = mid - min_range / 2, mid + min_range / 2
    axis_span = axis_hi - axis_lo

    denom = max(window_days - 1, 1)
    plot_left, plot_right = margin_left, width - margin_right
    plot_top, plot_bottom = margin_top, height - margin_bottom
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    def x_at(offset):
        return plot_left + plot_width * offset / denom

    def y_at(v):
        if axis_span == 0:
            return plot_top + plot_height / 2
        return plot_top + plot_height * (1 - (v - axis_lo) / axis_span)

    coords = " ".join(f"{x_at(offset):.1f},{y_at(v):.1f}" for offset, v in points)
    return {
        "points": coords,
        "width": width,
        "height": height,
        "min": lo,
        "max": hi,
        "latest": points[-1][1],
        "axis_min": axis_lo,
        "axis_max": axis_hi,
        "plot_left": plot_left,
        "plot_right": plot_right,
        "plot_top": plot_top,
        "plot_bottom": plot_bottom,
    }


app = Flask(__name__)
app.secret_key = SECRET_KEY or secrets.token_hex(32)
app.config.update(
    # Sliding 30-day login: Flask re-issues a permanent session's cookie (with
    # a fresh expiry) on every request by default, so regular use never logs
    # you out -- only 30 days without a visit does.
    PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure whenever auth is on (production is HTTPS-only). ALLOW_INSECURE_COOKIES=1
    # is an opt-out for trying the login flow over plain http://localhost.
    SESSION_COOKIE_SECURE=bool(AUTH_PASSWORD_HASH)
    and os.environ.get("ALLOW_INSECURE_COOKIES") != "1",
)

app.teardown_appcontext(dbmod.close_db)
dbmod.init_db()


def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = _csrf_token
app.jinja_env.filters["fiber"] = _format_fiber


@app.context_processor
def _auth_template_context():
    return {"auth_enabled": bool(AUTH_PASSWORD_HASH)}


def _safe_next_path(raw):
    # Only ever redirect to a local path -- "//host" and "/\host" are
    # protocol-relative URLs browsers will happily follow off-site.
    # raw is relative to the app (request.full_path excludes the mount
    # prefix), so re-add script_root -- in production Passenger mounts the
    # app under /food-tracker, which url_for() adds itself but this doesn't.
    if raw and raw.startswith("/") and not raw.startswith(("//", "/\\")):
        return request.script_root + raw
    return url_for("food_page")


@app.before_request
def _require_auth_and_csrf():
    # AUTH_PASSWORD_HASH unset (e.g. local dev, or the test suite, which never
    # sets it) means the login gate is off -- consistent with how
    # ANTHROPIC_API_KEY is mocked away rather than required in tests.
    # Production must set it. CSRF is checked either way, so a form missing
    # its token fails locally too, not only once deployed.
    if request.endpoint == "static":
        return None
    if (
        AUTH_PASSWORD_HASH
        and request.endpoint != "login"
        and not session.get("authenticated")
    ):
        next_path = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("login", next=next_path))
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not secrets.compare_digest(sent, expected):
            abort(400, "Form expired or invalid -- reload the page and try again.")
    return None


def _recent_login_failures(db, ip):
    return db.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE ip = ? AND attempted_at > datetime('now', ?)",
        (ip, f"-{LOGIN_FAILURE_WINDOW_MINUTES} minutes"),
    ).fetchone()[0]


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_PASSWORD_HASH or session.get("authenticated"):
        return redirect(url_for("food_page"))
    next_path = request.values.get("next", "")
    if request.method == "GET":
        return render_template("login.html", next_path=next_path)

    db = dbmod.get_db()
    ip = request.remote_addr or "unknown"
    # Locked out: refuse without even checking the password, so a guess made
    # during lockout can't succeed (or reveal anything) and isn't counted.
    if _recent_login_failures(db, ip) >= LOGIN_MAX_FAILURES:
        flash(
            f"Too many failed attempts. Try again in {LOGIN_FAILURE_WINDOW_MINUTES} minutes.",
            "error",
        )
        return render_template("login.html", next_path=next_path), 429

    if not check_password_hash(AUTH_PASSWORD_HASH, request.form.get("password", "")):
        db.execute(
            "DELETE FROM login_attempts WHERE attempted_at <= datetime('now', ?)",
            (f"-{LOGIN_FAILURE_WINDOW_MINUTES} minutes",),
        )
        db.execute("INSERT INTO login_attempts (ip) VALUES (?)", (ip,))
        db.commit()
        flash("Incorrect password.", "error")
        return render_template("login.html", next_path=next_path), 401

    db.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))
    db.commit()
    # Fresh session on login, so the pre-login CSRF token doesn't carry over.
    session.clear()
    session.permanent = True
    session["authenticated"] = True
    return redirect(_safe_next_path(next_path))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    if AUTH_PASSWORD_HASH:
        return redirect(url_for("login"))
    return redirect(url_for("food_page"))


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
    description = _describe_item(
        item["food"], item["estimated_calories"], item.get("estimated_fiber_grams")
    )
    if item.get("assumption_note"):
        description += f' -- {item["assumption_note"]}'
    return description


@app.route("/trends", methods=["GET"])
def trends_page():
    db = dbmod.get_db()

    window_start = date.today() - timedelta(days=TRENDS_WINDOW_DAYS - 1)
    x_axis_start_label = f"{window_start.month}/{window_start.day}"
    x_axis_end_label = f"{date.today().month}/{date.today().day}"

    weight_points = _weight_trend(db)
    weight_sparkline = _sparkline_svg(weight_points, TRENDS_WINDOW_DAYS, min_range=WEIGHT_MIN_AXIS_RANGE)
    weight_caption = None
    if weight_sparkline:
        weight_sparkline["axis_min_label"] = f'{weight_sparkline["axis_min"]:g}'
        weight_sparkline["axis_max_label"] = f'{weight_sparkline["axis_max"]:g}'
        latest_offset, latest_value = weight_points[-1]
        latest_date = date.today() - timedelta(days=TRENDS_WINDOW_DAYS - 1 - latest_offset)
        when = "" if latest_date == date.today() else f" on {latest_date.isoformat()}"
        weight_caption = (
            f'{latest_value:g} lbs{when} · '
            f'range {weight_sparkline["min"]:g}–{weight_sparkline["max"]:g}'
        )

    calorie_points = _calorie_trend(db)
    calorie_sparkline = _sparkline_svg(calorie_points, TRENDS_WINDOW_DAYS)
    calorie_caption = None
    if calorie_sparkline:
        calorie_sparkline["axis_min_label"] = f'{calorie_sparkline["axis_min"]:g}'
        calorie_sparkline["axis_max_label"] = f'{calorie_sparkline["axis_max"]:g}'
        calorie_caption = (
            f'{calorie_sparkline["latest"]} cal today · '
            f'range {calorie_sparkline["min"]}–{calorie_sparkline["max"]}'
        )

    fiber_points = _fiber_trend(db)
    fiber_sparkline = _sparkline_svg(fiber_points, TRENDS_WINDOW_DAYS)
    fiber_caption = None
    if fiber_sparkline:
        fiber_sparkline["axis_min_label"] = f'{fiber_sparkline["axis_min"]:g}'
        fiber_sparkline["axis_max_label"] = f'{fiber_sparkline["axis_max"]:g}'
        latest_offset, latest_value = fiber_points[-1]
        latest_date = date.today() - timedelta(days=TRENDS_WINDOW_DAYS - 1 - latest_offset)
        when = "today" if latest_date == date.today() else f"on {latest_date.isoformat()}"
        fiber_caption = (
            f'{latest_value} g {when} · '
            f'range {fiber_sparkline["min"]}–{fiber_sparkline["max"]}'
        )

    return render_template(
        "trends.html",
        window_days=TRENDS_WINDOW_DAYS,
        x_axis_start_label=x_axis_start_label,
        x_axis_end_label=x_axis_end_label,
        weight_sparkline=weight_sparkline,
        weight_caption=weight_caption,
        calorie_sparkline=calorie_sparkline,
        calorie_caption=calorie_caption,
        fiber_sparkline=fiber_sparkline,
        fiber_caption=fiber_caption,
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
                (log_entry_id, description, quantity, calories, fiber_grams,
                 is_estimate, assumption_note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_entry_id,
                item["food"],
                item.get("quantity"),
                item["estimated_calories"],
                # .get: a missing fiber estimate is stored as unknown rather
                # than failing the whole log.
                item.get("estimated_fiber_grams"),
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

    fiber, fiber_ok = _parse_optional_fiber(request.form.get("fiber"))
    if not fiber_ok:
        flash("Fiber must be a whole number of grams (0 or more), or left blank.", "error")
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
            (log_entry_id, description, calories, fiber_grams, is_estimate)
        VALUES (?, ?, ?, ?, 0)
        """,
        (cursor.lastrowid, food, calories, fiber),
    )
    db.commit()

    flash(f"Logged: {_describe_item(food, calories, fiber)}", "success")
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
        SELECT description, quantity, calories, fiber_grams, is_estimate, assumption_note
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
                (log_entry_id, description, quantity, calories, fiber_grams,
                 is_estimate, assumption_note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_entry_id,
                item["description"],
                item["quantity"],
                item["calories"],
                item["fiber_grams"],
                item["is_estimate"],
                item["assumption_note"],
            ),
        )
    db.commit()

    summary = ", ".join(
        _describe_item(item["description"], item["calories"], item["fiber_grams"])
        for item in source_items
    )
    flash(f"Logged: {summary}", "success")
    return redirect(url_for("food_page"))


@app.route("/food/entries/<int:item_id>/edit", methods=["GET"])
def food_entry_edit_form(item_id):
    db = dbmod.get_db()
    entry = db.execute(
        "SELECT id AS item_id, description, calories, fiber_grams FROM log_entry_items WHERE id = ?",
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

    fiber, fiber_ok = _parse_optional_fiber(request.form.get("fiber"))
    if not fiber_ok:
        flash("Fiber must be a whole number of grams (0 or more), or left blank.", "error")
        return redirect(url_for("food_entry_edit_form", item_id=item_id))

    db.execute(
        "UPDATE log_entry_items SET description = ?, calories = ?, fiber_grams = ? WHERE id = ?",
        (food, calories, fiber, item_id),
    )
    db.commit()

    flash(f"Updated: {_describe_item(food, calories, fiber)}", "success")
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
