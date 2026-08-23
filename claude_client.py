import anthropic

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = (
    "You parse a single-user food log message into structured entries with "
    "estimated calories. Use your best nutrition-estimation judgment for "
    "typical portion sizes when the user doesn't specify one, and mark such "
    "items as estimates with a short assumption note. Only ask for "
    "clarification when the message is genuinely ambiguous about what was "
    "eaten or which meal it belongs to -- don't ask about things you can "
    "reasonably assume."
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
            "needs_clarification": {"type": "boolean"},
            "clarification_question": {"type": "string"},
        },
        "required": ["items", "meal_type", "needs_clarification"],
    },
}


def call_claude(messages):
    client = anthropic.Anthropic()
    return client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[LOG_FOOD_ENTRY_TOOL],
        tool_choice={"type": "tool", "name": "log_food_entry"},
        messages=messages,
    )


def extract_tool_input(response):
    tool_use = next(block for block in response.content if block.type == "tool_use")
    return tool_use.input


def response_as_message(response):
    return {
        "role": "assistant",
        "content": [block.model_dump() for block in response.content],
    }


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
