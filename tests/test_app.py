import os
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

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
            "is_estimate": True,
            "assumption_note": "Assumed 1 medium apple",
        }
    ],
    "meal_type": "Breakfast",
    "needs_clarification": False,
}

TOOL_INPUT_CLARIFY = {
    "items": [],
    "meal_type": "Snack",
    "needs_clarification": True,
    "clarification_question": "How many cookies?",
}


def fake_response(tool_input, tool_use_id="toolu_test"):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tool_use_id, "name": "log_food_entry", "input": tool_input}
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


class FoodTrackerTestCase(unittest.TestCase):
    """Shared setup: fresh tables in the scratch SQLite file before each test."""

    def setUp(self):
        dbmod.init_db()
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            conn.executescript(
                "DELETE FROM log_entry_items; DELETE FROM log_entries; DELETE FROM weight_log;"
            )
        self.client = appmod.app.test_client()


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
                "meal_type": "Breakfast",
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
                "meal_type": "Breakfast",
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


class ManualFoodLogRouteTests(FoodTrackerTestCase):
    def test_valid_entry_writes_to_db_as_non_estimate_without_calling_claude(self):
        with patch("claude_client.call_claude") as mock_call_claude:
            response = self.client.post(
                "/food/log-direct",
                data={"food": "Protein bar", "calories": "200", "meal_type": "Snack"},
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
            data={"food": "Protein bar", "calories": "a lot", "meal_type": "Snack"},
            follow_redirects=True,
        )

        self.assertIn(b"whole number", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
        self.assertEqual(count, 0)

    def test_zero_calories_is_rejected(self):
        response = self.client.post(
            "/food/log-direct",
            data={"food": "Water", "calories": "0", "meal_type": "Drink"},
            follow_redirects=True,
        )

        self.assertIn(b"whole number", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
        self.assertEqual(count, 0)

    def test_missing_food_label_does_not_write_and_flashes_error(self):
        response = self.client.post(
            "/food/log-direct",
            data={"food": "", "calories": "100", "meal_type": "Snack"},
            follow_redirects=True,
        )

        self.assertIn(b"Enter a food label", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
        self.assertEqual(count, 0)

    def test_invalid_meal_type_does_not_write_and_flashes_error(self):
        response = self.client.post(
            "/food/log-direct",
            data={"food": "Chips", "calories": "150", "meal_type": "Midnight Feast"},
            follow_redirects=True,
        )

        self.assertIn(b"Enter a food label", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
        self.assertEqual(count, 0)


class EditDeleteEntryRouteTests(FoodTrackerTestCase):
    def _create_single_item_entry(self, food="Toast", calories=120, meal_type="Breakfast"):
        self.client.post(
            "/food/log-direct",
            data={"food": food, "calories": str(calories), "meal_type": meal_type},
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
                    "meal_type": "Breakfast",
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

    def test_edit_updates_description_calories_and_meal_type(self):
        item_id = self._create_single_item_entry(food="Toast", calories=120, meal_type="Breakfast")

        response = self.client.post(
            f"/food/entries/{item_id}/edit",
            data={"food": "Buttered toast", "calories": "180", "meal_type": "Lunch"},
            follow_redirects=True,
        )

        self.assertIn(b"Buttered toast", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            item = conn.execute(
                "SELECT description, calories FROM log_entry_items WHERE id = ?", (item_id,)
            ).fetchone()
            meal_type = conn.execute(
                """
                SELECT le.meal_type FROM log_entries le
                JOIN log_entry_items lei ON lei.log_entry_id = le.id
                WHERE lei.id = ?
                """,
                (item_id,),
            ).fetchone()[0]
        self.assertEqual(item, ("Buttered toast", 180))
        self.assertEqual(meal_type, "Lunch")

    def test_edit_rejects_invalid_calories_without_writing(self):
        item_id = self._create_single_item_entry(food="Toast", calories=120)

        response = self.client.post(
            f"/food/entries/{item_id}/edit",
            data={"food": "Toast", "calories": "not a number", "meal_type": "Breakfast"},
            follow_redirects=True,
        )

        self.assertIn(b"whole number", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            calories = conn.execute(
                "SELECT calories FROM log_entry_items WHERE id = ?", (item_id,)
            ).fetchone()[0]
        self.assertEqual(calories, 120)

    def test_editing_one_item_of_a_multi_item_meal_changes_meal_type_for_both(self):
        eggs_id = self._create_multi_item_entry()

        self.client.post(
            f"/food/entries/{eggs_id}/edit",
            data={"food": "Eggs", "calories": "140", "meal_type": "Lunch"},
        )

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            meal_types = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT DISTINCT le.meal_type FROM log_entries le
                    JOIN log_entry_items lei ON lei.log_entry_id = le.id
                    """
                ).fetchall()
            ]
        self.assertEqual(meal_types, ["Lunch"])

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
    def _seed_past_entry(self, entry_date, meal_type, items):
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            cursor = conn.execute(
                "INSERT INTO log_entries (entry_date, entry_time, meal_type) VALUES (?, '08:00', ?)",
                (entry_date, meal_type),
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

    def test_food_page_shows_up_to_three_most_recent_breakfasts_newest_first(self):
        self._seed_past_entry("2026-08-19", "Breakfast", [("Cereal", 180, 0, None)])
        self._seed_past_entry("2026-08-20", "Breakfast", [("Oatmeal", 200, 0, None)])
        self._seed_past_entry("2026-08-22", "Breakfast", [("Omelette", 400, 0, None)])
        self._seed_past_entry("2026-08-21", "Breakfast", [("Toast", 150, 0, None)])

        response = self.client.get("/")
        body = response.data.decode()

        self.assertIn("Omelette", body)
        self.assertIn("Toast", body)
        self.assertIn("Oatmeal", body)
        self.assertNotIn("Cereal", body)
        self.assertLess(body.index("Omelette"), body.index("Toast"))
        self.assertLess(body.index("Toast"), body.index("Oatmeal"))

    def test_todays_own_entry_is_excluded_from_repeat_options(self):
        today = date.today().isoformat()
        self._seed_past_entry(today, "Breakfast", [("Pancakes", 300, 0, None)])

        response = self.client.get("/")

        self.assertIn(b"Nothing to repeat yet", response.data)

    def test_only_breakfast_lunch_dinner_are_repeatable(self):
        self._seed_past_entry("2026-08-20", "Snack", [("Chips", 150, 0, None)])

        response = self.client.get("/")

        self.assertIn(b"Nothing to repeat yet", response.data)

    def test_empty_state_when_no_past_meals(self):
        response = self.client.get("/")
        self.assertIn(b"Nothing to repeat yet", response.data)

    def test_repeating_a_meal_copies_items_to_today_preserving_estimate_flag(self):
        entry_id = self._seed_past_entry(
            "2026-08-20",
            "Breakfast",
            [("Omelette", 400, 0, None), ("Juice", 110, 1, "Assumed 8oz")],
        )

        response = self.client.post(
            "/food/repeat", data={"entry_id": str(entry_id)}, follow_redirects=True
        )

        self.assertIn(b"Omelette", response.data)
        with sqlite3.connect(dbmod.DB_PATH) as conn:
            today_rows = conn.execute(
                """
                SELECT lei.description, lei.calories, lei.is_estimate, lei.assumption_note, le.meal_type
                FROM log_entry_items lei
                JOIN log_entries le ON le.id = lei.log_entry_id
                WHERE le.entry_date = ?
                ORDER BY lei.id
                """,
                (date.today().isoformat(),),
            ).fetchall()
        self.assertEqual(len(today_rows), 2)
        self.assertEqual(today_rows[0][:4], ("Omelette", 400, 0, None))
        self.assertEqual(today_rows[1][:4], ("Juice", 110, 1, "Assumed 8oz"))
        self.assertEqual(today_rows[0][4], "Breakfast")

    def test_repeating_with_explicit_time_uses_that_time(self):
        entry_id = self._seed_past_entry("2026-08-20", "Breakfast", [("Omelette", 400, 0, None)])

        self.client.post("/food/repeat", data={"entry_id": str(entry_id), "entry_time": "07:00"})

        with sqlite3.connect(dbmod.DB_PATH) as conn:
            entry_time = conn.execute(
                "SELECT entry_time FROM log_entries WHERE entry_date = ?",
                (date.today().isoformat(),),
            ).fetchone()[0]
        self.assertEqual(entry_time, "07:00")

    def test_repeating_with_blank_time_falls_back_to_current_time(self):
        entry_id = self._seed_past_entry("2026-08-20", "Breakfast", [("Omelette", 400, 0, None)])

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
            ["<td>2026-08-22</td>", "<td>2026-08-21</td>", "<td>2026-08-20</td>"],
        )

    def test_no_entries_shows_empty_state(self):
        response = self.client.get("/weight")
        self.assertIn(b"No weight logged yet.", response.data)


if __name__ == "__main__":
    unittest.main()
