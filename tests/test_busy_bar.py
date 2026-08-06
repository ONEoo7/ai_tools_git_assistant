"""One progress bar for the window, shared by every tab that runs something."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui.agents_panel import AgentsPanel  # noqa: E402
from git_assistant.ui.busy_bar import BusyBar  # noqa: E402
from git_assistant.ui.preview_dialog import CommitPanel  # noqa: E402
from git_assistant.ui.review_panel import ReviewPanel  # noqa: E402
from git_assistant.ui.settings_dialog import SettingsDialog  # noqa: E402

AUDIT, REVIEW = object(), object()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bar(qapp):
    return BusyBar()


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None
    s.repos = [RepoEntry("/x/demo")]
    s.active_repo = "/x/demo"
    return s


# ---- what it shows -------------------------------------------------------------
def test_it_is_invisible_until_something_is_running(bar):
    assert not bar.is_busy()
    assert bar.isHidden() or not bar.isVisible()


def test_a_running_task_puts_it_up_and_names_it(bar):
    bar.start(AUDIT, "Repository audit")

    assert bar.is_busy()
    assert "Repository audit" in bar.label.text()


def test_a_task_of_unknown_length_leaves_the_bar_indeterminate(bar):
    bar.start(AUDIT, "Repository audit")
    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 0)


def test_progress_that_is_reported_is_shown(bar):
    bar.start(AUDIT, "Repository audit")
    bar.step(AUDIT, 40)

    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 100)
    assert bar.bar.value() == 40


def test_a_negative_percentage_means_unknown_again(bar):
    """A scan that cannot say how far along it is goes back to indeterminate."""
    bar.start(AUDIT, "Repository audit")
    bar.step(AUDIT, 40)
    bar.step(AUDIT, -1)

    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 0)


def test_finishing_takes_it_down(bar):
    bar.start(AUDIT, "Repository audit")
    bar.stop(AUDIT)
    assert not bar.is_busy()


# ---- two tabs at once ------------------------------------------------------------
def test_it_stays_up_while_any_task_is_running(bar):
    """An audit runs for minutes; a review can be started over the top of it."""
    bar.start(AUDIT, "Repository audit")
    bar.start(REVIEW, "Code review")

    bar.stop(REVIEW)

    assert bar.is_busy(), "the audit is still going"


def test_two_tasks_are_counted_rather_than_named(bar):
    bar.start(AUDIT, "Repository audit")
    bar.start(REVIEW, "Code review")
    assert bar.label.text() == "2 tasks running..."


def test_no_percentage_is_invented_for_two_tasks(bar):
    """40% and 90% have no single number between them."""
    bar.start(AUDIT, "Repository audit")
    bar.step(AUDIT, 40)
    bar.start(REVIEW, "Code review")

    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 0)


def test_the_one_left_running_is_named_again(bar):
    bar.start(AUDIT, "Repository audit")
    bar.start(REVIEW, "Code review")
    bar.stop(REVIEW)
    assert "Repository audit" in bar.label.text()


# ---- stray reports ---------------------------------------------------------------
def test_progress_from_a_finished_run_does_not_put_it_back_up(bar):
    """A worker's last signal can arrive after its panel has stopped."""
    bar.start(AUDIT, "Repository audit")
    bar.stop(AUDIT)

    bar.step(AUDIT, 80)

    assert not bar.is_busy()


def test_stopping_something_that_never_started_is_harmless(bar):
    bar.stop(AUDIT)
    assert not bar.is_busy()


# ---- the tabs no longer have their own -------------------------------------------
@pytest.mark.parametrize("panel", [AgentsPanel, ReviewPanel, CommitPanel])
def test_no_tab_keeps_a_progress_bar_of_its_own(qapp, settings, panel):
    from PyQt6.QtWidgets import QProgressBar

    made = panel(settings) if panel is not CommitPanel else panel(settings, False)
    assert not made.findChildren(QProgressBar)


@pytest.mark.parametrize("panel", [AgentsPanel, ReviewPanel, CommitPanel])
def test_a_panel_without_a_window_still_runs(qapp, settings, panel):
    """The tray opens one on its own; reporting to nothing must be a no-op."""
    made = panel(settings) if panel is not CommitPanel else panel(settings, False)
    assert made.busy is None
    made._busy_start("x")
    made._busy_step(50)
    made._busy_stop()


# ---- wired into the window ----------------------------------------------------------
def test_the_window_has_one_and_every_tab_reports_to_it(qapp, settings):
    dialog = SettingsDialog(settings)
    for panel in (dialog.commit_panel, dialog.agents_panel, dialog.review_panel):
        assert panel.busy is dialog.busy


def test_it_sits_in_the_middle_of_the_window(qapp, settings):
    """Centred in the window, not merely in the space the buttons left over --
    which is what equal stretch on the two outer columns buys."""
    dialog = SettingsDialog(settings)
    bar = dialog.bottom_bar

    row, column, rows, columns = bar.getItemPosition(bar.indexOf(dialog.busy))
    assert (row, column, rows, columns) == (0, 1, 1, 1)
    assert bar.columnStretch(0) == bar.columnStretch(2) > 0
    assert bar.columnStretch(1) == 0, "the bar keeps its natural width"


@pytest.mark.parametrize("width", [1200, 1400, 1900])
def test_it_really_lands_on_the_centre_line(qapp, settings, width):
    """Measured, not assumed: the first attempt at this sat 152px left of centre
    and a loose tolerance here let it through. Two pixels, or it is not centred."""
    dialog = SettingsDialog(settings)
    dialog.review_panel._set_running(True)
    dialog.show()
    dialog.resize(width, 800)
    qapp.processEvents()
    try:
        middle = dialog.busy.geometry().center().x()
        assert abs(middle - dialog.width() // 2) <= 2, (middle, dialog.width())
    finally:
        dialog.review_panel._set_running(False)
        dialog.close()


def test_a_review_shows_in_the_shared_bar(qapp, settings):
    dialog = SettingsDialog(settings)

    dialog.review_panel._set_running(True)

    assert dialog.busy.is_busy()
    assert "Code review" in dialog.busy.label.text()


def test_an_audit_shows_its_percentage_there(qapp, settings):
    dialog = SettingsDialog(settings)

    dialog.agents_panel._set_running(True)
    dialog.agents_panel._on_pct(60)

    assert dialog.busy.bar.value() == 60
    assert "Repository audit" in dialog.busy.label.text()


def test_generating_a_message_shows_there_too(qapp, settings):
    dialog = SettingsDialog(settings)

    dialog.commit_panel._set_busy(True)
    assert "Commit message" in dialog.busy.label.text()

    dialog.commit_panel._set_busy(False)
    assert not dialog.busy.is_busy()
