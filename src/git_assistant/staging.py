"""Staging part of a file: the patch that stages a hunk, or a few lines, of a diff.

The way git gui and ``git add -p`` do it. Take the file's diff, keep the changes
that were chosen, and turn every other change back into what it would be had it
not been made. Staging applies the patch to the index, so a removed line that
was not chosen is still there -- it becomes context -- and an added line that
was not chosen is not, so it goes. Unstaging runs a patch built from the staged
diff backwards, which swaps the two: an added line left alone stays in the
index as context, and a removed one stays removed.

Everything is bytes. A diff's lines carry the file's own line endings, and a
patch whose lines have lost their carriage returns no longer matches the file it
was taken from, so git refuses it.

Lines are numbered as they come in the diff, from 0, counting the header lines:
the same numbering as the diff shown on screen line for line, which is how a
selection in the view becomes a selection here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

#: Header lines that make a diff all or nothing. Part of a new file, of a
#: deleted one, of a rename or of a mode change is not a patch `git apply` takes;
#: nor is part of a binary file.
_WHOLE_FILE_ONLY = (
    b"new file mode",
    b"deleted file mode",
    b"rename from",
    b"copy from",
    b"old mode",
    b"Binary files",
    b"GIT binary patch",
)

NO_NEWLINE = b"\\"  # "\ No newline at end of file" -- about the line before it


class PartialPatchError(ValueError):
    """The chosen lines do not add up to a patch git could apply."""


@dataclass
class Hunk:
    old_start: int
    new_start: int
    #: Where the hunk's ``@@`` line is among the diff's lines.
    at: int
    #: Its body, prefix and all, each without the newline that ended it.
    lines: list[bytes] = field(default_factory=list)

    def numbers(self) -> range:
        """The diff line numbers of the body."""
        return range(self.at + 1, self.at + 1 + len(self.lines))

    def changes(self) -> set[int]:
        """The diff line numbers of every added or removed line in the hunk."""
        return {
            number
            for number, line in zip(self.numbers(), self.lines)
            if line[:1] in (b"+", b"-")
        }


@dataclass
class FileDiff:
    """One file's diff, taken apart into what precedes the hunks and the hunks."""

    head: list[bytes]
    hunks: list[Hunk]
    #: Whether a hunk or a few lines of it can be staged on their own.
    partial: bool

    def hunk_at(self, number: int) -> Hunk | None:
        """The hunk a diff line belongs to, its ``@@`` line included."""
        for hunk in self.hunks:
            if hunk.at <= number < hunk.at + 1 + len(hunk.lines):
                return hunk
        return None

    def changes_between(self, first: int, last: int) -> set[int]:
        """The added and removed lines from ``first`` to ``last``, inclusive."""
        low, high = sorted((first, last))
        return {
            number
            for hunk in self.hunks
            for number in hunk.changes()
            if low <= number <= high
        }


def parse(raw: bytes) -> FileDiff:
    """Take a single file's diff, as `git_ops.file_diff` returns it, apart."""
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()  # what followed the last newline
    head: list[bytes] = []
    hunks: list[Hunk] = []
    partial = True
    current: Hunk | None = None
    for number, line in enumerate(lines):
        match = _HUNK_RE.match(line)
        if match:
            current = Hunk(int(match.group(1)), int(match.group(2)), number)
            hunks.append(current)
        elif current is None:
            head.append(line)
            if line.startswith(_WHOLE_FILE_ONLY):
                partial = False
        else:
            # Anything but a diff line inside a hunk -- another file's header,
            # say -- is not a diff this can safely take apart.
            if line[:1] not in (b" ", b"-", b"+", NO_NEWLINE):
                partial = False
            current.lines.append(line)
    return FileDiff(head, hunks, partial and bool(hunks))


def build_patch(
    diff: FileDiff, chosen: set[int], *, reverse: bool = False
) -> bytes | None:
    """The patch that applies only the ``chosen`` lines of ``diff`` to the index.

    ``reverse`` builds it from a staged diff, for `git apply --reverse`, to take
    the chosen lines back out. Returns None when no change was chosen.

    Raises PartialPatchError when the lines chosen cannot stand without one that
    was not: a file's last line losing its missing newline, and nothing else.
    """
    if not diff.partial:
        raise PartialPatchError("This change can only be staged as a whole file.")
    out = list(diff.head)
    delta = 0  # how far the side being built has drifted from the side matched
    emitted = False
    for hunk in diff.hunks:
        body = _rebuild(hunk, chosen, reverse)
        old = sum(1 for line in body if line[:1] in (b" ", b"-"))
        new = sum(1 for line in body if line[:1] in (b" ", b"+"))
        if not any(line[:1] in (b"+", b"-") for line in body):
            continue
        _check_newlines(body)
        emitted = True
        if reverse:
            new_start = hunk.new_start  # matched against the index
            old_start = new_start + delta
            delta += old - new
        else:
            old_start = hunk.old_start  # matched against the index
            new_start = old_start + delta
            delta += new - old
        out.append(b"@@ -%d,%d +%d,%d @@" % (old_start, old, new_start, new))
        out.extend(body)
    if not emitted:
        return None
    return b"\n".join(out) + b"\n"


