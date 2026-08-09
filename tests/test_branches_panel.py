"""The Branches half of the Branches & Tags tab.

Driven against a real repository, because the questions worth asking are about
what git did: whether the branch exists afterwards, and under what name.
"""

import os
import subprocess
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from git_assistant import git_ops, repo_config  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui.branches_tags_panel import BranchesTagsPanel  # noqa: E402

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def config_store(tmp_path, monkeypatch):
    """The user's defaults must not be the real ones."""
    monkeypatch.setattr(
        repo_config, "user_config_dir", lambda *a, **k: str(tmp_path / "config")
    )
    return repo_config


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Stefan Ghitescu")
    (path / "f.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-m", "initial")
    return path


@pytest.fixture
def panel(qapp, repo):
    settings = Settings()
    settings.save = lambda: None
    settings.repos = [RepoEntry(str(repo))]
    settings.active_repo = str(repo)
    return BranchesTagsPanel(settings)


def _set_repo_config(repo, data):
    path = repo_config.repo_config_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    path.write_text(json.dumps(data), encoding="utf-8")


def _use_pattern(panel, pattern=""):
    """Switch to the pattern card, as clicking it does, and pick a pattern."""
    panel._on_card_picked(panel.pattern_card)
    if pattern:
        combo = panel.pattern_card.pattern_combo
        combo.setCurrentIndex(combo.findData(pattern))
    return panel.pattern_card


# ---- what a new branch will be called -----------------------------------------
def test_the_tab_opens_on_the_plain_name(panel):
    """Most branches are not part of anybody's convention."""
    assert panel.plain_card.radio.isChecked()
    assert not panel.pattern_card.radio.isChecked()


def test_the_plain_card_creates_exactly_what_was_typed(panel):
    panel.plain_card.name_edit.setText("new-thing")

    assert "Will create:  new-thing" in panel.branch_preview.text()
    assert panel.create_branch_btn.isEnabled()


def test_a_slash_typed_into_the_plain_card_is_kept(panel):
    """`feature/login` typed whole is a branch name, not one word."""
    panel.plain_card.name_edit.setText("feature/login")
    assert "feature/login" in panel.branch_preview.text()


def test_the_patterns_are_offered_without_configuring_anything(panel):
    combo = panel.pattern_card.pattern_combo
    offered = [combo.itemData(i) for i in range(combo.count())]
    assert offered == ["dev/rem/{user}/{name}", "test/rem/{user}/{name}"]


def test_a_pattern_names_the_branch_after_the_user_and_what_was_typed(panel):
    """A pattern nobody can see is a rule nobody can follow."""
    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.name_edit.setText("new thing")

    assert "dev/rem/Stefan-Ghitescu/new-thing" in panel.branch_preview.text()


def test_the_user_can_be_typed_in_instead_of_gits(panel):
    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.user_edit.setText("rem")
    card.name_edit.setText("thing")

    assert "dev/rem/rem/thing" in panel.branch_preview.text()


def test_the_projects_own_pattern_is_offered_first(panel, repo):
    _set_repo_config(
        repo, {"branch": {"pattern": "dev/rem/{user}/{name}", "user": "sg"}}
    )
    panel._reload_config()

    assert panel.pattern_card.pattern_combo.itemData(0) == "dev/rem/{user}/{name}"

    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.name_edit.setText("new thing")
    assert "dev/rem/sg/new-thing" in panel.branch_preview.text()


def test_nothing_is_written_under_the_cards_until_a_name_is_typed(panel, repo):
    """The button sits under the card, not under two empty lines.

    Which settings are in force is in the bar above the tabs, and what each
    card does is on the card. Both were said again here, above the button, in
    a place that was permanently one or two lines tall either way.
    """
    _set_repo_config(repo, {"branch": {"patterns": ["x/{name}"]}})
    panel._reload_config()
    _use_pattern(panel)

    assert not panel.branch_preview.isVisibleTo(panel)
    assert not panel.branch_conflict.isVisibleTo(panel)


def test_the_preview_appears_as_the_name_is_typed(panel):
    panel.plain_card.name_edit.setText("thing")

    assert panel.branch_preview.isVisibleTo(panel)
    assert "thing" in panel.branch_preview.text()


