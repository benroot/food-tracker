import os

import requests

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = (
    "You parse a single-user food log message into structured entries with "
    "estimated calories. Use your best nutrition-estimation judgment for "
    "typical portion sizes when the user doesn't specify one, and mark such "
    "items as estimates with a short assumption note. When the message "
    "lists several raw ingredients that together make up one home-cooked "
    "dish (measurement units like tbsp/tsp/cup for components such as rice, "
    "sauce, oil, or greens; preparation verbs like 'made', 'cooked', "
    "'threw together'; combining language like 'with' or 'topped with'), "
    "log them as a single item with a descriptive dish name and the summed "
    "calories, noting the ingredient breakdown in assumption_note -- not "
    "one item per ingredient. For example, 'rice bowl with 1 cup rice, 5 "
    "oz cooked salmon, 1 tbsp oil, 1 cup cooked collard greens, 2 tbsp soy "
    "sauce' is one item ('Salmon rice bowl with collard greens'), but "
    "'eggs and toast for breakfast' stays two items (Eggs, Toast) since "
    "each is already a complete, independently-recognizable food rather "
    "than a raw component of something else. Only ask for clarification "
    "when the message is genuinely ambiguous about what was eaten or which "
    "meal it belongs to -- don't ask about things you can reasonably "
    "assume. If the message explicitly states what time the food was eaten "
    "(e.g. 'at 7am', 'around 8:30pm', 'breakfast at 7'), set entry_time to "
    "that time in 24-hour HH:MM format. If no time is stated, omit "
    "entry_time entirely so the app can default to the current time."
)

LOG_FOOD_ENTRY_TOOL = {
    "name": "log_food_entry",
    "description": "Log one or more food items parsed from the user's message, with estimated calories.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "food": {"type": "string"},
                        "quantity": {"type": "string"},
                        "estimated_calories": {"type": "integer"},
                        "is_estimate": {"type": "boolean"},
                        "assumption_note": {"type": "string"},
                    },
                    "required": ["food", "estimated_calories", "is_estimate"],
                },
            },
            "meal_type": {
                "type": "string",
                "enum": ["Breakfast", "Lunch", "Dinner", "Snack", "Dessert", "Drink"],
            },
            "entry_time": {
                "type": "string",
                "description": "24-hour HH:MM time the food was eaten, only if explicitly stated in the message. Omit if no time was mentioned.",
            },
            "needs_clarification": {"type": "boolean"},
            "clarification_question": {"type": "string"},
        },
        "required": ["items", "meal_type", "needs_clarification"],
    },
}


EXERCISE_SYSTEM_PROMPT = (
    "You parse a single-user exercise log message into a structured entry "
    "with an estimated calorie equivalent burned. Use your best judgment "
    "for typical calorie burn given the activity, duration, and intensity "
    "described, and mark the estimate with a short assumption note. If the "
    "message describes more than one activity (e.g. 'ran 2 miles then did "
    "20 pushups'), combine them into a single descriptive activity summary "
    "with the total calories burned summed across them -- there is one "
    "entry per message, not one per activity. Only ask for clarification "
    "when the message is genuinely ambiguous about what activity was done "
    "-- don't ask about things you can reasonably assume. If the message "
    "explicitly states what time the activity happened (e.g. 'at 7am', "
    "'around 6pm'), set entry_time to that time in 24-hour HH:MM format. "
    "If no time is stated, omit entry_time entirely so the app can "
    "default to the current time."
)

LOG_EXERCISE_ENTRY_TOOL = {
    "name": "log_exercise_entry",
    "description": "Log one exercise activity parsed from the user's message, with an estimated calorie equivalent burned.",
    "input_schema": {
        "type": "object",
        "properties": {
            "activity": {"type": "string"},
            "estimated_calories_burned": {"type": "integer"},
            "is_estimate": {"type": "boolean"},
            "assumption_note": {"type": "string"},
            "entry_time": {
                "type": "string",
                "description": "24-hour HH:MM time the activity was done, only if explicitly stated in the message. Omit if no time was mentioned.",
            },
            "needs_clarification": {"type": "boolean"},
            "clarification_question": {"type": "string"},
        },
        "required": ["activity", "estimated_calories_burned", "is_estimate", "needs_clarification"],
    },
}


def call_claude(messages, tool=None, system_prompt=None):
    tool = tool or LOG_FOOD_ENTRY_TOOL
    system_prompt = system_prompt or SYSTEM_PROMPT
    response = requests.post(
        API_URL,
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 1024,
            "system": system_prompt,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool["name"]},
            "messages": messages,
        },
    )
    response.raise_for_status()
    return response.json()


def extract_tool_input(response):
    tool_use = next(block for block in response["content"] if block["type"] == "tool_use")
    return tool_use["input"]


def response_as_message(response):
    return {"role": "assistant", "content": response["content"]}


def build_user_turn(history, user_text):
    """Build the next user message, satisfying the API's requirement that a
    tool_use block (forced on every prior assistant turn) be immediately
    followed by a matching tool_result before any further text."""
    if not history:
        return {"role": "user", "content": user_text}

    last_assistant = history[-1]
    tool_use_id = next(
        block["id"] for block in last_assistant["content"] if block["type"] == "tool_use"
    )
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": "Acknowledged."},
            {"type": "text", "text": user_text},
        ],
    }
