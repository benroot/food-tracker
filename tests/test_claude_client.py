import os
import unittest
from unittest.mock import Mock, patch

import claude_client

TOOL_USE_BLOCK = {
    "type": "tool_use",
    "id": "toolu_test123",
    "name": "log_food_entry",
    "input": {
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
    },
}


def make_response(content_blocks):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": claude_client.MODEL,
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


class CallClaudeTests(unittest.TestCase):
    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    @patch("claude_client.requests.post")
    def test_sends_auth_header_and_forced_tool_choice(self, mock_post):
        mock_post.return_value = Mock(json=lambda: make_response([TOOL_USE_BLOCK]))

        messages = [{"role": "user", "content": "an apple for breakfast"}]
        response = claude_client.call_claude(messages)

        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["headers"]["x-api-key"], "test-key")
        self.assertEqual(kwargs["json"]["tool_choice"], {"type": "tool", "name": "log_food_entry"})
        self.assertEqual(kwargs["json"]["messages"], messages)
        self.assertEqual(response["content"], [TOOL_USE_BLOCK])

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    @patch("claude_client.requests.post")
    def test_explicit_tool_and_system_prompt_override_the_food_defaults(self, mock_post):
        mock_post.return_value = Mock(json=lambda: make_response([]))

        messages = [{"role": "user", "content": "ran 3 miles"}]
        claude_client.call_claude(
            messages,
            tool=claude_client.LOG_EXERCISE_ENTRY_TOOL,
            system_prompt=claude_client.EXERCISE_SYSTEM_PROMPT,
        )

        _, kwargs = mock_post.call_args
        self.assertEqual(
            kwargs["json"]["tool_choice"], {"type": "tool", "name": "log_exercise_entry"}
        )
        self.assertEqual(kwargs["json"]["system"], claude_client.EXERCISE_SYSTEM_PROMPT)
        self.assertEqual(kwargs["json"]["tools"], [claude_client.LOG_EXERCISE_ENTRY_TOOL])

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    @patch("claude_client.requests.post")
    def test_raises_on_http_error(self, mock_post):
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = Exception("HTTP 401")
        mock_post.return_value = mock_response

        with self.assertRaises(Exception):
            claude_client.call_claude([{"role": "user", "content": "eggs"}])


class ExtractToolInputTests(unittest.TestCase):
    def test_finds_tool_use_block_among_other_content(self):
        response = make_response([{"type": "text", "text": "ignored"}, TOOL_USE_BLOCK])
        self.assertEqual(claude_client.extract_tool_input(response), TOOL_USE_BLOCK["input"])


class ResponseAsMessageTests(unittest.TestCase):
    def test_wraps_content_as_assistant_turn(self):
        response = make_response([TOOL_USE_BLOCK])
        self.assertEqual(
            claude_client.response_as_message(response),
            {"role": "assistant", "content": [TOOL_USE_BLOCK]},
        )


class BuildUserTurnTests(unittest.TestCase):
    def test_first_turn_is_plain_text(self):
        turn = claude_client.build_user_turn([], "two eggs")
        self.assertEqual(turn, {"role": "user", "content": "two eggs"})

    def test_followup_turn_includes_tool_result_for_prior_tool_use(self):
        history = [
            {"role": "user", "content": "eggs"},
            {"role": "assistant", "content": [TOOL_USE_BLOCK]},
        ]
        turn = claude_client.build_user_turn(history, "scrambled, two of them")
        self.assertEqual(
            turn,
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_test123",
                        "content": "Acknowledged.",
                    },
                    {"type": "text", "text": "scrambled, two of them"},
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
