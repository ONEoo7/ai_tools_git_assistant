"""Comparing any two sets of settings, taking from either, and saving the result."""

import json

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant import repo_config, settings_diff  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui.settings_merge_dialog import (  # noqa: E402
    BUILT_IN,
    SettingsMergeDialog,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        repo_config, "user_config_dir", lambda *a, **k: str(tmp_path / "config")
    )
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "demo"
    path.mkdir()
    return path


@pytest.fixture
def settings(repo):
    s = Settings()
    s.save = lambda: None
    s.repos = [RepoEntry(str(repo))]
    s.active_repo = str(repo)
    return s


def _write(tier, repo, data):
    repo_config.write_text(tier, repo, json.dumps(data))


def _pick(combo, key):
    combo.setCurrentIndex(combo.findData(key))


def _rows(dialog) -> dict:
    return {
        dialog.tree.topLevelItem(i).text(0): (
            dialog.tree.topLevelItem(i).text(1),
            dialog.tree.topLevelItem(i).text(2),
            dialog.tree.topLevelItem(i).text(3),
        )
        for i in range(dialog.tree.topLevelItemCount())
    }


# ---- what can be compared -------------------------------------------------------
def test_any_of_the_three_can_be_compared_with_any_other(qapp, settings, repo):
    dialog = SettingsMergeDialog(settings, str(repo))

    offered = [
        dialog.left_combo.itemData(i) for i in range(dialog.left_combo.count())
    ]
    assert offered == ["user", "repo", "custom", BUILT_IN]


def test_a_source_that_is_not_there_says_so_in_its_name(qapp, settings, repo):
    """Comparing against a file that is not there is legitimate, and surprising
    to do by accident."""
    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 2}})
    dialog = SettingsMergeDialog(settings, str(repo))

    names = {
        dialog.left_combo.itemData(i): dialog.left_combo.itemText(i)
        for i in range(dialog.left_combo.count())
    }

    assert "(none)" not in names["repo"]
    assert "(none)" in names["custom"]


def test_it_opens_on_what_is_in_force_against_what_the_repository_ships(
    qapp, settings, repo
):
    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 2}})

    dialog = SettingsMergeDialog(settings, str(repo))

    assert dialog.left_combo.currentData() == "repo"  # in force
    assert dialog.right_combo.currentData() == "user"


# ---- the rows -------------------------------------------------------------------
def test_every_setting_is_a_row_with_both_sides_and_the_result(qapp, settings, repo):
    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 2, "prune": True}})
    _write(repo_config.Tier.CUSTOM, repo, {"fetch": {"depth": 9, "prune": True}})
    dialog = SettingsMergeDialog(settings, str(repo))
    _pick(dialog.left_combo, "repo")
    _pick(dialog.right_combo, "custom")

    rows = _rows(dialog)

    assert rows["fetch.depth"] == ("2", "9", "2")  # result starts on the left
    assert rows["fetch.prune"] == ("true", "true", "true")


def test_a_row_that_is_the_same_on_both_sides_cannot_be_chosen(qapp, settings, repo):
    """Nothing to choose, so nothing that reads as a choice."""
    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 2, "prune": True}})
    _write(repo_config.Tier.CUSTOM, repo, {"fetch": {"depth": 9, "prune": True}})
    dialog = SettingsMergeDialog(settings, str(repo))
    _pick(dialog.left_combo, "repo")
    _pick(dialog.right_combo, "custom")

    same = [
        dialog.tree.topLevelItem(i)
        for i in range(dialog.tree.topLevelItemCount())
        if dialog.tree.topLevelItem(i).text(0) == "fetch.prune"
    ][0]

    assert same.isDisabled()


def test_taking_a_side_changes_the_result_column(qapp, settings, repo):
    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 2}})
    _write(repo_config.Tier.CUSTOM, repo, {"fetch": {"depth": 9}})
    dialog = SettingsMergeDialog(settings, str(repo))
    _pick(dialog.left_combo, "repo")
    _pick(dialog.right_combo, "custom")

    dialog._take(settings_diff.RIGHT, everything=True)

    assert _rows(dialog)["fetch.depth"][2] == "9"
    assert dialog.result()["fetch"]["depth"] == 9


