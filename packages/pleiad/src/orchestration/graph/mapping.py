from collections.abc import Mapping

from pleiad.contracts import JsonObject, JsonValue


def get_pointer(value: JsonValue, pointer: str) -> JsonValue:
    """Read one RFC 6901 JSON Pointer without evaluating code."""
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError(f"invalid JSON Pointer: {pointer}")
    current = value
    for token in pointer_tokens(pointer):
        if isinstance(current, Mapping):
            current = current[token]
        elif isinstance(current, list):
            if not token.isascii() or not token.isdecimal() or (len(token) > 1 and token.startswith("0")):
                raise ValueError(f"invalid JSON Pointer array index: {token}")
            current = current[int(token)]
        else:
            raise KeyError(pointer)
    return current


def set_pointer(target: JsonObject, pointer: str, value: JsonValue) -> None:
    """Set one non-root JSON Pointer while creating object parents."""
    if pointer == "":
        raise ValueError("root replacement is not supported for Graph inputs")
    if not pointer.startswith("/"):
        raise ValueError(f"invalid JSON Pointer: {pointer}")
    tokens = pointer_tokens(pointer)
    current: JsonObject = target
    for token in tokens[:-1]:
        child_value = current.get(token)
        if not isinstance(child_value, dict):
            child_object: JsonObject = {}
            current[token] = child_object
            current = child_object
        else:
            current = child_value
    current[tokens[-1]] = value


def pointer_tokens(pointer: str) -> tuple[str, ...]:
    """Return decoded JSON Pointer segments for deterministic conflict checks."""
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise ValueError(f"invalid JSON Pointer: {pointer}")
    raw_tokens = pointer[1:].split("/")
    for token in raw_tokens:
        remainder = token.replace("~1", "").replace("~0", "")
        if "~" in remainder:
            raise ValueError(f"invalid JSON Pointer escape: {pointer}")
    return tuple(raw.replace("~1", "/").replace("~0", "~") for raw in raw_tokens)
