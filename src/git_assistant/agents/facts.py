"""Formatting measurements, and the vocabulary the model is allowed to use.

Every figure is rendered once, here, and the rendered string is what goes into
the report, into the block handed to the model, and into the set the narrator
checks the model's prose against. That is why formatting lives in one place: if
the model may only write figures that appear in the facts block, then the facts
block has to be the only place figures are made.
"""

from __future__ import annotations

import re

from git_assistant.agents.base import Fact, Table

#: Base-1024, with the units git itself uses. `git count-objects` reports KiB,
#: so labelling the same quantity "GB" would be quietly 7% wrong at the sizes
#: these reports deal with.
_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_bytes(size: float) -> str:
    """Bytes as a short base-1024 string: ``190.3 GiB``, ``848 B``."""
    value = float(size)
    for unit in _UNITS:
        if abs(value) < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} {_UNITS[-1]}"  # unreachable; kept for the type checker


def human_count(value: int) -> str:
    return f"{value:,}"


def percent(part: float, whole: float) -> str:
    """``part`` of ``whole`` as a whole-number percentage, or "n/a"."""
    if not whole:
        return "n/a"
    return f"{round(100 * part / whole)}%"


def fact(key: str, label: str, value: str, raw: int | float | None = None) -> Fact:
    return Fact(key=key, label=label, value=value, raw=raw)


def size_fact(key: str, label: str, size: int) -> Fact:
    return Fact(key=key, label=label, value=human_bytes(size), raw=size)


def count_fact(key: str, label: str, value: int) -> Fact:
    return Fact(key=key, label=label, value=human_count(value), raw=value)


# ---- the block handed to the model -----------------------------------------
def facts_block(facts: list[Fact], tables: list[Table]) -> str:
    """Serialize facts and tables as the model is meant to read them back.

    Deliberately flat and boring: ``label: value`` lines and pipe tables. The
    model's job is to copy these strings into sentences, not to interpret a
    structure -- and it is the label, not the programmatic key, because a model
    handed ``git_dir_files`` writes ``git_dir_files`` into the paragraph.
    """
    lines = [f"{f.label}: {f.value}" for f in facts]
    for table in tables:
        lines.append("")
        lines.append(table.title.upper())
        lines.append("| " + " | ".join(table.columns) + " |")
        for row in table.rows:
            lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines).strip()


# ---- what counts as a figure, and whether it was measured ------------------
#: A number, optionally with a unit. Matches "190.3 GiB", "9,997", "44%", "882".
_FIGURE_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:%|[KMGTP]i?B|B|bytes|GB|MB|KB|TB)?", re.IGNORECASE
)


def normalize_figure(text: str) -> str:
    """Comparison form of a figure: no thousands separators, no spaces, lowercase.

    ``"190.3 GiB"``, ``"190.3GiB"`` and ``"190.3 gib"`` are the same measurement
    written three ways, and the model should not be failed for the spacing.
    """
    return re.sub(r"[\s,]", "", text).lower()


#: Quantities written as words. A figure spelled out cannot be checked against
#: the facts, which are digits -- so it is treated as unsupported and asked for
#: again. Deliberately starts at eleven: "one file", "a couple of things" and
#: the like are prose, not measurements.
_WORD_NUMBER_RE = re.compile(
    r"\b(eleven|twelve|thir|four|fif|six|seven|eigh|nine)teen\b"
    r"|\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(-\w+)?\b"
    r"|\b(hundred|thousand|million|billion)\b",
    re.IGNORECASE,
)


def figures_in(text: str) -> list[str]:
    """Every number-shaped run in ``text``, as written."""
    return [m.group(0).strip() for m in _FIGURE_RE.finditer(text) if m.group(0).strip()]


def spelled_out_numbers(text: str) -> list[str]:
    """Quantities written as words, which no check can trace to a measurement."""
    seen: list[str] = []
    for match in _WORD_NUMBER_RE.finditer(text):
        word = match.group(0)
        if word.lower() not in [s.lower() for s in seen]:
            seen.append(word)
    return seen


def allowed_figures(facts: list[Fact], tables: list[Table]) -> set[str]:
    """Normalized figures the model may use, taken from what was measured.

    Every fact value and table cell contributes, and so does each figure found
    *inside* one -- a cell reading ``882`` and a value reading ``94.1 GiB``
    should both be quotable, and so should the bare number in ``9,997 commits``.
    """
    allowed: set[str] = set()
    sources = [f.value for f in facts]
    for table in tables:
        for row in table.rows:
            sources.extend(str(cell) for cell in row)
    for source in sources:
        allowed.add(normalize_figure(source))
        for found in figures_in(source):
            allowed.add(normalize_figure(found))
            # A size may be quoted without its unit ("190.3" of "190.3 GiB").
            bare = re.match(r"[\d,]+(?:\.\d+)?", found)
            if bare:
                allowed.add(normalize_figure(bare.group(0)))
    return {a for a in allowed if a}


def unsupported_figures(prose: str, allowed: set[str]) -> list[str]:
    """Figures in ``prose`` that were never measured.

    The check that turns "the model was told not to invent numbers" into
    something the code knows. Years and small ordinals are ignored: "one file",
    "section 2" and a date the model echoed from the title are not claims about
    the repository. Quantities written as words are reported too -- not because
    they are wrong, but because they cannot be shown to be right.
    """
    bad: list[str] = spelled_out_numbers(prose)
    for found in figures_in(prose):
        key = normalize_figure(found)
        if key in allowed:
            continue
        bare = key.rstrip("%")
        if bare.isdigit() and (int(bare) <= 12 or 1990 <= int(bare) <= 2100):
            continue  # small counts and years say nothing about the repository
        if found not in bad:
            bad.append(found)
    return bad