def test_the_preview_goes_away_again_when_the_name_is_cleared(panel):
    panel.plain_card.name_edit.setText("thing")

    panel.plain_card.name_edit.setText("")

    assert not panel.branch_preview.isVisibleTo(panel)


def test_the_user_comes_from_git_when_nobody_names_one(panel, repo):
    """repo_config runs no git; this is the caller that can."""
    _set_repo_config(repo, {"branch": {"patterns": ["{user}/{name}"]}})
    panel._reload_config()
    card = _use_pattern(panel, "{user}/{name}")

    card.name_edit.setText("thing")

    assert "Stefan-Ghitescu/thing" in panel.branch_preview.text()


def test_a_name_that_leaves_nothing_usable_cannot_be_created(panel):
    panel.plain_card.name_edit.setText("///")

    assert panel.branch_preview.text() == ""
    assert not panel.create_branch_btn.isEnabled()


def test_an_empty_name_is_not_a_branch(panel):
    panel.plain_card.name_edit.setText("")
    assert not panel.create_branch_btn.isEnabled()


# ---- what is remembered ---------------------------------------------------------
def test_the_chosen_pattern_is_remembered_for_this_repository(panel, repo):
    _use_pattern(panel, "test/rem/{user}/{name}")

    assert panel.settings.branch_pattern_for(str(repo)) == "test/rem/{user}/{name}"
    assert panel.settings.branch_pattern_for("/somewhere/else") == ""


def test_going_back_to_the_plain_card_forgets_the_pattern(panel, repo):
    _use_pattern(panel, "test/rem/{user}/{name}")

    panel._on_card_picked(panel.plain_card)

    assert panel.settings.branch_pattern_for(str(repo)) == ""


def test_a_remembered_pattern_is_what_the_tab_opens_on(panel, repo):
    panel.settings.set_branch_pattern(str(repo), "test/rem/{user}/{name}")
    panel.settings.set_branch_user(str(repo), "rem")

    panel._reload_config()

    assert panel.pattern_card.radio.isChecked()
    assert panel.pattern_card.pattern_combo.currentData() == "test/rem/{user}/{name}"
    assert panel.pattern_card.user_edit.text() == "rem"


def test_a_pattern_that_is_no_longer_offered_is_still_the_one_selected(panel, repo):
    """Dropping it silently would rename the next branch without saying so."""
    panel.settings.set_branch_pattern(str(repo), "gone/{name}")

    panel._reload_config()

    assert panel.pattern_card.pattern_combo.currentData() == "gone/{name}"


def test_the_choice_is_not_written_into_any_repositorys_settings(panel, repo):
    """A selection must not make a settings file nobody asked for."""
    _use_pattern(panel, "test/rem/{user}/{name}")

    assert not repo_config.exists(repo_config.Tier.CUSTOM, str(repo))
    assert not repo_config.exists(repo_config.Tier.REPO, str(repo))


# ---- creating ------------------------------------------------------------------
def test_creating_makes_the_branch_the_preview_promised(panel, repo):
    _set_repo_config(
        repo, {"branch": {"patterns": ["dev/rem/{user}/{name}"], "user": "sg"}}
    )
    panel._reload_config()
    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.name_edit.setText("the-thing")

    panel._on_create_branch()

    assert git_ops.current_branch(repo) == "dev/rem/sg/the-thing"
    assert card.name_edit.text() == ""  # ready for the next one
    assert "dev/rem/sg/the-thing" in panel.branch_status.text()


def test_creating_one_that_exists_says_so_rather_than_moving_it(
    panel, repo, monkeypatch
):
    git_ops.create_branch(repo, "taken", switch=False)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    panel.plain_card.name_edit.setText("taken")

    panel._on_create_branch()

    assert warned and "already exists" in warned[0]


def test_a_new_branch_appears_in_the_list_as_the_current_one(panel, repo):
    panel.plain_card.name_edit.setText("fresh")
    panel._on_create_branch()

    rows = [
        panel.branch_list.topLevelItem(i)
        for i in range(panel.branch_list.topLevelItemCount())
    ]
    current = [r for r in rows if r.text(0).startswith("*")]
    assert len(current) == 1
    assert "fresh" in current[0].text(0)


