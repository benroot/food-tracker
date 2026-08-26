import os
import sqlite3
import tempfile
import unittest
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


class WeightLogRouteTests(FoodTrackerTestCase):
    def test_logging_weight_persists_and_shows_on_index(self):
        self.client.post("/log", data={"weight_lbs": "199.5"})
        response = self.client.get("/weight")
        self.assertIn(b"199.5", response.data)


if __name__ == "__main__":
    unittest.main()
