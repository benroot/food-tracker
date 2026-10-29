import os
import re
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from flask.testing import FlaskClient
from werkzeug.security import generate_password_hash

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["FOOD_LOG_DB_PATH"] = _db_path

import app as appmod  # noqa: E402  (import after FOOD_LOG_DB_PATH is set, so db.py picks up the scratch file)
import db as dbmod  # noqa: E402


def tearDownModule():
    os.remove(_db_path)

TOOL_INPUT_APPLE = {
    "items": [
        {
            "food": "Apple",
            "quantity": "1 medium",
            "estimated_calories": 95,
            "estimated_fiber_grams": 4,
            "is_estimate": True,
            "assumption_note": "Assumed 1 medium apple",
        }
    ],
    "needs_clarification": False,
}

TOOL_INPUT_CLARIFY = {
    "items": [],
    "needs_clarification": True,
    "clarification_question": "How many cookies?",
}

TOOL_INPUT_RUN = {
    "activity": "Ran 3 miles",
    "estimated_calories_burned": 350,
    "is_estimate": True,
    "assumption_note": "Assumed a moderate pace, ~150 lb runner",
    "needs_clarification": False,
}

TOOL_INPUT_EXERCISE_CLARIFY = {
    "needs_clarification": True,
    "clarification_question": "How long did you do yoga for?",
}


def fake_response(tool_input, tool_use_id="toolu_test", tool_name="log_food_entry"):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input}
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


CSRF_TEST_TOKEN = "test-csrf-token"


class CsrfClient(FlaskClient):
    """Test client that sends CSRF_TEST_TOKEN with every POST, so route tests
    don't each have to scrape a token first. CsrfTests below covers the
    check itself, and that every rendered POST form actually carries one."""

    def post(self, *args, **kwargs):
        data = dict(kwargs.get("data") or {})
        data.setdefault("csrf_token", CSRF_TEST_TOKEN)
        kwargs["data"] = data
        return super().post(*args, **kwargs)


class FoodTrackerTestCase(unittest.TestCase):
    """Shared setup: fresh tables in the scratch SQLite file before each test."""

    def setUp(self):
        dbmod.init_db()
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            conn.executescript(
                "DELETE FROM log_entry_items; DELETE FROM log_entries; "
                "DELETE FROM weight_log; DELETE FROM exercise_log; "
                "DELETE FROM login_attempts;"
            )
        appmod.app.test_client_class = CsrfClient
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = CSRF_TEST_TOKEN


class ResolveEntryDateTests(unittest.TestCase):
    def test_today_offset(self):
        self.assertEqual(appmod._resolve_entry_date("0"), date.today())

    def test_yesterday_offset(self):
        self.assertEqual(appmod._resolve_entry_date("-1"), date.today() - timedelta(days=1))

    def test_missing_or_malformed_defaults_to_today(self):
        self.assertEqual(appmod._resolve_entry_date(None), date.today())
        self.assertEqual(appmod._resolve_entry_date(""), date.today())
        self.assertEqual(appmod._resolve_entry_date("abc"), date.today())

    def test_out_of_range_offset_defaults_to_today(self):
        self.assertEqual(appmod._resolve_entry_date("1"), date.today())
        self.assertEqual(appmod._resolve_entry_date("-2"), date.today())