# ---- the list ------------------------------------------------------------------
def test_every_branch_is_listed_with_what_it_tracks(panel, repo):
    git_ops.create_branch(repo, "second", switch=False)
    panel._reload_branches()

    rows = {
        panel.branch_list.topLevelItem(i).text(0).lstrip("* "): (
            panel.branch_list.topLevelItem(i).text(1)
        )
        for i in range(panel.branch_list.topLevelItemCount())
    }

    assert set(rows) == {git_ops.current_branch(repo), "second"}
    assert rows["second"] == "no upstream"


def test_the_branch_you_are_on_cannot_be_switched_to_or_deleted(panel, repo):
    git_ops.create_branch(repo, "other", switch=False)
    panel._reload_branches()

    for i in range(panel.branch_list.topLevelItemCount()):
        item = panel.branch_list.topLevelItem(i)
        panel.branch_list.setCurrentItem(item)
        on_it = item.text(0).startswith("*")
        assert panel.switch_btn.isEnabled() is not on_it
        assert panel.delete_branch_btn.isEnabled() is not on_it


def test_switching_checks_the_branch_out(panel, repo):
    git_ops.create_branch(repo, "elsewhere", switch=False)
    panel._reload_branches()
    for i in range(panel.branch_list.topLevelItemCount()):
        item = panel.branch_list.topLevelItem(i)
        if "elsewhere" in item.text(0):
            panel.branch_list.setCurrentItem(item)

    panel._on_switch_branch()

    assert git_ops.current_branch(repo) == "elsewhere"
    assert "elsewhere" in panel.branch_status.text()


# ---- deleting -------------------------------------------------------------------
def _select(panel, name):
    for i in range(panel.branch_list.topLevelItemCount()):
        item = panel.branch_list.topLevelItem(i)
        if item.text(0).lstrip("* ") == name:
            panel.branch_list.setCurrentItem(item)
            return
    raise AssertionError(f"{name} is not listed")


def test_a_merged_branch_is_deleted_after_one_question(panel, repo, monkeypatch):
    git_ops.create_branch(repo, "spare", switch=False)
    panel._reload_branches()
    _select(panel, "spare")
    asked = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes,
    )

    panel._on_delete_branch()

    assert not git_ops.branch_exists(repo, "spare")
    assert len(asked) == 1  # no second question: nothing was at risk


def test_declining_the_question_keeps_the_branch(panel, repo, monkeypatch):
    git_ops.create_branch(repo, "spare", switch=False)
    panel._reload_branches()
    _select(panel, "spare")
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel
    )

    panel._on_delete_branch()

    assert git_ops.branch_exists(repo, "spare")


def _make_unmerged(repo, name="unmerged"):
    main = git_ops.current_branch(repo)
    git_ops.create_branch(repo, name)
    (repo / "only-here.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "only-here.txt")
    _git(repo, "commit", "-m", "work")
    _git(repo, "switch", main)


def test_an_unmerged_branch_is_asked_about_twice(panel, repo, monkeypatch):
    """The second question is the only warning that commits are about to go."""
    _make_unmerged(repo)
    panel._reload_branches()
    _select(panel, "unmerged")
    asked = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes,
    )

    panel._on_delete_branch()

    assert len(asked) == 2
    assert "on no other branch" in asked[1]
    assert not git_ops.branch_exists(repo, "unmerged")


def test_declining_the_second_question_keeps_the_commits(panel, repo, monkeypatch):
    _make_unmerged(repo)
    panel._reload_branches()
    _select(panel, "unmerged")
    answers = iter(
        [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Cancel]
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: next(answers))

    panel._on_delete_branch()

    assert git_ops.branch_exists(repo, "unmerged")
    assert "not deleted" in panel.branch_status.text()


# ---- fetching -------------------------------------------------------------------
def test_a_fetch_uses_the_depth_the_repository_asked_for(panel, repo, monkeypatch):
    _set_repo_config(repo, {"fetch": {"shallow": True, "depth": 12, "prune": False}})
    panel._reload_config()
    seen = {}
    monkeypatch.setattr(
        git_ops,
        "fetch",
        lambda r, **kw: seen.update(kw) or git_ops.GitResult(True, "", "", 0),
    )
    monkeypatch.setattr(
        "git_assistant.ui.branches_tags_panel.run_worker",
        lambda worker: worker.run(),
    )

    panel._on_fetch()

    assert seen == {"depth": 12, "prune": False, "tags": True}
    assert "12" in panel.branch_status.text()


