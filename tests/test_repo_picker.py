"""The branch each repository is on, shown beside its name in the picker.

Against real repositories: the branch is read out of ``.git`` rather than asked
of ``git``, and a stub for the one thing that writes that file would only be
testing this file's idea of what git writes.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402

from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui.repo_picker import (  # noqa: E402
    ADD_FAVORITE,
    ALL_GROUP,
    BRANCH_ROLE,
    FAVORITES_GROUP,
    RECENT_GROUP,
    REMOVE_FAVORITE,
    OtherRepoPicker,
    RepoPicker,
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


def _repo(path, branch):
    """A repository with one commit, on ``branch``."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "f.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "initial")
    _git(path, "branch", "-M", branch)
    return path


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repos(tmp_path):
    return {
        "alpha": _repo(tmp_path / "alpha", "main"),
        "beta": _repo(tmp_path / "beta", "release/2026.09"),
    }


@pytest.fixture
def settings(repos):
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    s.repos = [RepoEntry(str(p)) for p in repos.values()]
    s.active_repo = str(repos["alpha"])
    return s


def _branches(picker):
    """The branch shown against each repository, keyed by its folder name.

    By path rather than by the row's own text: the text is `display()`, which
    names the folder the repository sits in -- here a pytest temporary
    directory, whose name is different on every run.
    """
    return {
        Path(path).name: item.data(0, BRANCH_ROLE)
        for item in picker._items()
        if (path := item.data(0, Qt.ItemDataRole.UserRole))
    }


def test_every_repository_is_labelled_with_its_branch(qapp, settings, repos):
    picker = RepoPicker(settings)

    assert _branches(picker) == {"alpha": "main", "beta": "release/2026.09"}


def test_the_branch_is_in_the_tooltip_too(qapp, settings, repos):
    """It is elided out of a narrow list before the name is; the tooltip keeps it."""
    picker = RepoPicker(settings)

    tips = [
        item.toolTip(0)
        for item in picker._items()
        if item.data(0, Qt.ItemDataRole.UserRole) == str(repos["beta"])
    ]

    assert tips and all(
        tip == f"{repos['beta']}\nOn branch release/2026.09" for tip in tips
    )


def test_a_repository_that_is_not_on_a_branch_is_labelled_with_nothing(
    qapp, settings, repos
):
    """A detached HEAD, and a repository that has been moved or deleted."""
    _git(repos["alpha"], "checkout", "-q", "--detach", "HEAD")
    settings.repos.append(RepoEntry(str(repos["alpha"].parent / "gone")))

    picker = RepoPicker(settings)

    assert _branches(picker) == {"alpha": "", "beta": "release/2026.09", "gone": ""}


def test_a_checkout_is_picked_up_without_rebuilding_the_list(qapp, settings, repos):
    """`refresh_branches` is what the tabs call after a checkout.

    It must not rebuild: `refresh` would take the scroll position and whatever
    the user had folded open with it, for a change to one label.
    """
    picker = RepoPicker(settings)
    rows = list(picker._items())

    _git(repos["alpha"], "checkout", "-q", "-b", "dev/rem/sg/thing")
    picker.refresh_branches()

    assert _branches(picker)["alpha"] == "dev/rem/sg/thing"
    # The same row objects, so nothing about the tree itself was disturbed.
    assert [id(row) for row in picker._items()] == [id(row) for row in rows]


def test_the_filter_matches_repository_names_and_not_branches(qapp, settings, repos):
    """The box says "Filter repositories", and a branch is an annotation.

    A branch that matched here would hide repositories for a reason that is not
    on screen -- and every repository on `main` would answer to "mai".
    """
    picker = RepoPicker(settings)

    picker.filter_edit.setText("release")

    shown = [
        Path(path).name
        for item in picker._items()
        if (path := item.data(0, Qt.ItemDataRole.UserRole)) and not item.isHidden()
    ]
    assert shown == ["alpha"]  # the selected one, which stays visible regardless


