"""What two settings files say differently.

Settings are a shallow tree of scalars, so the useful unit is the *path* to a
value -- ``fetch.depth`` -- and not the section it sits in. Flattened to those
paths, two files compare row by row, and a row is something a person can point
at and choose between.

Pure, and deliberately not about JSON text. Two files that differ only in the
order of their keys or the width of their indentation say the same thing, and a
textual diff would report a change nobody made.
"""

from __future__ import annotations

import json

from git_assistant import jsonc
from dataclasses import dataclass
from enum import StrEnum

#: Keys that are about the file rather than about the settings in it.
_BOOKKEEPING = ("version",)


class State(StrEnum):
    SAME = "same"
    CHANGED = "changed"
    ADDED = "added"  # only the second one has it
    REMOVED = "removed"  # only the first one does


@dataclass(frozen=True)
class Change:
    """One value, as the two sides have it."""

    key: str
    before: object
    after: object
    state: State

    @property
    def differs(self) -> bool:
        return self.state is not State.SAME

    def shown(self, value: object) -> str:
        """A value as a row shows it. ``-`` where the side does not have one."""
        if value is None:
            return "-"
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def describe(self) -> str:
        if self.state is State.ADDED:
            return f"{self.key}: added, {self.shown(self.after)}"
        if self.state is State.REMOVED:
            return f"{self.key}: removed (was {self.shown(self.before)})"
        if self.state is State.CHANGED:
            return f"{self.key}: {self.shown(self.before)} -> {self.shown(self.after)}"
        return f"{self.key}: {self.shown(self.after)}"


def flatten(data: object, prefix: str = "") -> dict[str, object]:
    """``{"fetch": {"depth": 1}}`` as ``{"fetch.depth": 1}``.

    Anything that is not an object is a value, including a list: a list of
    globs is one setting, and half of it is not a setting at all.
    """
    if not isinstance(data, dict):
        return {prefix: data} if prefix else {}
    out: dict[str, object] = {}
    for key, value in data.items():
        if not prefix and key in _BOOKKEEPING:
            continue
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def compare(before: object, after: object) -> list[Change]:
    """Every value either side has, in a stable order."""
    left, right = flatten(before), flatten(after)
    changes: list[Change] = []
    for key in sorted(set(left) | set(right)):
        has_left, has_right = key in left, key in right
        old, new = left.get(key), right.get(key)
        if has_left and not has_right:
            state = State.REMOVED
        elif has_right and not has_left:
            state = State.ADDED
        elif old == new:
            state = State.SAME
        else:
            state = State.CHANGED
        changes.append(Change(key=key, before=old, after=new, state=state))
    return changes


def differences(before: object, after: object) -> list[Change]:
    """Only the rows that are not the same on both sides."""
    return [change for change in compare(before, after) if change.differs]


def unflatten(flat: dict[str, object]) -> dict:
    """``{"fetch.depth": 1}`` back to ``{"fetch": {"depth": 1}}``."""
    out: dict = {}
    for key, value in flat.items():
        parts = key.split(".")
        node = out
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


#: Which side of a comparison a value is taken from.
LEFT = "left"
RIGHT = "right"


def merged(left: object, right: object, taken: dict[str, str]) -> dict:
    """The two sides, one key at a time, as ``taken`` says.

    A key the chosen side does not have is left out rather than filled in from
    the other one. That is what taking the side without it *means*, and it is
    the only way a merge can produce a file that says less than both inputs.

    Keys nobody chose come from the left, which is the side the window shows
    first and the one every row starts on.
    """
    sides = {LEFT: flatten(left), RIGHT: flatten(right)}
    out: dict[str, object] = {}
    for key in sorted(set(sides[LEFT]) | set(sides[RIGHT])):
        source = sides.get(taken.get(key, LEFT), sides[LEFT])
        if key in source:
            out[key] = source[key]
    return unflatten(out)


def parse(text: str) -> dict:
    """``text`` as settings, or ``{}``. For comparing against what is on disk."""
    try:
        data = jsonc.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def summarise(changes: list[Change]) -> str:
    """One line for a status bar: how many, and of what kind."""
    if not changes:
        return "No differences."
    counts = {
        state: sum(1 for c in changes if c.state is state)
        for state in (State.CHANGED, State.ADDED, State.REMOVED)
    }
    parts = [
        f"{count} {state.value}" for state, count in counts.items() if count
    ]
    return ", ".join(parts) + "."
