"""Colour for the JSON shown in the settings editor.

Two halves, deliberately: :func:`tokens` decides what each part of a line *is*,
and knows nothing about Qt; :class:`JsonHighlighter` decides what colour that
is, and knows nothing about JSON. The first half is where the mistakes live --
a key mistaken for a string, a number swallowing the digits in a name -- and
splitting it out is what lets those be tested against a string instead of
against a repainted widget.

Line at a time, with no state carried between them. That is not a shortcut: a
JSON string cannot contain a literal newline, so no token here spans a line,
and a highlighter that tracked block state would be tracking a state that never
changes.

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
    """
    spans = [Span(m.start(), 1, PUNCT) for m in _PUNCT.finditer(line)]
    spans += [Span(m.start(), len(m.group()), NUMBER) for m in _NUMBER.finditer(line)]
    spans += [Span(m.start(), len(m.group()), KEYWORD) for m in _KEYWORD.finditer(line)]
    for match in _STRING.finditer(line):
        kind = KEY if _IS_KEY.match(line, match.end()) else STRING
        spans.append(Span(match.start(), len(match.group()), kind))
    return spans


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
        return formats

    def repaint_for_theme(self) -> None:
        """Work the colours out again and redraw. Called when the theme changes."""
        self._formats = self._build()
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt's name
        for span in tokens(text):
            self.setFormat(span.start, span.length, self._formats[span.kind])


def attach(editor) -> JsonHighlighter:
    """Colour the JSON in ``editor``, following the theme from now on."""
    return JsonHighlighter(editor)
