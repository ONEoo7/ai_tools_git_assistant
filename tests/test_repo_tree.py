"""The Repositories tab: submodules nest under the repository that contains them."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    s.repos = [
        RepoEntry("/x/alpha"),
        RepoEntry("/x/alpha/libs/inner"),
        RepoEntry("/x/beta"),
    ]
    s.scan_roots = ["/x"]
    s.active_repo = "/x/alpha"
    return s


def _rows(item, depth=0):
    """(label, depth) for every row below ``item``."""
    out = []
    for i in range(item.childCount()):
        child = item.child(i)
        out.append((child.text(0), depth))
        out.extend(_rows(child, depth + 1))
    return out


def test_folder_group_nests_submodules(qapp, settings):
    dlg = SettingsDialog(settings)
    root = dlg.repo_tree.topLevelItem(0)

    assert _rows(root) == [("alpha", 0), ("inner", 1), ("beta", 0)]


def test_folder_count_includes_nested_repos(qapp, settings):
    dlg = SettingsDialog(settings)
    assert dlg.repo_tree.topLevelItem(0).text(0) == "/x   (3)"


def test_nested_repos_survive_a_round_trip_through_the_tree(qapp, settings):
    """The tree is the source of truth on save; nesting must not lose a repo."""
    dlg = SettingsDialog(settings)
    repos, roots, _watched = dlg._collect_repos_and_roots()

    assert [r.path for r in repos] == [
        "/x/alpha",
        "/x/alpha/libs/inner",
        "/x/beta",
    ]
    assert roots == ["/x"]


def test_removing_a_repo_removes_its_submodules(qapp, settings):
    """They live inside it on disk - keeping them would list a missing folder."""
    dlg = SettingsDialog(settings)
    root = dlg.repo_tree.topLevelItem(0)
    root.child(0).setSelected(True)  # alpha, which contains inner

    dlg._on_remove_repo()

    assert _rows(root) == [("beta", 0)]
    repos, _roots, _watched = dlg._collect_repos_and_roots()
    assert [r.path for r in repos] == ["/x/beta"]


def test_a_submodule_can_be_removed_on_its_own(qapp, settings):
    dlg = SettingsDialog(settings)
    root = dlg.repo_tree.topLevelItem(0)
    root.child(0).child(0).setSelected(True)  # inner

    dlg._on_remove_repo()

    assert _rows(root) == [("alpha", 0), ("beta", 0)]


def test_a_submodule_row_rescans_its_folder_group(qapp, settings):
    """Rescan works off the folder header above the row, however deep it sits."""
    dlg = SettingsDialog(settings)
    inner = dlg.repo_tree.topLevelItem(0).child(0).child(0)
    inner.setSelected(True)

    assert dlg._selected_root_folder() == "/x"
