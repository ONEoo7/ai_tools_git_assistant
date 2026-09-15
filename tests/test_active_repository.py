"""The active repository, named in the bar above the tabs.

The repository list folds away now, so this is where the selected repository and
its branch are read -- and it has to stay right through a checkout, which changes
the branch without changing which repository is selected.
"""

import subprocess
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from git_assistant import git_ops  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.identities import IdentityStore  # noqa: E402
from git_assistant.ui import theme  # noqa: E402
from git_assistant.ui.identity_bar import IdentityBar  # noqa: E402

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
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "f.txt").write_bytes(b"f\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    _git(path, "branch", "-M", branch)
    return path


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def app(qapp):
    yield qapp
    theme.apply(qapp, theme.SYSTEM)  # never leave it on the next test


@pytest.fixture
def repos(tmp_path):
    return {
        "alpha": _repo(tmp_path / "ONEoo7" / "alpha", "main"),
        "beta": _repo(tmp_path / "forks" / "beta", "feature/login"),
    }


@pytest.fixture
def settings(repos):
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    s.repos = [RepoEntry(str(path)) for path in repos.values()]
    s.active_repo = str(repos["alpha"])
    return s


@pytest.fixture
def bar(qapp, settings):
    return IdentityBar(settings, IdentityStore([]))


# ---- what it says ----------------------------------------------------------------
def test_it_names_the_active_repository_and_its_branch(bar, repos):
    assert bar.repo_name.text() == "ONEoo7\\alpha"
    assert bar.repo_name.toolTip() == str(repos["alpha"])
    assert bar.repo_branch.text() == "main"


def test_it_follows_the_selection(bar, repos):
    bar.set_repo(str(repos["beta"]))

    assert bar.repo_name.text() == "forks\\beta"
    assert bar.repo_branch.text() == "feature/login"


def test_a_repository_with_a_label_goes_by_it(qapp, settings, repos):
    settings.repos[0].label = "Work"

    bar = IdentityBar(settings, IdentityStore([]))

    assert bar.repo_name.text() == "Work"


def test_with_no_repository_it_says_so(qapp):
    empty = Settings()
    empty.save = lambda: None

    bar = IdentityBar(empty, IdentityStore([]))

    assert bar.repo_name.text() == "(none)"
    assert bar.repo_branch.text() == ""
    assert bar.auth_status.text() == "(none)"  # and nowhere to push to


def test_a_detached_head_names_no_branch(qapp, settings, repos):
    _git(repos["alpha"], "checkout", "-q", "--detach", "HEAD")

    bar = IdentityBar(settings, IdentityStore([]))

    assert bar.repo_branch.text() == ""


def test_a_checkout_is_shown_without_looking_the_identity_up_again(
    bar, repos, monkeypatch
):
    """`show_active_repository` is what runs on every checkout, on any tab."""
    asked = []
    monkeypatch.setattr(git_ops, "get_identity", lambda *a: asked.append(a) or ("", ""))
    _git(repos["alpha"], "checkout", "-q", "-b", "fix/typo")

    bar.show_active_repository()

    assert bar.repo_branch.text() == "fix/typo"
    assert asked == []


def test_a_new_bar_names_the_inference_straight_away(qapp, settings):
    settings.provider = "lmstudio"
    settings.set_provider_model("lmstudio", "qwen3.5-4b")

    bar = IdentityBar(settings, IdentityStore([]))

    assert (bar.inference_name.text(), bar.inference_model.text()) == (
        "LM Studio",
        "qwen3.5-4b",
    )


def test_it_names_the_active_inference_right_of_the_repository(bar, settings):
    settings.provider = "openai"
    settings.set_provider_model("openai", "gpt-4o-mini")

    bar.show_active_inference()

    assert bar.inference_name.text() == "OpenAI"
    assert bar.inference_model.text() == "gpt-4o-mini"
    box = bar.layout()
    assert box.indexOf(bar.repo_branch) < box.indexOf(bar.inference_name)
    assert box.indexOf(bar.inference_name) < box.indexOf(bar.inference_model)


def test_with_no_model_chosen_it_says_so_as_a_warning(bar, settings):
    settings.provider = "openai"
    settings.provider_models.pop("openai", None)

    bar.show_active_inference()

    assert bar.inference_model.text() == "no model selected"
    assert "#b36b00" in bar.inference_model.styleSheet()


def test_an_experimental_provider_is_named_short(bar, settings):
    """"(experimental)" is for choosing one; the bar keeps it in the tooltip."""
    settings.provider = "claude-cli"

    bar.show_active_inference()

    assert bar.inference_name.text() == "Claude Code CLI"
    assert bar.inference_name.toolTip() == "Claude Code CLI (experimental)"


# ---- how the bar is divided ---------------------------------------------------------
CAPTIONS = (
    "Commit as:",
    "Active Settings:",
    "Active Repository:",
    "Active Inference:",
    "Remote:",
)


def _drawn(bar):
    """The bar's widgets left to right, leaving out any that draw nothing."""
    from PyQt6.QtWidgets import QLabel

    box = bar.layout()
    widgets = [box.itemAt(i).widget() for i in range(box.count())]
    return [
        w
        for w in widgets
        if w is not None
        and not w.isHidden()
        and not (isinstance(w, QLabel) and not w.text())
    ]


def test_a_line_divides_each_group_on_the_bar_from_the_next(bar):
    from PyQt6.QtWidgets import QLabel

    from git_assistant.ui.identity_bar import _Divider

    row = [
        "|" if isinstance(w, _Divider) else w.text()
        for w in _drawn(bar)
        if isinstance(w, _Divider) or (isinstance(w, QLabel) and w.text() in CAPTIONS)
    ]

    assert row == [
        "Commit as:",
        "|",
        "Active Settings:",
        "|",
        "Active Repository:",
        "|",
        "Active Inference:",
        "|",
        "Remote:",
        "|",
    ]


def test_remote_is_a_caption_in_the_colour_of_the_others(bar):
    """And what follows it names the remote a push goes to."""
    from PyQt6.QtWidgets import QLabel

    captions = {w.text(): w for w in bar.findChildren(QLabel) if w.text() in CAPTIONS}
    for caption in captions.values():
        caption.ensurePolished()
    colours = {
        text: caption.palette().color(caption.foregroundRole()).name()
        for text, caption in captions.items()
    }

    assert set(colours) == set(CAPTIONS)
    assert len(set(colours.values())) == 1, colours
    box = bar.layout()
    assert box.indexOf(captions["Remote:"]) + 1 == box.indexOf(bar.auth_status)
    assert bar.auth_status.text() == "no remote"


def test_each_line_stands_as_far_from_the_group_before_it_as_the_one_after(qapp, bar):
    """Push to: included -- it follows Active Inference, and the room to spare
    comes after the last line rather than before any group."""
    from git_assistant.ui.identity_bar import _Divider

    spare = 300
    bar.resize(bar.sizeHint().width() + spare, bar.sizeHint().height())
    bar.show()
    qapp.processEvents()
    drawn = _drawn(bar)
    dividers = [w for w in drawn if isinstance(w, _Divider)]

    for divider in dividers[:-1]:
        i = drawn.index(divider)
        before, after = drawn[i - 1].geometry(), drawn[i + 1].geometry()
        gap_before = divider.geometry().left() - before.right()
        gap_after = after.left() - divider.geometry().right()
        assert gap_before == gap_after, (drawn[i - 1], drawn[i + 1])
    assert drawn[0].geometry().left() == 0
    assert bar.width() - 1 - dividers[-1].geometry().right() >= spare
    bar.close()


def test_the_lines_are_the_text_colour_faded_toward_the_window_in_either_theme(
    app, settings
):
    from PyQt6.QtGui import QPalette

    from git_assistant.ui.identity_bar import _Divider

    for key in (theme.DARK, theme.LIGHT):
        theme.apply(app, key)
        bar = IdentityBar(settings, IdentityStore([]))
        bar.show()
        app.processEvents()
        divider = bar.findChildren(_Divider)[0]
        middle = divider.geometry().center()
        image = bar.grab().toImage()
        line = image.pixelColor(middle)
        window = image.pixelColor(divider.geometry().left(), middle.y())
        text = app.palette().color(QPalette.ColorRole.WindowText)

        # This theme's own text colour, let partly through onto the window...
        ink = divider.colour()
        assert ink.rgb() == text.rgb(), key
        for part in ("red", "green", "blue"):
            through = getattr(window, part)() * (1 - ink.alphaF())
            through += getattr(ink, part)() * ink.alphaF()
            assert abs(getattr(line, part)() - through) <= 2, (key, part)
        # ...and less of it than of the window, so it reads as a line, not a letter.
        assert line.name() != window.name(), key
        assert abs(line.lightness() - window.lightness()) < abs(
            line.lightness() - text.lightness()
        ), key
        bar.close()


# ---- a window too narrow for all of it ----------------------------------------------
LONG_MODEL = "claude-sonnet-4-5-20250929"
LONG_PUSH = "github-personal over SSH (key from SSH config)"


@pytest.fixture
def crowded(qapp, settings):
    """A bar with more to say than a narrow window has room for."""
    settings.provider = "claude"
    settings.set_provider_model("claude", LONG_MODEL)
    bar = IdentityBar(settings, IdentityStore([]))
    bar.auth_status.setText(LONG_PUSH)
    bar.whole_width = bar.sizeHint().width()  # read before anything is cut
    host = QWidget()
    bar.setParent(host)
    yield bar
    host.close()


def _lay_out(qapp, bar, width):
    """Give `bar` `width` pixels, as the window above the tabs would."""
    bar.setGeometry(0, 0, width, bar.sizeHint().height())
    bar.parentWidget().resize(width, bar.height())
    bar.parentWidget().show()
    qapp.processEvents()
    # And the pass the event loop has queued since, if it has. A combo whose
    # entries changed resizes itself to them on a timer and asks for the row to
    # be laid out again, and the window does that before it next paints.
    bar.layout().activate()


def _readouts(bar):
    return (
        bar.status,
        bar.auth_status,
        bar.repo_name,
        bar.repo_branch,
        bar.inference_model,
    )


@pytest.mark.parametrize("short_by", [40, 300, 900])
def test_a_window_too_narrow_never_draws_one_part_of_the_bar_over_another(
    qapp, crowded, short_by
):
    width = crowded.whole_width - short_by
    _lay_out(qapp, crowded, width)

    widgets = _drawn(crowded)
    for left, right in zip(widgets, widgets[1:]):
        assert left.geometry().right() < right.geometry().left(), (left, right)
    if width >= crowded.minimumSizeHint().width():
        # Below its minimum, Qt shrinks the gaps by integer division and the last
        # item can end a pixel or two past the edge: clipped there, over nothing.
        assert widgets[-1].geometry().right() < width


def test_the_explanations_give_way_before_the_names(qapp, crowded):
    """"set for this repository" is cut before a name loses a letter."""
    _lay_out(qapp, crowded, crowded.whole_width - 20)

    assert crowded.status.shown().endswith("…")
    for name in (crowded.repo_name, crowded.repo_branch, crowded.inference_model):
        assert name.shown() == name.text()


def test_a_short_name_is_not_cut_to_spare_a_long_sentence(qapp, settings):
    """Qt's own row would have taken as much from "gpt-4o-mini" as from the rest."""
    settings.provider = "openai"
    settings.set_provider_model("openai", "gpt-4o-mini")
    bar = IdentityBar(settings, IdentityStore([]))
    bar.auth_status.setText(LONG_PUSH)
    whole = bar.sizeHint().width()
    host = QWidget()
    bar.setParent(host)

    _lay_out(qapp, bar, whole - 150)

    assert bar.inference_model.shown() == "gpt-4o-mini"
    assert bar.auth_status.shown().endswith("…")
    host.close()


def test_each_readout_gives_only_as_far_as_it_still_reads_while_others_can_give(
    qapp, crowded
):
    """Not the first one down to "se…" while the model's name is still whole."""
    _lay_out(qapp, crowded, crowded.whole_width - 250)

    assert crowded.inference_model.shown() != LONG_MODEL
    for readout in _readouts(crowded):
        shown = readout.shown()
        assert shown == readout.text() or len(shown) >= 9, shown


def test_the_first_offered_is_down_to_an_ellipsis_before_the_last_gives_that_far(
    qapp, crowded
):
    """Given 40 pixels more than the row's least, they stay with the last offered."""
    _lay_out(qapp, crowded, crowded.minimumSizeHint().width() + 40)

    first, last = crowded.status, crowded.inference_model
    assert first.width() == first.minimumSizeHint().width()
    assert last.width() > last.minimumSizeHint().width()


def test_past_every_readable_length_the_first_offered_is_cut_further_first(qapp):
    """Not a few pixels from each, which is what Qt's own row would do next."""
    from git_assistant.ui.identity_bar import _Row, _Shrinking

    host = QWidget()
    strip = QWidget(host)
    row = _Row(strip)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    first, second = _Shrinking(), _Shrinking()
    for readout in (first, second):
        readout.setText("a sentence long enough to be cut short")
        row.addWidget(readout)
        row.give_way(readout, 10)
    readable = first.fontMetrics().averageCharWidth() * 10

    strip.setGeometry(0, 0, 2 * readable - 10, 30)
    host.show()
    qapp.processEvents()

    assert (first.width(), second.width()) == (readable - 10, readable)
    host.close()


def test_widening_the_window_again_gives_every_readout_its_whole_text_back(
    qapp, crowded
):
    _lay_out(qapp, crowded, crowded.whole_width - 300)
    assert crowded.auth_status.shown() != LONG_PUSH

    _lay_out(qapp, crowded, crowded.whole_width + 50)

    for readout in _readouts(crowded):
        assert readout.shown() == readout.text()


def test_a_readout_cut_short_still_holds_its_whole_text(qapp, crowded):
    _lay_out(qapp, crowded, crowded.whole_width - 300)

    assert crowded.auth_status.shown().endswith("…")
    assert crowded.auth_status.text() == LONG_PUSH
    assert crowded.status.text() == "set for this repository"


def test_what_is_painted_is_the_text_cut_short(qapp, crowded):
    from PyQt6.QtWidgets import QLabel

    _lay_out(qapp, crowded, crowded.whole_width - 300)
    readout = crowded.auth_status
    plain = QLabel(readout.shown(), crowded.parentWidget())  # a window grabs opaque
    plain.setStyleSheet(readout.styleSheet())
    plain.resize(readout.size())

    assert readout.grab().toImage() == plain.grab().toImage()


def test_a_repository_keeps_both_ends_of_its_name_and_a_branch_its_last_part(bar):
    bar.repo_name.setText("work\\some-monorepo-with-a-long-name")
    bar.repo_branch.setText("feature/a-branch-with-a-long-name")
    for readout in (bar.repo_name, bar.repo_branch):
        readout.resize(90, readout.height())

    name, branch = bar.repo_name.shown(), bar.repo_branch.shown()
    assert name.startswith("work") and name.endswith("name") and "…" in name
    assert branch.startswith("…") and branch.endswith("long-name")


def test_a_name_that_looks_like_markup_is_shown_as_it_is_written(bar):
    bar.repo_name.setText("<b>work</b>")
    bar.repo_name.resize(bar.repo_name.sizeHint())

    assert bar.repo_name.shown() == "<b>work</b>"


def test_an_identity_added_after_the_bar_is_shown_still_fits_in_it(qapp, settings):
    from git_assistant.identities import Identity

    store = IdentityStore([])
    bar = IdentityBar(settings, store)
    bar.show()
    qapp.processEvents()
    address = "someone.with.a.rather.long.address@example-company.com"

    store.identities.append(Identity("Someone", address))
    bar.refresh()

    assert bar.combo.sizeHint().width() > bar.combo.fontMetrics().horizontalAdvance(
        address
    )
    bar.close()


def test_the_branch_is_the_green_the_repository_list_uses(app, settings):
    theme.apply(app, theme.DARK)
    bar = IdentityBar(settings, IdentityStore([]))
    dark = bar.repo_branch.styleSheet()

    theme.apply(app, theme.LIGHT)
    app.processEvents()

    assert "#5fd39a" in dark
    assert "#1a7f4b" in bar.repo_branch.styleSheet()


# ---- in the window ------------------------------------------------------------------
def test_clone_and_create_sits_just_left_of_commit(qapp, settings):
    from git_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(settings)
    commit = dlg.tabs.indexOf(dlg.commit_panel)

    assert dlg.tabs.tabText(commit) == "Commit"
    assert dlg.tabs.widget(commit - 1) is dlg.clone_panel
    assert dlg.tabs.tabText(commit - 1) == "Clone && Create"


def test_the_window_still_opens_on_commit(qapp, settings):
    """It is the tab used every day; a tab to its left does not change that."""
    from git_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(settings)

    assert dlg.tabs.currentWidget() is dlg.commit_panel


def test_choosing_a_repository_on_clone_and_create_updates_the_bar(
    qapp, settings, repos
):
    from git_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(settings)
    dlg.clone_panel.repo_picker.select(str(repos["beta"]))

    assert dlg.identity_bar.repo_name.text() == "forks\\beta"
    assert dlg.identity_bar.repo_branch.text() == "feature/login"


def test_switching_branch_on_the_commit_tab_updates_the_bar(qapp, settings, repos):
    from git_assistant.ui.settings_dialog import SettingsDialog

    _git(repos["alpha"], "branch", "release/1.0")
    dlg = SettingsDialog(settings)
    assert dlg.identity_bar.repo_branch.text() == "main"
    picker = dlg.commit_panel.branch_picker

    picker.branch_list.itemClicked.emit(picker.item_for("release/1.0"))

    assert git_ops.current_branch(repos["alpha"]) == "release/1.0"
    assert dlg.identity_bar.repo_branch.text() == "release/1.0"


@pytest.mark.parametrize("tab", ["commit_panel", "agents_panel", "review_panel"])
def test_changing_the_provider_on_a_tab_updates_the_bar(qapp, settings, tab):
    from git_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(settings)
    combo = getattr(dlg, tab).provider_combo

    combo.setCurrentIndex(combo.findData("ollama"))

    assert dlg.identity_bar.inference_name.text() == "Ollama"


def test_choosing_a_provider_on_connection_and_model_updates_the_bar(qapp, settings):
    from PyQt6.QtCore import Qt

    from git_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(settings)
    row = next(
        i
        for i in range(dlg.provider_list.count())
        if dlg.provider_list.item(i).data(Qt.ItemDataRole.UserRole) == "claude"
    )

    dlg.provider_list.setCurrentRow(row)

    assert dlg.identity_bar.inference_name.text() == "Claude"


def test_a_model_chosen_on_connection_and_model_reaches_the_bar_when_saved(
    qapp, settings
):
    from git_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(settings)
    dlg.model_combo.addItem("qwen3.5-4b", "qwen3.5-4b")
    dlg.model_combo.setCurrentIndex(dlg.model_combo.count() - 1)

    dlg._autosave()

    assert dlg.identity_bar.inference_model.text() == "qwen3.5-4b"
