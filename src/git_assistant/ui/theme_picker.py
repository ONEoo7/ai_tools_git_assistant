"""The theme control, top right, beside what the identity bar says about pushing.

Its own widget rather than three lines in the window that hosts it: it is the
only control in this application that changes every other one, and a setting
with that reach should be findable in one place and testable on its own.

Applied on selection and stored in the same breath. There is no preview and no
Apply: the window in front of you *is* the preview.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QComboBox, QHBoxLayout, QLabel, QWidget

from git_assistant.config import Settings
from git_assistant.ui import theme as theme_mod


class ThemePicker(QWidget):
    """"Theme: <name>", and the window repaints when it changes."""

    #: Emitted after a new theme has been applied and stored.
    themeChanged = pyqtSignal(str)  # noqa: N815 - Qt signal naming

    def __init__(self, settings: Settings, app=None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        # Injectable so a test can paint something that is not the whole
        # application, and so the tray's window and this one can share one.
        self._app = app

        self.combo = QComboBox()
        self.combo.setToolTip("How this window is painted.")
        for entry in theme_mod.THEMES:
            self.combo.addItem(entry.label, entry.key)
            self.combo.setItemData(
                self.combo.count() - 1,
                entry.description,
                Qt.ItemDataRole.ToolTipRole,
            )
        self.show_stored()
        self.combo.currentIndexChanged.connect(self._on_selected)

        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(QLabel("Theme:"))
        box.addWidget(self.combo)

    def _application(self):
        return self._app if self._app is not None else QApplication.instance()

    def show_stored(self) -> None:
        """Show the stored theme without treating that as a user choice."""
        index = self.combo.findData(theme_mod.get(self.settings.theme).key)
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(max(0, index))
        self.combo.blockSignals(False)

    def _on_selected(self, _index: int) -> None:
        key = self.combo.currentData()
        if not key:
            return
        app = self._application()
        if app is not None:
            key = theme_mod.apply(app, key)
        self.settings.theme = key
        self.settings.save()
        self.themeChanged.emit(key)
