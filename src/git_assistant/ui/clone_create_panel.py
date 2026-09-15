"""The Clone & Create tab: bring a repository onto this machine, and start it off.

Left to right:

- the Repository pane every repo-driven tab has, folded until it is wanted. The
  starter files are added to the repository selected there;
- **Clone** -- a shallow copy of every branch, one commit deep unless asked for
  more, or the whole history -- and
  **Create**, a new repository in a folder of your choosing;
- **Starter files**: README.md, .gitignore, .gitattributes and LICENSE, each set
  up and previewed before anything is written.

A repository cloned or created here joins the list and is selected, so it is
where the starter files go next. What those files say, and how they meet files
already there, is `starter_files`' business; running git is `git_ops`'.
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QFontDatabase
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from git_assistant import git_ops, starter_files
from git_assistant.config import RepoEntry, Settings, norm_path
from git_assistant.review import languages
from git_assistant.ui import side_panel as side_panel_mod
from git_assistant.ui.preview_dialog import SECTION_GAP
from git_assistant.ui.repo_pane import RepoPane
from git_assistant.ui.repo_picker import RepoPicker
from git_assistant.ui.workers import CloneWorker, run_worker

INFO_COLOUR = "color: #8ab;"
WARN_COLOUR = "color: #b36b00;"
MUTED_COLOUR = "color: #888;"
_PROBLEM_COLOUR = "#c0392b"

#: The starter files, in the order they are offered.
FILES = (
    starter_files.README,
    starter_files.GITIGNORE,
    starter_files.GITATTRIBUTES,
    starter_files.LICENSE,
)

#: Names Windows keeps for devices: a folder called CON cannot be made at all.
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{n}" for n in range(1, 10)),
    *(f"LPT{n}" for n in range(1, 10)),
}
_NOT_IN_NAMES = set('<>:"/\\|?*')

#: A name before the host of an http(s) URL: a user, and usually a password or
#: token -- which `git clone` then keeps in the repository's .git/config.
_CREDENTIALS_RE = re.compile(r"^https?://[^/@\s]+@", re.IGNORECASE)

#: Git's progress lines can be long; a status label is not the place for all of it.
_PROGRESS_MAX = 160

#: How deep a clone is unless the user chooses otherwise: the latest commit of
#: every branch. Quick to download, and the rest can always be fetched later.
CLONE_DEPTH = 1


def folder_name_problem(name: str) -> str:
    """Why Windows would refuse ``name`` for a folder, or "" when it would not."""
    if not name.strip():
        return "Name the folder."
    if name in (".", ".."):
        return f"'{name}' is not a folder name."
    bad = sorted({c for c in name if c in _NOT_IN_NAMES or ord(c) < 32})
    if bad:
        shown = " ".join(c if ord(c) >= 32 else f"\\x{ord(c):02x}" for c in bad)
        return f"A folder name cannot contain {shown}"
    if name[-1] in " .":
        return "A folder name cannot end with a space or a dot."
    stem = name.split(".")[0].rstrip()
    if stem.upper() in _RESERVED:
        return f"Windows keeps the name {stem} for a device."
    return ""


class CloneCreatePanel(QWidget):
    """Clone or create a repository, and add the files a repository starts with."""

    def __init__(self, settings: Settings, *, add_repository=None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        #: Lists a new repository where the window keeps its list -- the
        #: Repositories tree -- and returns the path it is stored under. None for
        #: a panel on its own, where settings are all there is.
        self._add_repository = add_repository
        #: The window's shared progress bar, set by the window that owns this.
        self.busy = None
        self._worker: CloneWorker | None = None
        self._cancelling = False
        #: The folder name follows the URL until somebody types one of their own.
        self._name_follows_url = True
        self._branch_typed = False
        self._holder_typed = False
        #: Git is asked for defaults (branch, name) when the tab is first shown,
        #: not when the window is built: that costs processes nobody may use.
        self._git_asked = False
        #: The repository the holder was last filled from: None before it was.
        self._holder_repo: str | None = None
        self._built = False

        self.repo_picker = RepoPicker(settings)
        self.repo_picker.repoChanged.connect(self._on_repo_changed)
        self.repo_pane = RepoPane(self.repo_picker, margins=(0, 0, SECTION_GAP, 0))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.repo_pane)
        splitter.addWidget(self._build_clone_and_create())
        splitter.addWidget(self._build_starter_files())
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 3)
        side_panel_mod.attach(splitter, self.repo_pane, open_sizes=[240, 420, 520])

        # Default margins, as every other tab has.
        outer = QVBoxLayout(self)
        outer.addWidget(splitter)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(150)
        self._preview_timer.timeout.connect(self.update_preview)

        self._built = True
        self.update_preview()

    # ---- clone and create ----------------------------------------------------------
    def _build_clone_and_create(self) -> QWidget:
        pane = QWidget()
        column = QVBoxLayout(pane)
        column.setContentsMargins(SECTION_GAP, 0, SECTION_GAP, 0)
        column.addWidget(self._build_clone())
        column.addWidget(self._build_create())
        column.addStretch(1)
        return pane

    def _build_clone(self) -> QGroupBox:
        box = QGroupBox("Clone")
        form = QFormLayout(box)

        self.clone_url = QLineEdit()
        self.clone_url.setPlaceholderText("https://github.com/org/repo.git")
        self.clone_url.setToolTip(
            "An https or ssh URL (git@host:org/repo.git), or a folder on this machine."
        )
        self.clone_url.textChanged.connect(self._on_url_changed)
        form.addRow("URL:", self.clone_url)

        self.clone_parent = QLineEdit(self._default_parent())
        self.clone_parent.textChanged.connect(self._update_buttons)
        form.addRow("Clone into:", self._with_browse(self.clone_parent, "Clone into"))

        self.clone_name = QLineEdit()
        self.clone_name.setPlaceholderText("Named after the URL")
        self.clone_name.textEdited.connect(self._on_name_edited)
        self.clone_name.textChanged.connect(self._update_buttons)
        form.addRow("Folder name:", self.clone_name)

        self.full_history = QRadioButton("Full history")
        self.shallow = QRadioButton("Shallow:")
        self.shallow.setToolTip(
            "Only the most recent commits of every branch: quicker to download.\n"
            "The rest of the history can be fetched later: git fetch --unshallow"
        )
        self.depth = QSpinBox()
        self.depth.setRange(1, 1_000_000)
        self.depth.setValue(CLONE_DEPTH)
        # A depth only means something for a shallow clone.
        self.shallow.toggled.connect(self.depth.setEnabled)
        self.shallow.setChecked(True)
        history = QHBoxLayout()
        history.addWidget(self.full_history)
        history.addWidget(self.shallow)
        history.addWidget(self.depth)
        history.addWidget(QLabel("commits per branch"))
        history.addStretch(1)
        form.addRow("History:", history)

        self.clone_btn = QPushButton("Clone")
        self.clone_btn.clicked.connect(self._on_clone)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Stop the clone and remove what it downloaded.")
        self.cancel_btn.clicked.connect(self._on_cancel)
        buttons = QHBoxLayout()
        buttons.addWidget(self.clone_btn)
        buttons.addWidget(self.cancel_btn)
        buttons.addStretch(1)
        form.addRow(buttons)

        self.clone_status = self._status_label()
        form.addRow(self.clone_status)
        return box

    def _build_create(self) -> QGroupBox:
        box = QGroupBox("Create")
        form = QFormLayout(box)

        self.create_parent = QLineEdit(self._default_parent())
        self.create_parent.textChanged.connect(self._update_buttons)
        form.addRow("Location:", self._with_browse(self.create_parent, "Create in"))

        self.create_name = QLineEdit()
        self.create_name.setPlaceholderText("new-project")
        self.create_name.textChanged.connect(self._update_buttons)
        form.addRow("Name:", self.create_name)

        self.initial_branch = QLineEdit("main")
        self.initial_branch.textEdited.connect(self._on_branch_edited)
        self.initial_branch.textChanged.connect(self._update_buttons)
        form.addRow("Initial branch:", self.initial_branch)

        self.create_with_files = QCheckBox("Add the starter files chosen on the right")
        self.create_with_files.setChecked(True)
        form.addRow(self.create_with_files)

        self.create_btn = QPushButton("Create repository")
        self.create_btn.clicked.connect(self._on_create)
        buttons = QHBoxLayout()
        buttons.addWidget(self.create_btn)
        buttons.addStretch(1)
        form.addRow(buttons)

        self.create_status = self._status_label()
        form.addRow(self.create_status)
        return box

    def _with_browse(self, edit: QLineEdit, title: str) -> QWidget:
        row = QWidget()
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(edit, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(lambda: self._browse(edit, title))
        box.addWidget(browse)
        return row

    def _browse(self, edit: QLineEdit, title: str) -> None:
        start = edit.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, title, start)
        if folder:
            edit.setText(os.path.normpath(folder))

    def _default_parent(self) -> str:
        """Where a new repository most likely goes: beside the ones already known.

        A watched folder first -- new repositories are what it is watched for --
        then a scanned one, then the folder the active repository sits in.
        """
        for folder in (*self.settings.watched_roots, *self.settings.scan_roots):
            if folder and os.path.isdir(folder):
                return os.path.normpath(folder)
        active = self.settings.active_repo
        if active and os.path.isdir(os.path.dirname(active)):
            return os.path.normpath(os.path.dirname(active))
        return str(Path.home())

    # ---- starter files -------------------------------------------------------------
    def _build_starter_files(self) -> QWidget:
        pane = QWidget()
        column = QVBoxLayout(pane)
        column.setContentsMargins(SECTION_GAP, 0, 0, 0)

        title = QLabel("Starter files")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        column.addWidget(title)

        include = QHBoxLayout()
        self.include: dict[str, QCheckBox] = {}
        for name in FILES:
            box = QCheckBox(name)
            # A license is a decision, not a default.
            box.setChecked(name != starter_files.LICENSE)
            box.toggled.connect(self._schedule_preview)
            box.toggled.connect(self._update_buttons)
            self.include[name] = box
            include.addWidget(box)
        include.addStretch(1)
        column.addLayout(include)

        self.file_tabs = QTabWidget()
        self.file_tabs.setDocumentMode(True)
        pages = (
            self._build_readme_page(),
            self._build_gitignore_page(),
            self._build_attributes_page(),
            self._build_license_page(),
        )
        for page, name in zip(pages, FILES, strict=True):
            self.file_tabs.addTab(page, name)
        self.file_tabs.currentChanged.connect(self._schedule_preview)
        column.addWidget(self.file_tabs, 2)

        self.preview_title = QLabel("")
        self.preview_title.setWordWrap(True)
        column.addWidget(self.preview_title)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.preview.setFont(fixed)
        column.addWidget(self.preview, 3)
        self.kept_label = self._status_label(WARN_COLOUR)
        self.kept_label.hide()
        column.addWidget(self.kept_label)

        actions = QHBoxLayout()
        self.add_btn = QPushButton("Add files")
        self.add_btn.clicked.connect(self._on_add_files)
        actions.addWidget(self.add_btn)
        actions.addStretch(1)
        column.addLayout(actions)
        self.files_status = self._status_label()
        column.addWidget(self.files_status)
        return pane

    def _build_readme_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        note = QLabel(
            "An empty README.md. It is only created where the repository has no "
            "README of any kind, and never overwrites one."
        )
        note.setWordWrap(True)
        box.addWidget(note)
        box.addStretch(1)
        return page

    def _build_gitignore_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.addWidget(QLabel("Ignore what the tools of these languages leave behind:"))
        self.language_list = QListWidget()
        # A grid, filled row by row: twelve names in one column is a list to
        # scroll, and a ticked language scrolled out of sight reads as unticked.
        self.language_list.setFlow(QListView.Flow.LeftToRight)
        self.language_list.setWrapping(True)
        self.language_list.setResizeMode(QListView.ResizeMode.Adjust)
        metrics = self.language_list.fontMetrics()
        widest = max(metrics.horizontalAdvance(l.label) for l in languages.LANGUAGES)
        self.language_list.setGridSize(QSize(widest + 56, metrics.height() + 14))
        for language in languages.LANGUAGES:
            item = QListWidgetItem(language.label)
            item.setData(Qt.ItemDataRole.UserRole, language.id)
            item.setCheckState(Qt.CheckState.Unchecked)
            template = starter_files.GITIGNORE_TEMPLATE.get(language.id)
            if template:
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setToolTip(f"Adds GitHub's {template}.gitignore.")
            else:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setToolTip(
                    f"GitHub's .gitignore collection has no template for "
                    f"{language.label}: there is nothing to add."
                )
            self.language_list.addItem(item)
        self.language_list.itemChanged.connect(self._schedule_preview)
        box.addWidget(self.language_list, 1)
        return page

    def _build_attributes_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)

        default = QHBoxLayout()
        default.addWidget(QLabel("Every other text file:"))
        self.default_ending = QComboBox()
        for ending in ("lf", "crlf", "native"):
            self.default_ending.addItem(starter_files.ENDING_LABELS[ending], ending)
        self.default_ending.currentIndexChanged.connect(self._schedule_preview)
        default.addWidget(self.default_ending)
        default.addStretch(1)
        box.addLayout(default)

        self.rules_table = QTableWidget(0, 2)
        self.rules_table.setHorizontalHeaderLabels(["Files", "Line endings"])
        header = self.rules_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.rules_table.verticalHeader().setVisible(False)
        self.rules_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        for rule in starter_files.DEFAULT_RULES:
            self._add_rule_row(rule.pattern, rule.ending)
        self.rules_table.itemChanged.connect(self._on_rule_edited)
        box.addWidget(self.rules_table, 1)

        buttons = QHBoxLayout()
        self.add_rule_btn = QPushButton("Add rule")
        self.add_rule_btn.clicked.connect(self._on_add_rule)
        self.remove_rule_btn = QPushButton("Remove rule")
        self.remove_rule_btn.clicked.connect(self._on_remove_rule)
        buttons.addWidget(self.add_rule_btn)
        buttons.addWidget(self.remove_rule_btn)
        buttons.addStretch(1)
        box.addLayout(buttons)

        hint = QLabel(
            "Patterns as .gitattributes reads them: *.bat, docs/**, Makefile. "
            "CRLF on Windows only is Native."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(MUTED_COLOUR)
        box.addWidget(hint)
        return page

    def _add_rule_row(self, pattern: str, ending: str) -> int:
        row = self.rules_table.rowCount()
        self.rules_table.insertRow(row)
        self.rules_table.setItem(row, 0, QTableWidgetItem(pattern))
        combo = QComboBox()
        for value in starter_files.ENDINGS:
            combo.addItem(starter_files.ENDING_LABELS[value], value)
        combo.setCurrentIndex(starter_files.ENDINGS.index(ending))
        combo.currentIndexChanged.connect(self._schedule_preview)
        self.rules_table.setCellWidget(row, 1, combo)
        return row

    def _on_add_rule(self) -> None:
        row = self._add_rule_row("", "lf")
        self.rules_table.setCurrentCell(row, 0)
        self.rules_table.editItem(self.rules_table.item(row, 0))

    def _on_remove_rule(self) -> None:
        rows = sorted({index.row() for index in self.rules_table.selectedIndexes()})
        if not rows and self.rules_table.currentRow() >= 0:
            rows = [self.rules_table.currentRow()]
        for row in reversed(rows):
            self.rules_table.removeRow(row)
        self._schedule_preview()

    def _on_rule_edited(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        pattern = item.text().strip()
        problem = starter_files.pattern_problem(pattern) if pattern else ""
        # Blocked: colouring the cell is itself a change to it.
        self.rules_table.blockSignals(True)
        colour = QColor(_PROBLEM_COLOUR) if problem else self.palette().text()
        item.setForeground(colour)
        item.setToolTip(problem)
        self.rules_table.blockSignals(False)
        self._schedule_preview()

    def _build_license_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.license_kind = QComboBox()
        for kind in starter_files.LICENSES:
            self.license_kind.addItem(starter_files.LICENSE_LABELS[kind], kind)
        self.license_kind.currentIndexChanged.connect(self._on_license_kind)
        form.addRow("License:", self.license_kind)

        self.holder = QLineEdit()
        self.holder.setPlaceholderText("You, or the organization the code belongs to")
        self.holder.textEdited.connect(self._on_holder_edited)
        self.holder.textChanged.connect(self._schedule_preview)
        form.addRow("Copyright holder:", self.holder)

        self.year = QSpinBox()
        self.year.setRange(1970, 9999)
        self.year.setValue(datetime.date.today().year)
        self.year.valueChanged.connect(self._schedule_preview)
        form.addRow("Year:", self.year)

        self.license_note = QLabel("")
        self.license_note.setWordWrap(True)
        self.license_note.setStyleSheet(MUTED_COLOUR)
        form.addRow(self.license_note)
        self._on_license_kind()
        return page

    def _on_license_kind(self, *_) -> None:
        mit = self.license_kind.currentData() == "MIT"
        self.holder.setEnabled(mit)
        self.year.setEnabled(mit)
        self.license_note.setText(
            "The holder and year go at the top of the license."
            if mit
            else "Apache 2.0 is added word for word. Its appendix is the notice to "
            "put at the top of each source file, not something to fill in here."
        )
        self._schedule_preview()

    # ---- what is chosen ------------------------------------------------------------
    def chosen_languages(self) -> list[str]:
        items = map(self.language_list.item, range(self.language_list.count()))
        return [
            item.data(Qt.ItemDataRole.UserRole)
            for item in items
            if item.checkState() == Qt.CheckState.Checked
        ]

    def attributes(self) -> starter_files.Attributes:
        rules = []
        for row in range(self.rules_table.rowCount()):
            item = self.rules_table.item(row, 0)
            pattern = item.text().strip() if item is not None else ""
            if pattern:  # a row still being typed into says nothing yet
                ending = self.rules_table.cellWidget(row, 1).currentData()
                rules.append(starter_files.EolRule(pattern, ending))
        return starter_files.Attributes(self.default_ending.currentData(), tuple(rules))

    def choices(self) -> starter_files.Choices:
        on = {name: box.isChecked() for name, box in self.include.items()}
        languages_chosen = tuple(self.chosen_languages())
        return starter_files.Choices(
            readme=on[starter_files.README],
            gitignore=languages_chosen if on[starter_files.GITIGNORE] else None,
            attributes=self.attributes() if on[starter_files.GITATTRIBUTES] else None,
            license=(
                self.license_kind.currentData() if on[starter_files.LICENSE] else ""
            ),
            holder=self.holder.text(),
            year=self.year.value(),
        )

    # ---- preview -------------------------------------------------------------------
    def _schedule_preview(self, *_) -> None:
        if self._built:
            self._preview_timer.start()

    def update_preview(self) -> None:
        """Show what adding the file on the open tab would do, to the selected repo."""
        repo = self.repo_picker.current_path()
        name = FILES[self.file_tabs.currentIndex()]
        where = Path(repo).name if repo else "a new repository"
        planned = starter_files.plan(repo or None, self.choices())
        item = next((p for p in planned if p.kind == name), None)
        if item is None:
            self.preview_title.setText(f"{name} is not being added.")
            self.preview.setPlainText("")
            self.kept_label.hide()
        else:
            self.preview_title.setText(f"In {where}: {item.summary}")
            self.preview.setPlainText(item.added)
            self.kept_label.setText("\n".join(item.kept))
            self.kept_label.setVisible(bool(item.kept))
        self._update_buttons()

    # ---- the repository -------------------------------------------------------------
    def refresh_repos(self) -> None:
        """Reload the list (another tab may have added to it)."""
        self.repo_picker.refresh()
        self._on_repo_changed(self.repo_picker.current_path())

    def _on_repo_changed(self, _path: str = "") -> None:
        self._fill_holder()
        self._schedule_preview()
        self._update_buttons()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._git_asked:
            self._git_asked = True
            if not self._branch_typed:
                self.initial_branch.setText(git_ops.default_branch_name() or "main")
            self._fill_holder()

    def _fill_holder(self) -> None:
        """Whoever git commits as in the selected repository, until one is typed.

        Asked again only for another repository: this runs every time the tab is
        shown, and the answer is a git command.
        """
        if self._holder_typed or not self._git_asked:
            return
        repo = self.repo_picker.current_path()
        if repo == self._holder_repo:
            return
        self._holder_repo = repo
        # A repository's identity falls back to the global one already, so the
        # global one is only asked for when there is no repository to ask.
        name = git_ops.get_identity(repo)[0] if repo else git_ops.get_global_identity()[0]
        self.holder.setText(name)

    def _register(self, path: str) -> str:
        """List ``path`` as a repository and select it; the path as it is stored."""
        if self._add_repository is not None:
            stored = self._add_repository(path)
        else:
            stored = self._remember(path)
        self.repo_picker.refresh()
        self.repo_picker.select(stored)
        return stored

    def _remember(self, path: str) -> str:
        """Settings-only listing, for a panel with no window to keep the list."""
        key = norm_path(path)
        for entry in self.settings.repos:
            if norm_path(entry.path) == key:
                return entry.path
        path = os.path.normpath(path)
        self.settings.repos.append(RepoEntry(path=path))
        return path

    # ---- actions --------------------------------------------------------------------
    def _on_url_changed(self, text: str) -> None:
        if self._name_follows_url:
            self.clone_name.setText(git_ops.repo_name_from_url(text))
        self._update_buttons()

    def _on_name_edited(self, text: str) -> None:
        # Typed by hand, the name stops following the URL -- until it is cleared.
        self._name_follows_url = not text.strip()

    def _on_branch_edited(self, _text: str) -> None:
        self._branch_typed = True

    def _on_holder_edited(self, _text: str) -> None:
        self._holder_typed = True

    def _update_buttons(self, *_) -> None:
        if not self._built:
            return
        cloning = self._worker is not None
        self.clone_btn.setEnabled(
            not cloning
            and bool(self.clone_url.text().strip())
            and bool(self.clone_parent.text().strip())
            and bool(self.clone_name.text().strip())
        )
        self.cancel_btn.setEnabled(cloning and not self._cancelling)
        self.create_btn.setEnabled(
            bool(self.create_parent.text().strip())
            and bool(self.create_name.text().strip())
            and bool(self.initial_branch.text().strip())
        )
        repo = self.repo_picker.current_path()
        self.add_btn.setEnabled(
            bool(repo) and any(box.isChecked() for box in self.include.values())
        )
        self.add_btn.setText(f"Add to {Path(repo).name}" if repo else "Add files")

    def _on_clone(self) -> None:
        url = self.clone_url.text().strip()
        parent = self.clone_parent.text().strip()
        name = self.clone_name.text().strip()
        if problem := folder_name_problem(name):
            self._say(self.clone_status, problem, warn=True)
            return
        destination = Path(parent) / name
        if destination.exists() and (
            not destination.is_dir() or any(destination.iterdir())
        ):
            self._say(
                self.clone_status,
                f"{destination} already exists and is not empty.",
                warn=True,
            )
            return
        if _CREDENTIALS_RE.match(url) and not self._confirm(
            "Clone with a password in the URL?",
            "The URL names a user, and probably a password or token. Git keeps the "
            "URL in the new repository's .git/config, readable by anything that "
            "can read the folder.\n\nClone anyway?",
        ):
            return
        if not self._outside_other_repositories(parent):
            return

        depth = self.depth.value() if self.shallow.isChecked() else None
        worker = CloneWorker(url, str(destination), depth=depth)
        # Bound methods, not lambdas: Qt disconnects those when this panel goes.
        worker.progress.connect(self._on_clone_progress)
        worker.percent.connect(self._on_clone_percent)
        worker.finished.connect(self._on_clone_finished)
        worker.error.connect(self._on_clone_error)
        self._worker = worker
        self._cancelling = False
        self._say(self.clone_status, f"Cloning into {destination}...")
        self._busy_start(f"Cloning {name}")
        self._update_buttons()
        run_worker(worker)

    def _outside_other_repositories(self, folder: str) -> bool:
        """True to go ahead: ``folder`` is not in a working tree, or that is wanted."""
        if not git_ops.is_git_repo(folder):
            return True
        return self._confirm(
            "Inside another repository",
            f"{folder} is inside a git repository, so the new one would sit in its "
            "working tree as untracked files.\n\nGo ahead anyway?",
        )

    def _on_clone_progress(self, line: str) -> None:
        if len(line) > _PROGRESS_MAX:
            line = line[: _PROGRESS_MAX - 3] + "..."
        self._say(self.clone_status, line)

    def _on_clone_percent(self, percent: int) -> None:
        self._busy_step(percent)

    def _on_cancel(self) -> None:
        if self._worker is None:
            return
        self._cancelling = True
        self._worker.cancel()
        self._say(self.clone_status, "Cancelling...")
        self._update_buttons()

    def cancel_running(self) -> None:
        """Stop a clone in flight (the window is closing)."""
        if self._worker is not None:
            self._worker.cancel()

    def _on_clone_finished(self, result: git_ops.CloneResult) -> None:
        self._worker = None
        self._cancelling = False
        self._busy_stop()
        self._update_buttons()
        if result.cancelled:
            if result.leftover:
                self._say(
                    self.clone_status,
                    f"Clone cancelled, but {result.leftover} could not be removed: "
                    "something still has a file in it open.",
                    warn=True,
                )
            else:
                self._say(self.clone_status, "Clone cancelled.")
            return
        if not (result.ok or result.checkout_failed):
            self._say(self.clone_status, "The clone failed.", warn=True)
            QMessageBox.critical(
                self, "Clone failed", result.stderr.strip() or "git clone failed."
            )
            return

        stored = self._register(result.destination)
        if result.checkout_failed:
            self._say(
                self.clone_status,
                f"Cloned into {stored}, but not every file could be written.",
                warn=True,
            )
            QMessageBox.warning(
                self,
                "Some files were not written",
                f"{result.stderr.strip()}\n\nThe repository itself is complete. A path "
                "too long for Windows is the usual reason; see git's advice above.",
            )
            return
        text = f"Cloned into {stored}."
        if git_ops.blocked_by_ownership(stored):
            text += (
                " Git will not work in it until it is trusted: use Mark listed "
                "repos as safe... on the Repositories & Settings tab."
            )
        self._say(self.clone_status, text)

    def _on_clone_error(self, message: str) -> None:
        self._worker = None
        self._cancelling = False
        self._busy_stop()
        self._update_buttons()
        self._say(self.clone_status, "The clone failed.", warn=True)
        QMessageBox.critical(self, "Clone failed", message)

    def _on_create(self) -> None:
        parent = self.create_parent.text().strip()
        name = self.create_name.text().strip()
        branch = self.initial_branch.text().strip()
        if problem := folder_name_problem(name):
            self._say(self.create_status, problem, warn=True)
            return
        if not git_ops.valid_branch_name(branch):
            self._say(
                self.create_status,
                f"'{branch}' is not a name git accepts for a branch.",
                warn=True,
            )
            return
        target = Path(parent) / name
        if git_ops.has_git_dir(target):
            if self._confirm(
                "Already a repository",
                f"{target} is already a git repository.\n\nSelect it instead?",
            ):
                stored = self._register(str(target))
                self._say(self.create_status, f"Selected {stored}.")
            return
        if target.is_dir() and any(target.iterdir()):
            if not self._confirm(
                "The folder is not empty",
                f"{target} already has files in it.\n\nMake it a repository anyway? "
                "Nothing in it is changed, staged or committed.",
            ):
                return
        if not self._outside_other_repositories(parent):
            return

        result = git_ops.init(target, initial_branch=branch)
        if not result.ok:
            self._say(self.create_status, "The repository was not created.", warn=True)
            QMessageBox.critical(
                self,
                "Could not create the repository",
                result.stderr.strip() or result.stdout.strip() or "git init failed.",
            )
            return
        stored = self._register(str(target))
        text = f"Created {stored} on branch {branch}."
        if self.create_with_files.isChecked():
            text += " " + self.add_files_to(stored)
        self._say(self.create_status, text.strip())

    def _on_add_files(self) -> None:
        repo = self.repo_picker.current_path()
        if not repo:
            return
        self._say(self.files_status, self.add_files_to(repo))
        self.update_preview()

    def add_files_to(self, repo: str) -> str:
        """Write the chosen starter files into ``repo``, and say what happened."""
        planned = starter_files.plan(repo, self.choices())
        replace = False
        replacing = [item for item in planned if item.action == "replace"]
        if replacing:
            label = starter_files.LICENSE_LABELS[self.license_kind.currentData()]
            replace = self._confirm(
                "Replace the license?",
                f"{Path(repo).name} already has {replacing[0].name}.\n\n"
                f"Replace it with the {label} license?",
            )
        report = starter_files.write(repo, planned, replace=replace)

        said = [report.summary()] if report.done else []
        said += [
            item.summary
            for item in planned
            if item.action == "skip" or (item.action == "replace" and not replace)
        ]
        said += report.problems
        attributes = next(
            (p for p in planned if p.kind == starter_files.GITATTRIBUTES), None
        )
        if (
            attributes is not None
            and attributes.action in ("create", "append")
            and git_ops.has_head(repo)
        ):
            said.append(
                "Files already committed keep their line endings until you run "
                "git add --renormalize ."
            )
        return " ".join(said) if said else "Nothing was chosen to add."

    # ---- helpers --------------------------------------------------------------------
    def _confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    @staticmethod
    def _status_label(colour: str = INFO_COLOUR) -> QLabel:
        label = QLabel("")
        label.setWordWrap(True)
        label.setStyleSheet(colour)
        return label

    @staticmethod
    def _say(label: QLabel, text: str, *, warn: bool = False) -> None:
        label.setStyleSheet(WARN_COLOUR if warn else INFO_COLOUR)
        label.setText(text)

    # ---- the window's shared progress bar ------------------------------------------
    def _busy_start(self, what: str) -> None:
        """Say a clone has begun, if this panel is in a window that has a bar."""
        if self.busy is not None:
            self.busy.start(self, what)

    def _busy_step(self, percent: int) -> None:
        if self.busy is not None:
            self.busy.step(self, percent)

    def _busy_stop(self) -> None:
        if self.busy is not None:
            self.busy.stop(self)
