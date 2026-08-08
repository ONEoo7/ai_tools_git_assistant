"""The theme picker, and the four things it can paint.

The palette is application-wide state, so every test here puts it back: a test
that leaves the window pink makes the next one fail somewhere unrelated.
"""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtGui import QPalette  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.config import Settings  # noqa: E402
from git_assistant.ui import theme  # noqa: E402
from git_assistant.ui.theme_picker import ThemePicker  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def app(qapp):
    yield qapp
    theme.apply(qapp, theme.SYSTEM)  # never leave it on the next test


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None
    return s


def _window(app) -> str:
    return app.palette().color(QPalette.ColorRole.Window).name()


def _text(app) -> str:
    return app.palette().color(QPalette.ColorRole.WindowText).name()


def _brightness(colour: str) -> int:
    r, g, b = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return (r * 299 + g * 587 + b * 114) // 1000


# ---- what is offered ------------------------------------------------------------
def test_following_the_system_is_the_default(settings):
    """Windows already knows whether this user reads dark or light."""
    assert settings.theme == theme.SYSTEM
    assert theme.DEFAULT == theme.SYSTEM
    assert theme.THEMES[0].key == theme.SYSTEM


def test_all_four_are_offered_in_order(app, settings):
    picker = ThemePicker(settings, app=app)
    offered = [picker.combo.itemData(i) for i in range(picker.combo.count())]
    assert offered == [theme.SYSTEM, theme.LIGHT, theme.DARK, theme.PONY]


def test_the_pink_one_says_what_it_is(app, settings):
    """Ponies and rainbows, named as such, so nobody picks it by accident."""
    picker = ThemePicker(settings, app=app)
    index = picker.combo.findData(theme.PONY)
    assert "🦄" in picker.combo.itemText(index)
    assert "Pink" in picker.combo.itemText(index)


# ---- what it paints -------------------------------------------------------------
def test_dark_is_dark_and_light_is_light(app):
    """The regression: `style().standardPalette()` hands back Windows 95 grey.

    It looks like the obvious way to undo a custom palette, and it produced
    black text on #d4d0c8 with dark-mode chrome drawn over the top of it.
    """
    theme.apply(app, theme.DARK)
    dark_window, dark_text = _window(app), _text(app)
    theme.apply(app, theme.LIGHT)
    light_window, light_text = _window(app), _text(app)

    assert _brightness(dark_window) < 100
    assert _brightness(dark_text) > 150
    assert _brightness(light_window) > 200
    assert _brightness(light_text) < 100
    assert dark_window != "#d4d0c8" and light_window != "#d4d0c8"


def test_the_pink_one_is_pink_and_has_rainbows(app):
    theme.apply(app, theme.PONY)

    window = app.palette().color(QPalette.ColorRole.Window)
    assert window.red() > window.green() and window.blue() > window.green()
    assert _brightness(_text(app)) < 100  # readable dark ink on it
    sheet = app.styleSheet()
    assert "qlineargradient" in sheet
    for colour in theme.RAINBOW:
        assert colour in sheet


def test_leaving_the_pink_one_takes_the_rainbows_with_it(app):
    theme.apply(app, theme.PONY)
    assert app.styleSheet()

    theme.apply(app, theme.DARK)

    assert app.styleSheet() == ""
    assert _brightness(_window(app)) < 100  # dark, not dark-pink


# ---- what the audit cards are drawn with ------------------------------------------
def _lightness(colour) -> int:
    return colour.lightness()