def test_the_row_reserves_room_for_the_branch(qapp, settings, repos):
    """The branch is drawn after the name, so it has to be measured with it."""
    picker = RepoPicker(settings)
    delegate = picker.repo_list.itemDelegate()
    model = picker.repo_list.model()
    group = next(
        i
        for i in range(picker.repo_list.topLevelItemCount())
        if picker.repo_list.topLevelItem(i).text(0) == ALL_GROUP
    )
    # All, then the folder the repositories are in, then the first of them.
    index = model.index(0, 0, model.index(0, 0, model.index(group, 0)))
    option = QStyleOptionViewItem()
    option.initFrom(picker.repo_list)

    with_branch = delegate.sizeHint(option, index)
    model.setData(index, "", BRANCH_ROLE)
    without = delegate.sizeHint(option, index)

    assert with_branch.width() > without.width()


# ---- selecting from code ------------------------------------------------------------
def test_selecting_a_repository_does_everything_a_click_does(qapp, settings, repos):
    """A repository cloned on Clone & Create is selected this way."""
    saved, changed = [], []
    settings.save = lambda: saved.append(True)
    picker = RepoPicker(settings)
    picker.repoChanged.connect(changed.append)

    assert picker.select(str(repos["beta"])) is True

    assert picker.current_path() == str(repos["beta"])
    assert settings.active_repo == str(repos["beta"])
    assert settings.recent_repos[0] == str(repos["beta"])
    assert (changed, saved) == ([str(repos["beta"])], [True])


def test_selecting_the_repository_already_selected_is_still_announced(
    qapp, settings, repos
):
    """The row does not change, so no click-like signal would fire on its own."""
    picker = RepoPicker(settings)
    changed = []
    picker.repoChanged.connect(changed.append)

    assert picker.select(str(repos["alpha"])) is True

    assert changed == [str(repos["alpha"])]


def test_a_repository_that_is_not_listed_cannot_be_selected(
    qapp, settings, repos, tmp_path
):
    picker = RepoPicker(settings)
    changed = []
    picker.repoChanged.connect(changed.append)

    assert picker.select(str(tmp_path / "elsewhere")) is False

    assert changed == []
    assert picker.current_path() == str(repos["alpha"])


# ---- favorites ---------------------------------------------------------------------
def _plain(*names, active="", recent=(), favorites=()):
    """Settings naming repositories that need not exist: these tests are about rows."""
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    s.repos = [RepoEntry(f"/x/{name}") for name in names]
    s.active_repo = f"/x/{active or names[0]}"
    s.recent_repos = [f"/x/{name}" for name in recent]
    s.favorite_repos = [f"/x/{name}" for name in favorites]
    return s


def _groups(picker):
    tree = picker.repo_list
    groups = (tree.topLevelItem(i) for i in range(tree.topLevelItemCount()))
    return [
        (group.text(0), [group.child(i).text(0) for i in range(group.childCount())])
        for group in groups
    ]


def _row(picker, group_title, path):
    """The row for ``path`` under a group, however deep: under All it is in a folder."""
    tree = picker.repo_list
    group = next(
        tree.topLevelItem(i)
        for i in range(tree.topLevelItemCount())
        if tree.topLevelItem(i).text(0) == group_title
    )
    return next(
        item for item in picker._under(group) if item.data(0, Qt.ItemDataRole.UserRole) == path
    )


def _group_of(item):
    """The group a row is listed under: the title at the top of the rows above it."""
    while item.parent() is not None:
        item = item.parent()
    return item.text(0)


def _outside_favorites(picker):
    """Every row that is not the Favorites group or in it, as the objects they are."""
    return [
        id(row)
        for row in picker._items()
        if FAVORITES_GROUP not in (row.text(0), row.parent() and row.parent().text(0))
    ]


def _choose_from_menu(picker, item, monkeypatch):
    """Open `item`'s menu as a right-click would, choose what it offers, and say what
    that was."""
    from PyQt6.QtWidgets import QMenu

    offered = []
    monkeypatch.setattr(QMenu, "exec", lambda menu, *a, **k: offered.extend(menu.actions()))
    picker._on_menu(picker.repo_list.visualItemRect(item).center())
    assert len(offered) == 1, [action.text() for action in offered]
    label = offered[0].text()
    offered[0].trigger()
    return label


def test_favorites_come_first_above_recently_used_by_name(qapp):
    picker = RepoPicker(
        _plain("alpha", "beta", "gamma", recent=["beta"], favorites=["gamma", "alpha"])
    )

    assert _groups(picker) == [
        (FAVORITES_GROUP, ["x\\alpha", "x\\gamma"]),
        (RECENT_GROUP, ["x\\beta"]),
        (ALL_GROUP, ["x"]),  # the folder all three are in
    ]


