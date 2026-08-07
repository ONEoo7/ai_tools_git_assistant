"""One card per audit: a tick to run it, and the settings only it reads.

The Audit tab used to stack every option in one column beneath the audit list.
That column was a lie about ownership. "Stale branches after" is read by the
consistency audit and by nothing else, but it sat there under a selected size
audit looking like something about to change what the size audit does -- and
"Never delete" beneath it looked like a promise the size audit was making.

So an option lives inside the audit that reads it, drawn inside its border, and
is enabled only while that audit is ticked to run. Nothing here decides
anything: each card writes straight into ``Settings`` and says so, because the
agents read the settings file and not these widgets.

Every card shows everything it has, always. Selecting one changes its colour
and not its size: a card that unfolds when clicked shifts the cards under it,
so the second click of a pair lands on something the user did not aim at.

Adding an audit means adding its options class and one line in ``OPTIONS``.
An audit with nothing to configure needs neither, and gets a card with a tick
and a description.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

MUTED_COLOUR = "color: #888;"
#: How far an audit's own settings sit in from its tick. Enough to read as
#: "belonging to the line above", which is the whole point of this file.
INDENT = 22

#: Every audit is a card, drawn as one: a border around what belongs together
#: says it more plainly than the spacing between groups ever did. The audit
#: being read differs by colour and by nothing else -- same border width, same
#: contents, so clicking one moves the highlight and never the layout.
#:
#: Palette roles rather than colours: this window follows the system theme, and
#: a hard-coded grey is either invisible or garish in one of them.
CARD_STYLE = """
QFrame#auditCard {
    border: 1px solid palette(mid);
    border-radius: 6px;
    background-color: palette(base);
}
QFrame#auditCard[selected="true"] {
    border: 1px solid palette(highlight);
    background-color: palette(alternate-base);
}
"""


class AuditOptions(QWidget):
    """The settings one audit reads.

    Subclasses build their widgets, load them from ``settings`` and write back
    on every change -- there is no Apply, because there is no dialog: what is
    on screen is what the next run will use.
    """

    changed = pyqtSignal()

    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(INDENT, 0, 0, 0)
        self.box.setSpacing(4)

    def _caption(self, text: str) -> None:
        """A label for the widget about to be added below it."""
        label = QLabel(text)
        label.setStyleSheet(MUTED_COLOUR)
        self.box.addWidget(label)

    def _store(self, *_args) -> None:
        """Write these widgets into ``settings``. Called on every change."""
        raise NotImplementedError


class SizeOptions(AuditOptions):
    """The one shortcut the size audit has: skip the per-path history scan."""

    def __init__(self, settings, parent=None) -> None:
        super().__init__(settings, parent)
        self.fast_check = QCheckBox("Fast mode (totals only)")
        self.fast_check.setToolTip(
            "Skips the per-file breakdown of history, which is the slow part on "
            "a large repository. The totals are still measured."
        )
        self.fast_check.setChecked(settings.agent_fast_mode)
        self.fast_check.toggled.connect(self._store)
        self.box.addWidget(self.fast_check)

    def _store(self, *_args) -> None:
        self.settings.agent_fast_mode = self.fast_check.isChecked()
        self.changed.emit()


class ConfigOptions(AuditOptions):
    """What the configuration audit counts as a file worth flagging.

    Read by ``checks.run_all`` and by nothing else. It was in the settings file
    with no way to reach it, which is a default rather than a setting.
    """

    def __init__(self, settings, parent=None) -> None:
        super().__init__(settings, parent)
        self.large_file_spin = QSpinBox()
        self.large_file_spin.setRange(1, 4096)
        self.large_file_spin.setSuffix(" MB")
        self.large_file_spin.setToolTip(
            "A tracked binary at least this large is reported as one Git LFS "
            "would normally hold. Nothing is moved or changed either way."
        )
        self.large_file_spin.setValue(settings.agent_large_file_mb)
        self.large_file_spin.valueChanged.connect(self._store)
        self._caption("Flag binaries larger than:")
        self.box.addWidget(self.large_file_spin)

    def _store(self, *_args) -> None:
        self.settings.agent_large_file_mb = self.large_file_spin.value()
        self.changed.emit()


class ConsistencyOptions(AuditOptions):
    """When a branch counts as stale, and when deleting one may be proposed.

    Held as settings rather than as widget state: the audit asks
    ``settings.stale_rules()`` for an object, so what is stored has to survive a
    round trip through the settings file. Reading the widgets directly would
    work until someone hand-edited that file.
    """

    def __init__(self, settings, parent=None) -> None:
        super().__init__(settings, parent)
        self.stale_months_spin = QSpinBox()
        self.stale_months_spin.setRange(0, 120)
        self.stale_months_spin.setSuffix(" months")
        self.stale_months_spin.setToolTip(
            "A branch untouched for longer than this counts as stale. Nothing "
            "is deleted either way."
        )
        self.stale_months_spin.valueChanged.connect(self._store)

        self.merged_only_check = QCheckBox("Only propose merged branches")
        self.merged_only_check.setToolTip(
            "On, deletion is proposed only for branches whose commits are "
            "already on the default branch. Off, an unmerged branch can be "
            "proposed -- and its commits exist nowhere else."
        )
        self.merged_only_check.toggled.connect(self._store)

        self.keep_unpushed_check = QCheckBox("Keep unpushed work")
        self.keep_unpushed_check.setToolTip(
            "Never propose a branch holding commits its upstream has not got."
        )
        self.keep_unpushed_check.toggled.connect(self._store)

        self.protect_edit = QLineEdit()
        self.protect_edit.setToolTip(
            "Branch names never proposed for deletion, comma separated. "
            "Globs work: release/* spares every release branch. The default "
            "branch is protected whether or not it is listed."
        )
        self.protect_edit.editingFinished.connect(self._store)

        self._caption("Stale branches after:")
        self.box.addWidget(self.stale_months_spin)
        self.box.addWidget(self.merged_only_check)
        self.box.addWidget(self.keep_unpushed_check)
        self._caption("Never delete:")
        self.box.addWidget(self.protect_edit)
        self.show_rules(settings.stale_rules())

    def show_rules(self, rules) -> None:
        for widget, value in (
            (self.merged_only_check, rules.merged_only),
            (self.keep_unpushed_check, rules.keep_unpushed),
        ):
            widget.blockSignals(True)
            widget.setChecked(value)
            widget.blockSignals(False)
        self.stale_months_spin.blockSignals(True)
        self.stale_months_spin.setValue(rules.months)
        self.stale_months_spin.blockSignals(False)
        self.protect_edit.setText(", ".join(rules.protect))

    def _store(self, *_args) -> None:
        from git_assistant.agents.branches import StaleRules

        self.settings.set_stale_rules(
            StaleRules(
                months=self.stale_months_spin.value(),
                protect=[
                    part.strip()
                    for part in self.protect_edit.text().split(",")
                    if part.strip()
                ],
                merged_only=self.merged_only_check.isChecked(),
                keep_unpushed=self.keep_unpushed_check.isChecked(),
            )
        )
        self.changed.emit()


#: Which audit configures what. An audit missing from here has nothing to set.
OPTIONS = {
    "size-audit": SizeOptions,
    "config-audit": ConfigOptions,
    "consistency-audit": ConsistencyOptions,
}


def options_for(agent_id: str, settings, parent=None) -> AuditOptions | None:
    builder = OPTIONS.get(agent_id)
    return builder(settings, parent) if builder else None


class AuditCard(QFrame):
    """One audit: whether it runs, what it runs with, and what it is for.

    Two signals because there are two questions. ``ticked`` is "does this run";
    ``picked`` is "show me this one's report", which is a different thing
    entirely -- a run of three leaves three reports and the tab shows one.
    """

    ticked = pyqtSignal()
    picked = pyqtSignal()

    def __init__(self, info, settings, parent=None) -> None:
        super().__init__(parent)
        self.info = info
        self.agent_id = info.id
        self.setObjectName("auditCard")
        self.setStyleSheet(CARD_STYLE)
        self.setProperty("selected", False)

        # The whole card explains itself on hover. In the card body it would be
        # a paragraph per audit -- a wall of prose above the three controls
        # anyone came here to change.
        self.setToolTip(info.description)

        box = QVBoxLayout(self)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(6)

        self.check = QCheckBox(info.label)
        font = self.check.font()
        font.setBold(True)
        self.check.setFont(font)
        self.check.toggled.connect(lambda _on: self._on_toggled())
        # `clicked`, not `toggled`: a tick restored from the stored settings is
        # not someone asking to read this audit. A click on the card is.
        self.check.clicked.connect(self.picked.emit)
        box.addWidget(self.check)

        # What it costs to run, which is the one thing worth knowing before
        # ticking it. One line, always, on every card: a card that changes
        # height when it is clicked moves the two below it out from under the
        # pointer.
        self.hint = QLabel(info.cost_hint)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(MUTED_COLOUR)
        self.hint.setContentsMargins(INDENT, 0, 0, 0)
        # A wrapped label asks for room for its longest line, which would push
        # this pane wider than anything in it needs. Let it take whatever width
        # the pane ends up with instead of arguing for one.
        self.hint.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        box.addWidget(self.hint)

        self.options = options_for(info.id, settings, self)
        if self.options is not None:
            box.addWidget(self.options)
        self._sync_options()

    # ---- what a run does ----------------------------------------------------
    def is_ticked(self) -> bool:
        return self.check.isChecked()

    def set_ticked(self, on: bool) -> None:
        """Tick without calling it a request to run: no signal comes back."""
        self.check.blockSignals(True)
        self.check.setChecked(on)
        self.check.blockSignals(False)
        self._sync_options()

    def _on_toggled(self) -> None:
        self._sync_options()
        self.ticked.emit()

    def _sync_options(self) -> None:
        """Grey an audit's settings while it is not going to run.

        Still readable, so what a run *would* use can be seen without ticking
        it, and unmistakably not part of the next run.
        """
        if self.options is not None:
            self.options.setEnabled(self.check.isChecked())

    # ---- what is being read -------------------------------------------------
    def set_selected(self, on: bool) -> None:
        """Colour, and nothing else. Selecting an audit must not resize it."""
        self.setProperty("selected", on)
        # A property a stylesheet selects on is read when the widget is
        # polished, and that already happened.
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Anywhere on the card means "this is the one I am looking at"."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.picked.emit()
        super().mousePressEvent(event)