Line = tuple[int, bytes, bytes | None]  # its number, its text, its no-newline marker


def _rebuild(hunk: Hunk, chosen: set[int], reverse: bool) -> list[bytes]:
    """The hunk's body with only the chosen changes left as changes."""
    lines: list[Line] = []
    for number, text in zip(hunk.numbers(), hunk.lines):
        if text[:1] == NO_NEWLINE:
            if lines:  # it describes the line before it, and travels with it
                lines[-1] = (lines[-1][0], lines[-1][1], text)
            continue
        lines.append((number, text, None))

    body: list[Line] = []
    at = 0
    while at < len(lines):
        if lines[at][1][:1] == b" ":
            body.append(lines[at])
            at += 1
            continue
        run: list[Line] = []
        while at < len(lines) and lines[at][1][:1] != b" ":
            run.append(lines[at])
            at += 1
        removed = [line for line in run if line[1][:1] == b"-"]
        added = [line for line in run if line[1][:1] == b"+"]
        body.extend(_change(removed, added, chosen, reverse))
    return [part for _n, text, marker in body for part in (text, marker) if part]


def _change(
    removed: list[Line], added: list[Line], chosen: set[int], reverse: bool
) -> list[Line]:
    """One run of changes -- removals, then additions -- with only the chosen kept.

    The index's side of the run keeps its lines in their order; the question is
    where, among them, the lines being applied go. Git lists a replaced run as
    every removal and then every addition, so leaving the additions in their
    listed place puts a new line after old lines it replaced, and the file comes
    out reordered.

    A selection in the view is one unbroken range, so a pair inside a replaced
    run cannot be chosen on its own -- it gets staged a step at a time, and the
    common case is lines chosen on one side of the run and none on the other.
    Each rule below was worked out on those step-by-step sequences:

    - Staging, with removals chosen: the additions go straight after the last
      of them, which is where they replace it.
    - Staging, with none: the addition at position k in its half of the run
      goes before the removal at position k -- a line lands where the line it
      stands for stood.
    - Unstaging mirrors both: restored removals go straight before the first
      chosen addition, or, with none chosen, the removal at position k goes
      before the addition at position k.
    """

    def kept_as(line: Line, prefix: bytes) -> Line:
        return (line[0], prefix + line[1][1:], line[2])

    if reverse:
        stays, goes, mark = added, removed, b"+"
    else:
        stays, goes, mark = removed, added, b"-"
    index = [kept_as(line, mark if line[0] in chosen else b" ") for line in stays]
    applied = [line for line in goes if line[0] in chosen]
    anchored = [at for at, line in enumerate(stays) if line[0] in chosen]
    if anchored:
        split = anchored[0] if reverse else anchored[-1] + 1
    elif applied:
        first = next(at for at, line in enumerate(goes) if line[0] in chosen)
        split = min(first, len(index))
    else:
        split = len(index)
    return index[:split] + applied + index[split:]


def _check_newlines(body: list[bytes]) -> None:
    """Refuse a hunk that says a line has no newline, then carries on past it.

    Happens when the last line of a file that had no final newline is changed
    and a line is added after it, and only the added line is chosen: the old
    last line stays as context, still without its newline, with a line after
    it. That is not a file git can write.
    """
    ended_old = ended_new = False
    previous = b""
    for line in body:
        kind = line[:1]
        if kind == NO_NEWLINE:
            ended_old = ended_old or previous in (b" ", b"-")
            ended_new = ended_new or previous in (b" ", b"+")
            continue
        if (ended_old and kind in (b" ", b"-")) or (ended_new and kind in (b" ", b"+")):
            raise PartialPatchError(
                "The last line of the file is changed as well, and these lines "
                "cannot be staged without it. Include the changed last line."
            )
        previous = kind