class DateSelectorRouteTests(FoodTrackerTestCase):
    def test_food_log_direct_can_target_yesterday(self):
        self.client.post(
            "/food/log-direct",
            data={
                "food": "Leftover pizza",
                "calories": "400",
                "entry_date_offset": "-1",
            },
        )

        # entry_date lives on log_entries, not log_entry_items -- join to get it.
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_date = conn.execute(
                """
                SELECT le.entry_date FROM log_entries le
                JOIN log_entry_items lei ON lei.log_entry_id = le.id
                WHERE lei.description = 'Leftover pizza'
                """
            ).fetchone()[0]
        self.assertEqual(entry_date, (date.today() - timedelta(days=1)).isoformat())

    def test_food_log_direct_defaults_to_today_when_offset_missing(self):
        self.client.post(
            "/food/log-direct",
            data={"food": "Cereal", "calories": "200"},
        )

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_date = conn.execute(
                """
                SELECT le.entry_date FROM log_entries le
                JOIN log_entry_items lei ON lei.log_entry_id = le.id
                WHERE lei.description = 'Cereal'
                """
            ).fetchone()[0]
        self.assertEqual(entry_date, date.today().isoformat())

    @patch("claude_client.call_claude")
    def test_food_log_chat_can_target_yesterday(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(TOOL_INPUT_APPLE)

        self.client.post(
            "/food/log", data={"message": "an apple", "entry_date_offset": "-1"}
        )

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_date = conn.execute("SELECT entry_date FROM log_entries").fetchone()[0]
        self.assertEqual(entry_date, (date.today() - timedelta(days=1)).isoformat())

    def test_food_repeat_can_target_yesterday(self):
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            cursor = conn.execute(
                "INSERT INTO log_entries (entry_date, entry_time) VALUES ('2026-08-20', '08:00')"
            )
            entry_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO log_entry_items (log_entry_id, description, calories, is_estimate) "
                "VALUES (?, 'Oatmeal', 200, 0)",
                (entry_id,),
            )

        self.client.post(
            "/food/repeat", data={"entry_id": str(entry_id), "entry_date_offset": "-1"}
        )

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_date = conn.execute(
                "SELECT entry_date FROM log_entries WHERE entry_date != '2026-08-20'"
            ).fetchone()[0]
        self.assertEqual(entry_date, (date.today() - timedelta(days=1)).isoformat())

    def test_exercise_log_direct_can_target_yesterday(self):
        self.client.post(
            "/exercise/log-direct",
            data={"activity": "Pushups", "calories": "50", "entry_date_offset": "-1"},
        )

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_date = conn.execute("SELECT entry_date FROM exercise_log").fetchone()[0]
        self.assertEqual(entry_date, (date.today() - timedelta(days=1)).isoformat())

    @patch("claude_client.call_claude")
    def test_exercise_log_chat_can_target_yesterday(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(TOOL_INPUT_RUN, tool_name="log_exercise_entry")

        self.client.post(
            "/exercise/log", data={"message": "ran 3 miles", "entry_date_offset": "-1"}
        )

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_date = conn.execute("SELECT entry_date FROM exercise_log").fetchone()[0]
        self.assertEqual(entry_date, (date.today() - timedelta(days=1)).isoformat())

    def test_exercise_repeat_can_target_yesterday(self):
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            cursor = conn.execute(
                "INSERT INTO exercise_log (entry_date, entry_time, activity, calories_burned) "
                "VALUES ('2026-08-20', '08:00', 'Yoga', 120)"
            )
            entry_id = cursor.lastrowid

        self.client.post(
            "/exercise/repeat", data={"entry_id": str(entry_id), "entry_date_offset": "-1"}
        )

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_date = conn.execute(
                "SELECT entry_date FROM exercise_log WHERE entry_date != '2026-08-20'"
            ).fetchone()[0]
        self.assertEqual(entry_date, (date.today() - timedelta(days=1)).isoformat())

    def test_food_page_shows_both_today_and_yesterday_entries(self):
        self.client.post(
            "/food/log-direct",
            data={"food": "Bagel", "calories": "300"},
        )
        self.client.post(
            "/food/log-direct",
            data={
                "food": "Leftover tacos",
                "calories": "500",
                "entry_date_offset": "-1",
            },
        )

        response = self.client.get("/")

        self.assertIn(b"Bagel", response.data)
        self.assertIn(b"Leftover tacos", response.data)

    def test_exercise_page_shows_both_today_and_yesterday_entries(self):
        self.client.post(
            "/exercise/log-direct", data={"activity": "Pushups", "calories": "50"}
        )
        self.client.post(
            "/exercise/log-direct",
            data={"activity": "Cycled to work", "calories": "200", "entry_date_offset": "-1"},
        )

        response = self.client.get("/exercise")

        self.assertIn(b"Pushups", response.data)
        self.assertIn(b"Cycled to work", response.data)


class FoodLogRouteTests(FoodTrackerTestCase):
    @patch("claude_client.call_claude")
    def test_logging_a_clear_entry_writes_to_db_and_flashes_summary(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(TOOL_INPUT_APPLE)

        response = self.client.post(
            "/food/log", data={"message": "an apple for breakfast"}, follow_redirects=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Apple", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            row = conn.execute("SELECT calories FROM log_entry_items").fetchone()
        self.assertEqual(row[0], 95)

    @patch("claude_client.call_claude")
    def test_flash_message_includes_assumption_note_when_present(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(TOOL_INPUT_APPLE)

        response = self.client.post(
            "/food/log", data={"message": "an apple"}, follow_redirects=True
        )

        self.assertIn(b"Assumed 1 medium apple", response.data)

    @patch("claude_client.call_claude")
    def test_flash_message_omits_note_separator_when_absent(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(
            {
                "items": [{"food": "Water", "estimated_calories": 0, "is_estimate": False}],
                "needs_clarification": False,
            }
        )

        response = self.client.post(
            "/food/log", data={"message": "a glass of water"}, follow_redirects=True
        )

        self.assertIn(b"Logged: Water (0 cal)", response.data)

    @patch("claude_client.call_claude")
    def test_ambiguous_entry_holds_for_clarification_without_writing_to_db(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(TOOL_INPUT_CLARIFY)

        response = self.client.post(
            "/food/log", data={"message": "some cookies"}, follow_redirects=True
        )

        self.assertIn(b"How many cookies?", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
        self.assertEqual(count, 0)

    @patch("claude_client.call_claude")
    def test_api_failure_flashes_error_and_does_not_crash(self, mock_call_claude):
        mock_call_claude.side_effect = Exception("boom")

        response = self.client.post(
            "/food/log", data={"message": "an apple"}, follow_redirects=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Couldn", response.data)

    @patch("claude_client.call_claude")
    def test_explicit_entry_time_from_message_is_used(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(
            {
                "items": [{"food": "Omelette", "estimated_calories": 400, "is_estimate": False}],
                "entry_time": "07:00",
                "needs_clarification": False,
            }
        )

        self.client.post(
            "/food/log", data={"message": "breakfast at 7am: omelette for 400 calories"}
        )

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_time = conn.execute("SELECT entry_time FROM log_entries").fetchone()[0]
        self.assertEqual(entry_time, "07:00")

    @patch("claude_client.call_claude")
    def test_malformed_entry_time_falls_back_to_current_time(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(
            {
                "items": [{"food": "Toast", "estimated_calories": 100, "is_estimate": True}],
                "entry_time": "7am",
                "needs_clarification": False,
            }
        )

        self.client.post("/food/log", data={"message": "toast"})

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_time = conn.execute("SELECT entry_time FROM log_entries").fetchone()[0]
        self.assertRegex(entry_time, r"^([01]\d|2[0-3]):[0-5]\d$")

    @patch("claude_client.call_claude")
    def test_missing_entry_time_falls_back_to_current_time(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(TOOL_INPUT_APPLE)

        self.client.post("/food/log", data={"message": "an apple"})

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_time = conn.execute("SELECT entry_time FROM log_entries").fetchone()[0]
        self.assertRegex(entry_time, r"^([01]\d|2[0-3]):[0-5]\d$")


class ExerciseLogRouteTests(FoodTrackerTestCase):
    @patch("claude_client.call_claude")
    def test_logging_a_clear_activity_writes_to_db_and_flashes_summary(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(TOOL_INPUT_RUN, tool_name="log_exercise_entry")

        response = self.client.post(
            "/exercise/log", data={"message": "ran 3 miles"}, follow_redirects=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Ran 3 miles", response.data)
        self.assertIn(b"Assumed a moderate pace", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            row = conn.execute(
                "SELECT activity, calories_burned, is_estimate FROM exercise_log"
            ).fetchone()
        self.assertEqual(row, ("Ran 3 miles", 350, 1))

    @patch("claude_client.call_claude")
    def test_ambiguous_activity_holds_for_clarification_without_writing_to_db(
        self, mock_call_claude
    ):
        mock_call_claude.return_value = fake_response(
            TOOL_INPUT_EXERCISE_CLARIFY, tool_name="log_exercise_entry"
        )

        response = self.client.post(
            "/exercise/log", data={"message": "did some yoga"}, follow_redirects=True
        )

        self.assertIn(b"How long did you do yoga for?", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM exercise_log").fetchone()[0]
        self.assertEqual(count, 0)

    @patch("claude_client.call_claude")
    def test_api_failure_flashes_error_and_does_not_crash(self, mock_call_claude):
        mock_call_claude.side_effect = Exception("boom")

        response = self.client.post(
            "/exercise/log", data={"message": "ran 3 miles"}, follow_redirects=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Couldn", response.data)

    @patch("claude_client.call_claude")
    def test_explicit_entry_time_from_message_is_used(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(
            {**TOOL_INPUT_RUN, "entry_time": "06:30"}, tool_name="log_exercise_entry"
        )

        self.client.post("/exercise/log", data={"message": "ran 3 miles at 6:30am"})

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_time = conn.execute("SELECT entry_time FROM exercise_log").fetchone()[0]
        self.assertEqual(entry_time, "06:30")

    @patch("claude_client.call_claude")
    def test_missing_entry_time_falls_back_to_current_time(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(TOOL_INPUT_RUN, tool_name="log_exercise_entry")

        self.client.post("/exercise/log", data={"message": "ran 3 miles"})

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_time = conn.execute("SELECT entry_time FROM exercise_log").fetchone()[0]
        self.assertRegex(entry_time, r"^([01]\d|2[0-3]):[0-5]\d$")

    def test_exercise_page_shows_todays_total(self):
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO exercise_log (entry_date, entry_time, activity, calories_burned) "
                "VALUES (?, '07:00', 'Ran 3 miles', 350)",
                (date.today().isoformat(),),
            )

        response = self.client.get("/exercise")

        self.assertIn(b"350 cal burned", response.data)
        self.assertIn(b"Ran 3 miles", response.data)


class ManualExerciseLogRouteTests(FoodTrackerTestCase):
    def test_valid_entry_writes_to_db_as_non_estimate_without_calling_claude(self):
        with patch("claude_client.call_claude") as mock_call_claude:
            response = self.client.post(
                "/exercise/log-direct",
                data={"activity": "Pushups", "calories": "50"},
                follow_redirects=True,
            )
            mock_call_claude.assert_not_called()

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Pushups", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            row = conn.execute(
                "SELECT activity, calories_burned, is_estimate FROM exercise_log"
            ).fetchone()
        self.assertEqual(row, ("Pushups", 50, 0))

    def test_missing_activity_does_not_write_and_flashes_error(self):
        response = self.client.post(
            "/exercise/log-direct",
            data={"activity": "", "calories": "50"},
            follow_redirects=True,
        )

        self.assertIn(b"Enter an activity", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM exercise_log").fetchone()[0]
        self.assertEqual(count, 0)

    def test_non_numeric_calories_does_not_write_and_flashes_error(self):
        response = self.client.post(
            "/exercise/log-direct",
            data={"activity": "Pushups", "calories": "a lot"},
            follow_redirects=True,
        )

        self.assertIn(b"whole number", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM exercise_log").fetchone()[0]
        self.assertEqual(count, 0)

    def test_zero_calories_is_rejected(self):
        response = self.client.post(
            "/exercise/log-direct",
            data={"activity": "Stretching", "calories": "0"},
            follow_redirects=True,
        )

        self.assertIn(b"whole number", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM exercise_log").fetchone()[0]
        self.assertEqual(count, 0)


class EditDeleteExerciseEntryRouteTests(FoodTrackerTestCase):
    def _create_entry(self, activity="Pushups", calories=50):
        self.client.post(
            "/exercise/log-direct", data={"activity": activity, "calories": str(calories)}
        )
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            return conn.execute("SELECT id FROM exercise_log").fetchone()[0]

    def test_edit_form_shows_current_values(self):
        entry_id = self._create_entry(activity="Pushups", calories=50)

        response = self.client.get(f"/exercise/entries/{entry_id}/edit")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'value="Pushups"', response.data)
        self.assertIn(b'value="50"', response.data)

    def test_edit_updates_activity_and_calories(self):
        entry_id = self._create_entry(activity="Pushups", calories=50)

        response = self.client.post(
            f"/exercise/entries/{entry_id}/edit",
            data={"activity": "100 pushups", "calories": "80"},
            follow_redirects=True,
        )

        self.assertIn(b"100 pushups", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            row = conn.execute(
                "SELECT activity, calories_burned FROM exercise_log WHERE id = ?", (entry_id,)
            ).fetchone()
        self.assertEqual(row, ("100 pushups", 80))

    def test_edit_rejects_invalid_calories_without_writing(self):
        entry_id = self._create_entry(activity="Pushups", calories=50)

        response = self.client.post(
            f"/exercise/entries/{entry_id}/edit",
            data={"activity": "Pushups", "calories": "not a number"},
            follow_redirects=True,
        )

        self.assertIn(b"whole number", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            calories = conn.execute(
                "SELECT calories_burned FROM exercise_log WHERE id = ?", (entry_id,)
            ).fetchone()[0]
        self.assertEqual(calories, 50)

    def test_edit_nonexistent_entry_flashes_error_and_redirects(self):
        response = self.client.get("/exercise/entries/9999/edit", follow_redirects=True)
        self.assertIn(b"no longer exists", response.data)

    def test_delete_removes_entry(self):
        entry_id = self._create_entry()

        response = self.client.post(
            f"/exercise/entries/{entry_id}/delete", follow_redirects=True
        )

        self.assertIn(b"Deleted", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM exercise_log").fetchone()[0]
        self.assertEqual(count, 0)

    def test_delete_nonexistent_entry_flashes_error_and_redirects(self):
        response = self.client.post(
            "/exercise/entries/9999/delete", follow_redirects=True
        )
        self.assertIn(b"no longer exists", response.data)


class RepeatExerciseRouteTests(FoodTrackerTestCase):
    def _seed_past_entry(self, entry_date, activity, calories):
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            cursor = conn.execute(
                "INSERT INTO exercise_log (entry_date, entry_time, activity, calories_burned) "
                "VALUES (?, '08:00', ?, ?)",
                (entry_date, activity, calories),
            )
            return cursor.lastrowid

    def test_exercise_page_shows_up_to_three_most_recent_newest_first(self):
        self._seed_past_entry("2026-08-19", "Cereal walk", 50)
        self._seed_past_entry("2026-08-20", "Yoga", 120)
        self._seed_past_entry("2026-08-22", "Ran 3 miles", 350)
        self._seed_past_entry("2026-08-21", "Pushups", 50)

        response = self.client.get("/exercise")
        body = response.data.decode()

        self.assertIn("Ran 3 miles", body)
        self.assertIn("Pushups", body)
        self.assertIn("Yoga", body)
        self.assertNotIn("Cereal walk", body)
        self.assertLess(body.index("Ran 3 miles"), body.index("Pushups"))
        self.assertLess(body.index("Pushups"), body.index("Yoga"))

    def test_todays_own_entry_is_included_in_repeat_options(self):
        today = date.today().isoformat()
        self._seed_past_entry(today, "Cycled to work", 200)

        response = self.client.get("/exercise")

        self.assertIn(b"Cycled to work", response.data)
        self.assertNotIn(b"Nothing to repeat yet", response.data)

    def test_repeating_an_activity_copies_it_to_today(self):
        entry_id = self._seed_past_entry("2026-08-20", "Ran 3 miles", 350)

        response = self.client.post(
            "/exercise/repeat", data={"entry_id": str(entry_id)}, follow_redirects=True
        )

        self.assertIn(b"Ran 3 miles", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            row = conn.execute(
                "SELECT activity, calories_burned FROM exercise_log WHERE entry_date = ?",
                (date.today().isoformat(),),
            ).fetchone()
        self.assertEqual(row, ("Ran 3 miles", 350))

    def test_repeating_with_explicit_time_uses_that_time(self):
        entry_id = self._seed_past_entry("2026-08-20", "Ran 3 miles", 350)

        self.client.post(
            "/exercise/repeat", data={"entry_id": str(entry_id), "entry_time": "06:30"}
        )

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_time = conn.execute(
                "SELECT entry_time FROM exercise_log WHERE entry_date = ?",
                (date.today().isoformat(),),
            ).fetchone()[0]
        self.assertEqual(entry_time, "06:30")

    def test_repeating_nonexistent_entry_flashes_error(self):
        response = self.client.post(
            "/exercise/repeat", data={"entry_id": "9999"}, follow_redirects=True
        )
        self.assertIn(b"no longer exists", response.data)

    def test_repeating_with_missing_entry_id_flashes_error(self):
        response = self.client.post("/exercise/repeat", data={}, follow_redirects=True)
        self.assertIn(b"no longer exists", response.data)


class ManualFoodLogRouteTests(FoodTrackerTestCase):
    def test_valid_entry_writes_to_db_as_non_estimate_without_calling_claude(self):
        with patch("claude_client.call_claude") as mock_call_claude:
            response = self.client.post(
                "/food/log-direct",
                data={"food": "Protein bar", "calories": "200"},
                follow_redirects=True,
            )
            mock_call_claude.assert_not_called()

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Protein bar", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            row = conn.execute(
                "SELECT description, calories, is_estimate FROM log_entry_items"
            ).fetchone()
        self.assertEqual(row, ("Protein bar", 200, 0))

    def test_non_numeric_calories_does_not_write_and_flashes_error(self):
        response = self.client.post(
            "/food/log-direct",
            data={"food": "Protein bar", "calories": "a lot"},
            follow_redirects=True,
        )

        self.assertIn(b"whole number", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
        self.assertEqual(count, 0)

    def test_zero_calories_is_rejected(self):
        response = self.client.post(
            "/food/log-direct",
            data={"food": "Water", "calories": "0"},
            follow_redirects=True,
        )

        self.assertIn(b"whole number", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
        self.assertEqual(count, 0)

    def test_missing_food_label_does_not_write_and_flashes_error(self):
        response = self.client.post(
            "/food/log-direct",
            data={"food": "", "calories": "100"},
            follow_redirects=True,
        )

        self.assertIn(b"Enter a food label", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
        self.assertEqual(count, 0)


class FoodPageOrderingTests(FoodTrackerTestCase):
    def test_todays_entries_show_most_recently_logged_first(self):
        self.client.post(
            "/food/log-direct",
            data={"food": "Coffee", "calories": "5"},
            follow_redirects=True,
        )
        self.client.post(
            "/food/log-direct",
            data={"food": "Toast", "calories": "150"},
            follow_redirects=True,
        )

        response = self.client.get("/")
        body = response.data.decode()

        self.assertLess(body.index("Toast"), body.index("Coffee"))


class NetCaloriesDisplayTests(FoodTrackerTestCase):
    def test_plain_total_shown_when_no_exercise_logged_today(self):
        self.client.post(
            "/food/log-direct",
            data={"food": "Toast", "calories": "150"},
        )

        response = self.client.get("/")

        self.assertIn(b"150 cal</span>", response.data)
        self.assertNotIn(b"cal net", response.data)
        self.assertNotIn(b"consumed", response.data)

    def test_net_calories_shown_when_exercise_logged_today(self):
        self.client.post(
            "/food/log-direct",
            data={"food": "Toast", "calories": "150"},
        )
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO exercise_log (entry_date, entry_time, activity, calories_burned) "
                "VALUES (?, '07:00', 'Ran 3 miles', 350)",
                (date.today().isoformat(),),
            )

        response = self.client.get("/")

        self.assertIn(b"-200 cal net", response.data)
        self.assertIn(b"150 consumed", response.data)
        self.assertIn(b"350 burned", response.data)


class EditDeleteEntryRouteTests(FoodTrackerTestCase):
    def _create_single_item_entry(self, food="Toast", calories=120):
        self.client.post(
            "/food/log-direct", data={"food": food, "calories": str(calories)}
        )
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            return conn.execute("SELECT id FROM log_entry_items").fetchone()[0]

    def _create_multi_item_entry(self):
        with patch("claude_client.call_claude") as mock_call_claude:
            mock_call_claude.return_value = fake_response(
                {
                    "items": [
                        {"food": "Eggs", "estimated_calories": 140, "is_estimate": True},
                        {"food": "Toast", "estimated_calories": 90, "is_estimate": True},
                    ],
                    "needs_clarification": False,
                }
            )
            self.client.post("/food/log", data={"message": "eggs and toast"})
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            return conn.execute(
                "SELECT id FROM log_entry_items WHERE description = 'Eggs'"
            ).fetchone()[0]

    def test_edit_form_shows_current_values(self):
        item_id = self._create_single_item_entry(food="Toast", calories=120)

        response = self.client.get(f"/food/entries/{item_id}/edit")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'value="Toast"', response.data)
        self.assertIn(b'value="120"', response.data)

    def test_edit_updates_description_and_calories(self):
        item_id = self._create_single_item_entry(food="Toast", calories=120)

        response = self.client.post(
            f"/food/entries/{item_id}/edit",
            data={"food": "Buttered toast", "calories": "180"},
            follow_redirects=True,
        )

        self.assertIn(b"Buttered toast", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            item = conn.execute(
                "SELECT description, calories FROM log_entry_items WHERE id = ?", (item_id,)
            ).fetchone()
        self.assertEqual(item, ("Buttered toast", 180))

    def test_edit_rejects_invalid_calories_without_writing(self):
        item_id = self._create_single_item_entry(food="Toast", calories=120)

        response = self.client.post(
            f"/food/entries/{item_id}/edit",
            data={"food": "Toast", "calories": "not a number"},
            follow_redirects=True,
        )

        self.assertIn(b"whole number", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            calories = conn.execute(
                "SELECT calories FROM log_entry_items WHERE id = ?", (item_id,)
            ).fetchone()[0]
        self.assertEqual(calories, 120)

    def test_edit_nonexistent_item_flashes_error_and_redirects(self):
        response = self.client.get("/food/entries/9999/edit", follow_redirects=True)
        self.assertIn(b"no longer exists", response.data)

    def test_delete_removes_item_and_parent_entry_when_last_item(self):
        item_id = self._create_single_item_entry()

        response = self.client.post(f"/food/entries/{item_id}/delete", follow_redirects=True)

        self.assertIn(b"Deleted", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            item_count = conn.execute("SELECT COUNT(*) FROM log_entry_items").fetchone()[0]
            entry_count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
        self.assertEqual(item_count, 0)
        self.assertEqual(entry_count, 0)

    def test_delete_one_item_of_multi_item_meal_keeps_the_other(self):
        eggs_id = self._create_multi_item_entry()

        self.client.post(f"/food/entries/{eggs_id}/delete")

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            remaining = [
                row[0] for row in conn.execute("SELECT description FROM log_entry_items").fetchall()
            ]
            entry_count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
        self.assertEqual(remaining, ["Toast"])
        self.assertEqual(entry_count, 1)

    def test_delete_nonexistent_item_flashes_error_and_redirects(self):
        response = self.client.post("/food/entries/9999/delete", follow_redirects=True)
        self.assertIn(b"no longer exists", response.data)


class RepeatMealRouteTests(FoodTrackerTestCase):
    def _seed_past_entry(self, entry_date, items):
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            cursor = conn.execute(
                "INSERT INTO log_entries (entry_date, entry_time) VALUES (?, '08:00')",
                (entry_date,),
            )
            entry_id = cursor.lastrowid
            for description, calories, is_estimate, assumption_note in items:
                conn.execute(
                    """
                    INSERT INTO log_entry_items
                        (log_entry_id, description, calories, is_estimate, assumption_note)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (entry_id, description, calories, is_estimate, assumption_note),
                )
        return entry_id

    def test_food_page_shows_recent_meals_newest_first(self):
        self._seed_past_entry("2026-08-20", [("Oatmeal", 200, 0, None)])
        self._seed_past_entry("2026-08-22", [("Omelette", 400, 0, None)])
        self._seed_past_entry("2026-08-21", [("Toast", 150, 0, None)])

        response = self.client.get("/")
        body = response.data.decode()

        self.assertIn("Omelette", body)
        self.assertIn("Toast", body)
        self.assertIn("Oatmeal", body)
        self.assertLess(body.index("Omelette"), body.index("Toast"))
        self.assertLess(body.index("Toast"), body.index("Oatmeal"))

    def test_todays_own_entry_is_included_in_repeat_options(self):
        today = date.today().isoformat()
        self._seed_past_entry(today, [("Pancakes", 300, 0, None)])

        response = self.client.get("/")

        self.assertIn(b"Pancakes", response.data)
        self.assertNotIn(b"Nothing to repeat yet", response.data)

    def test_empty_state_when_no_past_meals(self):
        response = self.client.get("/")
        self.assertIn(b"Nothing to repeat yet", response.data)

    def test_repeating_a_meal_copies_items_to_today_preserving_estimate_flag(self):
        entry_id = self._seed_past_entry(
            "2026-08-20",
            [("Omelette", 400, 0, None), ("Juice", 110, 1, "Assumed 8oz")],
        )

        response = self.client.post(
            "/food/repeat", data={"entry_id": str(entry_id)}, follow_redirects=True
        )

        self.assertIn(b"Omelette", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            today_rows = conn.execute(
                """
                SELECT lei.description, lei.calories, lei.is_estimate, lei.assumption_note
                FROM log_entry_items lei
                JOIN log_entries le ON le.id = lei.log_entry_id
                WHERE le.entry_date = ?
                ORDER BY lei.id
                """,
                (date.today().isoformat(),),
            ).fetchall()
        self.assertEqual(len(today_rows), 2)
        self.assertEqual(tuple(today_rows[0]), ("Omelette", 400, 0, None))
        self.assertEqual(tuple(today_rows[1]), ("Juice", 110, 1, "Assumed 8oz"))

    def test_repeating_with_explicit_time_uses_that_time(self):
        entry_id = self._seed_past_entry("2026-08-20", [("Omelette", 400, 0, None)])

        self.client.post("/food/repeat", data={"entry_id": str(entry_id), "entry_time": "07:00"})

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_time = conn.execute(
                "SELECT entry_time FROM log_entries WHERE entry_date = ?",
                (date.today().isoformat(),),
            ).fetchone()[0]
        self.assertEqual(entry_time, "07:00")

    def test_repeating_with_blank_time_falls_back_to_current_time(self):
        entry_id = self._seed_past_entry("2026-08-20", [("Omelette", 400, 0, None)])

        self.client.post("/food/repeat", data={"entry_id": str(entry_id), "entry_time": ""})

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_time = conn.execute(
                "SELECT entry_time FROM log_entries WHERE entry_date = ?",
                (date.today().isoformat(),),
            ).fetchone()[0]
        self.assertRegex(entry_time, r"^([01]\d|2[0-3]):[0-5]\d$")

    def test_repeating_nonexistent_entry_flashes_error(self):
        response = self.client.post(
            "/food/repeat", data={"entry_id": "9999"}, follow_redirects=True
        )
        self.assertIn(b"no longer exists", response.data)

    def test_repeating_with_missing_entry_id_flashes_error(self):
        response = self.client.post("/food/repeat", data={}, follow_redirects=True)
        self.assertIn(b"no longer exists", response.data)


class RecentMealOptionsHeuristicTests(FoodTrackerTestCase):
    """Directly exercises the 7-day / 50-item repeat-options heuristic
    without needing to seed 50 real rows through the HTTP layer."""

    def _seed_meal(self, entry_date, description="Meal", calories=100):
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            cursor = conn.execute(
                "INSERT INTO log_entries (entry_date, entry_time) VALUES (?, '08:00')",
                (entry_date,),
            )
            entry_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO log_entry_items (log_entry_id, description, calories, is_estimate) "
                "VALUES (?, ?, ?, 0)",
                (entry_id, description, calories),
            )

    def _query(self, min_days, min_count):
        conn = sqlite3.connect(dbmod.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            return appmod._recent_meal_options(conn, min_days=min_days, min_count=min_count)
        finally:
            conn.close()

    def test_window_extends_backward_to_reach_min_count_when_sparse(self):
        self._seed_meal("2026-08-01", "Old meal")
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self._seed_meal(yesterday, "Yesterday meal")
        self._seed_meal(date.today().isoformat(), "Today meal")

        options = self._query(min_days=7, min_count=3)

        self.assertEqual(len(options), 3)
        self.assertEqual(options[0]["items"], ["Today meal"])
        self.assertEqual(options[2]["items"], ["Old meal"])

    def test_window_never_shrinks_below_min_days_even_past_min_count(self):
        for offset in range(4):
            day = (date.today() - timedelta(days=offset)).isoformat()
            self._seed_meal(day, f"Meal {offset}")

        options = self._query(min_days=7, min_count=2)

        self.assertEqual(len(options), 4)

    def test_reverse_chronological_order(self):
        self._seed_meal("2026-08-19", "Cereal")
        self._seed_meal("2026-08-20", "Oatmeal")
        self._seed_meal("2026-08-22", "Omelette")
        self._seed_meal("2026-08-21", "Toast")

        options = self._query(min_days=7, min_count=50)

        dates_in_order = [option["entry_date"] for option in options]
        self.assertEqual(dates_in_order, ["2026-08-22", "2026-08-21", "2026-08-20", "2026-08-19"])


class WeightLogRouteTests(FoodTrackerTestCase):
    def test_logging_weight_persists_and_shows_on_index(self):
        self.client.post("/log", data={"weight_lbs": "199.5"})
        response = self.client.get("/weight")
        self.assertIn(b"199.5", response.data)

    def test_history_shows_all_entries_in_reverse_chronological_order(self):
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            conn.executescript(
                """
                INSERT INTO weight_log (entry_date, weight_lbs) VALUES ('2026-08-20', 201.0);
                INSERT INTO weight_log (entry_date, weight_lbs) VALUES ('2026-08-22', 200.0);
                INSERT INTO weight_log (entry_date, weight_lbs) VALUES ('2026-08-21', 200.5);
                """
            )

        response = self.client.get("/weight")

        dates_in_order = [
            line for line in response.data.decode().splitlines() if "2026-08-2" in line
        ]
        self.assertEqual(
            [d.strip() for d in dates_in_order],
            ["<span>2026-08-22</span>", "<span>2026-08-21</span>", "<span>2026-08-20</span>"],
        )

    def test_no_entries_shows_empty_state(self):
        response = self.client.get("/weight")
        self.assertIn(b"No weight logged yet.", response.data)


class TrendsRouteTests(FoodTrackerTestCase):
    def _seed_entry(self, entry_date, items):
        """items: list of (description, calories) tuples logged as one meal event."""
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            cursor = conn.execute(
                "INSERT INTO log_entries (entry_date, entry_time) VALUES (?, '08:00')",
                (entry_date,),
            )
            entry_id = cursor.lastrowid
            for description, calories in items:
                conn.execute(
                    "INSERT INTO log_entry_items (log_entry_id, description, calories, is_estimate) "
                    "VALUES (?, ?, ?, 0)",
                    (entry_id, description, calories),
                )

    def _trends(self, window_days=30):
        conn = sqlite3.connect(dbmod.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            return appmod._daily_food_trends(conn, window_days=window_days)
        finally:
            conn.close()

    def test_page_renders_both_sparklines(self):
        self._seed_entry(date.today().isoformat(), [("Eggs", 300)])
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO weight_log (entry_date, weight_lbs) VALUES (?, ?)",
                (date.today().isoformat(), 182.5),
            )

        response = self.client.get("/trends")

        self.assertEqual(response.status_code, 200)
        rendered = response.data.decode()
        self.assertEqual(rendered.count("<svg"), 2)
        self.assertIn("182.5 lbs", rendered)
        self.assertIn("300 cal today", rendered)

    def test_no_weight_logged_shows_empty_state(self):
        response = self.client.get("/trends")
        self.assertIn(b"No weight logged in the last 30 days.", response.data)

    def test_multi_item_meal_counts_as_one_entry(self):
        today = date.today().isoformat()
        self._seed_entry(today, [("Eggs", 140), ("Toast", 120), ("Coffee", 5)])

        days = self._trends()

        todays_row = next(d for d in days if d["entry_date"] == today)
        self.assertEqual(todays_row["entry_count"], 1)
        self.assertEqual(todays_row["total_calories"], 265)

    def test_boundary_day_is_included_but_one_day_older_is_excluded(self):
        boundary = (date.today() - timedelta(days=29)).isoformat()
        too_old = (date.today() - timedelta(days=30)).isoformat()
        self._seed_entry(boundary, [("Boundary meal", 50)])
        self._seed_entry(too_old, [("Too old meal", 999)])

        days = self._trends(window_days=30)
        dates = [d["entry_date"] for d in days]

        self.assertIn(boundary, dates)
        self.assertNotIn(too_old, dates)
        boundary_row = next(d for d in days if d["entry_date"] == boundary)
        self.assertEqual(boundary_row["total_calories"], 50)


class WeightTrendTests(FoodTrackerTestCase):
    def _seed_weight(self, entry_date, weight_lbs):
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO weight_log (entry_date, weight_lbs) VALUES (?, ?)",
                (entry_date, weight_lbs),
            )

    def _trend(self, window_days=30):
        conn = sqlite3.connect(dbmod.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            return appmod._weight_trend(conn, window_days=window_days)
        finally:
            conn.close()

    def test_no_entries_returns_empty_list(self):
        self.assertEqual(self._trend(), [])

    def test_sparse_entries_get_correct_day_offsets(self):
        window_start = date.today() - timedelta(days=29)
        self._seed_weight(window_start.isoformat(), 180.0)
        self._seed_weight(date.today().isoformat(), 183.5)

        self.assertEqual(self._trend(), [(0, 180.0), (29, 183.5)])

    def test_entries_outside_window_are_excluded(self):
        too_old = (date.today() - timedelta(days=30)).isoformat()
        self._seed_weight(too_old, 999.0)

        self.assertEqual(self._trend(), [])


class CalorieTrendTests(FoodTrackerTestCase):
    def _seed_entry(self, entry_date, calories):
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            cursor = conn.execute(
                "INSERT INTO log_entries (entry_date, entry_time) VALUES (?, '08:00')",
                (entry_date,),
            )
            conn.execute(
                "INSERT INTO log_entry_items (log_entry_id, description, calories, is_estimate) "
                "VALUES (?, 'Meal', ?, 0)",
                (cursor.lastrowid, calories),
            )

    def _trend(self, window_days=30):
        conn = sqlite3.connect(dbmod.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            return appmod._calorie_trend(conn, window_days=window_days)
        finally:
            conn.close()

    def test_oldest_first_with_zero_fill(self):
        window_start = date.today() - timedelta(days=29)
        self._seed_entry(window_start.isoformat(), 300)
        self._seed_entry(date.today().isoformat(), 500)

        points = self._trend()

        self.assertEqual(len(points), 30)
        self.assertEqual(points[0], (0, 300))
        self.assertEqual(points[-1], (29, 500))
        self.assertEqual(points[15][1], 0)


class SparklineHelperTests(unittest.TestCase):
    def test_empty_points_returns_none(self):
        self.assertIsNone(appmod._sparkline_svg([], window_days=30))

    def test_min_value_maps_near_bottom_max_near_top(self):
        result = appmod._sparkline_svg([(0, 100), (29, 200)], window_days=30)
        coords = [pair.split(",") for pair in result["points"].split(" ")]
        first_x, first_y = float(coords[0][0]), float(coords[0][1])
        second_x, second_y = float(coords[1][0]), float(coords[1][1])

        self.assertLess(first_x, second_x)
        self.assertGreater(first_y, second_y)
        self.assertEqual(result["min"], 100)
        self.assertEqual(result["max"], 200)
        self.assertEqual(result["latest"], 200)
        # no min_range given -> axis matches the actual data extremes exactly
        self.assertEqual(result["axis_min"], 100)
        self.assertEqual(result["axis_max"], 200)

    def test_flat_data_centers_vertically(self):
        result = appmod._sparkline_svg([(0, 150), (10, 150), (29, 150)], window_days=30)
        ys = [float(pair.split(",")[1]) for pair in result["points"].split(" ")]
        expected_mid = (result["plot_top"] + result["plot_bottom"]) / 2
        self.assertTrue(all(y == expected_mid for y in ys))

    def test_x_position_is_time_proportional_not_index_based(self):
        result = appmod._sparkline_svg([(0, 100), (29, 200)], window_days=30)
        first_x = float(result["points"].split(" ")[0].split(",")[0])
        second_x = float(result["points"].split(" ")[1].split(",")[0])
        self.assertAlmostEqual(first_x, result["plot_left"], places=1)
        self.assertAlmostEqual(second_x, result["plot_right"], places=1)

    def test_min_range_pads_a_tight_cluster_symmetrically(self):
        # Mirrors the user's own example: data 230-235 (span 5, midpoint
        # 232.5) padded to a 15-wide axis -> 225-240, same midpoint.
        result = appmod._sparkline_svg([(0, 230), (29, 235)], window_days=30, min_range=15)
        self.assertEqual(result["min"], 230)
        self.assertEqual(result["max"], 235)
        self.assertEqual(result["axis_min"], 225)
        self.assertEqual(result["axis_max"], 240)

    def test_min_range_does_not_shrink_an_already_wider_span(self):
        result = appmod._sparkline_svg([(0, 100), (29, 200)], window_days=30, min_range=15)
        self.assertEqual(result["axis_min"], 100)
        self.assertEqual(result["axis_max"], 200)


def _seed_meal(entry_date, items, entry_time="08:00"):
    """items: list of (description, calories, fiber_grams) logged as one meal
    event; fiber_grams may be None (unknown)."""
    with sqlite3.connect(dbmod.DB_PATH) as conn:
        entry_id = conn.execute(
            "INSERT INTO log_entries (entry_date, entry_time) VALUES (?, ?)",
            (entry_date, entry_time),
        ).lastrowid
        for description, calories, fiber_grams in items:
            conn.execute(
                "INSERT INTO log_entry_items (log_entry_id, description, calories, fiber_grams) "
                "VALUES (?, ?, ?, ?)",
                (entry_id, description, calories, fiber_grams),
            )
    return entry_id


def _all_fiber():
    with sqlite3.connect(dbmod.DB_PATH) as conn:
        return [row[0] for row in conn.execute("SELECT fiber_grams FROM log_entry_items ORDER BY id")]


class FiberLoggingTests(FoodTrackerTestCase):
    @patch("claude_client.call_claude")
    def test_chat_log_stores_fiber_and_flashes_it(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(TOOL_INPUT_APPLE)

        response = self.client.post("/food/log", data={"message": "an apple"}, follow_redirects=True)

        self.assertEqual(_all_fiber(), [4])
        self.assertIn(b"Apple (95 cal, 4 g fiber)", response.data)

    @patch("claude_client.call_claude")
    def test_chat_log_missing_fiber_is_stored_as_unknown(self, mock_call_claude):
        mock_call_claude.return_value = fake_response(
            {
                "items": [{"food": "Mystery", "estimated_calories": 100, "is_estimate": True}],
                "needs_clarification": False,
            }
        )

        self.client.post("/food/log", data={"message": "something"})

        self.assertEqual(_all_fiber(), [None])

    def test_direct_entry_fiber_is_optional(self):
        self.client.post("/food/log-direct", data={"food": "Bran", "calories": "120", "fiber": "5"})
        self.client.post("/food/log-direct", data={"food": "Soda", "calories": "150", "fiber": "0"})
        self.client.post("/food/log-direct", data={"food": "Bar", "calories": "200", "fiber": ""})
        self.client.post("/food/log-direct", data={"food": "Gum", "calories": "5"})

        self.assertEqual(_all_fiber(), [5, 0, None, None])

    def test_direct_entry_rejects_invalid_fiber(self):
        for bad in ("-1", "lots", "2.5"):
            with self.subTest(fiber=bad):
                response = self.client.post(
                    "/food/log-direct",
                    data={"food": "Bran", "calories": "120", "fiber": bad},
                    follow_redirects=True,
                )
                self.assertIn(b"Fiber must be a whole number", response.data)
        self.assertEqual(_all_fiber(), [])

    def test_edit_sets_and_clears_fiber(self):
        _seed_meal(date.today().isoformat(), [("Toast", 100, None)])
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            item_id = conn.execute("SELECT id FROM log_entry_items").fetchone()[0]

        response = self.client.post(
            f"/food/entries/{item_id}/edit",
            data={"food": "Toast", "calories": "100", "fiber": "3"},
            follow_redirects=True,
        )
        self.assertEqual(_all_fiber(), [3])
        self.assertIn(b"Toast (100 cal, 3 g fiber)", response.data)

        self.client.post(
            f"/food/entries/{item_id}/edit", data={"food": "Toast", "calories": "100", "fiber": ""}
        )
        self.assertEqual(_all_fiber(), [None])

    def test_edit_rejects_invalid_fiber_without_writing(self):
        _seed_meal(date.today().isoformat(), [("Toast", 100, 2)])
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            item_id = conn.execute("SELECT id FROM log_entry_items").fetchone()[0]

        self.client.post(
            f"/food/entries/{item_id}/edit", data={"food": "Toast", "calories": "150", "fiber": "-3"}
        )

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            self.assertEqual(
                conn.execute("SELECT calories, fiber_grams FROM log_entry_items").fetchone(), (100, 2)
            )

    def test_edit_form_prefills_fiber_or_leaves_it_blank(self):
        _seed_meal(date.today().isoformat(), [("Toast", 100, 3), ("Jam", 50, None)])
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            toast_id, jam_id = [r[0] for r in conn.execute("SELECT id FROM log_entry_items ORDER BY id")]

        self.assertIn(b'name="fiber" min="0" step="1" value="3"', self.client.get(f"/food/entries/{toast_id}/edit").data)
        self.assertIn(b'name="fiber" min="0" step="1" value=""', self.client.get(f"/food/entries/{jam_id}/edit").data)

    def test_repeat_copies_fiber_including_unknown(self):
        entry_id = _seed_meal("2026-08-20", [("Oatmeal", 200, 4), ("Coffee", 5, None)])

        response = self.client.post(
            "/food/repeat", data={"entry_id": str(entry_id)}, follow_redirects=True
        )

        self.assertEqual(_all_fiber(), [4, None, 4, None])
        self.assertIn(b"Oatmeal (200 cal, 4 g fiber), Coffee (5 cal)", response.data)


class FiberDisplayTests(FoodTrackerTestCase):
    def test_daily_total_counts_unknown_as_zero_and_items_show_fiber(self):
        _seed_meal(date.today().isoformat(), [("Oatmeal", 200, 4), ("Coffee", 5, None), ("Egg", 70, 0)])

        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("&middot; 4 g fiber</span>", body)
        self.assertIn("&middot; — fiber</span>", body)
        self.assertIn("&middot; 0 g fiber</span>", body)

    def test_fiber_total_shown_alongside_net_calories(self):
        _seed_meal(date.today().isoformat(), [("Oatmeal", 200, 6)])
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO exercise_log (entry_date, entry_time, activity, calories_burned) "
                "VALUES (?, '07:00', 'Walk', 50)",
                (date.today().isoformat(),),
            )

        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("150 cal net", body)
        self.assertIn("&middot; 6 g fiber", body)

    def test_repeat_option_shows_fiber_only_when_known(self):
        _seed_meal("2026-08-20", [("Oatmeal", 200, 4), ("Coffee", 5, None)])
        _seed_meal("2026-08-21", [("Mystery bar", 250, None)])

        body = self.client.get("/").get_data(as_text=True)

        self.assertIn("Oatmeal, Coffee &mdash; 205 cal, 4 g fiber", body)
        self.assertIn("Mystery bar &mdash; 250 cal\n", body)


class FiberTrendTests(FoodTrackerTestCase):
    def _trend(self):
        conn = sqlite3.connect(dbmod.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            return appmod._fiber_trend(conn)
        finally:
            conn.close()

    def test_only_days_with_known_fiber_are_plotted(self):
        window_start = date.today() - timedelta(days=29)
        _seed_meal(window_start.isoformat(), [("Old meal", 500, None)])
        _seed_meal((window_start + timedelta(days=3)).isoformat(), [("Beans", 200, 8), ("Rice", 200, None)])
        _seed_meal(date.today().isoformat(), [("Apple", 95, 4)])
        _seed_meal(date.today().isoformat(), [("Pear", 100, 6)], entry_time="12:00")
        _seed_meal((window_start - timedelta(days=1)).isoformat(), [("Too old", 100, 9)])

        self.assertEqual(self._trend(), [(3, 8), (29, 10)])

    def test_trends_page_renders_fiber_chart(self):
        _seed_meal(date.today().isoformat(), [("Apple", 95, 4)])

        body = self.client.get("/trends").get_data(as_text=True)

        self.assertEqual(body.count("<svg"), 2)  # calories + fiber; no weight logged
        self.assertIn("4 g today", body)

    def test_trends_page_fiber_empty_state(self):
        _seed_meal(date.today().isoformat(), [("Mystery", 95, None)])

        body = self.client.get("/trends").get_data(as_text=True)

        self.assertIn("No fiber logged in the last 30 days.", body)

    def test_caption_names_the_date_when_latest_fiber_day_is_not_today(self):
        three_days_ago = date.today() - timedelta(days=3)
        _seed_meal(three_days_ago.isoformat(), [("Beans", 200, 8)])

        body = self.client.get("/trends").get_data(as_text=True)

        self.assertIn(f"8 g on {three_days_ago.isoformat()}", body)


class FiberMigrationTests(unittest.TestCase):
    def test_adds_nullable_fiber_column_to_a_pre_fiber_database(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE log_entries (id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL,
                    entry_time TEXT NOT NULL, source_bundle_id INTEGER, notes TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')));
                CREATE TABLE log_entry_items (id INTEGER PRIMARY KEY, log_entry_id INTEGER NOT NULL,
                    food_item_id INTEGER, description TEXT NOT NULL, quantity TEXT,
                    calories INTEGER NOT NULL, is_estimate INTEGER NOT NULL DEFAULT 0,
                    assumption_note TEXT);
                INSERT INTO log_entries (entry_date, entry_time) VALUES ('2026-01-01', '08:00');
                INSERT INTO log_entry_items (log_entry_id, description, calories) VALUES (1, 'Toast', 100);
                """
            )
        conn.close()

        with patch.object(dbmod, "DB_PATH", path):
            dbmod.init_db()
            dbmod.init_db()  # second boot is a no-op, not a duplicate-column error

        conn = sqlite3.connect(path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(log_entry_items)")}
            self.assertIn("fiber_grams", columns)
            self.assertEqual(conn.execute("SELECT fiber_grams FROM log_entry_items").fetchone(), (None,))
        finally:
            conn.close()


class AuthGateTests(FoodTrackerTestCase):
    """AUTH_PASSWORD_HASH is unset throughout the rest of the suite (same
    treatment as ANTHROPIC_API_KEY -- never configured in tests), so these
    tests patch it on per-test instead."""

    TEST_HASH = generate_password_hash("correct-horse", method="pbkdf2:sha256")

    def setUp(self):
        super().setUp()
        patcher = patch.object(appmod, "AUTH_PASSWORD_HASH", self.TEST_HASH)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _login(self, password, **extra):
        return self.client.post("/login", data=dict(password=password, **extra))

    def _reset_csrf(self):
        # Logging in or out clears the session, CSRF token included.
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = CSRF_TEST_TOKEN

    def _failure_count(self):
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            return conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0]

    def test_no_hash_configured_leaves_routes_open(self):
        with patch.object(appmod, "AUTH_PASSWORD_HASH", None):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Log out", response.data)

    def test_unauthenticated_get_redirects_to_login_with_next(self):
        response = self.client.get("/trends")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login?next=/trends", response.headers["Location"])

    def test_unauthenticated_post_redirects_to_login_and_writes_nothing(self):
        response = self.client.post("/log", data={"weight_lbs": "180"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM weight_log").fetchone()[0], 0)

    def test_login_page_renders_without_nav(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'type="password"', response.data)
        self.assertNotIn(b'aria-label="Main"', response.data)

    def test_static_files_are_reachable_without_login(self):
        response = self.client.get("/static/style.css")
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_wrong_password_is_rejected_and_counted(self):
        response = self._login("wrong-password")
        self.assertEqual(response.status_code, 401)
        self.assertIn(b"Incorrect password", response.data)
        self.assertEqual(self._failure_count(), 1)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_correct_password_logs_in_with_permanent_session(self):
        response = self._login("correct-horse")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/"))
        with self.client.session_transaction() as sess:
            self.assertTrue(sess["authenticated"])
            self.assertTrue(sess.permanent)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Log out", page.data)

    def test_session_cookie_has_expiry_and_security_flags(self):
        response = self._login("correct-horse")
        cookie = response.headers["Set-Cookie"]
        self.assertIn("Expires=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_login_redirects_to_safe_next_path(self):
        response = self._login("correct-horse", next="/trends")
        self.assertTrue(response.headers["Location"].endswith("/trends"))

    def test_login_redirects_within_mount_prefix(self):
        """Regression: in production Passenger mounts the app under
        /food-tracker, and the next path must keep that prefix."""
        prefixed = "http://localhost/food-tracker/"
        gate = self.client.get("/trends", base_url=prefixed)
        self.assertIn("/food-tracker/login?next=/trends", gate.headers["Location"])
        for next_path, expected in (("/trends", "/food-tracker/trends"), ("/", "/food-tracker/"), ("", "/food-tracker/")):
            with self.subTest(next=next_path):
                self._reset_csrf()
                self.client.post("/logout", base_url=prefixed)
                self._reset_csrf()
                response = self.client.post(
                    "/login", data={"password": "correct-horse", "next": next_path}, base_url=prefixed
                )
                self.assertTrue(response.headers["Location"].endswith(expected), response.headers["Location"])

    def test_login_ignores_off_site_next(self):
        for evil in ("//evil.example", "https://evil.example", "/\\evil.example"):
            with self.subTest(next=evil):
                self._reset_csrf()
                self.client.post("/logout")
                self._reset_csrf()
                response = self._login("correct-horse", next=evil)
                self.assertNotIn("evil", response.headers["Location"])

    def test_successful_login_clears_that_ips_failures(self):
        self._login("wrong-password")
        self._login("wrong-password")
        self._login("correct-horse")
        self.assertEqual(self._failure_count(), 0)

    def test_lockout_after_max_failures_blocks_even_correct_password(self):
        for _ in range(appmod.LOGIN_MAX_FAILURES):
            self._login("wrong-password")
        response = self._login("correct-horse")
        self.assertEqual(response.status_code, 429)
        self.assertIn(b"Too many failed attempts", response.data)
        self.assertEqual(self._failure_count(), appmod.LOGIN_MAX_FAILURES)
        with self.client.session_transaction() as sess:
            self.assertNotIn("authenticated", sess)

    def test_lockout_is_per_ip(self):
        for _ in range(appmod.LOGIN_MAX_FAILURES):
            self._login("wrong-password")
        response = self.client.post(
            "/login",
            data={"password": "correct-horse"},
            environ_base={"REMOTE_ADDR": "10.0.0.2"},
        )
        self.assertEqual(response.status_code, 302)

    def test_failures_older_than_window_dont_count(self):
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            conn.executemany(
                "INSERT INTO login_attempts (ip, attempted_at) VALUES ('127.0.0.1', datetime('now', ?))",
                [(f"-{appmod.LOGIN_FAILURE_WINDOW_MINUTES + 1} minutes",)] * appmod.LOGIN_MAX_FAILURES,
            )
        response = self._login("correct-horse")
        self.assertEqual(response.status_code, 302)

    def test_logout_clears_session(self):
        self._login("correct-horse")
        self._reset_csrf()
        response = self.client.post("/logout")
        self.assertIn("/login", response.headers["Location"])
        self.assertEqual(self.client.get("/").status_code, 302)


class CsrfTests(FoodTrackerTestCase):
    def test_post_with_blank_token_is_rejected(self):
        response = self.client.post("/log", data={"weight_lbs": "180", "csrf_token": ""})
        self.assertEqual(response.status_code, 400)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM weight_log").fetchone()[0], 0)

    def test_post_with_wrong_token_is_rejected(self):
        response = self.client.post("/log", data={"weight_lbs": "180", "csrf_token": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_post_with_no_session_token_is_rejected(self):
        with self.client.session_transaction() as sess:
            sess.pop("csrf_token")
        response = self.client.post("/log", data={"weight_lbs": "180"})
        self.assertEqual(response.status_code, 400)

    def test_login_post_requires_token(self):
        test_hash = generate_password_hash("pw", method="pbkdf2:sha256")
        with patch.object(appmod, "AUTH_PASSWORD_HASH", test_hash):
            response = self.client.post("/login", data={"password": "pw", "csrf_token": ""})
        self.assertEqual(response.status_code, 400)

    def test_every_rendered_post_form_carries_the_session_token(self):
        """Guards against a new form being added without the hidden input --
        which would otherwise only show up as a 400 in real use."""
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            cur = conn.execute(
                "INSERT INTO log_entries (entry_date, entry_time) VALUES (?, '08:00')",
                (date.today().isoformat(),),
            )
            item_id = conn.execute(
                "INSERT INTO log_entry_items (log_entry_id, description, calories) VALUES (?, 'Toast', 100)",
                (cur.lastrowid,),
            ).lastrowid
            exercise_id = conn.execute(
                "INSERT INTO exercise_log (entry_date, entry_time, activity, calories_burned) "
                "VALUES (?, '07:00', 'Walk', 100)",
                (date.today().isoformat(),),
            ).lastrowid

        pages = ["/", "/exercise", "/weight", f"/food/entries/{item_id}/edit",
                 f"/exercise/entries/{exercise_id}/edit"]
        # Auth on (and already logged in) so the nav's logout form renders too.
        with patch.object(appmod, "AUTH_PASSWORD_HASH", "unused"):
            with self.client.session_transaction() as sess:
                sess["authenticated"] = True
            for path in pages:
                html = self.client.get(path).get_data(as_text=True)
                forms = re.findall(r'<form method="post".*?</form>', html, re.S)
                self.assertGreater(len(forms), 1, path)
                for form in forms:
                    self.assertIn(f'name="csrf_token" value="{CSRF_TEST_TOKEN}"', form, path)


if __name__ == "__main__":
    unittest.main()
