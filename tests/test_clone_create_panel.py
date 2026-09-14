"""The Clone & Create tab: what it asks, what it refuses, and where results land.

Git runs for real against local repositories. The clone's worker runs on the
test's own thread (`run_worker` is replaced), so its signals arrive before the
call that started it returns.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from git_assistant import git_ops, starter_files  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.review import languages  # noqa: E402
from git_assistant.ui import clone_create_panel as panel_module  # noqa: E402
from git_assistant.ui.clone_create_panel import (  # noqa: E402
    CloneCreatePanel,
    folder_name_problem,
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
YES = QMessageBox.StandardButton.Yes
CANCEL = QMessageBox.StandardButton.Cancel


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


def _repo(path, *, commit=True):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "--initial-branch=main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "core.autocrlf", "false")
    if commit:
        (path / "f.txt").write_text("one\n", encoding="utf-8")
        _git(path, "add", "f.txt")
        _git(path, "commit", "-qm", "initial")
    return path


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def slot_errors(monkeypatch):
    """Exceptions raised inside Qt slots, re-raised when the test ends."""
    caught = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: caught.append(exc))
    yield caught
    if caught:
        _kind, error, trace = caught[0]
        raise error.with_traceback(trace)


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    return s


@pytest.fixture
def source(tmp_path):
    """A repository to clone: two commits on main, and a second branch."""
    path = _repo(tmp_path / "source")
    (path / "f.txt").write_text("two\n", encoding="utf-8")
    _git(path, "commit", "-qam", "second")
    _git(path, "branch", "other", "HEAD~1")
    return path


@pytest.fixture
def existing(tmp_path, settings):
    """A repository already on the list, and selected."""
    path = _repo(tmp_path / "work" / "existing")
    settings.repos = [RepoEntry(str(path))]
    settings.active_repo = str(path)
    return path


@pytest.fixture
def inline(monkeypatch):
    """Run the clone worker on this thread; the workers it was given."""
    started = []

    def run(worker):
        started.append(worker)
        worker.run()

    monkeypatch.setattr(panel_module, "run_worker", run)
    return started


@pytest.fixture
def answer(monkeypatch):
    """Answer every question with ``button``; ``answer.asked`` has their titles."""
    asked = []

    def set_to(button):
        def question(parent, title, *args, **kwargs):
            asked.append(title)
            return button

        monkeypatch.setattr(QMessageBox, "question", staticmethod(question))

    set_to.asked = asked
    return set_to


@pytest.fixture
def shown(monkeypatch):
    """Record critical and warning boxes instead of blocking on them."""
    boxes = []
    for kind in ("critical", "warning"):
        monkeypatch.setattr(
            QMessageBox,
            kind,
            staticmethod(
                lambda parent, title, text, *a, _kind=kind, **k: boxes.append(
                    (_kind, title, text)
                )
            ),
        )
    return boxes


def _clone_setup(panel, url, parent):
    panel.clone_url.setText(str(url))
    panel.clone_parent.setText(str(parent))


# ---- naming the folder ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("payments", True),
        ("my project", True),
        ("", False),
        ("CON", False),
        ("aux.txt", False),
        ("a:b", False),
        ("ends.", False),
        ("ends ", False),
    ],
)
def test_names_windows_refuses_are_refused_here_first(name, ok):
    assert (folder_name_problem(name) == "") is ok


def test_the_folder_name_follows_the_url_until_one_is_typed(qapp, settings):
    panel = CloneCreatePanel(settings)

    panel.clone_url.setText("https://github.com/ONEoo7/payments.git")
    assert panel.clone_name.text() == "payments"

    panel.clone_name.textEdited.emit("billing")
    panel.clone_name.setText("billing")
    panel.clone_url.setText("https://github.com/ONEoo7/ledger.git")
    assert panel.clone_name.text() == "billing"

    panel.clone_name.setText("")
    panel.clone_name.textEdited.emit("")
    panel.clone_url.setText("https://github.com/ONEoo7/ledger.git/")
    assert panel.clone_name.text() == "ledger"


def test_clone_waits_for_a_url_a_folder_and_a_name(qapp, settings, tmp_path):
    panel = CloneCreatePanel(settings)
    panel.clone_parent.setText(str(tmp_path))
    assert not panel.clone_btn.isEnabled()

    panel.clone_url.setText("https://example.com/org/repo.git")

    assert panel.clone_btn.isEnabled()
    assert not panel.cancel_btn.isEnabled()


# ---- cloning -------------------------------------------------------------------------
def test_a_clone_is_listed_and_selected(
    qapp, settings, source, tmp_path, inline, slot_errors
):
    panel = CloneCreatePanel(settings)
    _clone_setup(panel, source, tmp_path / "clones")

    panel._on_clone()

    destination = str(tmp_path / "clones" / "source")
    assert git_ops.has_git_dir(destination)
    assert [entry.path for entry in settings.repos] == [destination]
    assert panel.repo_picker.current_path() == destination
    assert settings.active_repo == destination
    assert "Cloned into" in panel.clone_status.text()
    assert panel.clone_btn.isEnabled() and not panel.cancel_btn.isEnabled()


def _commits(destination):
    return _git(destination, "rev-list", "--count", "HEAD").stdout.strip()


def _is_shallow(destination):
    return _git(destination, "rev-parse", "--is-shallow-repository").stdout.strip()


def test_a_clone_is_one_commit_of_every_branch_unless_asked_otherwise(
    qapp, settings, source, tmp_path, inline, slot_errors
):
    panel = CloneCreatePanel(settings)
    assert panel.shallow.isChecked() and not panel.full_history.isChecked()
    assert panel.depth.isEnabled() and panel.depth.value() == 1
    _clone_setup(panel, source, tmp_path / "clones")

    panel._on_clone()

    destination = tmp_path / "clones" / "source"
    assert inline[0].depth == 1
    assert (_is_shallow(destination), _commits(destination)) == ("true", "1")
    assert "origin/other" in _git(destination, "branch", "-r").stdout


def test_a_shallow_clone_takes_the_depth_chosen(
    qapp, settings, source, tmp_path, inline, slot_errors
):
    panel = CloneCreatePanel(settings)
    _clone_setup(panel, source, tmp_path / "clones")
    panel.depth.setValue(2)

    panel._on_clone()

    assert inline[0].depth == 2
    assert _commits(tmp_path / "clones" / "source") == "2"


def test_full_history_is_one_click_away(
    qapp, settings, source, tmp_path, inline, slot_errors
):
    panel = CloneCreatePanel(settings)
    _clone_setup(panel, source, tmp_path / "clones")

    panel.full_history.setChecked(True)
    assert not panel.depth.isEnabled()
    panel._on_clone()

    destination = tmp_path / "clones" / "source"
    assert inline[0].depth is None
    assert _is_shallow(destination) == "false"


def test_a_clone_the_window_lists_is_selected_under_the_windows_spelling(
    qapp, settings, source, tmp_path, inline, slot_errors
):
    """The window returns the path it stored; selection compares strings."""
    told = []

    def add_repository(path):
        told.append(path)
        stored = path.upper()
        settings.repos.append(RepoEntry(stored))
        return stored

    panel = CloneCreatePanel(settings, add_repository=add_repository)
    _clone_setup(panel, source, tmp_path / "clones")

    panel._on_clone()

    assert told == [str(tmp_path / "clones" / "source")]
    assert panel.repo_picker.current_path() == told[0].upper()


def test_cancelling_a_clone_removes_it_and_asks_nothing(
    qapp, settings, source, tmp_path, monkeypatch, slot_errors
):
    def cancel_then_run(worker):
        worker.cancel()
        worker.run()

    monkeypatch.setattr(panel_module, "run_worker", cancel_then_run)
    panel = CloneCreatePanel(settings)
    _clone_setup(panel, source, tmp_path / "clones")

    panel._on_clone()

    assert not (tmp_path / "clones" / "source").exists()
    assert panel.clone_status.text() == "Clone cancelled."
    assert settings.repos == []


def test_a_clone_that_fails_says_why(
    qapp, settings, tmp_path, inline, shown, slot_errors
):
    panel = CloneCreatePanel(settings)
    _clone_setup(panel, tmp_path / "no-such-repository", tmp_path / "clones")

    panel._on_clone()

    ((kind, title, text),) = shown
    assert (kind, title) == ("critical", "Clone failed")
    assert "fatal:" in text
    assert settings.repos == []


def test_a_destination_with_files_in_it_is_refused_before_cloning(
    qapp, settings, source, tmp_path, inline, slot_errors
):
    (tmp_path / "clones" / "source").mkdir(parents=True)
    (tmp_path / "clones" / "source" / "mine.txt").write_text("x", encoding="utf-8")
    panel = CloneCreatePanel(settings)
    _clone_setup(panel, source, tmp_path / "clones")

    panel._on_clone()

    assert inline == []
    assert "already exists" in panel.clone_status.text()


def test_a_password_in_the_url_is_asked_about_first(
    qapp, settings, tmp_path, inline, answer, slot_errors
):
    answer(CANCEL)
    panel = CloneCreatePanel(settings)
    _clone_setup(panel, "https://me:token@example.com/org/repo.git", tmp_path)

    panel._on_clone()

    assert answer.asked == ["Clone with a password in the URL?"]
    assert inline == []


def test_cloning_into_another_working_tree_is_asked_about_first(
    qapp, settings, source, existing, inline, answer
):
    answer(CANCEL)
    panel = CloneCreatePanel(settings)
    _clone_setup(panel, source, existing)

    panel._on_clone()

    assert answer.asked == ["Inside another repository"]
    assert inline == []


def test_closing_mid_clone_stops_it(qapp, settings):
    panel = CloneCreatePanel(settings)
    stopped = []

    class Running:
        def cancel(self):
            stopped.append(True)

    panel._worker = Running()
    panel.cancel_running()

    assert stopped == [True]


# ---- creating ------------------------------------------------------------------------
def test_create_makes_a_repository_on_the_branch_given_and_selects_it(
    qapp, settings, tmp_path, slot_errors
):
    panel = CloneCreatePanel(settings)
    panel.create_parent.setText(str(tmp_path))
    panel.create_name.setText("project")
    panel.initial_branch.setText("trunk")

    panel._on_create()

    target = tmp_path / "project"
    assert _git(target, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/trunk"
    assert panel.repo_picker.current_path() == str(target)
    assert (target / "README.md").read_bytes() == b""
    assert (target / ".gitattributes").read_text(encoding="utf-8").count("text=auto")
    assert "Created" in panel.create_status.text()
    status = _git(target, "status", "--porcelain").stdout
    assert status.startswith("??"), "nothing staged"


def test_create_can_leave_the_starter_files_out(qapp, settings, tmp_path):
    panel = CloneCreatePanel(settings)
    panel.create_parent.setText(str(tmp_path))
    panel.create_name.setText("bare")
    panel.create_with_files.setChecked(False)

    panel._on_create()

    assert sorted(p.name for p in (tmp_path / "bare").iterdir()) == [".git"]


def test_a_folder_that_is_already_a_repository_is_offered_instead(
    qapp, settings, tmp_path, answer
):
    target = _repo(tmp_path / "already")
    answer(YES)
    panel = CloneCreatePanel(settings)
    panel.create_parent.setText(str(tmp_path))
    panel.create_name.setText("already")

    panel._on_create()

    assert answer.asked == ["Already a repository"]
    assert panel.repo_picker.current_path() == str(target)
    assert not (target / "README.md").exists(), "nothing added to a repository offered"


@pytest.mark.parametrize(("name", "branch"), [("CON", "main"), ("ok", "a..b")])
def test_create_refuses_what_windows_or_git_would(
    qapp, settings, tmp_path, name, branch
):
    panel = CloneCreatePanel(settings)
    panel.create_parent.setText(str(tmp_path))
    panel.create_name.setText(name)
    panel.initial_branch.setText(branch)

    panel._on_create()

    assert not (tmp_path / name).exists()
    assert panel.create_status.text()


# ---- starter files -------------------------------------------------------------------
def test_the_languages_are_code_reviews_and_those_without_a_template_are_disabled(
    qapp, settings
):
    panel = CloneCreatePanel(settings)
    items = [panel.language_list.item(i) for i in range(panel.language_list.count())]

    assert [item.text() for item in items] == [l.label for l in languages.LANGUAGES]
    for item in items:
        language = item.data(Qt.ItemDataRole.UserRole)
        enabled = bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)
        assert enabled is starter_files.has_template(language), language
        if not enabled:
            assert "no template" in item.toolTip()


def test_adding_files_waits_for_a_repository_and_names_it(qapp, settings, existing):
    alone = CloneCreatePanel(Settings())
    assert not alone.add_btn.isEnabled()

    panel = CloneCreatePanel(settings)

    assert panel.add_btn.isEnabled()
    assert panel.add_btn.text() == "Add to existing"


def test_the_preview_shows_the_file_on_the_open_tab(qapp, settings, existing):
    panel = CloneCreatePanel(settings)
    panel.file_tabs.setCurrentIndex(panel_module.FILES.index(".gitattributes"))
    panel.update_preview()
    assert "* text=auto eol=lf" in panel.preview.toPlainText()

    panel.default_ending.setCurrentIndex(panel.default_ending.findData("crlf"))
    panel.update_preview()

    assert "* text=auto eol=crlf" in panel.preview.toPlainText()
    assert panel.preview_title.text() == "In existing: Creates .gitattributes."


def test_a_pattern_git_would_misread_is_marked(qapp, settings):
    panel = CloneCreatePanel(settings)
    panel._on_add_rule()
    row = panel.rules_table.rowCount() - 1

    panel.rules_table.item(row, 0).setText("docs/")

    assert panel.rules_table.item(row, 0).toolTip()
    panel.update_preview()
    planned = starter_files.plan(None, panel.choices())
    (item,) = [p for p in planned if p.kind == ".gitattributes"]
    assert item.action == "skip"


def test_adding_files_to_a_repository_says_what_was_and_was_not_done(
    qapp, settings, existing
):
    (existing / "README.md").write_bytes(b"# Mine\n")
    panel = CloneCreatePanel(settings)
    panel.language_list.item(
        [l.id for l in languages.LANGUAGES].index("python")
    ).setCheckState(Qt.CheckState.Checked)

    panel._on_add_files()

    said = panel.files_status.text()
    assert "Created .gitattributes and .gitignore." in said
    assert "Skipped: README.md already exists." in said
    assert "git add --renormalize ." in said, "the repository has history"
    assert (existing / "README.md").read_bytes() == b"# Mine\n"
    ignored = (existing / ".gitignore").read_text(encoding="utf-8")
    assert "git-assistant: Python" in ignored


def test_replacing_a_license_is_asked_first(qapp, settings, existing, answer):
    (existing / "LICENSE").write_bytes(b"All rights reserved.\n")
    panel = CloneCreatePanel(settings)
    for name, box in panel.include.items():
        box.setChecked(name == "LICENSE")
    panel.holder.setText("Someone")

    answer(CANCEL)
    panel._on_add_files()
    assert answer.asked == ["Replace the license?"]
    assert (existing / "LICENSE").read_bytes() == b"All rights reserved.\n"

    answer(YES)
    panel._on_add_files()
    assert (existing / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    assert panel.files_status.text().startswith("Replaced LICENSE.")
