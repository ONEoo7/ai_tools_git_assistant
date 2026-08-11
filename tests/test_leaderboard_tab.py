"""The Leaderboard tab: what it ranks, and what it refuses to average."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from git_assistant.review import leaderboard, rule_files  # noqa: E402
from git_assistant.ui.leaderboard_tab import EMPTY, LeaderboardTab  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def board_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(rule_files, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


def _record(model="qwen", judge="sonnet-5", scores=(7.0,)):
    leaderboard.record(
        provider="lmstudio",
        model=model,
        judge_provider="claude",
        judge_model=judge,
        scores=list(scores),
    )


def _column(tab, column):
    return [tab.tree.topLevelItem(i).text(column) for i in range(tab.tree.topLevelItemCount())]


def test_an_empty_board_says_how_to_fill_it(qapp):
    tab = LeaderboardTab()

    assert tab.tree.topLevelItemCount() == 0
    assert tab.note.text() == EMPTY
    assert tab.clear_btn.isEnabled() is False


def test_every_row_is_listed_best_first(qapp):
    _record(model="poor", scores=[2.0])
    _record(model="good", scores=[9.0])
    _record(model="fair", scores=[5.0])

    tab = LeaderboardTab()

    assert _column(tab, 0) == [
        "good (lmstudio)",
        "fair (lmstudio)",
        "poor (lmstudio)",
    ]


def test_the_score_is_shown_to_two_places(qapp):
    _record(scores=[7.0, 8.0, 6.5])
    tab = LeaderboardTab()
    assert _column(tab, 4) == ["7.17"]


def test_runs_and_files_are_both_shown(qapp):
    """Two runs of three files is a better measurement than one of one."""
    _record(scores=[7.0, 8.0])
    _record(scores=[6.0])

    tab = LeaderboardTab()

    assert _column(tab, 2) == ["2"]  # runs
    assert _column(tab, 3) == ["3"]  # files


def test_each_judge_gets_its_own_row_and_is_named(qapp):
    _record(judge="sonnet-5", scores=[7.0])
    _record(judge="opus-5", scores=[9.0])

    tab = LeaderboardTab()

    assert sorted(_column(tab, 1)) == ["opus-5 (claude)", "sonnet-5 (claude)"]


def test_more_than_one_judge_warns_that_the_rows_are_not_comparable(qapp):
    """The methodological point, said where somebody would otherwise compare."""
    _record(judge="sonnet-5", scores=[7.0])
    _record(judge="opus-5", scores=[9.0])

    tab = LeaderboardTab()
    assert "2 different judges" in tab.note.text()
    assert "same judge are comparable" in tab.note.text()


def test_one_judge_names_the_file_instead(qapp):
    _record()
    tab = LeaderboardTab()
    assert "leaderboard.json" in tab.note.text()


def test_nothing_here_can_be_edited(qapp):
    _record()
    tab = LeaderboardTab()  # held: a temporary is collected mid-expression
    item = tab.tree.topLevelItem(0)
    assert not item.flags() & Qt.ItemFlag.ItemIsEditable


def test_refreshing_picks_up_a_run_recorded_since(qapp):
    tab = LeaderboardTab()
    assert tab.tree.topLevelItemCount() == 0

    _record()
    tab.refresh()

    assert tab.tree.topLevelItemCount() == 1


# ---- clearing ------------------------------------------------------------------------
def test_clearing_asks_first(qapp, monkeypatch):
    _record()
    asked = []
    monkeypatch.setattr(
        "git_assistant.ui.leaderboard_tab.QMessageBox.question",
        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Cancel,
    )
    tab = LeaderboardTab()

    tab._on_clear()

    assert asked, "it must not throw scores away silently"
    assert "cannot be rebuilt" in asked[0]
    assert leaderboard.load().rows, "cancelling kept them"


def test_confirming_throws_them_away(qapp, monkeypatch):
    _record()
    monkeypatch.setattr(
        "git_assistant.ui.leaderboard_tab.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.Yes,
    )
    tab = LeaderboardTab()
    told = []
    tab.cleared.connect(lambda: told.append(True))

    tab._on_clear()

    assert leaderboard.load().rows == []
    assert tab.tree.topLevelItemCount() == 0
    assert told == [True]


def test_clearing_an_empty_board_asks_nothing(qapp):
    """The autouse dialog guard in conftest fails this if it asks."""
    LeaderboardTab()._on_clear()


# ---- how long the model took ---------------------------------------------------------
def test_the_time_per_file_is_shown(qapp):
    leaderboard.record(
        provider="lmstudio",
        model="qwen",
        judge_provider="claude",
        judge_model="sonnet-5",
        scores=[7.0, 8.0],
        seconds=5.0,
    )
    tab = LeaderboardTab()
    assert _column(tab, 5) == ["2.50s"]


def test_a_row_with_no_time_shows_nothing_rather_than_zero(qapp):
    """A board written before times were kept must not claim they were 0s."""
    _record()
    tab = LeaderboardTab()
    assert _column(tab, 5) == [""]


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [(0.42, "0.42s"), (9.99, "9.99s"), (12.5, "12.5s"), (75.0, "1m 15s"), (0.0, "")],
)
def test_a_duration_is_written_the_way_it_is_read(seconds, shown):
    from git_assistant.ui.leaderboard_tab import _duration

    assert _duration(seconds) == shown


def test_the_column_says_it_is_per_call_not_per_run(qapp):
    """Files are reviewed several at a time, so elapsed time would measure the
    worker count rather than the model."""
    leaderboard.record(
        provider="lmstudio",
        model="qwen",
        judge_provider="claude",
        judge_model="sonnet-5",
        scores=[7.0],
        seconds=3.0,
    )
    tab = LeaderboardTab()

    assert "not how long the run took" in tab.tree.topLevelItem(0).toolTip(5)
