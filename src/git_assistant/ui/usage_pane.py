"""What each provider has been asked to do, in tokens.

Sits beside the connection settings because that is where the question comes
up: having chosen a provider and a model, how much has this been using them.

Two views of one file. The tree is the lifetime answer, provider by provider
and model by model, and it is never pruned. The list below it is the last few
hundred calls in the shape they happened -- provider, model, when, in, out,
total -- which is what tells you *which run* was expensive.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import usage
from git_assistant.providers import PROVIDERS
from git_assistant.ui.preview_dialog import SECTION_GAP

MUTED_COLOUR = "color: #888;"
_MUTED = QColor("#888888")

#: Said once, under the totals, when any of them were counted by this build
#: rather than reported by the provider.
ESTIMATE_NOTE = (
    "Some calls were counted by this application because the provider did not "
    "report usage; those rows are marked with ~."
)


def _n(value: int) -> str:
    return f"{value:,}"


class UsagePane(QWidget):
    """Lifetime totals per provider and model, and the recent calls behind them."""

    def __init__(self, margins: tuple[int, int, int, int] = (0, 0, 0, 0), parent=None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(*margins)

        box.addWidget(QLabel("LLM usage"))

        split = QSplitter(Qt.Orientation.Vertical)

        self.totals_tree = QTreeWidget()
        self.totals_tree.setHeaderLabels(
            ["Provider / model", "Last used", "Calls", "Input", "Output", "Total"]
        )
        _size_columns(self.totals_tree, stretch=0)
        # Wrapped so it can be inset from the handle below it. A tree added to
        # a splitter directly has no layout, and so no way to keep a margin.
        totals = QWidget()
        totals_box = QVBoxLayout(totals)
        totals_box.setContentsMargins(0, 0, 0, SECTION_GAP)
        totals_box.addWidget(self.totals_tree)
        split.addWidget(totals)

        recent = QWidget()
        recent_box = QVBoxLayout(recent)
        recent_box.setContentsMargins(0, SECTION_GAP, 0, 0)
        recent_box.addWidget(QLabel("Recent calls"))
        self.calls_tree = QTreeWidget()
        self.calls_tree.setHeaderLabels(
            ["Provider", "Model", "When", "Input", "Output", "Total"]
        )
        self.calls_tree.setRootIsDecorated(False)
        _size_columns(self.calls_tree, stretch=1)
        recent_box.addWidget(self.calls_tree, 1)
        split.addWidget(recent)

        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        box.addWidget(split, 1)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(MUTED_COLOUR)
        box.addWidget(self.note)

        row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setToolTip("Copy the totals as a table.")
        self.copy_btn.clicked.connect(self._on_copy)
        self.clear_btn = QPushButton("Clear...")
        self.clear_btn.setToolTip("Forget everything recorded so far.")
        self.clear_btn.clicked.connect(self._on_clear)
        for button in (self.refresh_btn, self.copy_btn, self.clear_btn):
            row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)

        self.refresh()

    # ---- drawing ------------------------------------------------------------
    def refresh(self) -> None:
        self._usage = usage.load()
        self._fill_totals()
        self._fill_recent()
        self._fill_note()

    def _fill_totals(self) -> None:
        self.totals_tree.clear()
        used = {t.provider for t in self._usage.totals}
        # Every provider is listed, used or not: "nothing yet" is an answer,
        # and a provider that silently has no row reads as a missing feature.
        for provider in PROVIDERS:
            totals = self._usage.for_provider(provider.key)
            item = QTreeWidgetItem([provider.label, "", "", "", "", ""])
            font = QFont(item.font(0))
            font.setBold(True)
            item.setFont(0, font)
            if provider.key in used:
                calls = sum(t.calls for t in totals)
                inp = sum(t.input_tokens for t in totals)
                out = sum(t.output_tokens for t in totals)
                last = max((t.last for t in totals), default="")
                item.setText(1, _label(last))
                for column, value in ((2, calls), (3, inp), (4, out), (5, inp + out)):
                    item.setText(column, _n(value))
                for total in sorted(totals, key=lambda t: t.last, reverse=True):
                    item.addChild(_model_row(total))
            else:
                item.setText(1, "not used yet")
                item.setForeground(0, _MUTED)
                item.setForeground(1, _MUTED)
            self._align(item)
            self.totals_tree.addTopLevelItem(item)
            # After adding, not before: an item that is not in a tree yet has
            # nothing to expand, and the call is silently lost.
            item.setExpanded(True)

    def _fill_recent(self) -> None:
        self.calls_tree.clear()
        labels = {p.key: p.label for p in PROVIDERS}
        for event in self._usage.events:
            mark = "~" if event.estimated else ""
            row = QTreeWidgetItem(
                [
                    labels.get(event.provider, event.provider),
                    event.model,
                    event.when_label(),
                    f"{mark}{_n(event.input_tokens)}",
                    f"{mark}{_n(event.output_tokens)}",
                    f"{mark}{_n(event.total)}",
                ]
            )
            if event.estimated:
                row.setToolTip(3, "counted by this application, not by the provider")
            self._align(row)
            self.calls_tree.addTopLevelItem(row)

    def _align(self, item: QTreeWidgetItem) -> None:
        for column in range(3, 6):
            item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight)

    def _fill_note(self) -> None:
        calls, inp, out = self._usage.grand_total()
        if not calls:
            self.note.setText(
                "Nothing recorded yet. Every completion is counted here, "
                "whichever tab or tool asked for it."
            )
            return
        text = (
            f"{_n(calls)} call(s) in total - {_n(inp)} in, {_n(out)} out, "
            f"{_n(inp + out)} tokens."
        )
        if any(t.estimated_calls for t in self._usage.totals):
            text += " " + ESTIMATE_NOTE
        self.note.setText(text)

    # ---- what the user can do with it ----------------------------------------
    def to_markdown(self) -> str:
        rows = ["| Provider | Model | Last used | Calls | Input | Output | Total |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
        labels = {p.key: p.label for p in PROVIDERS}
        for total in sorted(self._usage.totals, key=lambda t: t.last, reverse=True):
            rows.append(
                f"| {labels.get(total.provider, total.provider)} | {total.model} | "
                f"{total.last_label()} | {_n(total.calls)} | {_n(total.input_tokens)} | "
                f"{_n(total.output_tokens)} | {_n(total.total)} |"
            )
        return "\n".join(rows) + "\n"

    def _on_copy(self) -> None:
        if not self._usage.totals:
            return
        QGuiApplication.clipboard().setText(self.to_markdown())
        self.note.setText("Usage totals copied to the clipboard.")

    def _on_clear(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Clear usage",
                "Forget every recorded call?\n\n"
                "The totals are the only record of what has been spent; this "
                "cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            == QMessageBox.StandardButton.Yes
        ):
            usage.clear()
            self.refresh()


def _size_columns(tree: QTreeWidget, *, stretch: int) -> None:
    """Numbers take what they need; one text column absorbs the rest.

    This pane is the narrow half of a splitter, and fixed widths put a
    horizontal scrollbar under a table of six short numbers.
    """
    header = tree.header()
    for column in range(tree.columnCount()):
        header.setSectionResizeMode(
            column,
            QHeaderView.ResizeMode.Stretch
            if column == stretch
            else QHeaderView.ResizeMode.ResizeToContents,
        )
        tree.headerItem().setTextAlignment(
            column,
            Qt.AlignmentFlag.AlignRight
            if column >= 3
            else Qt.AlignmentFlag.AlignLeft,
        )


def _model_row(total) -> QTreeWidgetItem:
    mark = "~" if total.estimated_calls else ""
    item = QTreeWidgetItem(
        [
            total.model,
            total.last_label(),
            _n(total.calls),
            f"{mark}{_n(total.input_tokens)}",
            f"{mark}{_n(total.output_tokens)}",
            f"{mark}{_n(total.total)}",
        ]
    )
    for column in range(3, 6):
        item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight)
    if total.estimated_calls:
        item.setToolTip(
            0,
            f"{total.estimated_calls} of {total.calls} call(s) were counted by "
            "this application because the provider reported no usage.",
        )
    return item


def _label(when: str) -> str:
    return usage.Event(when, "", "").when_label() if when else ""
