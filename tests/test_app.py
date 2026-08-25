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


class WeightLogRouteTests(FoodTrackerTestCase):
    def test_logging_weight_persists_and_shows_on_index(self):
        self.client.post("/log", data={"weight_lbs": "199.5"})
        response = self.client.get("/")
        self.assertIn(b"199.5", response.data)


if __name__ == "__main__":
    unittest.main()
