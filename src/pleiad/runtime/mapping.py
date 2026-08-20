"""Small JSON Pointer helpers used for graph data flow."""

from typing import Any


def get_pointer(value: object, pointer: str) -> object:
    """Read a JSON Pointer; the empty pointer refers to the whole value."""
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError(f"invalid JSON Pointer: {pointer}")
    current: Any = value
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[token]
        elif isinstance(current, list):
            current = current[int(token)]
        else:
            raise KeyError(pointer)
    return current


def set_pointer(target: dict[str, Any], pointer: str, value: object) -> None:
    """Set a JSON Pointer in an object, creating intermediate mappings."""
    if pointer == "":
        raise ValueError("root replacement is not supported for graph inputs")
    if not pointer.startswith("/"):
        raise ValueError(f"invalid JSON Pointer: {pointer}")
    tokens = [raw.replace("~1", "/").replace("~0", "~") for raw in pointer[1:].split("/")]
    current = target
    for token in tokens[:-1]:
        child = current.get(token)
        if not isinstance(child, dict):
            child = {}
            current[token] = child
        current = child
    current[tokens[-1]] = value
