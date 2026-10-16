from pleiad.contracts import JsonObject


def handoff_input_schema() -> JsonObject:
    """Return the only model-visible input contract for every Handoff."""
    return {
        "type": "object",
        "properties": {"task": {"type": "string", "minLength": 1}},
        "required": ["task"],
        "additionalProperties": False,
    }