def test_there_is_no_favorites_group_until_there_is_a_favorite(qapp):
    """As with Recently Used: a group with nothing in it is noise."""
    picker = RepoPicker(_plain("alpha", "beta"))

    assert [title for title, _rows in _groups(picker)] == [ALL_GROUP]


def test_any_number_are_added_from_a_rows_menu_and_each_taken_off_again(
    qapp, monkeypatch
):
    names = ["alpha", "beta", "gamma", "delta", "epsilon"]
    settings = _plain(*names)
    picker = RepoPicker(settings)
    picker.resize(320, 480)
    picker.show()

    for name in names:
        row = _row(picker, ALL_GROUP, f"/x/{name}")
        assert _choose_from_menu(picker, row, monkeypatch) == ADD_FAVORITE

    assert _groups(picker)[0] == (
        FAVORITES_GROUP,
        [f"x\\{name}" for name in sorted(names)],
    )
    assert settings.favorite_repos == [f"/x/{name}" for name in names]

    for name in names:
        row = _row(picker, FAVORITES_GROUP, f"/x/{name}")
        assert _choose_from_menu(picker, row, monkeypatch) == REMOVE_FAVORITE

    assert [title for title, _rows in _groups(picker)] == [ALL_GROUP]
    assert settings.favorite_repos == []
    picker.close()


def test_the_menu_under_all_knows_a_favorite_too(qapp, monkeypatch):
    """A favorite is still listed under All, and can be taken off from there."""
    picker = RepoPicker(_plain("alpha", "beta", favorites=["beta"]))
    picker.resize(320, 480)
    picker.show()

    row = _row(picker, ALL_GROUP, "/x/beta")

    assert _choose_from_menu(picker, row, monkeypatch) == REMOVE_FAVORITE
    assert FAVORITES_GROUP not in [title for title, _rows in _groups(picker)]
    picker.close()


def test_a_right_click_opens_the_menu_and_switches_nothing(qapp, monkeypatch):
    """Selecting a row makes it the active repository, and every tab reloads for it.

    Sent through the window rather than to the list, so it arrives as a real one
    does: a mouse press and release, and the context-menu event after them.
    """
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QMenu

    picker = RepoPicker(_plain("alpha", "beta"))
    picker.resize(320, 480)
    picker.show()
    QTest.qWaitForWindowExposed(picker)
    offered, heard = [], []
    monkeypatch.setattr(
        QMenu,
        "exec",
        lambda menu, *a, **k: offered.extend(action.text() for action in menu.actions()),
    )
    picker.repoChanged.connect(heard.append)
    tree = picker.repo_list
    at = tree.visualItemRect(_row(picker, ALL_GROUP, "/x/beta")).center()

    QTest.mouseClick(
        picker.windowHandle(),
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
        tree.viewport().mapTo(picker, at),
    )
    qapp.processEvents()

    assert offered == [ADD_FAVORITE]
    assert picker.current_path() == "/x/alpha"
    assert heard == []
    picker.close()


def test_a_group_title_has_no_menu(qapp, monkeypatch):
    from PyQt6.QtWidgets import QMenu

    picker = RepoPicker(_plain("alpha"))
    picker.resize(320, 480)
    picker.show()
    opened = []
    monkeypatch.setattr(QMenu, "exec", lambda menu, *a, **k: opened.append(menu))
    tree = picker.repo_list

    picker._on_menu(tree.visualItemRect(tree.topLevelItem(0)).center())

    assert opened == []
    picker.close()


def test_every_group_title_says_what_a_right_click_does(qapp):
    """A list does not say it answers to one."""
    picker = RepoPicker(_plain("alpha", "beta", recent=["beta"], favorites=["alpha"]))
    tree = picker.repo_list

    for i in range(tree.topLevelItemCount()):
        assert "Right-click" in tree.topLevelItem(i).toolTip(0)


def test_a_favorite_is_saved_and_comes_back(qapp):
    saved = []
    settings = _plain("alpha", "beta")
    settings.save = lambda: saved.append(list(settings.favorite_repos))
    picker = RepoPicker(settings)

    picker.set_favorite("/x/beta", True)

    assert saved == [["/x/beta"]]
    assert Settings.from_dict(settings.to_dict()).favorite_repos == ["/x/beta"]