def test_a_full_fetch_asks_for_no_depth(panel, repo, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        git_ops,
        "fetch",
        lambda r, **kw: seen.update(kw) or git_ops.GitResult(True, "", "", 0),
    )
    monkeypatch.setattr(
        "git_assistant.ui.branches_tags_panel.run_worker",
        lambda worker: worker.run(),
    )

    panel._on_fetch()

    assert seen["depth"] is None
    assert panel.branch_status.text() == "Fetched."


def test_pushing_honours_whether_the_project_wants_an_upstream(
    panel, repo, monkeypatch
):
    _set_repo_config(repo, {"branch": {"push_sets_upstream": False}})
    panel._reload_config()
    git_ops.create_branch(repo, "to-push", switch=False)
    panel._reload_branches()
    _select(panel, "to-push")
    seen = {}
    monkeypatch.setattr(
        git_ops,
        "push_branch",
        lambda r, n, **kw: seen.update({"name": n, **kw})
        or git_ops.GitResult(True, "", "", 0),
    )
    monkeypatch.setattr(
        "git_assistant.ui.branches_tags_panel.run_worker",
        lambda worker: worker.run(),
    )

    panel._on_push_branch()

    assert seen == {"name": "to-push", "set_upstream": False}


def test_a_failed_remote_command_re_enables_the_buttons(panel, repo, monkeypatch):
    """Otherwise one failed fetch leaves the pane dead until the tab is left."""
    monkeypatch.setattr(
        git_ops, "fetch", lambda r, **kw: git_ops.GitResult(False, "", "no remote", 1)
    )
    monkeypatch.setattr(
        "git_assistant.ui.branches_tags_panel.run_worker",
        lambda worker: worker.run(),
    )
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

    panel._on_fetch()

    assert panel.fetch_btn.isEnabled()
    assert "failed" in panel.branch_status.text().lower()


# ---- against git itself, not against my reading of the manual -------------------------
@pytest.mark.parametrize(
    "typed",
    [
        "Stefan Ghitescu",
        "JIRA-412 fix login",
        "feature/login",
        "index.lock",
        ".hidden",
        "a/.hidden",
        "release.",
        "a..b",
        "wip~1",
        "user@{now}",
        r"back\slash",
        "caret^and:colon",
        "star*and?q[",
        "  spaced  ",
        "a///b",
        "x.lock/y",
    ],
)
def test_whatever_is_typed_becomes_a_name_git_will_take(panel, typed):
    """`git check-ref-format` is the authority, so it is what the test asks.

    A name this application offers on screen and git then refuses is the worst
    of both: the user read it, pressed the button, and got a fatal several
    seconds later with none of their words in it.
    """
    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.name_edit.setText(typed)

    name = panel._full_branch_name()
    if not name:
        assert not panel.create_branch_btn.isEnabled()
        return
    assert (
        subprocess.run(
            ["git", "check-ref-format", "--branch", name], capture_output=True
        ).returncode
        == 0
    ), name


def test_a_branch_the_preview_promised_can_really_be_created(panel, repo):
    """The end of it: git itself takes the name, not just check-ref-format."""
    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.name_edit.setText("index.lock and spaces")

    panel._on_create_branch()

    # `.lock` only has to go from the *end* of a piece, and it is not at the
    # end of this one -- git takes it, so it is kept rather than mangled.
    assert (
        git_ops.current_branch(repo)
        == "dev/rem/Stefan-Ghitescu/index.lock-and-spaces"
    )


def test_a_name_that_is_only_lock_is_not_left_as_a_ref_git_refuses(panel, repo):
    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.name_edit.setText("index.lock")

    panel._on_create_branch()

    assert git_ops.current_branch(repo) == "dev/rem/Stefan-Ghitescu/index"


# ---- a name git has no room for ------------------------------------------------------
def test_a_branch_in_the_way_is_said_before_the_button_is_pressed(panel, repo):
    """Git's own message arrives after the click. This one arrives while typing."""
    _git(repo, "branch", "dev")
    panel._reload_branches()
    card = _use_pattern(panel, "dev/rem/{user}/{name}")

    card.name_edit.setText("login")

    assert "'dev' is already a branch" in panel.branch_conflict.text()
    assert not panel.create_branch_btn.isEnabled()


