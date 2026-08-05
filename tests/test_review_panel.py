"""The Code Review tab: what is offered, what is remembered, what is shown."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from git_assistant import git_ops  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.review import history  # noqa: E402
from git_assistant.review import rules as rules_mod  # noqa: E402
from git_assistant.review.parse import Finding  # noqa: E402
from git_assistant.review.reviewer import FileReview, ReviewRun  # noqa: E402
from git_assistant.review.rules import Rule, RuleStore, RuleTable  # noqa: E402
from git_assistant.ui.review_panel import (  # noqa: E402
    NO_REPOS_MESSAGE,
    ReviewPanel,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Both stores redirected: a panel test must not touch the real config."""
    monkeypatch.setattr(rules_mod, "user_config_dir", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(history, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    return s


@pytest.fixture
def with_repo(settings):
    settings.repos = [RepoEntry("/x/demo"), RepoEntry("/x/other")]
    settings.active_repo = "/x/demo"
    return settings


@pytest.fixture
def staged(monkeypatch):
    """Two ordinary files and one the noise filter drops."""

    def diff(repo, mode):
        return "".join(
            f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n+one line\n"
            for p in ("app.py", "util.py", "uv.lock")
        )

    monkeypatch.setattr(git_ops, "get_diff", diff)
    return diff


def _confirm(monkeypatch):
    """Answer the next confirmation dialog with Yes."""
    monkeypatch.setattr(
        "git_assistant.ui.review_panel.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.Yes,
    )


def _table(name="House rules", count=2):
    return RuleTable(
        name=name, rules=[Rule(f"R-{i}", f"rule {i}") for i in range(1, count + 1)]
    )


def _with_table(name="House rules"):
    store = RuleStore()
    store.add(_table(name))
    return store


def _run(repo="/x/demo", findings=1, error="", truncated=False):
    return ReviewRun(
        repo_path=repo,
        table_name="House rules",
        rules_total=2,
        rules_sent=2,
        model="qwen",
        provider="lmstudio",
        started_at="2026-08-05T10:00:00Z",
        files=[
            FileReview(
                path="app.py",
                findings=[
                    Finding("R-1", "rule 1", "app.py", 42, "swallows the error")
                    for _ in range(findings)
                ],
                raw_reply="FINDING | R-1 | 42 | swallows the error",
                diff_truncated=truncated,
            ),
            FileReview(path="util.py", raw_reply="NO FINDINGS", error=error),
        ],
    )


# ---- the empty cases -----------------------------------------------------------
def test_warns_when_no_repositories(qapp, settings):
    panel = ReviewPanel(settings)
    assert panel.status.text() == NO_REPOS_MESSAGE
    assert not panel.review_btn.isEnabled()


def test_review_is_refused_until_there_is_a_rule_table(qapp, with_repo, staged):
    panel = ReviewPanel(with_repo)
    assert panel.files_list.count() == 3
    assert not panel.review_btn.isEnabled()


def test_nothing_staged_says_so(qapp, with_repo, monkeypatch):
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: "")
    panel = ReviewPanel(with_repo)
    assert "nothing staged" in panel.files_label.text()


def test_a_repository_git_cannot_read_shows_no_files_rather_than_failing(
    qapp, with_repo, monkeypatch
):
    def refuse(repo, mode):
        raise git_ops.GitError("dubious ownership")

    monkeypatch.setattr(git_ops, "get_diff", refuse)
    assert ReviewPanel(with_repo).files_list.count() == 0


# ---- marking files ----------------------------------------------------------------
def test_every_staged_file_starts_marked_for_review(qapp, with_repo, staged):
    panel = ReviewPanel(with_repo)
    assert panel.marked_paths() == ["app.py", "util.py"]
    assert "2 of 2 marked" in panel.files_label.text()


def test_a_file_filtered_as_noise_is_shown_as_unreviewable(qapp, with_repo, staged):
    panel = ReviewPanel(with_repo)
    row = [
        panel.files_list.item(i)
        for i in range(panel.files_list.count())
        if panel.files_list.item(i).data(Qt.ItemDataRole.UserRole) == "uv.lock"
    ][0]

    assert "filtered as noise" in row.text()
    assert not row.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert "uv.lock" not in panel.marked_paths()


def test_unticking_a_file_survives_a_refresh_of_the_repository_list(
    qapp, with_repo, staged
):
    """Switching tabs calls refresh_repos; it must not undo the user's choice."""
    panel = ReviewPanel(with_repo)
    panel.files_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert panel.marked_paths() == ["util.py"]

    panel.refresh_repos()

    assert panel.marked_paths() == ["util.py"]


def test_what_was_unmarked_is_remembered_per_repository(qapp, with_repo, staged):
    panel = ReviewPanel(with_repo)
    panel.files_list.item(0).setCheckState(Qt.CheckState.Unchecked)

    with_repo.active_repo = "/x/other"
    panel.repo_picker.refresh()
    panel._on_repo_changed()

    assert panel.marked_paths() == ["app.py", "util.py"]


def test_marking_none_and_all_again(qapp, with_repo, staged):
    panel = ReviewPanel(with_repo)
    panel._mark_all(False)
    assert panel.marked_paths() == []
    panel._mark_all(True)
    assert panel.marked_paths() == ["app.py", "util.py"]


def test_review_is_refused_when_no_files_are_marked(qapp, with_repo, staged, monkeypatch):
    monkeypatch.setattr(RuleStore, "load", staticmethod(_with_table))
    panel = ReviewPanel(with_repo)
    assert panel.review_btn.isEnabled()

    panel._mark_all(False)

    assert not panel.review_btn.isEnabled()


# ---- rule tables --------------------------------------------------------------------
def test_the_table_a_repository_is_assigned_is_the_one_selected(
    qapp, with_repo, staged, monkeypatch
):
    def two_tables():
        store = RuleStore()
        store.add(_table("Python"))
        store.add(_table("Go"))
        return store

    monkeypatch.setattr(RuleStore, "load", staticmethod(two_tables))
    with_repo.repos[0].review_rules = "Go"

    panel = ReviewPanel(with_repo)

    assert panel.rules_combo.currentText() == "Go"
    assert panel.rules_table.rowCount() == 2


def test_choosing_a_table_is_remembered_for_that_repository_only(
    qapp, with_repo, staged, monkeypatch
):
    def two_tables():
        store = RuleStore()
        store.add(_table("Python"))
        store.add(_table("Go"))
        return store

    monkeypatch.setattr(RuleStore, "load", staticmethod(two_tables))
    panel = ReviewPanel(with_repo)

    panel.rules_combo.setCurrentIndex(panel.rules_combo.findText("Go"))

    assert with_repo.review_table_for_repo("/x/demo") == "Go"
    assert with_repo.review_table_for_repo("/x/other") == ""


def test_renaming_a_table_repoints_the_repositories_that_used_it(
    qapp, with_repo, staged, monkeypatch
):
    monkeypatch.setattr(RuleStore, "load", staticmethod(_with_table))
    panel = ReviewPanel(with_repo)
    assert with_repo.review_table_for_repo("/x/demo") == "House rules"

    monkeypatch.setattr(
        "git_assistant.ui.review_panel.QInputDialog.getText",
        lambda *a, **k: ("Team rules", True),
    )
    panel._on_rename_table()

    assert panel.rules_combo.currentText() == "Team rules"
    assert with_repo.review_table_for_repo("/x/demo") == "Team rules"


def test_deleting_a_table_leaves_its_repositories_without_one(
    qapp, with_repo, staged, monkeypatch
):
    monkeypatch.setattr(RuleStore, "load", staticmethod(_with_table))
    panel = ReviewPanel(with_repo)

    _confirm(monkeypatch)
    panel._on_delete_table()

    assert panel.rules_combo.count() == 0
    assert with_repo.review_table_for_repo("/x/demo") == ""
    assert not panel.review_btn.isEnabled()


def test_the_rules_grid_shows_what_will_be_sent(qapp, with_repo, staged, monkeypatch):
    monkeypatch.setattr(RuleStore, "load", staticmethod(_with_table))
    panel = ReviewPanel(with_repo)

    assert panel.rules_table.item(0, 0).text() == "R-1"
    assert panel.rules_table.item(0, 1).text() == "rule 1"
    assert "2 rule(s)" in panel.rules_note.text()


# ---- the provider ---------------------------------------------------------------------
def test_the_provider_can_be_chosen_here_too(qapp, with_repo, staged):
    panel = ReviewPanel(with_repo)
    panel.provider_combo.setCurrentIndex(1)
    assert with_repo.provider == panel.provider_combo.currentData()


# ---- what a finished review shows ------------------------------------------------------
def test_findings_are_shown_under_the_file_they_belong_to(qapp, with_repo, staged):
    panel = ReviewPanel(with_repo)
    panel._show_run(_run())

    top = panel.findings_tree.topLevelItem(0)
    assert top.text(0).startswith("app.py")
    assert top.childCount() == 1
    assert "R-1" in top.child(0).text(0)
    assert panel.findings_tree.topLevelItem(1).text(0).startswith("util.py")


def test_selecting_a_finding_shows_the_rule_and_the_model_s_own_words(
    qapp, with_repo, staged
):
    panel = ReviewPanel(with_repo)
    panel._show_run(_run())
    child = panel.findings_tree.topLevelItem(0).child(0)

    panel.findings_tree.setCurrentItem(child)

    shown = panel.detail_view.toPlainText()
    assert "swallows the error" in shown and "rule 1" in shown


def test_a_finding_that_could_not_be_parsed_is_shown_rather_than_hidden(
    qapp, with_repo, staged
):
    run = _run(findings=0)
    run.files[0].findings = [
        Finding("", "", "app.py", 0, "unreadable", raw_line="prose", parsed=False)
    ]
    panel = ReviewPanel(with_repo)
    panel._show_run(run)

    child = panel.findings_tree.topLevelItem(0).child(0)
    assert "could not be read" in child.text(0)


def test_a_file_that_was_not_reviewed_is_not_shown_as_clean(qapp, with_repo, staged):
    panel = ReviewPanel(with_repo)
    panel._show_run(_run(error="the model returned nothing"))

    row = panel.findings_tree.topLevelItem(1)
    assert "returned nothing" in row.text(1)
    assert "1 file(s) were not reviewed" in panel.coverage_note.text()


def test_a_partly_sent_file_says_so_on_its_row(qapp, with_repo, staged):
    panel = ReviewPanel(with_repo)
    panel._show_run(_run(truncated=True))

    assert "truncated" in panel.findings_tree.topLevelItem(0).text(1)
    assert "only in part" in panel.coverage_note.text()


def test_findings_from_another_repository_stop_being_shown(qapp, with_repo, staged):
    panel = ReviewPanel(with_repo)
    panel._show_run(_run())
    assert panel.findings_tree.topLevelItemCount() == 2

    with_repo.active_repo = "/x/other"
    panel.repo_picker.refresh()
    panel._on_repo_changed()

    assert panel.findings_tree.topLevelItemCount() == 0
    assert not panel.export_btn.isEnabled()


def test_a_review_can_be_copied_only_once_there_is_one(qapp, with_repo, staged):
    panel = ReviewPanel(with_repo)
    assert not panel.copy_btn.isEnabled()
    panel._show_run(_run())
    assert panel.copy_btn.isEnabled()


# ---- previous reviews --------------------------------------------------------------------
def test_previous_reviews_lists_this_repository_s_runs_newest_first(
    qapp, with_repo, staged
):
    history.record(_run())
    history.record(_later_run())
    panel = ReviewPanel(with_repo)

    assert panel.runs_tree.topLevelItemCount() == 2
    assert panel.runs_tree.topLevelItem(0).text(0)  # a readable timestamp
    assert "finding(s)" in panel.runs_tree.topLevelItem(0).text(1)


def _later_run():
    run = _run()
    run.started_at = "2026-08-06T10:00:00Z"
    return run


def test_a_finished_review_is_recorded_and_appears_in_the_list(qapp, with_repo, staged):
    panel = ReviewPanel(with_repo)

    panel._on_finished(_run())

    assert panel.runs_tree.topLevelItemCount() == 1
    assert history.list_runs("/x/demo")[0].headline["findings"] == 1
    assert "1 finding(s)" in panel.status.text()


def test_opening_a_stored_review_shows_its_findings_and_says_where_they_came_from(
    qapp, with_repo, staged
):
    history.record(_run())
    panel = ReviewPanel(with_repo)
    panel.runs_tree.setCurrentItem(panel.runs_tree.topLevelItem(0))

    panel._on_open_run()

    assert panel.findings_tree.topLevelItemCount() == 2
    assert "Stored review" in panel.coverage_note.text()
    assert "not recorded" in panel.calls_pane.call_view.toPlainText()


def test_deleting_a_review_removes_it_from_the_list(qapp, with_repo, staged, monkeypatch):
    history.record(_run())
    panel = ReviewPanel(with_repo)
    panel.runs_tree.setCurrentItem(panel.runs_tree.topLevelItem(0))

    _confirm(monkeypatch)
    panel._on_delete_run()

    assert panel.runs_tree.topLevelItemCount() == 0


def test_open_and_delete_are_offered_only_when_a_review_is_selected(
    qapp, with_repo, staged
):
    history.record(_run())
    panel = ReviewPanel(with_repo)

    assert not panel.open_run_btn.isEnabled()
    panel.runs_tree.setCurrentItem(panel.runs_tree.topLevelItem(0))
    assert panel.open_run_btn.isEnabled() and panel.delete_run_btn.isEnabled()


# ---- the shared right-hand pane -------------------------------------------------------------
def test_the_review_tab_has_the_same_calls_pane_as_the_commit_tab(qapp, with_repo, staged):
    from git_assistant.ui.calls_pane import CallsPane

    panel = ReviewPanel(with_repo)
    assert isinstance(panel.calls_pane, CallsPane)


def test_previous_runs_is_the_default_half_of_the_right_hand_pane(qapp, with_repo, staged):
    from git_assistant.ui.side_panel import CALLS_TAB, HISTORY_TAB

    panel = ReviewPanel(with_repo)

    assert panel.side_panel.tabs.tabText(0) == HISTORY_TAB
    assert panel.side_panel.tabs.tabText(1) == CALLS_TAB
    assert panel.side_panel.tabs.currentIndex() == 0


def test_the_middle_pane_is_findings_and_rules_only(qapp, with_repo, staged):
    """The reviews moved out to the right, where the other tabs keep theirs."""
    panel = ReviewPanel(with_repo)
    assert [panel.tabs.tabText(i) for i in range(panel.tabs.count())] == [
        "Findings",
        "Rules",
    ]