def test_changing_favorites_leaves_the_rest_of_the_list_as_it_was(qapp):
    """Its rows, and what was folded open -- as after a checkout."""
    picker = RepoPicker(_plain("alpha", "alpha/libs/inner", "beta", recent=["beta"]))
    alpha = _row(picker, ALL_GROUP, "/x/alpha")
    alpha.setExpanded(True)
    before = _outside_favorites(picker)

    picker.set_favorite("/x/beta", True)
    picker.set_favorite("/x/alpha", True)
    picker.set_favorite("/x/beta", False)

    assert _outside_favorites(picker) == before
    assert alpha.isExpanded()


def test_a_selected_favorite_taken_off_stays_selected_under_all(qapp):
    settings = _plain("alpha", "beta", favorites=["beta"])
    picker = RepoPicker(settings)
    picker.repo_list.setCurrentItem(_row(picker, FAVORITES_GROUP, "/x/beta"))
    heard = []
    picker.repoChanged.connect(heard.append)

    picker.set_favorite("/x/beta", False)

    assert picker.current_path() == "/x/beta" == settings.active_repo
    assert _group_of(picker.repo_list.currentItem()) == ALL_GROUP
    assert heard == []  # the same repository, so nothing to reload


def test_a_favorite_since_removed_is_not_offered(qapp):
    picker = RepoPicker(_plain("alpha", favorites=["gone", "alpha"]))

    assert _groups(picker)[0] == (FAVORITES_GROUP, ["x\\alpha"])


def test_the_filter_reaches_favorites_too(qapp):
    picker = RepoPicker(
        _plain("alpha", "beta", "gamma", active="gamma", favorites=["alpha", "beta"])
    )

    picker.filter_edit.setText("bet")

    assert _row(picker, FAVORITES_GROUP, "/x/alpha").isHidden()
    assert not _row(picker, FAVORITES_GROUP, "/x/beta").isHidden()


def test_a_favorite_added_while_filtering_answers_to_the_filter(qapp):
    picker = RepoPicker(
        _plain("alpha", "beta", "gamma", active="gamma", favorites=["beta"])
    )
    picker.filter_edit.setText("bet")

    picker.set_favorite("/x/alpha", True)

    assert _row(picker, FAVORITES_GROUP, "/x/alpha").isHidden()
    assert not _row(picker, FAVORITES_GROUP, "/x/beta").isHidden()


def test_changing_favorites_forgets_any_no_longer_managed():
    """As the recently used are: the file should not go on naming folders nobody
    can pick."""
    settings = _plain("alpha", favorites=["gone"])

    settings.set_favorite("/x/alpha", True)

    assert settings.favorite_repos == ["/x/alpha"]


def test_a_hand_edited_favorites_list_loads_as_paths_once_each():
    loaded = Settings.from_dict({"favorite_repos": ["/x/a", 3, "", "/x/a", None, "/x/b"]})

    assert loaded.favorite_repos == ["/x/a", "/x/b"]
    assert Settings.from_dict({"favorite_repos": "/x/a"}).favorite_repos == []


def test_a_favorite_added_on_one_tab_is_there_on_the_next(qapp):
    from git_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(_plain("alpha", "beta"))
    dlg.commit_panel.repo_picker.set_favorite("/x/beta", True)

    dlg.tabs.setCurrentWidget(dlg.agents_panel)

    assert _groups(dlg.agents_panel.repo_picker)[0] == (FAVORITES_GROUP, ["x\\beta"])


# ---- a second repository, set beside the active one ------------------------------------
def test_choosing_the_repository_to_set_beside_the_active_one_changes_nothing_else(qapp):
    """The Compare tab's other side: comparing with a project does not make it yours."""
    settings = _plain("alpha", "beta", "gamma", recent=["gamma"])
    picker = OtherRepoPicker(settings)
    heard = []
    picker.repoChanged.connect(heard.append)

    assert picker.select("/x/beta")

    assert settings.compare_repo == "/x/beta"
    assert settings.active_repo == "/x/alpha"
    assert settings.recent_repos == ["/x/gamma"]
    assert heard == ["/x/beta"]


def test_nothing_is_set_beside_the_active_repository_until_something_is_chosen(qapp):
    """Not the first repository, as the active list falls back to: it may be the active one."""
    settings = _plain("alpha", "beta")
    assert OtherRepoPicker(settings).current_path() == ""

    settings.compare_repo = "/x/beta"
    assert OtherRepoPicker(settings).current_path() == "/x/beta"

    settings.compare_repo = "/x/gone"
    assert OtherRepoPicker(settings).current_path() == ""


