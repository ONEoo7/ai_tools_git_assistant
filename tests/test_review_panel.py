"""The Code Review tab: what is offered, what is remembered, what is shown."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from git_assistant import repo_config
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


def test_the_shipped_rules_are_offered_when_the_user_has_none_of_their_own(
    qapp, with_repo, staged
):
    """A fresh install can review straight away, against what ships with it."""
    panel = ReviewPanel(with_repo)

    assert panel.files_list.count() == 3
    assert panel.profile_combo.currentData() == "Built-in defaults"
    assert panel.review_btn.isEnabled()


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
def test_a_repository_that_had_one_table_keeps_reviewing_with_it(
    qapp, with_repo, staged, monkeypatch
):
    """The migration: the same rules for every file, as before profiles."""
    monkeypatch.setattr(RuleStore, "load", staticmethod(_with_table))
    with_repo.repos[0].review_rules = "House rules"

    panel = ReviewPanel(with_repo)

    assert panel.profile_combo.currentData() == "House rules (imported)"
    assert panel.rules_table.rowCount() == 2
    profile = panel._current_profile()
    assert [e.language for e in profile.languages] == ["*"]


def test_the_profile_a_repository_is_assigned_is_the_one_selected(
    qapp, with_repo, staged, monkeypatch
):
    from git_assistant.review.profiles import LanguageRules, Profile, Selection

    monkeypatch.setattr(RuleStore, "load", staticmethod(_with_table))
    _set_profiles(with_repo, [
        Profile("Mine", [LanguageRules("python", selections=[Selection("builtin:python")])]),
        Profile("Theirs", [LanguageRules("rust", selections=[Selection("builtin:rust")])]),
    ])
    with_repo.repos[0].review_profile = "Theirs"

    panel = ReviewPanel(with_repo)

    assert panel.profile_combo.currentData() == "Theirs"


def test_choosing_a_profile_is_remembered_for_that_repository_only(
    qapp, with_repo, staged, monkeypatch
):
    from git_assistant.review.profiles import LanguageRules, Profile, Selection

    monkeypatch.setattr(RuleStore, "load", staticmethod(_with_table))
    _set_profiles(with_repo, [
        Profile("Mine", [LanguageRules("python", selections=[Selection("builtin:python")])])
    ])
    panel = ReviewPanel(with_repo)

    panel.profile_combo.setCurrentIndex(panel.profile_combo.findData("Mine"))

    assert with_repo.review_profile_for_repo("/x/demo") == "Mine"
    assert with_repo.review_profile_for_repo("/x/other") == ""


# ---- the profile library ----------------------------------------------------------
# It lives in the settings a repository carries now: a profile decides which
# rules a review runs against, which is what a project would want to standardise.
# Which one a repository uses stays a selection, on the repo entry.
def _set_profiles(settings, profiles):
    repo_config.save_user_profiles(profiles)


def _profiles(settings):
    return repo_config.bind(settings).review_profiles_built()


# ---- reading a profile is not choosing one ---------------------------------------
def _two_profiles(settings):
    from git_assistant.review.profiles import LanguageRules, Profile, Selection

    _set_profiles(settings, [
        Profile("Mine", [LanguageRules("python", selections=[Selection("builtin:python")])]),
        Profile("Theirs", [LanguageRules("rust", selections=[Selection("builtin:rust")])]),
    ])
    settings.repos[0].review_profile = "Mine"


def test_the_tab_lists_every_profile_there_is(qapp, with_repo, staged):
    _two_profiles(with_repo)
    panel = ReviewPanel(with_repo)

    listed = [
        panel.profile_tab.profiles_list.item(i).text()
        for i in range(panel.profile_tab.profiles_list.count())
    ]
    assert listed == ["Mine", "Theirs", "Built-in defaults"]


def test_the_tab_opens_the_one_under_review_to_begin_with(qapp, with_repo, staged):
    _two_profiles(with_repo)
    panel = ReviewPanel(with_repo)
    assert panel.profile_tab.profile().name == "Mine"


def test_reading_another_profile_does_not_change_what_a_review_uses(
    qapp, with_repo, staged
):
    """The dropdown decides that, and only when Review is pressed."""
    _two_profiles(with_repo)
    panel = ReviewPanel(with_repo)

    panel.profile_tab.profiles_list.setCurrentRow(1)

    assert panel.profile_tab.profile().name == "Theirs"
    assert panel.profile_combo.currentData() == "Mine"
    assert with_repo.review_profile_for_repo("/x/demo") == "Mine"
    assert panel.build_plan().profile == "Mine"


def test_choosing_in_the_dropdown_does_not_change_what_the_tab_has_open(
    qapp, with_repo, staged
):
    _two_profiles(with_repo)
    panel = ReviewPanel(with_repo)
    panel.profile_tab.profiles_list.setCurrentRow(1)

    panel.profile_combo.setCurrentIndex(panel.profile_combo.findData("Built-in defaults"))

    assert panel.profile_tab.profile().name == "Theirs"
    assert "Built-in defaults" in panel.profile_tab.in_use.text()


def test_editing_a_profile_that_is_not_under_review_leaves_the_review_alone(
    qapp, with_repo, staged
):
    _two_profiles(with_repo)
    panel = ReviewPanel(with_repo)
    panel.profile_tab.profiles_list.setCurrentRow(1)

    entry = panel.profile_tab.profile().languages[0]
    panel.profile_tab._on_version_changed(entry, "rust2018")

    assert _profiles(with_repo)[1].languages[0].version == "rust2018"
    assert panel.profile_combo.currentData() == "Mine"


def test_editing_the_shipped_rules_keeps_the_edit_in_the_copy(qapp, with_repo, staged):
    """The defaults are rebuilt on every lookup; the edit must not be looked up again."""
    panel = ReviewPanel(with_repo)
    assert panel.profile_combo.currentData() == "Built-in defaults"

    entry = [e for e in panel.profile_tab.profile().languages if e.language == "python"][0]
    panel.profile_tab._on_version_changed(entry, "py38")

    copies = [p for p in _profiles(with_repo) if p.name == "Built-in defaults (edited)"]
    assert len(copies) == 1
    assert copies[0].version_for("python") == "py38"


def test_the_copy_is_not_put_under_review_behind_the_user_s_back(
    qapp, with_repo, staged
):
    panel = ReviewPanel(with_repo)
    entry = [e for e in panel.profile_tab.profile().languages if e.language == "python"][0]

    panel.profile_tab._on_version_changed(entry, "py38")

    assert panel.profile_combo.currentData() == "Built-in defaults"
    assert with_repo.review_profile_for_repo("/x/demo") == "Built-in defaults"
    assert "read-only" in panel.status.text()
    # ...but it is what the tab now has open, or the next edit would copy again.
    assert panel.profile_tab.profile().name == "Built-in defaults (edited)"


def test_editing_the_shipped_rules_twice_does_not_make_two_of_one_name(
    qapp, with_repo, staged
):
    panel = ReviewPanel(with_repo)

    for _ in range(2):
        index = panel.profile_combo.findData("Built-in defaults")
        panel.profile_tab.profiles_list.setCurrentRow(index)
        entry = [
            e for e in panel.profile_tab.profile().languages if e.language == "python"
        ][0]
        panel.profile_tab._on_version_changed(entry, "py38")

    names = [p.name for p in _profiles(with_repo)]
    assert names == ["Built-in defaults (edited)", "Built-in defaults (edited) 2"]


def test_renaming_a_table_repoints_the_repositories_that_used_it(
    qapp, with_repo, staged, monkeypatch
):
    monkeypatch.setattr(RuleStore, "load", staticmethod(_with_table))
    with_repo.repos[0].review_rules = "House rules"
    panel = ReviewPanel(with_repo)
    assert with_repo.review_table_for_repo("/x/demo") == "House rules"

    monkeypatch.setattr(
        "git_assistant.ui.review_panel.QInputDialog.getText",
        lambda *a, **k: ("Team rules", True),
    )
    panel._on_rename_table()

    assert with_repo.review_table_for_repo("/x/demo") == "Team rules"
    # And every profile that named it, or its next review runs against nothing.
    refs = [
        s.ref
        for p in _profiles(with_repo)
        for e in p.languages
        for s in e.selections
    ]
    assert "table:Team rules" in refs


def test_deleting_a_table_leaves_its_repositories_without_one(
    qapp, with_repo, staged, monkeypatch
):
    monkeypatch.setattr(RuleStore, "load", staticmethod(_with_table))
    panel = ReviewPanel(with_repo)

    _confirm(monkeypatch)
    panel._on_delete_table()

    assert with_repo.review_table_for_repo("/x/demo") == ""
    # The profile that used it keeps its name and loses the pointer, so the
    # tab says "no rules for python" rather than silently checking nothing.
    refs = [
        s.ref
        for p in _profiles(with_repo)
        for e in p.languages
        for s in e.selections
    ]
    assert "table:House rules" not in refs


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


def test_the_middle_pane_is_findings_the_profiles_and_the_rules(qapp, with_repo, staged):
    """The reviews moved out to the right, where the other tabs keep theirs."""
    panel = ReviewPanel(with_repo)
    assert [panel.tabs.tabText(i) for i in range(panel.tabs.count())] == [
        "Findings",
        "Profiles",
        "Rules",
    ]


# ---- the window shown before anything is sent -------------------------------------
def _decide(monkeypatch, answer, seen=None):
    def confirm(parent, plan, estimate, **kw):
        if seen is not None:
            seen.append((plan, estimate))
        return answer

    monkeypatch.setattr("git_assistant.ui.review_panel.confirm_plan", confirm)


def test_reviewing_shows_the_files_and_the_rules_first(qapp, with_repo, staged, monkeypatch):
    monkeypatch.setattr(RuleStore, "load", staticmethod(_with_table))
    seen = []
    _decide(monkeypatch, True, seen)
    started = []
    monkeypatch.setattr(
        "git_assistant.ui.review_panel.run_worker", lambda w: started.append(w)
    )
    panel = ReviewPanel(with_repo)

    panel._on_review()

    assert seen, "the window is shown before the first call"
    plan, estimate = seen[0]
    assert [f.path for f in plan.reviewable()] == ["app.py", "util.py"]
    assert estimate.feature == "Code review"
    assert estimate.calls == 2, "one per marked file"
    assert started


def test_declining_a_review_sends_nothing(qapp, with_repo, staged, monkeypatch):
    monkeypatch.setattr(RuleStore, "load", staticmethod(_with_table))
    _decide(monkeypatch, False)
    started = []
    monkeypatch.setattr(
        "git_assistant.ui.review_panel.run_worker", lambda w: started.append(w)
    )
    panel = ReviewPanel(with_repo)

    panel._on_review()

    assert started == []
    assert panel.review_btn.isEnabled()


def test_the_run_is_given_exactly_the_plan_that_was_shown(qapp, with_repo, staged, monkeypatch):
    """The window must not be able to list a file the run then skips."""
    monkeypatch.setattr(RuleStore, "load", staticmethod(_with_table))
    seen = []
    _decide(monkeypatch, True, seen)
    started = []
    monkeypatch.setattr(
        "git_assistant.ui.review_panel.run_worker", lambda w: started.append(w)
    )
    panel = ReviewPanel(with_repo)

    panel._on_review()

    assert started[0]._plan is seen[0][0]


# ---- selecting several previous reviews ---------------------------------------------
def test_several_reviews_can_be_selected_and_deleted(qapp, with_repo, staged, monkeypatch):
    """Open is a question about one review; deleting is a question about a list."""
    history.record(_run())
    history.record(_later_run())
    panel = ReviewPanel(with_repo)

    panel.runs_tree.selectAll()

    assert len(panel._selected_runs()) == 2
    assert not panel.open_run_btn.isEnabled()
    assert panel.delete_run_btn.isEnabled()

    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    panel._on_delete_run()

    assert panel.runs_tree.topLevelItemCount() == 0


def test_open_comes_back_when_one_review_is_selected(qapp, with_repo, staged):
    history.record(_run())
    history.record(_later_run())
    panel = ReviewPanel(with_repo)

    panel.runs_tree.selectAll()
    assert not panel.open_run_btn.isEnabled()

    panel.runs_tree.setCurrentItem(panel.runs_tree.topLevelItem(0))

    assert panel.open_run_btn.isEnabled()