@pytest.mark.parametrize("key", [t.key for t in theme.THEMES])
def test_an_audit_card_is_readable_in_every_theme(app, settings, key):
    """The reported bug, in both directions.

    The card being read was filled with `palette(alternate-base)`, which is
    white in the Windows 11 dark palette and black in its light one -- the
    opposite extreme from `Base`, not a near neighbour of it. That is white
    text on white, and black text on black.
    """
    from git_assistant.ui.audit_cards import card_colours

    theme.apply(app, key)
    fill, selected, border = card_colours(app.palette())
    text = app.palette().color(QPalette.ColorRole.Text)

    for background, what in ((fill, "a card"), (selected, "the selected card")):
        assert abs(_lightness(background) - _lightness(text)) > 90, (
            f"{what} in {key}: {background.name()} under text {text.name()}"
        )
    # And a border has to be visible against the card it goes round.
    assert abs(_lightness(border) - _lightness(fill)) >= 20, f"border in {key}"


def test_the_cards_follow_a_theme_chosen_while_the_window_is_open(app, settings):
    """The colours are baked into a stylesheet, so they must be rebaked.

    Qt's own PaletteChange cannot be relied on for this: with the colour scheme
    doing the work it reaches neither the window nor anything in it, which is
    why the theme announces itself instead.
    """
    from git_assistant.ui.agents_panel import AgentsPanel

    theme.apply(app, theme.DARK)
    panel = AgentsPanel(settings)
    dark_fill = panel.cards[0].colours[0].name()

    theme.apply(app, theme.LIGHT)
    app.processEvents()

    light_fill = panel.cards[0].colours[0].name()
    assert light_fill != dark_fill
    assert light_fill.lower() == (
        app.palette().color(QPalette.ColorRole.Base).name().lower()
    )


def test_a_card_that_is_gone_does_not_keep_the_theme_alive(app, settings):
    """The cards connect to a module-level signal, which outlives every window."""
    import gc

    from git_assistant.ui.agents_panel import AgentsPanel

    theme.apply(app, theme.DARK)
    panel = AgentsPanel(settings)
    panel.deleteLater()
    del panel
    app.processEvents()
    gc.collect()

    theme.apply(app, theme.LIGHT)  # must not raise on a deleted receiver
    app.processEvents()


def test_a_theme_nobody_recognises_is_not_a_reason_to_refuse_to_start(app):
    """Hand-edited, or written by a newer build."""
    assert theme.apply(app, "spooky") == theme.SYSTEM
    assert theme.get("spooky").key == theme.SYSTEM
    assert not theme.is_known("spooky")


# ---- choosing one ----------------------------------------------------------------
def test_choosing_a_theme_applies_it_and_stores_it(app, settings):
    picker = ThemePicker(settings, app=app)
    changed = []
    picker.themeChanged.connect(changed.append)

    picker.combo.setCurrentIndex(picker.combo.findData(theme.PONY))

    assert settings.theme == theme.PONY
    assert changed == [theme.PONY]
    assert "qlineargradient" in app.styleSheet()


def test_the_stored_theme_is_shown_on_open_not_the_first_one(app, settings):
    settings.theme = theme.DARK
    picker = ThemePicker(settings, app=app)
    assert picker.combo.currentData() == theme.DARK


def test_an_unknown_stored_theme_shows_the_default(app, settings):
    settings.theme = "spooky"
    picker = ThemePicker(settings, app=app)
    assert picker.combo.currentData() == theme.SYSTEM


def test_the_picker_is_top_right_at_the_end_of_the_identity_row(app, settings):
    """Below the close button, to the right of what the bar says about pushing."""
    from git_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(settings)
    top = dlg.layout().itemAt(0).layout()  # the first row, above the tabs

    assert [top.itemAt(i).widget() for i in range(top.count())] == [
        dlg.identity_bar,
        dlg.theme_picker,
    ]
    # The bar takes the slack, so the picker stays against the window's edge.
    assert top.stretch(0) == 1
    dlg.close()


def test_showing_the_stored_one_is_not_a_user_choice(app, settings):
    """Opening a window must not repaint or re-save anything."""
    settings.theme = theme.LIGHT
    picker = ThemePicker(settings, app=app)
    changed = []
    picker.themeChanged.connect(changed.append)

    picker.show_stored()

    assert changed == []