def test_the_repository_set_beside_stays_selected_when_favorites_change(qapp):
    settings = _plain("alpha", "beta", favorites=["beta"])
    settings.compare_repo = "/x/beta"
    picker = OtherRepoPicker(settings)
    picker.repo_list.setCurrentItem(_row(picker, FAVORITES_GROUP, "/x/beta"))

    picker.set_favorite("/x/beta", False)

    assert picker.current_path() == "/x/beta" == settings.compare_repo
    assert _group_of(picker.repo_list.currentItem()) == ALL_GROUP
    assert settings.active_repo == "/x/alpha"


# ---- folders, and the submodules of a repository ---------------------------------------
def _listing(*paths, active=""):
    """Settings listing ``paths``, which need not exist: these tests are about rows."""
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    s.repos = [RepoEntry(path) for path in paths]
    s.active_repo = active or paths[0]
    return s


def _all(picker):
    tree = picker.repo_list
    return next(
        tree.topLevelItem(i)
        for i in range(tree.topLevelItemCount())
        if tree.topLevelItem(i).text(0) == ALL_GROUP
    )


def _outline(item, depth=0):
    """``(text, depth, open)`` for every row below ``item``, top to bottom."""
    rows = []
    for i in range(item.childCount()):
        child = item.child(i)
        rows.append((child.text(0), depth, child.isExpanded()))
        rows += _outline(child, depth + 1)
    return rows


def test_repositories_are_inside_the_folder_they_sit_in_and_folders_start_folded(qapp):
    import os

    picker = RepoPicker(
        _listing(
            "/work/ONEoo7/beta",
            "/tools/toolbox/msys2",
            "/tools/toolbox/python3",
            "/work/ONEoo7/alpha",
            active="/work/ONEoo7/alpha",
        )
    )

    assert _outline(_all(picker)) == [
        ("ONEoo7", 0, True),  # open only because the selection is in it
        ("alpha", 1, False),
        ("beta", 1, False),
        ("toolbox", 0, False),
        ("msys2", 1, False),
        ("python3", 1, False),
    ]
    toolbox = _all(picker).child(1)
    assert not toolbox.icon(0).isNull()
    assert toolbox.toolTip(0) == os.path.normpath("/tools/toolbox")
    assert not (toolbox.flags() & Qt.ItemFlag.ItemIsSelectable)
    assert picker.count() == 4  # repositories, and not the folders they are in


def test_two_folders_of_one_name_say_where_each_is(qapp):
    picker = RepoPicker(_listing("/a/lib/one", "/b/lib/two"))

    first, second = _all(picker).child(0).text(0), _all(picker).child(1).text(0)

    assert first != second
    assert first.startswith("lib (") and second.startswith("lib (")


def test_submodules_are_under_their_repository_down_the_directories_they_are_kept_in(qapp):
    picker = RepoPicker(
        _listing(
            "/x/top",
            "/x/top/libs/can",
            "/x/top/libs/ccp",
            "/x/top/ADC",
            "/x/top/libs/can/deep/vendor",
            "/x/other",
            active="/x/other",
        )
    )

    assert _outline(_all(picker)) == [
        ("x", 0, True),
        ("other", 1, False),
        ("top", 1, False),  # a repository of its own, with submodules: folded
        ("Submodules", 2, True),  # everything inside a repository starts open
        ("libs", 3, True),  # directories before the submodules beside them
        ("can", 4, True),
        ("Submodules", 5, True),
        ("deep", 6, True),
        ("vendor", 7, False),
        ("ccp", 4, False),
        ("ADC", 3, False),
    ]
    assert picker.count() == 6


def test_a_submodule_can_be_chosen_from_inside_its_directories(qapp):
    settings = _listing("/x/top", "/x/top/libs/can", active="/x/top/libs/can")
    picker = RepoPicker(settings)

    assert picker.current_path() == "/x/top/libs/can"
    row = picker.repo_list.currentItem()
    parent = row.parent()
    while parent is not None:  # every row above it open, so it can be seen
        assert parent.isExpanded()
        parent = parent.parent()


