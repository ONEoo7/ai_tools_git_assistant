"""The Repositories tab: submodules nest under the repository that contains them."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant import git_ops  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui import settings_dialog as dialog_module  # noqa: E402
from git_assistant.ui.settings_dialog import SettingsDialog  # noqa: E402

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


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

    assert _rows(root) == [
        ("x\\alpha", 0),
        ("libs\\inner", 1),
        ("x\\beta", 0),
    ]


def test_the_folder_is_open_and_its_repositories_are_folded(qapp, settings):
    """One repository with forty submodules must not bury the next one."""
    dlg = SettingsDialog(settings)
    root = dlg.repo_tree.topLevelItem(0)

    assert root.isExpanded()
    assert not root.child(0).isExpanded()  # alpha, holding inner


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

    assert _rows(root) == [("x\\beta", 0)]
    repos, _roots, _watched = dlg._collect_repos_and_roots()
    assert [r.path for r in repos] == ["/x/beta"]


def test_a_submodule_can_be_removed_on_its_own(qapp, settings):
    dlg = SettingsDialog(settings)
    root = dlg.repo_tree.topLevelItem(0)
    root.child(0).child(0).setSelected(True)  # inner

    dlg._on_remove_repo()

    assert _rows(root) == [("x\\alpha", 0), ("x\\beta", 0)]


def test_a_submodule_row_rescans_its_folder_group(qapp, settings):
    """Rescan works off the folder header above the row, however deep it sits."""
    dlg = SettingsDialog(settings)
    inner = dlg.repo_tree.topLevelItem(0).child(0).child(0)
    inner.setSelected(True)

    assert dlg._selected_root_folder() == "/x"


# ---- the order submodules appear in ---------------------------------------------------
def _tree(*paths):
    from git_assistant.config import build_repo_tree

    return build_repo_tree([RepoEntry(p) for p in paths])


def _kids(node):
    return [child.entry.display() for child in node.children]


def test_submodules_are_listed_by_name(qapp):
    """`.gitmodules` lists them in whatever order they were added, which is
    arbitrary; forty of those is a list nobody can scan."""
    tree = _tree(
        "/x/super",
        "/x/super/libs/zeta",
        "/x/super/libs/alpha",
        "/x/super/libs/middleware",
        "/x/super/libs/beta",
    )

    assert _kids(tree[0]) == [
        "libs\\alpha",
        "libs\\beta",
        "libs\\middleware",
        "libs\\zeta",
    ]


def test_an_already_added_repository_is_sorted_without_rescanning(qapp):
    """The order in settings is whatever the scan produced at the time."""
    tree = _tree("/x/super", "/x/super/b", "/x/super/a")
    assert _kids(tree[0]) == ["super\\a", "super\\b"]


def test_case_does_not_split_the_list_in_two(qapp):
    """Sorting by raw bytes puts every capital ahead of every lowercase, which
    reads as two lists rather than one."""
    tree = _tree("/x/super", "/x/super/zulu", "/x/super/Alpha", "/x/super/beta")
    assert _kids(tree[0]) == [
        "super\\Alpha",
        "super\\beta",
        "super\\zulu",
    ]


def test_a_submodule_of_a_submodule_still_nests_under_it(qapp):
    tree = _tree(
        "/x/super",
        "/x/super/libs/core",
        "/x/super/libs/core/inner",
        "/x/super/libs/api",
    )

    assert _kids(tree[0]) == ["libs\\api", "libs\\core"]
    core = [
        c for c in tree[0].children if c.entry.display() == "libs\\core"
    ][0]
    assert _kids(core) == ["core\\inner"]


def test_the_top_level_keeps_the_order_it_was_given(qapp):
    """Which repository you are working in is what the top level answers, and
    `ordered_repos` puts the active one first. Sorting that would bury it."""
    tree = _tree("/x/zulu", "/x/alpha")
    assert [node.entry.display() for node in tree] == ["x\\zulu", "x\\alpha"]


def test_a_label_is_what_it_sorts_by(qapp):
    """The tree shows `display()`; sorting by path would look unsorted."""
    named = RepoEntry("/x/super/zzz")
    named.label = "aaa"
    from git_assistant.config import build_repo_tree

    tree = build_repo_tree([RepoEntry("/x/super"), named, RepoEntry("/x/super/bbb")])

    assert _kids(tree[0]) == ["aaa", "super\\bbb"]


# ---- finding repositories on disk ----------------------------------------------------
def _real_repo(path):
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", str(path), "init", "-q"],
        capture_output=True,
        creationflags=_NO_WINDOW,
        check=True,
    )
    return path


@pytest.fixture
def empty_settings():
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    return s


@pytest.fixture
def slot_errors(monkeypatch):
    """Exceptions raised inside Qt slots, kept so the test can fail on them.

    With Python's own ``sys.excepthook`` in place, PyQt ends the process when a
    slot raises: no traceback, no test name, and nothing after it runs. Given a
    hook of anyone else's it calls that instead.
    """
    caught = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: caught.append(exc))
    return caught


def _raise_first(caught):
    if caught:
        _kind, error, trace = caught[0]
        raise error.with_traceback(trace)


def _scan(qapp, dlg, folder, slot_errors, timeout=30.0):
    """Run the real scan, worker thread and all, and wait for it to report.

    The button is disabled for the length of a scan and enabled again by
    whichever of success or failure ends it.
    """
    dlg._start_scan(str(folder))
    deadline = time.monotonic() + timeout
    while not dlg.scan_btn.isEnabled() and not slot_errors:
        assert time.monotonic() < deadline, "the scan never reported back"
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()  # the rest of a slot that re-enabled the button first
    _raise_first(slot_errors)


def _listed(dlg):
    return sorted(Path(path).name for path in dlg._repo_items_by_path())


def test_a_scan_lists_every_repository_it_finds(
    qapp, empty_settings, tmp_path, slot_errors
):
    _real_repo(tmp_path / "alpha")
    _real_repo(tmp_path / "beta")
    dlg = SettingsDialog(empty_settings)

    _scan(qapp, dlg, tmp_path, slot_errors)

    assert _listed(dlg) == ["alpha", "beta"]
    assert dlg.scan_status.text() == f"Found 2 repo(s) in {tmp_path}; added 2 new."


def test_scanning_again_adds_nothing_twice(
    qapp, empty_settings, tmp_path, slot_errors
):
    _real_repo(tmp_path / "alpha")
    dlg = SettingsDialog(empty_settings)
    _scan(qapp, dlg, tmp_path, slot_errors)

    _scan(qapp, dlg, tmp_path, slot_errors)

    assert _listed(dlg) == ["alpha"]
    assert dlg.scan_status.text() == f"Found 1 repo(s) in {tmp_path}; added 0 new."


def test_a_blocked_repository_is_listed_all_the_same(
    qapp, empty_settings, tmp_path, monkeypatch, slot_errors
):
    """Blocked is reported, not filtered out.

    Which is why trusting every repository leaves nothing to rescan for: the
    blocked ones were in the list already, and simply work from then on.
    """
    _real_repo(tmp_path / "alpha")
    _real_repo(tmp_path / "beta")
    monkeypatch.setattr(
        git_ops, "blocked_by_ownership", lambda path: Path(path).name == "beta"
    )
    dlg = SettingsDialog(empty_settings)

    _scan(qapp, dlg, tmp_path, slot_errors)

    assert _listed(dlg) == ["alpha", "beta"]
    assert "1 blocked by git ownership check" in dlg.scan_status.text()


def test_a_repository_added_by_hand_is_listed(
    qapp, empty_settings, tmp_path, monkeypatch
):
    repo = _real_repo(tmp_path / "alpha")
    monkeypatch.setattr(
        dialog_module.QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *a, **k: str(repo)),
    )
    dlg = SettingsDialog(empty_settings)

    dlg._on_add_repo()

    assert _listed(dlg) == ["alpha"]
