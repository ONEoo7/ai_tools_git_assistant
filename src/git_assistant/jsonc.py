"""JSON with comments, for the settings files a person is expected to edit.

Every one of these files is meant to be opened and changed by hand, and plain
JSON gives it no way to say what a key is for. The choice was a comment or a
manual nobody reads beside the file they are editing, so: comments.

Reading is forgiving in the two ways a hand-edited file needs. ``//`` to the
end of the line and ``/* ... */`` are ignored, and so is a comma left before a
closing brace -- which is what you get when you delete the last entry of a
list. Both are what VS Code accepts in its own ``.jsonc`` files, which is the
nearest thing this format has to a specification.

Comments are blanked rather than deleted, space for space and newline for
newline. It costs nothing and it means that when the file *is* broken, the line
and column in the error are the line and column in the file the user has open.

Writing puts one comment above each key. Not beside it: a value long enough to
matter is a value long enough to push a trailing comment off the side of the
window, and the comments that are worth writing are the ones on the settings
that take the longest values.

Nothing here knows what any particular setting means. The comments are passed
in by the module that owns the schema -- see ``git_assistant.repo_config`` and
``git_assistant.config`` -- because a key and the sentence explaining it should
not be able to drift apart across two files.
"""

from __future__ import annotations

import json

#: One level of indentation in what this writes.
INDENT = 2


def strip(text: str) -> str:
    """``text`` with comments and trailing commas blanked out.

    The result is plain JSON of the same length, with the same line breaks in
    the same places, so a position reported against it is a position in the
    original.
    """
    out = list(text)
    length = len(text)
    i = 0
    in_string = False
    #: Whether anything that could be a value has been seen since the last
    #: comma or opening brace.
    opened = False
    # Where the last comma outside a string was, so a trailing one can be found
    # once its closing brace shows up. -1 means the last thing seen was not a
    # comma, which includes an opening brace: `{,}` is not a trailing comma,
    # it is a file with something missing, and saying so is the point.
    last_comma = -1
    while i < length:
        char = text[i]
        if in_string:
            if char == "\\":
                i += 2  # an escaped anything, including a quote
                continue
            if char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            last_comma = -1
            opened = True
            i += 1
            continue
        if char == "/" and i + 1 < length and text[i + 1] == "/":
            while i < length and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if char == "/" and i + 1 < length and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = length if end < 0 else end + 2
            for k in range(i, end):
                if text[k] != "\n":
                    out[k] = " "
            i = end
            continue
        if char == ",":
            # Only after something. A comma straight after `{`, `[` or another
            # comma has no entry in front of it to be trailing.
            last_comma = i if opened else -1
            opened = False
        elif char in "}]":
            if last_comma >= 0:
                out[last_comma] = " "  # nothing after it but this
            last_comma = -1
            opened = True
        elif char in "{[":
            last_comma = -1
            opened = False
        elif not char.isspace():
            last_comma = -1
            opened = True
        i += 1
    return "".join(out)


def loads(text: str):
    """Parse JSONC. Raises ``json.JSONDecodeError`` exactly as ``json`` does."""
    return json.loads(strip(text))


# ---- writing -------------------------------------------------------------------------
def dumps(
    data: dict,
    comments: dict[str, str] | None = None,
    header: str = "",
    indent: int = INDENT,
) -> str:
    """``data`` as JSONC, with a comment above each key that has one.

    ``comments`` is keyed by dotted path -- ``"commit.diff_mode"`` -- so a
    nested key is named the way it is spoken about, and a key that appears in
    two sections can still say two different things.

    A key with no comment simply gets none. That is deliberate: a file where
    every line has a comment and half of them say nothing is a file where the
    comments stop being read.
    """
    comments = comments or {}
    lines = ["{"]
    pad = " " * indent
    if header:
        lines += [f"{pad}// {line}" for line in header.splitlines()]
        lines.append("")
    lines += _entries(data, comments, "", 1, indent)
    lines.append("}")
    return "\n".join(lines) + "\n"


def _entries(
    data: dict, comments: dict[str, str], prefix: str, level: int, indent: int
) -> list[str]:
    pad = " " * (indent * level)
    lines: list[str] = []
    items = list(data.items())
    for position, (key, value) in enumerate(items):
        path = f"{prefix}{key}"
        note = comments.get(path, "")
        if note:
            # A blank line before each comment, so a comment belongs to the key
            # under it rather than floating between two.
            if lines:
                lines.append("")
            lines += [f"{pad}// {line}" for line in note.splitlines()]
        tail = "," if position < len(items) - 1 else ""
        name = json.dumps(str(key), ensure_ascii=False)
        if isinstance(value, dict) and value:
            lines.append(f"{pad}{name}: {{")
            lines += _entries(value, comments, f"{path}.", level + 1, indent)
            lines.append(f"{pad}}}{tail}")
        else:
            lines.append(f"{pad}{name}: {_value(value, level, indent)}{tail}")
    return lines


def _value(value, level: int, indent: int) -> str:
    """A leaf, indented to sit under a key that is already ``level`` deep."""
    text = json.dumps(value, ensure_ascii=False, indent=indent)
    if "\n" not in text:
        return text
    pad = " " * (indent * level)
    first, *rest = text.split("\n")
    return "\n".join([first, *(f"{pad}{line}" for line in rest)])
