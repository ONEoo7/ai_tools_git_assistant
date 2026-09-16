"""Where each commit of a history goes in a graph of it: its lane, and the lines to it.

A lane is a column of the graph waiting for one commit: the next parent of whatever
came down it. Commits arrive children first. Each one takes the lane waiting for it
-- the leftmost, if a branch and its fork both are -- and the other lanes waiting
for it end there, drawn in to it. Its first parent then waits in its lane, so a line
of commits runs straight down; every other parent of a merge waits in a lane of its
own, or joins the lane already waiting for it. A commit nothing was waiting for, a
branch's newest, starts a lane in the first free column.

A row is drawn in two halves, and a lane at an edge of one is at that edge of the
next: the lines above a commit run from the lanes at the row's top edge to its
middle, and those below from its middle to the lanes at the bottom edge.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Line:
    """Part of a lane's line in one row: from ``start`` to ``end``, lane numbers."""

    start: int
    end: int
    colour: int


@dataclass(frozen=True)
class GraphRow:
    """One commit's row of the graph."""

    lane: int
    colour: int
    #: From the lanes at the top edge to the middle: lines passing, and lines ending
    #: at this commit.
    above: tuple[Line, ...]
    #: From the middle to the lanes at the bottom edge: lines passing, and lines from
    #: this commit to its parents.
    below: tuple[Line, ...]

    @property
    def lanes(self) -> int:
        """How many lanes wide the row is drawn."""
        ends = [self.lane]
        for line in (*self.above, *self.below):
            ends += (line.start, line.end)
        return max(ends) + 1


def _free(lanes: list[str | None]) -> int:
    """The first lane waiting for nothing, made if there is none."""
    for index, waiting in enumerate(lanes):
        if waiting is None:
            return index
    lanes.append(None)
    return len(lanes) - 1


def lay_out(
    commits: Iterable[tuple[str, Sequence[str]]], *, fresh: Collection[str] = ()
) -> list[GraphRow]:
    """A row for each of ``commits``, given as ``(hash, parents)``, children first.

    A commit in ``fresh`` starts a colour of its own even where a lane was waiting
    for it: the line down to it keeps the colour it had, and the line on from it
    does not. For HEAD, under the rows standing for work not yet committed.
    """
    lanes: list[str | None] = []  # what each lane is waiting for
    colours: list[int] = []
    next_colour = 0
    rows: list[GraphRow] = []

    def colour_lane(index: int) -> None:
        nonlocal next_colour
        while len(colours) < len(lanes):
            colours.append(0)
        colours[index] = next_colour
        next_colour += 1

    for commit, parents in commits:
        waiting = [index for index, awaited in enumerate(lanes) if awaited == commit]
        above = tuple(
            Line(index, waiting[0] if awaited == commit else index, colours[index])
            for index, awaited in enumerate(lanes)
            if awaited is not None
        )
        if waiting:
            lane = waiting[0]
            for other in waiting[1:]:
                lanes[other] = None  # drawn in to this commit, above
            if commit in fresh:
                colour_lane(lane)
        else:
            lane = _free(lanes)
            colour_lane(lane)
        colour = colours[lane]

        below = [
            Line(index, index, colours[index])
            for index, awaited in enumerate(lanes)
            if awaited is not None and index != lane
        ]
        lanes[lane] = parents[0] if parents else None
        if parents:
            below.append(Line(lane, lane, colour))
        for parent in parents[1:]:
            joined = next((i for i, awaited in enumerate(lanes) if awaited == parent), None)
            if joined is None:
                joined = _free(lanes)
                lanes[joined] = parent
                colour_lane(joined)
            below.append(Line(lane, joined, colours[joined]))

        while lanes and lanes[-1] is None:
            lanes.pop()
        del colours[len(lanes) :]
        rows.append(GraphRow(lane, colour, above, tuple(below)))
    return rows