def test_the_name_is_still_shown_so_the_problem_is_readable(panel, repo):
    """Hiding the name would leave a warning about something invisible."""
    _git(repo, "branch", "dev")
    panel._reload_branches()
    card = _use_pattern(panel, "dev/rem/{user}/{name}")

    card.name_edit.setText("login")

    assert "dev/rem/Stefan-Ghitescu/login" in panel.branch_preview.text()


def test_a_name_that_is_already_a_folder_of_branches_says_the_other_thing(panel, repo):
    _git(repo, "branch", "dev/rem/x")
    panel._reload_branches()
    panel.plain_card.name_edit.setText("dev/rem")

    assert "already a folder of branches" in panel.branch_conflict.text()
    assert not panel.create_branch_btn.isEnabled()


def test_nothing_is_said_when_there_is_no_conflict(panel, repo):
    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.name_edit.setText("login")

    assert panel.branch_conflict.text() == ""
    assert panel.create_branch_btn.isEnabled()


def test_deleting_what_was_in_the_way_clears_the_warning(panel, repo):
    _git(repo, "branch", "dev")
    panel._reload_branches()
    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.name_edit.setText("login")
    assert panel.branch_conflict.text()

    _git(repo, "branch", "-D", "dev")
    panel._reload_branches()

    assert panel.branch_conflict.text() == ""
    assert panel.create_branch_btn.isEnabled()


# ---- who {user} is --------------------------------------------------------------------
def _identity(name, email):
    from git_assistant.identities import Identity, IdentityStore

    return IdentityStore([Identity(name=name, email=email)])


def test_the_user_is_the_saved_identity_rather_than_what_git_says(qapp, repo):
    """The saved name is the one the user curated, and the one the bar shows.

    Git's `user.name` in a repository is whatever was typed into a config file
    once. A commit stamped with that email should not put a second spelling of
    the same person into a branch name.
    """
    _git(repo, "config", "user.email", "s@e.example")
    _git(repo, "config", "user.name", "stefan g")
    settings = Settings(repos=[RepoEntry(str(repo))], active_repo=str(repo))
    settings.save = lambda: None

    panel = BranchesTagsPanel(settings, _identity("Stefan Ghitescu", "s@e.example"))
    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.name_edit.setText("thing")

    assert "dev/rem/Stefan-Ghitescu/thing" in panel.branch_preview.text()


def test_an_email_nobody_saved_falls_back_to_what_git_says(qapp, repo):
    """Which is every repository until somebody saves the identity."""
    _git(repo, "config", "user.email", "someone@else.example")
    _git(repo, "config", "user.name", "Someone Else")
    settings = Settings(repos=[RepoEntry(str(repo))], active_repo=str(repo))
    settings.save = lambda: None

    panel = BranchesTagsPanel(settings, _identity("Stefan Ghitescu", "s@e.example"))
    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.name_edit.setText("thing")

    assert "dev/rem/Someone-Else/thing" in panel.branch_preview.text()


def test_the_email_is_matched_whatever_its_case(qapp, repo):
    _git(repo, "config", "user.email", "S@E.Example")
    _git(repo, "config", "user.name", "typed by hand")
    settings = Settings(repos=[RepoEntry(str(repo))], active_repo=str(repo))
    settings.save = lambda: None

    panel = BranchesTagsPanel(settings, _identity("Stefan Ghitescu", "s@e.example"))
    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.name_edit.setText("thing")

    assert "dev/rem/Stefan-Ghitescu/thing" in panel.branch_preview.text()


def test_an_identity_saved_without_a_name_does_not_blank_the_user(qapp, repo):
    """An identity is an email; the name is optional and often left out."""
    from git_assistant.identities import Identity, IdentityStore

    _git(repo, "config", "user.email", "s@e.example")
    _git(repo, "config", "user.name", "Stefan Ghitescu")
    settings = Settings(repos=[RepoEntry(str(repo))], active_repo=str(repo))
    settings.save = lambda: None
    store = IdentityStore([Identity(name="", email="s@e.example")])

    panel = BranchesTagsPanel(settings, store)
    card = _use_pattern(panel, "dev/rem/{user}/{name}")
    card.name_edit.setText("thing")

    assert "dev/rem/Stefan-Ghitescu/thing" in panel.branch_preview.text()