def test_clicking_a_folder_opens_or_folds_it_and_chooses_nothing(qapp):
    """It used to leave the list with no repository selected, while every tab went on
    with the one it had."""
    from PyQt6.QtTest import QTest

    settings = _listing("/work/ONEoo7/alpha", "/tools/toolbox/msys2", active="/work/ONEoo7/alpha")
    picker = RepoPicker(settings)
    picker.resize(320, 400)
    picker.show()
    QTest.qWaitForWindowExposed(picker)
    heard = []
    picker.repoChanged.connect(heard.append)
    tree = picker.repo_list
    toolbox = _all(picker).child(1)

    def at(item):
        # Through the window, as real input arrives: a double click is then a press,
        # a release and a double click, where sent to the list it is the last alone.
        return tree.viewport().mapTo(picker, tree.visualItemRect(item).center())

    left, none = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    QTest.mouseClick(picker.windowHandle(), left, none, at(toolbox))
    qapp.processEvents()
    assert toolbox.isExpanded()
    assert picker.current_path() == "/work/ONEoo7/alpha" == settings.active_repo
    assert tree.currentItem().data(0, Qt.ItemDataRole.UserRole) == "/work/ONEoo7/alpha"

    QTest.mouseClick(picker.windowHandle(), left, none, at(toolbox))
    qapp.processEvents()
    assert not toolbox.isExpanded()
    # A double click is one press that opens it: not open and shut again.
    QTest.qWait(QApplication.doubleClickInterval() + 50)
    QTest.mouseDClick(picker.windowHandle(), left, none, at(toolbox))
    qapp.processEvents()
    assert toolbox.isExpanded()
    assert heard == []
    picker.close()


def test_the_keyboard_on_a_folder_leaves_the_repository_chosen(qapp):
    picker = RepoPicker(_listing("/work/ONEoo7/alpha", "/tools/toolbox/msys2"))
    heard = []
    picker.repoChanged.connect(heard.append)

    picker.repo_list.setCurrentItem(_all(picker).child(1))

    assert picker.current_path() == "/work/ONEoo7/alpha"
    assert heard == []


def test_a_folder_opened_or_folded_by_hand_stays_so_when_the_list_is_built_again(qapp):
    """Every tab builds its list again whenever it is shown."""
    picker = RepoPicker(_listing("/work/ONEoo7/alpha", "/tools/toolbox/msys2"))
    _all(picker).child(1).setExpanded(True)  # toolbox, opened
    _all(picker).child(0).setExpanded(False)  # ONEoo7, folded though the selection is in it

    picker.refresh()

    assert _all(picker).child(1).isExpanded()
    assert not _all(picker).child(0).isExpanded()
    assert picker.current_path() == "/work/ONEoo7/alpha"


def test_the_first_repository_is_chosen_when_the_active_one_is_gone_not_a_folder(qapp):
    picker = RepoPicker(_listing("/tools/toolbox/msys2", "/work/ONEoo7/alpha", active="/gone/x"))

    assert picker.current_path() == "/work/ONEoo7/alpha"  # ONEoo7 comes before toolbox


def test_a_folder_s_name_finds_everything_in_it(qapp):
    picker = RepoPicker(
        _listing("/work/ONEoo7/alpha", "/tools/toolbox/msys2", "/tools/toolbox/python3")
    )

    picker.filter_edit.setText("toolbox")

    ones, toolbox = _all(picker).child(0), _all(picker).child(1)
    assert not toolbox.isHidden() and toolbox.isExpanded()
    assert [toolbox.child(i).isHidden() for i in range(2)] == [False, False]
    assert not ones.isHidden()  # holds the selection, which is always kept


def test_the_submodules_heading_is_not_something_the_filter_matches(qapp):
    picker = RepoPicker(_listing("/x/other", "/x/top", "/x/top/libs/can"))

    picker.filter_edit.setText("submod")

    top = _all(picker).child(0).child(1)
    assert top.text(0) == "top" and top.isHidden()


def test_clearing_the_filter_leaves_rows_as_they_were_before_it(qapp):
    picker = RepoPicker(_listing("/work/ONEoo7/alpha", "/tools/toolbox/msys2"))
    _all(picker).child(1).setExpanded(True)  # opened by hand

    picker.filter_edit.setText("alp")
    assert _all(picker).child(1).isHidden()
    picker.filter_edit.setText("")

    assert _all(picker).child(1).isExpanded() and not _all(picker).child(1).isHidden()
    assert _all(picker).child(0).isExpanded()
