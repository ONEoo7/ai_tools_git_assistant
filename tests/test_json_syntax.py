"""Colouring the JSON in the settings editor.

Most of this tests :func:`tokens`, which is a string in and a list of spans out
-- the half where a key can be mistaken for a string. The rest checks the two
things the Qt half is actually for: that the colours come out readable against
whatever they are drawn on, and that they follow the theme.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtGui import QColor, QPalette  # noqa: E402
from PyQt6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

from git_assistant.ui import json_syntax, theme  # noqa: E402
from git_assistant.ui.json_syntax import KEY, KEYWORD, NUMBER, PUNCT, STRING  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _theme_is_put_back(qapp):
    """The palette is application-wide, so a test that paints must clean up."""
    yield
    theme.apply(qapp, theme.SYSTEM)


def kinds(line: str) -> dict[str, str]:
    """What each token *ends up* as: the text mapped to its winning kind.

    Applied in order with the last one winning, which is what the highlighter
    does to the widget. Anything a later span paints over is gone, exactly as
    it is on screen.
    """
    winner: dict[int, tuple[int, str]] = {}
    for span in json_syntax.tokens(line):
        for i in range(span.start, span.start + span.length):
            winner[i] = (span.start, span.kind)
    out: dict[str, str] = {}
    for start, kind in set(winner.values()):
        length = sum(1 for v in winner.values() if v == (start, kind))
        out[line[start : start + length]] = kind
    return out


# ---- what a thing is ------------------------------------------------------------
def test_a_string_before_a_colon_is_a_key():
    assert kinds('"diff_mode": "cached"') == {
        '"diff_mode"': KEY,
        '"cached"': STRING,
        ":": PUNCT,
    }


def test_the_space_before_the_colon_does_not_hide_the_key():
    assert kinds('  "diff_mode" : 1')['"diff_mode"'] == KEY


def test_a_string_in_a_list_is_not_a_key():
    found = kinds('["*.lock", "*.min.js"]')
    assert found['"*.lock"'] == STRING
    assert found['"*.min.js"'] == STRING


def test_numbers_keywords_and_punctuation_each_get_their_own():
    found = kinds('{"a": 4, "b": -1.5e3, "c": true, "d": null}')
    assert found["4"] == NUMBER
    assert found["-1.5e3"] == NUMBER
    assert found["true"] == KEYWORD
    assert found["null"] == KEYWORD
    assert found["{"] == PUNCT


def test_a_word_inside_a_string_is_not_a_keyword():
    """`true` in a value is a keyword; in a name it is four letters."""
    assert kinds('"true_north": "it is null"') == {
        '"true_north"': KEY,
        '"it is null"': STRING,
        ":": PUNCT,
    }


def test_digits_inside_a_string_are_not_a_number():
    assert kinds('"encoding": "utf-8"')['"utf-8"'] == STRING


def test_an_escaped_quote_does_not_end_the_string():
    line = r'"pattern": "say \"hi\""'
    assert kinds(line)[r'"say \"hi\""'] == STRING


def test_a_half_typed_string_is_left_alone():
    """Every string is unterminated while it is being typed."""
    assert '"cach' not in kinds('"diff_mode": "cach')


def test_an_empty_line_has_nothing_in_it():
    assert json_syntax.tokens("") == []


# ---- what colour that is ---------------------------------------------------------
def _palette(base: str, text: str):
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Base, QColor(base))
    palette.setColor(QPalette.ColorRole.Text, QColor(text))
    return palette


def _contrast(one, two) -> float:
    """WCAG contrast ratio, so "readable" is a number and not an opinion."""

    def channel(value: float) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    def luminance(colour):
        r, g, b = colour.red(), colour.green(), colour.blue()
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    light, dark = sorted((luminance(one), luminance(two)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


@pytest.mark.parametrize(
    "base,text",
    [
        ("#ffffff", "#000000"),  # light
        ("#1e1e1e", "#e0e0e0"),  # dark
        ("#fff6fb", "#4a1033"),  # the pink one
    ],
)
def test_every_colour_is_readable_on_every_theme(qapp, base, text):
    """A green that reads on near-black is one that vanishes on white."""
    colours = json_syntax.palette_colours(_palette(base, text))
    for kind, colour in colours.items():
        # 4.5:1 is WCAG AA for body text, which is what this is.
        assert _contrast(colour, QColor(base)) >= 4.5, f"{kind} on {base}"


def test_the_kinds_are_told_apart_by_colour(qapp):
    colours = json_syntax.palette_colours(_palette("#1e1e1e", "#e0e0e0"))
    assert len({c.name() for c in colours.values()}) == len(colours)


def test_punctuation_is_faded_rather_than_tinted(qapp):
    """Braces are structure; the loudest thing on the line should not be one."""
    colours = json_syntax.palette_colours(_palette("#ffffff", "#000000"))
    assert _contrast(colours[PUNCT], QColor("#ffffff")) < _contrast(
        QColor("#000000"), QColor("#ffffff")
    )


# ---- the widget -------------------------------------------------------------------
def _editor(qapp, text: str):
    editor = QPlainTextEdit()
    editor.setPlainText(text)
    return editor


def _painted(editor, qapp) -> list:
    # Qt schedules the first highlight rather than running it, so an editor that
    # is never given a turn is never coloured. In the window there is an event
    # loop; here there has to be one line of it.
    qapp.processEvents()
    block = editor.document().findBlockByNumber(0)
    return block.layout().formats()


def test_attaching_colours_what_is_already_there(qapp):
    editor = _editor(qapp, '{"diff_mode": "cached"}')

    json_syntax.attach(editor)

    assert _painted(editor, qapp), "nothing was coloured"


def test_a_caller_that_keeps_nothing_still_gets_colour(qapp):
    """Parenting to the document is not ownership; parenting to the widget is.

    Attached and dropped: without the widget as its parent the highlighter is
    collected here and the line comes back uncoloured.
    """
    import gc

    editor = _editor(qapp, '{"diff_mode": "cached"}')
    json_syntax.attach(editor)
    gc.collect()

    assert _painted(editor, qapp)


def test_text_set_afterwards_is_coloured_too(qapp):
    """The settings pane replaces the text every time a tier is chosen."""
    editor = _editor(qapp, "")
    json_syntax.attach(editor)

    editor.setPlainText('{"diff_mode": "cached"}')

    assert _painted(editor, qapp)


def test_the_colours_follow_the_theme(qapp):
    editor = _editor(qapp, '{"diff_mode": "cached"}')
    highlighter = json_syntax.attach(editor)  # noqa: F841 - kept alive on purpose
    theme.apply(qapp, theme.DARK)
    on_dark = [r.format.foreground().color().name() for r in _painted(editor, qapp)]

    theme.apply(qapp, theme.LIGHT)
    on_light = [r.format.foreground().color().name() for r in _painted(editor, qapp)]

    assert on_dark and on_light
    assert on_dark != on_light


def test_a_closed_editor_does_not_hold_the_theme_open(qapp):
    """The listener is weak, so a dialog that was closed takes no part."""
    import gc

    editor = _editor(qapp, "{}")
    json_syntax.attach(editor)
    del editor
    gc.collect()

    theme.apply(qapp, theme.DARK)  # must not raise on the gone highlighter
