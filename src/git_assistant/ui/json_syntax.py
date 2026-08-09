"""Colour for the JSON shown in the settings editor.

Two halves, deliberately: :func:`tokens` decides what each part of a line *is*,
and knows nothing about Qt; :class:`JsonHighlighter` decides what colour that
is, and knows nothing about JSON. The first half is where the mistakes live --
a key mistaken for a string, a number swallowing the digits in a name -- and
splitting it out is what lets those be tested against a string instead of
against a repainted widget.

Line at a time, and one thing carries between them: a ``/* ... */`` left open.
Nothing else can span a line -- a JSON string may not contain a literal newline
-- so that is the only state there is, and it is kept where Qt keeps it, in the
block state rather than on this object.

The colours are worked out from the palette rather than written down, so the
same code reads on a white background, a near-black one and a pink one. See
:func:`palette_colours`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PyQt6.QtGui import QColor, QPalette, QSyntaxHighlighter, QTextCharFormat
from PyQt6.QtWidgets import QApplication

from git_assistant.ui import theme

#: What a run of characters is. Deliberately not an enum of JSON grammar: this
#: is a list of things that get their own colour, which is a shorter list.
KEY = "key"
STRING = "string"
NUMBER = "number"
KEYWORD = "keyword"  # true, false, null
PUNCT = "punct"  # the braces, brackets, commas and colons
COMMENT = "comment"  # // to the end of the line, and /* ... */

#: A JSON string, including its quotes, with backslash escapes honoured so that
#: a `\"` inside one does not end it.
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
#: What makes a string a key: a colon after it, whatever the spacing.
_IS_KEY = re.compile(r"\s*:")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_KEYWORD = re.compile(r"\b(?:true|false|null)\b")
_PUNCT = re.compile(r"[{}\[\],:]")


@dataclass(frozen=True)
class Span:
    start: int
    length: int
    kind: str


def tokens(line: str) -> list[Span]:
    """What each part of ``line`` is, in the order the spans must be applied.

    Overlapping on purpose, and ordered so that the last one wins: a `true`
    inside a string, or the digits in ``"utf-8"``, are matched by the keyword
    and number patterns first and then painted over by the string that contains
    them. The alternative -- one pattern that excludes everything inside a
    string -- is a regex nobody can read for a result nobody can see.

    An unterminated string, which is every string half way through being typed,
    matches nothing and stays the ordinary text colour. That is the honest
    answer: there is no token there yet.

    Comments win over everything, because everything inside one is a comment --
    including the quotes, which is exactly the case a naive pass gets wrong:
    `// see "diff_mode"` is one comment, not a comment and a string.
    """
    spans = [Span(m.start(), 1, PUNCT) for m in _PUNCT.finditer(line)]
    spans += [Span(m.start(), len(m.group()), NUMBER) for m in _NUMBER.finditer(line)]
    spans += [Span(m.start(), len(m.group()), KEYWORD) for m in _KEYWORD.finditer(line)]
    for match in _STRING.finditer(line):
        kind = KEY if _IS_KEY.match(line, match.end()) else STRING
        spans.append(Span(match.start(), len(match.group()), kind))
    comment = _comment_at(line)
    if comment is not None:
        spans.append(comment)
    return spans


def _comment_at(line: str) -> Span | None:
    """The comment on this line, if the `//` is not inside a string.

    Found by scanning rather than by regex, because `"http://x"` contains two
    slashes and is not a comment -- and a pattern that could tell the
    difference would have to parse the line, which is what this is.

    A `/* ... */` that closes on the same line ends where it closes; one that
    does not runs to the end, and `leaves_block_open` says so.
    """
    in_string = False
    i = 0
    while i < len(line):
        char = line[i]
        if in_string:
            if char == "\\":
                i += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif line[i : i + 2] == "//":
            return Span(i, len(line) - i, COMMENT)
        elif line[i : i + 2] == "/*":
            end = line.find("*/", i + 2)
            length = len(line) - i if end < 0 else end + 2 - i
            return Span(i, length, COMMENT)
        i += 1
    return None


def leaves_block_open(line: str) -> bool:
    """Whether a ``/*`` on this line is still open at the end of it."""
    found = _comment_at(line)
    if found is None or line[found.start : found.start + 2] != "/*":
        return False
    # Open when there is no `*/` after the `/*` -- not when the *line* fails to
    # end with one, which is a different question and the wrong one:
    # `/* x */ "a": 1` ends with a digit and closes perfectly well.
    return line.find("*/", found.start + 2) < 0


# ---- what colour that is -------------------------------------------------------------
#: Hue and saturation per kind, on the wheel Qt uses (0-359, 0-255). Only the
#: lightness is decided by the background, so the same green is the string
#: colour in every theme and only its weight changes.
_HUES = {
    KEY: (207, 160),  # blue
    STRING: (135, 130),  # green
    NUMBER: (28, 175),  # amber
    KEYWORD: (280, 140),  # violet
}
#: How light each colour is against a dark background, and against a light one.
#: Both chosen by measuring: every colour clears 4.5:1 -- WCAG AA for body text
#: -- against this application's three backgrounds, which the tests assert.
_ON_DARK = 185
_ON_LIGHT = 80
#: How far the punctuation is faded towards the background. Past about half it
#: stops being readable; below a third it stops being faded.
_PUNCT_FADE = 0.35
#: And the comments. Less faded than the punctuation, not more, which is the
#: opposite of the first guess: a comment is the sentence explaining the line
#: under it and braces are scaffolding. It is also what the measurements
#: allowed -- 0.42 put the pink theme at 3.96:1, under the 4.5 the tests hold
#: every colour to, because that theme's background is the lightest of the
#: three and there is least room to fade into it.
_COMMENT_FADE = 0.30


def palette_colours(palette) -> dict[str, QColor]:
    """The colour for each kind, against whatever this palette is painted on.

    Worked out rather than written down. A fixed set of colours is a set chosen
    against one background: the greens that read on near-black are the ones
    that vanish on white, and this application has a white theme, a black one
    and a pink one. So the hue is fixed and the lightness follows the base.

    The punctuation is the ordinary text colour, faded rather than tinted.
    Braces and commas are structure, and giving them a colour of their own puts
    the loudest thing on the line on the part with the least to say.
    """
    base = palette.base().color()
    dark = base.lightness() < 128
    level = _ON_DARK if dark else _ON_LIGHT
    colours = {
        kind: QColor.fromHsl(hue, saturation, level)
        for kind, (hue, saturation) in _HUES.items()
    }
    # Mixed towards the background rather than lightened. Lightening keeps the
    # saturation, and the pink theme's plum text lightened is a loud pink --
    # brighter than the colours it was supposed to sit behind.
    colours[PUNCT] = _towards(palette.text().color(), base, _PUNCT_FADE)
    # Quiet, but the least quiet of the quiet things: these files have a
    # comment above every key, and a comment is what somebody opened the file
    # to read. Italic as well, so it is told apart by shape and not by a shade
    # of grey.
    colours[COMMENT] = _towards(palette.text().color(), base, _COMMENT_FADE)
    return colours


def _towards(colour: QColor, other: QColor, amount: float) -> QColor:
    """``colour`` mixed ``amount`` of the way to ``other``."""
    return QColor(
        *(
            round(one * (1 - amount) + two * amount)
            for one, two in zip(
                (colour.red(), colour.green(), colour.blue()),
                (other.red(), other.green(), other.blue()),
            )
        )
    )


#: Qt's per-block state for "a /* ... */ is still open here". -1 is Qt's own
#: value for "nothing to carry", so this only needs to name the other one.
_IN_BLOCK = 1


class JsonHighlighter(QSyntaxHighlighter):
    """Colours the JSON in a text document, and follows the theme.

    Parented to the *editor* and pointed at its document, which looks like one
    step too many and is not. Passing the document to the constructor parents
    it to the document, and PyQt does not treat that as taking ownership: the
    highlighter is collected as soon as the caller drops it, and the colour
    goes with it. Measured -- five formats on the line while a reference was
    held, none after a ``gc.collect()``. Parented to the widget it survives, so
    ``attach`` is safe to call without keeping what it returns.
    """

    def __init__(self, editor) -> None:
        super().__init__(editor)
        self.setDocument(editor.document())
        self._formats = self._build()
        # A QTextCharFormat that has already been applied cannot follow a
        # palette, so the theme says when it changed. Held weakly there, so this
        # does not keep a closed dialog alive.
        theme.on_change(self.repaint_for_theme)

    @staticmethod
    def _current_palette():
        """The palette the theme has just set.

        The application's rather than the editor's, and that is the whole
        subtlety here: changing the theme updates the application palette at
        once but reaches the widgets one event-loop turn later. This is called
        from the theme change itself, so asking the widget gets the *previous*
        theme's colours -- measured, going from dark to light, and it is why
        the colours appeared not to follow the theme at all.
        """
        app = QApplication.instance()
        return app.palette() if app is not None else QPalette()

    def _build(self) -> dict[str, QTextCharFormat]:
        formats = {}
        for kind, colour in palette_colours(self._current_palette()).items():
            fmt = QTextCharFormat()
            fmt.setForeground(colour)
            formats[kind] = fmt
        # A key is the one thing worth finding by shape rather than by colour:
        # it is what you scroll looking for.
        formats[KEY].setFontWeight(600)
        formats[COMMENT].setFontItalic(True)
        return formats

    def repaint_for_theme(self) -> None:
        """Work the colours out again and redraw. Called when the theme changes."""
        self._formats = self._build()
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt's name
        """Colour one line, continuing a block comment left open by the last.

        The state lives in Qt's per-block state rather than on this object
        because Qt re-highlights one line when one line changes: an attribute
        here would hold whatever the *last edited* line left behind, which is
        not the line above this one.
        """
        start = 0
        if self.previousBlockState() == _IN_BLOCK:
            end = text.find("*/")
            if end < 0:
                self.setFormat(0, len(text), self._formats[COMMENT])
                self.setCurrentBlockState(_IN_BLOCK)
                return
            self.setFormat(0, end + 2, self._formats[COMMENT])
            start = end + 2

        rest = text[start:]
        for span in tokens(rest):
            self.setFormat(start + span.start, span.length, self._formats[span.kind])
        self.setCurrentBlockState(_IN_BLOCK if leaves_block_open(rest) else -1)


def attach(editor) -> JsonHighlighter:
    """Colour the JSON in ``editor``, following the theme from now on."""
    return JsonHighlighter(editor)
