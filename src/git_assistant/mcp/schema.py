"""Just enough JSON Schema to check a tool's arguments before running it.

The tools here declare small, flat schemas -- objects of strings, integers,
booleans and enums -- so a full validator would be a dependency bought for a
fraction of its surface. What this checks is what the catalogue actually uses;
anything it does not understand it lets through, because the alternative is
rejecting a valid call over a keyword nobody wrote.
"""

from __future__ import annotations

_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def problems(schema: dict, value) -> list[str]:
    """Every reason ``value`` does not satisfy ``schema``, in plain words."""
    return list(_check(schema or {}, value, ""))


def _check(schema: dict, value, path: str):
    where = f"'{path}'" if path else "the arguments"

    kind = schema.get("type")
    if kind and kind in _TYPES:
        expected = _TYPES[kind]
        # bool is an int in Python and never what an integer field means.
        if kind in ("integer", "number") and isinstance(value, bool):
            yield f"{where} must be {kind}"
            return
        if not isinstance(value, expected):
            yield f"{where} must be {kind}"
            return

    choices = schema.get("enum")
    if choices and value not in choices:
        yield f"{where} must be one of: {', '.join(map(str, choices))}"
        return

    if isinstance(value, dict) and schema.get("type") == "object":
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if value.get(name) in (None, ""):
                yield f"'{name}' is required"
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    yield f"'{name}' is not a parameter of this tool"
        for name, sub in properties.items():
            if name in value and value[name] is not None:
                yield from _check(sub, value[name], f"{path}.{name}" if path else name)

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            yield from _check(schema["items"], item, f"{path}[{index}]")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        low, high = schema.get("minimum"), schema.get("maximum")
        if low is not None and value < low:
            yield f"{where} must be at least {low}"
        if high is not None and value > high:
            yield f"{where} must be at most {high}"