def test_taking_a_side_applies_to_the_selection_and_not_the_rest(
    qapp, settings, repo
):
    """"That side, except for these two" is what a merge usually is."""
    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 2, "tags": True}})
    _write(repo_config.Tier.CUSTOM, repo, {"fetch": {"depth": 9, "tags": False}})
    dialog = SettingsMergeDialog(settings, str(repo))
    _pick(dialog.left_combo, "repo")
    _pick(dialog.right_combo, "custom")
    for i in range(dialog.tree.topLevelItemCount()):
        item = dialog.tree.topLevelItem(i)
        if item.text(0) == "fetch.tags":
            dialog.tree.setCurrentItem(item)

    dialog._take(settings_diff.RIGHT)

    merged = dialog.result()
    assert merged["fetch"] == {"depth": 2, "tags": False}


def test_the_summary_counts_what_has_been_taken_from_the_right(qapp, settings, repo):
    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 2, "tags": True}})
    _write(repo_config.Tier.CUSTOM, repo, {"fetch": {"depth": 9, "tags": False}})
    dialog = SettingsMergeDialog(settings, str(repo))
    _pick(dialog.left_combo, "repo")
    _pick(dialog.right_combo, "custom")
    assert "0 taken from the right" in dialog.summary.text()

    dialog._take(settings_diff.RIGHT, everything=True)

    assert "2 taken from the right" in dialog.summary.text()


def test_two_sources_that_agree_say_so(qapp, settings, repo):
    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 2}})
    _write(repo_config.Tier.CUSTOM, repo, {"fetch": {"depth": 2}})
    dialog = SettingsMergeDialog(settings, str(repo))
    _pick(dialog.left_combo, "repo")
    _pick(dialog.right_combo, "custom")

    assert "No differences" in dialog.summary.text()


def test_the_built_in_defaults_can_be_one_of_the_sides(qapp, settings, repo):
    """"What did this look like before anybody configured anything" is the
    comparison people actually want."""
    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 40}})
    dialog = SettingsMergeDialog(settings, str(repo))
    _pick(dialog.left_combo, "repo")
    _pick(dialog.right_combo, BUILT_IN)

    rows = _rows(dialog)

    assert rows["fetch.depth"] == ("40", str(repo_config.FetchRules().depth), "40")


# ---- saving it ------------------------------------------------------------------
def test_saving_writes_the_merge_where_it_was_told_to(qapp, settings, repo):
    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 2}})
    _write(repo_config.Tier.USER, "", {"fetch": {"depth": 9}})
    dialog = SettingsMergeDialog(settings, str(repo))
    _pick(dialog.left_combo, "repo")
    _pick(dialog.right_combo, "user")
    dialog._take(settings_diff.RIGHT, everything=True)
    _pick(dialog.target_combo, "custom")

    dialog._on_save()

    assert dialog.saved_to is repo_config.Tier.CUSTOM
    assert repo_config.resolve(repo, "custom").fetch.depth == 9


def test_what_is_written_is_a_settings_file_this_build_would_have_written(
    qapp, settings, repo
):
    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 2}})
    dialog = SettingsMergeDialog(settings, str(repo))
    _pick(dialog.target_combo, "custom")

    dialog._on_save()

    written = json.loads(repo_config.read_text(repo_config.Tier.CUSTOM, repo))
    assert written["version"] == repo_config.SCHEMA_VERSION
    assert repo_config.resolve(repo, "custom").problem == ""


def test_saving_over_something_that_exists_asks_first(
    qapp, settings, repo, monkeypatch
):
    from git_assistant.ui import settings_merge_dialog as merge_mod

    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 2}})
    _write(repo_config.Tier.CUSTOM, repo, {"fetch": {"depth": 5}})
    dialog = SettingsMergeDialog(settings, str(repo))
    _pick(dialog.left_combo, "repo")
    _pick(dialog.target_combo, "custom")
    monkeypatch.setattr(
        "git_assistant.ui.settings_diff_dialog.SettingsDiffDialog",
        lambda *a, **k: type("_No", (), {"wanted": lambda self: False})(),
    )

    dialog._on_save()

    assert dialog.saved_to is None
    assert repo_config.resolve(repo, "custom").fetch.depth == 5


def test_discarding_writes_nothing(qapp, settings, repo):
    _write(repo_config.Tier.REPO, repo, {"fetch": {"depth": 2}})
    dialog = SettingsMergeDialog(settings, str(repo))

    dialog.reject()

    assert dialog.saved_to is None
    assert not repo_config.exists(repo_config.Tier.CUSTOM, repo)
