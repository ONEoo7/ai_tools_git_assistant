"""Two repositories' submodules side by side, each beside the same one on the other side.

The same one is the one at the same path. Two projects do not always keep a library
in the same place, though, so a submodule that nothing on the other side has the
path of is paired with the one fetched from the same repository -- when exactly one
on each side is. Two copies of one library on the same side could be either, and a
guess that pairs the wrong two would report a difference that is not there.
"""

from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from git_assistant import git_ops
from git_assistant.git_ops import CommitSummary, SubmoduleState


class Showing(Enum):
    """Which of a submodule's two commits the sides are compared at."""

    #: What is on disk: what `git submodule status` names.
    CHECKED_OUT = "checked out"
    #: What each repository's HEAD commits it to.
    RECORDED = "recorded"


class Match(Enum):
    SAME = "same"
    DIFFERENT = "different"
    ONLY_LEFT = "only left"
    ONLY_RIGHT = "only right"


def compared_hash(state: SubmoduleState, showing: Showing) -> str:
    """The commit ``state`` is compared at; "" when there is none to compare.

    Checked out, a submodule that was never checked out is at the commit that
    would be: the one recorded. One whose checkout git could not read is at
    none -- it is not known to be anywhere, and least of all the same.
    """
    if showing is Showing.RECORDED:
        return state.recorded_hash
    if state.checked_out is not None:
        return state.checked_out.hash
    return state.recorded_hash if state.problem == git_ops.NOT_CHECKED_OUT else ""


def shown_commit(state: SubmoduleState, showing: Showing) -> CommitSummary | None:
    """The commit whose details are shown for ``state``, where they can be read."""
    if showing is Showing.CHECKED_OUT and state.checked_out is not None:
        return state.checked_out
    return state.recorded if compared_hash(state, showing) == state.recorded_hash else None


@dataclass(frozen=True)
class Row:
    """A submodule of either side, and the same one on the other where it has one."""

    left: SubmoduleState | None
    right: SubmoduleState | None

    @property
    def path(self) -> str:
        state = self.left if self.left is not None else self.right
        return state.path if state is not None else ""

    def match(self, showing: Showing) -> Match:
        if self.right is None:
            return Match.ONLY_LEFT
        if self.left is None:
            return Match.ONLY_RIGHT
        mine = compared_hash(self.left, showing)
        return Match.SAME if mine and mine == compared_hash(self.right, showing) else Match.DIFFERENT


def _path_key(path: str) -> str:
    return path.casefold() if sys.platform == "win32" else path


#: ``[user@]host:path``, the short form of an SSH address -- but not ``C:\folder``.
_SCP_LIKE = re.compile(r"^(?:[^@/\\]+@)?([^:/\\]{2,}):(?!//)(.+)$")


def source_of(url: str) -> str:
    """What names the repository a URL fetches from, whichever way it is written.

    The host and the path, without the scheme, the user, a port or ``.git``: so
    ``git@github.com:team/can.git`` and ``https://github.com/team/can`` are the
    same source. A folder, or a URL relative to the containing repository's own,
    is compared as written.
    """
    text = url.strip().rstrip("/\\")
    if text.lower().endswith(".git"):
        text = text[: -len(".git")]
    if "://" in text:
        rest = text.split("://", 1)[1]
        authority, _slash, path = rest.partition("/")
        host = authority.rsplit("@", 1)[-1].split(":", 1)[0]
    elif scp := _SCP_LIKE.match(text):
        host, path = scp.group(1).rsplit("@", 1)[-1], scp.group(2)
    else:
        return text.replace("\\", "/").casefold()
    return f"{host}/{path.strip('/')}".casefold()


def _alone_by_source(states: list[SubmoduleState]) -> dict[str, SubmoduleState]:
    """Each source only one of ``states`` is fetched from, and that one."""
    by_source: dict[str, list[SubmoduleState]] = {}
    for state in states:
        if state.url.strip():
            by_source.setdefault(source_of(state.url), []).append(state)
    return {source: found[0] for source, found in by_source.items() if len(found) == 1}


def pair(left: list[SubmoduleState], right: list[SubmoduleState]) -> list[Row]:
    """Every submodule of either side, beside the same one on the other.

    Parents before the submodules inside them, in order of path, as each side
    lists its own; a pair made by source is placed by the left side's path.
    """
    theirs = {_path_key(state.path): state for state in right}
    rows: list[Row] = []
    unpaired: list[SubmoduleState] = []
    for mine in left:
        other = theirs.pop(_path_key(mine.path), None)
        if other is not None:
            rows.append(Row(mine, other))
        else:
            unpaired.append(mine)

    remaining_right = _alone_by_source(list(theirs.values()))
    for source, mine in _alone_by_source(unpaired).items():
        other = remaining_right.get(source)
        if other is not None:
            rows.append(Row(mine, other))
            unpaired.remove(mine)
            del theirs[_path_key(other.path)]

    rows += [Row(mine, None) for mine in unpaired]
    rows += [Row(None, other) for other in theirs.values()]
    return sorted(rows, key=lambda row: [part.casefold() for part in row.path.split("/")])


def read_both(left: str, right: str) -> tuple[list[SubmoduleState], list[SubmoduleState]]:
    """Both repositories' submodules, read side by side; one read when they are one."""
    if Path(left).resolve() == Path(right).resolve():
        states = git_ops.submodule_states(left)
        return states, states
    with ThreadPoolExecutor(max_workers=2) as pool:
        mine = pool.submit(git_ops.submodule_states, left)
        theirs = pool.submit(git_ops.submodule_states, right)
        return mine.result(), theirs.result()
