"""What the window looks like, and the one control that decides it.

Four themes. Three of them are Qt's own colour schemes -- follow the system,
force light, force dark -- and cost nothing but a hint to the style, so the
title bar, the scroll bars and every standard dialog change with them. The
fourth is a palette this file writes by hand.

Following the system is the default, and it is a real default rather than a
guess: Windows already knows whether this user reads dark or light, and an
application that ignores that is the only bright window on a dark desktop at
midnight.

Nothing here is remembered by this module. The choice is a setting like any
other, and this is asked to apply it -- at start-up, and again whenever the
picker changes it. See ``git_assistant.ui.theme_picker``.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette

SYSTEM = "system"
LIGHT = "light"
DARK = "dark"
PONY = "pony"

DEFAULT = SYSTEM


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    description: str


THEMES: tuple[Theme, ...] = (
    Theme(
        SYSTEM,
        "Follow system",
        "Whatever Windows is set to, and it changes when Windows does.",
    ),
    Theme(LIGHT, "Light", "Light, whatever the system is set to."),
    Theme(DARK, "Dark", "Dark, whatever the system is set to."),
    Theme(
        PONY,
        "Pink 🦄🌈",
        "Pink, with ponies and rainbows. It is not a joke setting: it is a "
        "readable light theme that happens to be pink.",
    ),
)

_SCHEMES = {
    SYSTEM: Qt.ColorScheme.Unknown,  # Unknown means "do not override"
    LIGHT: Qt.ColorScheme.Light,
    DARK: Qt.ColorScheme.Dark,
    PONY: Qt.ColorScheme.Light,  # dark text on pink; the palette does the rest
}


def is_known(key: str) -> bool:
    return any(theme.key == key for theme in THEMES)


def get(key: str) -> Theme:
    """The named theme, or the default. Never raises: this only paints."""
    return next((t for t in THEMES if t.key == key), THEMES[0])


# ---- the pink one ---------------------------------------------------------------
#: Deep plum rather than black: black on pink is a warning label.
_INK = "#4a1033"
_HOT = "#ff4fb5"

#: A rainbow, in the order everyone draws one. Used for the accents that are
#: allowed to be a gradient -- a tab that is selected, a bar that is filling --
#: and nowhere text has to be read on top of it.
RAINBOW = (
    "#ff6b6b",
    "#ffa94d",
    "#ffd43b",
    "#69db7c",
    "#4dabf7",
    "#b197fc",
)


def _rainbow_gradient(vertical: bool = False) -> str:
    """A rainbow as a CSS gradient, across or down.

    A ``background`` and never a ``border-image``: Qt's border-image wants an
    image and silently draws nothing when handed a gradient, which is a rainbow
    that only exists in the stylesheet.
    """
    stops = ", ".join(
        f"stop:{i / (len(RAINBOW) - 1):.2f} {colour}"
        for i, colour in enumerate(RAINBOW)
    )
    ends = "x1:0, y1:0, x2:0, y2:1" if vertical else "x1:0, y1:0, x2:1, y2:0"
    return f"qlineargradient({ends}, {stops})"


def _pony_palette() -> QPalette:
    palette = QPalette()
    ink = QColor(_INK)
    colours = {
        QPalette.ColorRole.Window: "#ffe4f3",
        QPalette.ColorRole.WindowText: _INK,
        QPalette.ColorRole.Base: "#fff6fb",
        QPalette.ColorRole.AlternateBase: "#ffd6ec",
        QPalette.ColorRole.Text: _INK,
        QPalette.ColorRole.Button: "#ffd0e8",
        QPalette.ColorRole.ButtonText: _INK,
        QPalette.ColorRole.ToolTipBase: "#fff6fb",
        QPalette.ColorRole.ToolTipText: _INK,
        QPalette.ColorRole.PlaceholderText: "#b5789c",
        QPalette.ColorRole.Highlight: _HOT,
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: "#c2185b",
        QPalette.ColorRole.LinkVisited: "#8e24aa",
        # The border roles, which is what the audit cards are drawn with.
        QPalette.ColorRole.Light: "#fff0f7",
        QPalette.ColorRole.Midlight: "#ffc8e4",
        QPalette.ColorRole.Mid: "#e79cc4",
        QPalette.ColorRole.Dark: "#c06a99",
        QPalette.ColorRole.Shadow: "#8d3f68",
    }
    for role, value in colours.items():
        palette.setColor(role, QColor(value))
    # Greyed-out text has to stay legible against pink, and the default
    # disabled colour is worked out from a grey window that is not this one.
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(
            QPalette.ColorGroup.Disabled, role, QColor("#b07e9c")
        )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, ink.lighter(300)
    )
    return palette


def _pony_stylesheet() -> str:
    """The rainbows. Only where nothing has to be read on top of one.

    The tab bar is styled down to its padding on purpose. Qt draws a tab
    natively until a stylesheet touches any part of it and then draws none of
    it natively, so a rule that only adds a rainbow to the selected tab takes
    the selected tab's *background* away with it -- leaving a row of flat text
    with nothing to say which one you are on.
    """
    return f"""
QTabWidget::pane {{
    border: 1px solid #e79cc4;
    background-color: #fff6fb;
}}
QTabBar::tab {{
    background-color: #ffd0e8;
    color: {_INK};
    border: 1px solid #e79cc4;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 5px 10px;
    margin-right: 2px;
}}
QTabBar::tab:hover {{
    background-color: #ffbfe0;
}}
QTabBar::tab:selected {{
    background-color: #fff6fb;
    font-weight: bold;
}}
QProgressBar {{
    border: 1px solid {_INK};
    border-radius: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {_rainbow_gradient()};
    border-radius: 5px;
}}
QPushButton {{
    border: 1px solid #e79cc4;
    border-radius: 6px;
    padding: 4px 10px;
}}
QPushButton:hover:enabled {{
    background-color: #ffc0e0;
    border: 1px solid {_HOT};
}}
QPushButton:default {{
    border: 2px solid {_HOT};
}}
QSplitter::handle:horizontal {{
    background: {_rainbow_gradient(vertical=True)};
    width: 5px;
}}
QSplitter::handle:vertical {{
    background: {_rainbow_gradient()};
    height: 5px;
}}
QScrollBar::handle {{
    background: {_rainbow_gradient(vertical=True)};
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar:vertical {{
    background-color: #ffe4f3;
    width: 12px;
    margin: 0;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
}}
"""


# ---- applying it ------------------------------------------------------------------
def apply(app, key: str) -> str:
    """Paint ``app`` in the named theme. Returns the key actually used.

    An unknown name -- a hand-edited settings file, or one written by a newer
    build -- falls back rather than raising: a theme nobody recognises is not a
    reason to refuse to start.
    """
    theme = get(key)
    app.styleHints().setColorScheme(_SCHEMES[theme.key])
    if theme.key == PONY:
        app.setPalette(_pony_palette())
        app.setStyleSheet(_pony_stylesheet())
    else:
        app.setStyleSheet("")
        # A default-constructed palette, which is how an application palette is
        # *unset*: Qt then resolves every role from the scheme just chosen.
        #
        # Not `style().standardPalette()`, which looks like the obvious answer
        # and is not one -- it hands back the grey-and-black palette of Windows
        # 95 regardless of the scheme, and the window comes up as black text on
        # #d4d0c8 with dark-mode chrome drawn over it.
        app.setPalette(QPalette())
    return theme.key
