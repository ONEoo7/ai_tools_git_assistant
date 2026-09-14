"""The staging window: files, hunks and lines, and line endings on the way in."""

import subprocess
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QTextOption  # noqa: E402
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QMessageBox  # noqa: E402

from git_assistant import git_ops  # noqa: E402
from git_assistant.ui import theme  # noqa: E402
from git_assistant.ui.staging_dialog import (  # noqa: E402
    APPLY_ATTRIBUTES,
    CR_MARK,
    CRLF_MARK,
    LF_MARK,
    NORMALIZE,
    STAGED_TITLE,
    UNSTAGED_TITLE,
    StagingDialog,
    diff_colours,
    shown_diff,
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

BASE = b"".join(b"line %d\n" % n for n in range(1, 21))


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def app(qapp):
    yield qapp
    theme.apply(qapp, theme.SYSTEM)  # never leave it on the next test


@pytest.fixture
def slot_errors(monkeypatch):
    """Exceptions raised inside Qt slots, kept so the test fails on them.

    With Python's own ``sys.excepthook`` in place, PyQt ends the process when a
    slot raises: no traceback, no test name, and nothing after it runs.
    """
    caught = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: caught.append(exc))
    yield caught
    if caught:
        _kind, error, trace = caught[0]
        raise error.with_traceback(trace)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "core.autocrlf", "false")
    (path / ".gitattributes").write_bytes(b"*.txt text eol=lf\n")
    (path / "a.txt").write_bytes(BASE)
    (path / "b.txt").write_bytes(BASE)
    (path / "raw.dat").write_bytes(b"one\r\ntwo\r\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return path


def _names(tree):
    """The path of every file in a list, however deep in folders it is."""
    return [entry.path for entry in StagingDialog._entries(tree)]


def _top(tree):
    """What a list shows at its top level: folder names and file names."""
    return [tree.topLevelItem(i).text(1) for i in range(tree.topLevelItemCount())]


def _markers(tree):
    return {
        row.data(1, Qt.ItemDataRole.UserRole).path: row.text(0)
        for row in StagingDialog._rows(tree)
        if row.data(1, Qt.ItemDataRole.UserRole) is not None
    }


def _marker_colours(tree):
    """Each file's marker colour, or None where the marker keeps the list's own."""
    colours = {}
    for row in StagingDialog._rows(tree):
        entry = row.data(1, Qt.ItemDataRole.UserRole)
        if entry is not None:
            brush = row.foreground(0)
            plain = brush.style() == Qt.BrushStyle.NoBrush
            colours[entry.path] = None if plain else brush.color().name()
    return colours


def _open_folders(tree):
    return {row.text(1) for row in StagingDialog._rows(tree) if row.isExpanded()}


def _write(repo, *paths):
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"new\n")


NESTED = (
    "src/app/main.py",
    "src/app/util.py",
    "src/lib.py",
    "docs/guide.md",
    "top.txt",
)


def _index(repo, name):
    return _git(repo, "show", f":{name}").stdout


def _shown(dialog):
    return dialog.diff_view.toPlainText().split("\n")


def _line(dialog, text):
    """The diff line number of the line on screen reading ``text``, ending aside."""
    for number, shown in enumerate(_shown(dialog)):
        if shown in (text, text + LF_MARK, text + CRLF_MARK, text + CR_MARK):
            return number
    raise ValueError(f"{text!r} is not a line of the diff on screen")


# ---- what it lists ------------------------------------------------------------------
def test_it_lists_unstaged_and_staged_changes_apart(qapp, repo, slot_errors):
    (repo / "a.txt").write_bytes(BASE + b"more\n")
    _git(repo, "add", "a.txt")
    (repo / "b.txt").write_bytes(b"changed\n")
    (repo / "new.txt").write_bytes(b"fresh\n")

    dialog = StagingDialog(str(repo))

    assert _names(dialog.unstaged_list) == ["b.txt", "new.txt"]
    assert _names(dialog.staged_list) == ["a.txt"]
    assert dialog.unstaged_title.text() == f"{UNSTAGED_TITLE} (2)"
    assert dialog.staged_title.text() == f"{STAGED_TITLE} (1)"


def test_apply_gitattributes_is_on_to_begin_with(qapp, repo, slot_errors):
    dialog = StagingDialog(str(repo))

    assert dialog.apply_attributes.text() == APPLY_ATTRIBUTES
    assert dialog.apply_attributes.isChecked()


def test_the_first_change_is_shown_to_begin_with(qapp, repo, slot_errors):
    (repo / "b.txt").write_bytes(BASE.replace(b"line 3\n", b"LINE 3\n"))

    dialog = StagingDialog(str(repo))

    assert "b.txt" in dialog.diff_title.text()
    assert _shown(dialog)[_line(dialog, "+LINE 3")] == "+LINE 3" + LF_MARK


# ---- how lines end, drawn -----------------------------------------------------------
def test_every_line_shows_how_it_ends_and_is_still_one_line():
    raw = (
        b"diff --git a/f b/f\n"
        b"@@ -1,2 +1,2 @@\n"
        b" same\r\n"
        b"-old\n"
        b"+new\r\n"
    )

    shown = shown_diff(raw)

    assert shown.text.split("\n") == [
        "diff --git a/f b/f" + LF_MARK,
        "@@ -1,2 +1,2 @@" + LF_MARK,
        " same" + CRLF_MARK,
        "-old" + LF_MARK,
        "+new" + CRLF_MARK,
    ]


def test_a_last_line_with_no_newline_shows_no_ending():
    """The newline after it is git's, and git says so on the line that follows."""
    raw = (
        b"@@ -1 +1 @@\n"
        b"-a\n"
        b"\\ No newline at end of file\n"
        b"+b\r\n"
        b"\\ No newline at end of file\n"
    )

    lines = shown_diff(raw).text.split("\n")

    assert lines == [
        "@@ -1 +1 @@" + LF_MARK,
        "-a",
        "\\ No newline at end of file",
        "+b" + CR_MARK,  # a carriage return with no newline after it
        "\\ No newline at end of file",
    ]


def test_a_carriage_return_inside_a_line_is_drawn_where_it_is():
    shown = shown_diff(b"+a\rb\n")

    assert shown.text == "+a" + CR_MARK + "b" + LF_MARK
    assert shown.marks == [[(2, len(CR_MARK)), (3 + len(CR_MARK), len(LF_MARK))]]


def test_a_line_separator_character_does_not_split_a_line():
    raw = ("+a" + chr(0x2028) + "b" + chr(0x2029) + "c\n").encode("utf-8")

    assert len(shown_diff(raw).text.split("\n")) == 1


def test_a_crlf_file_shows_its_carriage_returns_in_the_window(qapp, repo, slot_errors):
    (repo / "raw.dat").write_bytes(b"one\r\nTWO\r\n")

    dialog = StagingDialog(str(repo))
    dialog.show_file("raw.dat")

    assert "+TWO" + CRLF_MARK in _shown(dialog)
    assert _shown(dialog)[0] == "diff --git a/raw.dat b/raw.dat" + LF_MARK


def test_spaces_and_tabs_are_drawn_in_a_diff(qapp, repo, slot_errors):
    (repo / "a.txt").write_bytes(BASE.replace(b"line 5\n", b"LINE 5\n"))

    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")

    flags = dialog.diff_view.document().defaultTextOption().flags()
    assert flags & QTextOption.Flag.ShowTabsAndSpaces


def test_the_endings_and_spaces_are_drawn_quietly(qapp, repo, slot_errors):
    (repo / "a.txt").write_bytes(BASE.replace(b"line 5\n", b"LINE 5\n"))
    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")
    number = _line(dialog, "+LINE 5")
    layout = dialog.diff_view.document().findBlockByNumber(number).layout()

    def colour_at(position):
        for part in layout.formats():
            if part.start <= position < part.start + part.length:
                return part.format.foreground().color().name()
        return None

    quiet = diff_colours()["ws"]
    assert colour_at(len("+LINE 5")) == quiet, "the \\n after the text"
    assert colour_at(len("+LINE")) == quiet, "the space inside it"
    assert colour_at(0) == diff_colours()["+"], "and the text itself as before"


def test_git_header_lines_show_no_dots_as_git_extensions_shows_none(
    qapp, repo, slot_errors
):
    """They are git's lines, not the file's; their ending is still shown."""
    from PyQt6.QtGui import QPalette

    (repo / "a.txt").write_bytes(BASE.replace(b"line 5\n", b"LINE 5\n"))
    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")
    layout = dialog.diff_view.document().findBlockByNumber(0).layout()
    header = _shown(dialog)[0]  # "diff --git a/a.txt b/a.txt\n"

    def colour_at(position):
        for part in layout.formats():
            if part.start <= position < part.start + part.length:
                return part.format.foreground().color().name()
        return None

    page = QApplication.palette().color(QPalette.ColorRole.Base).name()
    assert colour_at(header.index(" ")) == page
    assert colour_at(len(header) - len(LF_MARK)) == diff_colours()["ws"]


def test_a_sentence_in_place_of_a_diff_has_no_dots_in_it(qapp, repo, slot_errors):
    _write(repo, "src/one.py")

    dialog = StagingDialog(str(repo))  # nothing selectable is shown: a folder

    flags = dialog.diff_view.document().defaultTextOption().flags()
    assert not flags & QTextOption.Flag.ShowTabsAndSpaces


# ---- line numbers --------------------------------------------------------------------
def test_each_line_is_numbered_in_the_old_file_and_the_new(qapp, repo, slot_errors):
    """Git Extensions' two columns: a removed line has only its old number, an
    added one only its new, and git's header and @@ lines have neither."""
    (repo / "a.txt").write_bytes(
        BASE.replace(b"line 5\n", b"LINE 5\n").replace(b"line 9\n", b"")
    )

    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")
    numbers = dialog.diff_view.numbers

    assert numbers[_line(dialog, " line 4")] == (4, 4)
    assert numbers[_line(dialog, "-line 5")] == (5, None)
    assert numbers[_line(dialog, "+LINE 5")] == (None, 5)
    assert numbers[_line(dialog, "-line 9")] == (9, None)
    assert numbers[_line(dialog, " line 10")] == (10, 9), "one line fewer after it"
    assert 0 not in numbers, "diff --git"
    assert _line(dialog, "@@ -2,11 +2,10 @@ line 1") not in numbers


def test_the_numbers_are_drawn_beside_the_diff_not_written_into_it(
    qapp, repo, slot_errors
):
    """A line on screen is still a line of the patch: selections depend on it."""
    (repo / "a.txt").write_bytes(BASE.replace(b"line 5\n", b"LINE 5\n"))

    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")
    view = dialog.diff_view

    assert "-line 5" + LF_MARK in _shown(dialog)
    assert view.gutter_width() > 0
    assert view.viewportMargins().left() == view.gutter_width()
    assert view.gutter.width() == view.gutter_width()


def test_the_columns_are_as_wide_as_the_longest_number_needs(qapp, repo, slot_errors):
    long_file = b"".join(b"line %d\n" % n for n in range(1, 1201))
    (repo / "long.txt").write_bytes(long_file)
    _git(repo, "add", "long.txt")
    _git(repo, "commit", "-q", "-m", "long")
    (repo / "long.txt").write_bytes(long_file.replace(b"line 1100\n", b"LINE 1100\n"))
    (repo / "a.txt").write_bytes(BASE.replace(b"line 5\n", b"LINE 5\n"))

    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")
    short = dialog.diff_view.column_width()
    dialog.show_file("long.txt")

    digit = dialog.diff_view.fontMetrics().horizontalAdvance("9")
    assert dialog.diff_view.column_width() - short == 3 * digit, "8 at most, then 1103"


def test_a_sentence_in_place_of_a_diff_has_no_line_numbers(qapp, repo, slot_errors):
    """Not even the ones the diff shown before it had."""
    (repo / "a.txt").write_bytes(BASE.replace(b"line 5\n", b"LINE 5\n"))
    _write(repo, "src/one.py")
    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")
    assert dialog.diff_view.numbers

    tree = dialog.unstaged_list
    folder = next(
        tree.topLevelItem(i)
        for i in range(tree.topLevelItemCount())
        if tree.topLevelItem(i).text(1) == "src"
    )
    tree.setCurrentItem(folder)  # a folder: a sentence about it, not a diff

    assert dialog.diff_view.numbers == {}
    assert dialog.diff_view.gutter_width() == 0
    assert dialog.diff_view.viewportMargins().left() == 0


def test_removed_and_added_lines_are_tinted_in_their_own_column(
    qapp, repo, slot_errors
):
    """As Git Extensions draws them: red behind an old number, green behind a new."""
    from PyQt6.QtGui import QPalette

    (repo / "a.txt").write_bytes(BASE.replace(b"line 5\n", b"LINE 5\n"))
    dialog = StagingDialog(str(repo))
    dialog.resize(1300, 760)
    dialog.show()
    dialog.show_file("a.txt")
    qapp.processEvents()
    view = dialog.diff_view
    image = view.gutter.grab().toImage()
    column = view.column_width()

    def pixel(text, x):
        block = view.document().findBlockByNumber(_line(dialog, text))
        top = view.blockBoundingGeometry(block).translated(view.contentOffset()).top()
        return image.pixelColor(x, int(top) + 2).name()

    page = QApplication.palette().color(QPalette.ColorRole.Base).name()
    assert pixel("-line 5", 1) != page, "the old number's cell of a removed line"
    assert pixel("-line 5", column + 1) == page, "and not the new one's"
    assert pixel("+LINE 5", column + 1) != page
    assert pixel("+LINE 5", 1) == page
    assert pixel(" line 4", 1) == page == pixel(" line 4", column + 1)
    dialog.close()


def test_removed_and_added_lines_are_tinted_right_across(qapp, repo, slot_errors):
    """Red behind a removed line and green behind an added one, to the far edge;
    nothing behind git's own lines -- "+++ b/a.txt" included -- or context."""
    from PyQt6.QtGui import QPalette

    (repo / "a.txt").write_bytes(BASE.replace(b"line 5\n", b"LINE 5\n"))
    dialog = StagingDialog(str(repo))
    dialog.resize(1300, 760)
    dialog.show()
    dialog.show_file("a.txt")
    qapp.processEvents()
    view = dialog.diff_view
    # The whole window: a viewport grabbed on its own is drawn without the
    # background its frame gives it.
    image = dialog.grab().toImage()
    viewport = view.viewport()
    corner = viewport.mapTo(dialog, viewport.rect().topLeft())
    edge = corner.x() + viewport.width() - 3  # past the end of every line's text

    def colour(text):
        block = view.document().findBlockByNumber(_line(dialog, text))
        top = view.blockBoundingGeometry(block).translated(view.contentOffset()).top()
        return image.pixelColor(edge, corner.y() + int(top) + 2)

    page = QApplication.palette().color(QPalette.ColorRole.Base).name()
    removed, added = colour("-line 5"), colour("+LINE 5")
    assert removed.name() != page and removed.red() > removed.green()
    assert added.name() != page and added.green() > added.red()
    for untinted in (" line 4", "+++ b/a.txt", "@@ -2,7 +2,7 @@ line 1"):
        assert colour(untinted).name() == page, untinted
    dialog.close()


# ---- how a file is marked ------------------------------------------------------------
def test_a_new_file_is_marked_plus_and_a_removed_one_minus(qapp, repo, slot_errors):
    """Not git's "?", which asks about a file rather than saying what changed."""
    (repo / "new.txt").write_bytes(b"fresh\n")
    (repo / "b.txt").unlink()
    (repo / "a.txt").write_bytes(b"changed\n")

    dialog = StagingDialog(str(repo))

    assert _markers(dialog.unstaged_list) == {
        "new.txt": "+",
        "b.txt": "-",
        "a.txt": "M",
    }


def test_staged_they_keep_the_same_marks(qapp, repo, slot_errors):
    (repo / "new.txt").write_bytes(b"fresh\n")
    (repo / "b.txt").unlink()
    (repo / "a.txt").write_bytes(b"changed\n")
    _git(repo, "add", "-A")

    dialog = StagingDialog(str(repo))

    assert _markers(dialog.staged_list) == {"new.txt": "+", "b.txt": "-", "a.txt": "M"}


@pytest.mark.parametrize(
    "key, green, red",
    [(theme.DARK, "#56d364", "#f85149"), (theme.LIGHT, "#116329", "#cf222e")],
)
def test_plus_is_green_and_minus_red_in_either_theme(
    app, repo, slot_errors, key, green, red
):
    """The diff's own colours for an added and a removed line."""
    theme.apply(app, key)
    (repo / "new.txt").write_bytes(b"fresh\n")
    (repo / "b.txt").unlink()
    (repo / "a.txt").write_bytes(b"changed\n")

    dialog = StagingDialog(str(repo))

    assert _marker_colours(dialog.unstaged_list) == {
        "new.txt": green,
        "b.txt": red,
        "a.txt": None,
    }


# ---- what .gitattributes would rewrite, and rewriting it -----------------------------
ARROW = chr(0x2192)


@pytest.fixture
def answer(monkeypatch):
    """Answer the Normalize Line Endings question, recording that it was asked."""
    asked = []

    def set_to(button):
        def question(*args, **kwargs):
            asked.append(args)
            return button

        monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
        return asked

    return set_to


def _badges(tree):
    """Each row's line-ending badge, by file path or folder name; "" for none."""
    badges = {}
    for row in StagingDialog._rows(tree):
        entry = row.data(1, Qt.ItemDataRole.UserRole)
        badges[entry.path if entry is not None else row.text(1) + "/"] = row.text(2)
    return badges


def test_each_file_says_whether_gitattributes_rewrites_its_line_endings(
    qapp, repo, slot_errors
):
    (repo / "a.txt").write_bytes(BASE.replace(b"\n", b"\r\n"))  # *.txt is eol=lf
    (repo / "b.txt").write_bytes(BASE.replace(b"line 1\n", b"LINE 1\n"))
    (repo / "raw.dat").write_bytes(b"one\ntwo\n")  # no rule for .dat

    dialog = StagingDialog(str(repo))

    assert _badges(dialog.unstaged_list) == {
        "a.txt": f"CRLF {ARROW} LF",
        "b.txt": "",
        "raw.dat": "",
    }


def test_a_folded_folder_says_how_many_in_it_would_be_rewritten(
    qapp, repo, slot_errors
):
    (repo / "src").mkdir()
    (repo / "src" / "one.txt").write_bytes(b"one\r\n")
    (repo / "src" / "two.txt").write_bytes(b"two\r\n")
    (repo / "src" / "three.txt").write_bytes(b"three\n")

    dialog = StagingDialog(str(repo))

    assert _badges(dialog.unstaged_list)["src/"] == "2 to normalize"


def test_normalize_line_endings_sits_right_of_stage_all(qapp, repo, slot_errors):
    dialog = StagingDialog(str(repo))

    rows = [
        layout
        for layout in dialog.findChildren(QHBoxLayout)
        if layout.indexOf(dialog.stage_all_btn) >= 0
    ]
    assert dialog.normalize_btn.text() == NORMALIZE
    row = rows[0]
    assert row.indexOf(dialog.normalize_btn) == row.indexOf(dialog.stage_all_btn) + 1


def test_normalize_line_endings_waits_until_there_is_something_to_normalize(
    qapp, repo, slot_errors
):
    (repo / "b.txt").write_bytes(BASE.replace(b"line 1\n", b"LINE 1\n"))
    assert not StagingDialog(str(repo)).normalize_btn.isEnabled()

    (repo / "a.txt").write_bytes(BASE.replace(b"\n", b"\r\n"))

    assert StagingDialog(str(repo)).normalize_btn.isEnabled()


def test_normalizing_rewrites_the_marked_files_and_stages_nothing(
    qapp, repo, slot_errors, answer
):
    (repo / "a.txt").write_bytes(BASE.replace(b"\n", b"\r\n"))
    (repo / "raw.dat").write_bytes(b"one\r\nthree\r\n")  # no rule: left alone
    asked = answer(QMessageBox.StandardButton.Yes)
    dialog = StagingDialog(str(repo))

    dialog.normalize_btn.click()

    assert len(asked) == 1 and "1 unstaged file(s)" in asked[0][2]
    assert (repo / "a.txt").read_bytes() == BASE
    assert (repo / "raw.dat").read_bytes() == b"one\r\nthree\r\n"
    assert dialog.status.text() == "Normalized line endings in 1 of 1 file(s)."
    assert _names(dialog.staged_list) == []
    assert not any(_badges(dialog.unstaged_list).values())
    assert not dialog.normalize_btn.isEnabled()


def test_cancelling_normalize_line_endings_changes_nothing(
    qapp, repo, slot_errors, answer
):
    (repo / "a.txt").write_bytes(BASE.replace(b"\n", b"\r\n"))
    answer(QMessageBox.StandardButton.Cancel)
    dialog = StagingDialog(str(repo))

    dialog.normalize_btn.click()

    assert (repo / "a.txt").read_bytes() == BASE.replace(b"\n", b"\r\n")
    assert _badges(dialog.unstaged_list)["a.txt"] == f"CRLF {ARROW} LF"


# ---- line endings --------------------------------------------------------------------
# The fixture's .gitattributes declares "*.txt text eol=lf" and nothing for .dat.
def test_the_selected_files_line_endings_are_shown(qapp, repo, slot_errors):
    (repo / "a.txt").write_bytes(BASE.replace(b"line 5\n", b"LINE 5\n"))

    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")

    assert dialog.line_endings.isVisibleTo(dialog)
    assert dialog.line_endings.text() == (
        "Line endings - working tree: LF | index: LF | .gitattributes: text eol=lf"
    )


def test_line_endings_other_than_gitattributes_wants_are_flagged(
    qapp, repo, slot_errors
):
    (repo / "a.txt").write_bytes(BASE.replace(b"\n", b"\r\n"))

    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")

    text = dialog.line_endings.text()
    assert "working tree: CRLF | index: LF" in text
    assert text.endswith(".gitattributes wants LF")


def test_without_a_rule_nothing_is_flagged(qapp, repo, slot_errors):
    (repo / "raw.dat").write_bytes(b"one\r\nTWO\r\n")

    dialog = StagingDialog(str(repo))
    dialog.show_file("raw.dat")

    assert dialog.line_endings.text() == (
        "Line endings - working tree: CRLF | index: CRLF | .gitattributes: (no rule)"
    )


def test_a_new_file_has_no_index_line_endings_yet(qapp, repo, slot_errors):
    (repo / "new.txt").write_bytes(b"fresh\n")

    dialog = StagingDialog(str(repo))
    dialog.show_file("new.txt")

    assert "index: (new file)" in dialog.line_endings.text()


def test_the_line_endings_follow_the_selection(qapp, repo, slot_errors):
    (repo / "a.txt").write_bytes(BASE.replace(b"\n", b"\r\n"))
    (repo / "raw.dat").write_bytes(b"one\ntwo\n")
    dialog = StagingDialog(str(repo))

    dialog.show_file("a.txt")
    assert "working tree: CRLF" in dialog.line_endings.text()
    dialog.show_file("raw.dat")

    assert "working tree: LF" in dialog.line_endings.text()


def test_a_folder_has_no_line_endings_to_show(qapp, repo, slot_errors):
    _write(repo, "src/one.py")
    dialog = StagingDialog(str(repo))

    dialog.unstaged_list.setCurrentItem(dialog.unstaged_list.topLevelItem(0))

    assert not dialog.line_endings.isVisibleTo(dialog)


# ---- folders ------------------------------------------------------------------------
def test_unstaged_files_are_listed_under_their_folders_all_folded(
    qapp, repo, slot_errors
):
    _write(repo, *NESTED)

    dialog = StagingDialog(str(repo))

    tree = dialog.unstaged_list
    assert _top(tree) == ["docs", "src", "top.txt"], "folders first, then files"
    src = tree.topLevelItem(1)
    assert [src.child(i).text(1) for i in range(src.childCount())] == ["app", "lib.py"]
    assert _open_folders(tree) == set()
    assert sorted(_names(tree)) == sorted(NESTED)


def test_the_staged_list_stays_a_flat_list_of_paths(qapp, repo, slot_errors):
    _write(repo, *NESTED)
    _git(repo, "add", "src/app/main.py", "docs/guide.md")

    dialog = StagingDialog(str(repo))

    assert _top(dialog.staged_list) == ["docs/guide.md", "src/app/main.py"]


def test_nothing_hidden_in_a_folded_folder_is_selected(qapp, repo, slot_errors):
    """Stage would act on a file nobody can see was chosen."""
    _write(repo, "src/one.py", "src/two.py")

    dialog = StagingDialog(str(repo))

    assert dialog._selected(dialog.unstaged_list) == []
    assert not dialog.stage_btn.isEnabled()
    assert dialog.diff_title.text() == "Select a file to see its diff."


def test_selecting_a_folder_stages_everything_in_it(qapp, repo, slot_errors):
    _write(repo, *NESTED)
    dialog = StagingDialog(str(repo))
    src = dialog.unstaged_list.topLevelItem(1)

    dialog.unstaged_list.setCurrentItem(src)
    assert "src/ - 3 file(s)" in dialog.diff_title.text()
    assert dialog.stage_btn.isEnabled()
    dialog.stage_btn.click()

    assert sorted(_names(dialog.staged_list)) == [
        "src/app/main.py",
        "src/app/util.py",
        "src/lib.py",
    ]
    assert sorted(_names(dialog.unstaged_list)) == ["docs/guide.md", "top.txt"]


def test_stage_all_takes_the_files_in_folded_folders_too(qapp, repo, slot_errors):
    _write(repo, *NESTED)
    dialog = StagingDialog(str(repo))

    dialog.stage_all_btn.click()

    assert _names(dialog.unstaged_list) == []
    assert sorted(_names(dialog.staged_list)) == sorted(NESTED)


def test_double_clicking_a_folder_does_not_stage_it(qapp, repo, slot_errors):
    _write(repo, *NESTED)
    dialog = StagingDialog(str(repo))

    dialog.unstaged_list.itemDoubleClicked.emit(dialog.unstaged_list.topLevelItem(1), 1)

    assert _names(dialog.staged_list) == []


def test_folders_opened_stay_open_and_the_next_file_in_them_is_shown(
    qapp, repo, slot_errors
):
    _write(repo, *NESTED)
    dialog = StagingDialog(str(repo))
    dialog.show_file("src/app/main.py")
    assert _open_folders(dialog.unstaged_list) == {"src", "app"}

    dialog.stage(dialog._selected(dialog.unstaged_list))

    assert _open_folders(dialog.unstaged_list) == {"src", "app"}
    assert dialog.diff_title.text().startswith("src/app/util.py")


# ---- whole files --------------------------------------------------------------------
def test_staging_a_file_moves_it_to_the_staged_list(qapp, repo, slot_errors):
    (repo / "b.txt").write_bytes(b"changed\n")
    dialog = StagingDialog(str(repo))

    dialog.stage(dialog._entries(dialog.unstaged_list))

    assert _names(dialog.unstaged_list) == []
    assert _names(dialog.staged_list) == ["b.txt"]


def test_unstaging_a_file_moves_it_back(qapp, repo, slot_errors):
    (repo / "b.txt").write_bytes(b"changed\n")
    _git(repo, "add", "b.txt")
    dialog = StagingDialog(str(repo))

    dialog.unstage(dialog._entries(dialog.staged_list))

    assert _names(dialog.staged_list) == []
    assert _names(dialog.unstaged_list) == ["b.txt"]


def test_double_clicking_a_file_stages_it(qapp, repo, slot_errors):
    (repo / "b.txt").write_bytes(b"changed\n")
    dialog = StagingDialog(str(repo))
    item = dialog.unstaged_list.topLevelItem(0)

    dialog.unstaged_list.itemDoubleClicked.emit(item, 1)

    assert _names(dialog.staged_list) == ["b.txt"]


def test_with_apply_gitattributes_on_the_file_on_disk_gets_its_line_endings(
    qapp, repo, slot_errors
):
    (repo / "b.txt").write_bytes(b"one\r\ntwo\r\n")
    dialog = StagingDialog(str(repo))

    dialog.stage(dialog._entries(dialog.unstaged_list))

    assert (repo / "b.txt").read_bytes() == b"one\ntwo\n"
    assert _index(repo, "b.txt") == b"one\ntwo\n"


def test_with_apply_gitattributes_off_the_file_on_disk_is_left_alone(
    qapp, repo, slot_errors
):
    (repo / "b.txt").write_bytes(b"one\r\ntwo\r\n")
    dialog = StagingDialog(str(repo))
    dialog.apply_attributes.setChecked(False)

    dialog.stage(dialog._entries(dialog.unstaged_list))

    assert (repo / "b.txt").read_bytes() == b"one\r\ntwo\r\n"
    assert _index(repo, "b.txt") == b"one\ntwo\n", "git cleans the index regardless"


def test_when_a_file_moves_the_next_one_is_shown(qapp, repo, slot_errors):
    (repo / "a.txt").write_bytes(b"changed a\n")
    (repo / "b.txt").write_bytes(b"changed b\n")
    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")

    dialog.stage(dialog._selected(dialog.unstaged_list))

    assert "b.txt" in dialog.diff_title.text()


# ---- hunks and lines ----------------------------------------------------------------
def test_selected_lines_are_staged_and_the_file_stays_in_both_lists(
    qapp, repo, slot_errors
):
    (repo / "a.txt").write_bytes(
        BASE.replace(b"line 2\n", b"LINE 2\n").replace(b"line 3\n", b"LINE 3\n")
    )
    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")

    dialog.apply_lines(_line(dialog, "-line 2"), _line(dialog, "-line 2"))
    dialog.show_file("a.txt")
    dialog.apply_lines(_line(dialog, "+LINE 2"), _line(dialog, "+LINE 2"))

    assert _index(repo, "a.txt") == BASE.replace(b"line 2\n", b"LINE 2\n")
    assert "a.txt" in _names(dialog.unstaged_list)
    assert "a.txt" in _names(dialog.staged_list)


def test_a_hunk_is_staged_on_its_own(qapp, repo, slot_errors):
    (repo / "a.txt").write_bytes(
        BASE.replace(b"line 2\n", b"LINE 2\n").replace(b"line 18\n", b"LINE 18\n")
    )
    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")

    dialog.apply_hunk(_line(dialog, "+LINE 18"))

    assert _index(repo, "a.txt") == BASE.replace(b"line 18\n", b"LINE 18\n")


def test_lines_are_unstaged_from_the_staged_diff(qapp, repo, slot_errors):
    (repo / "a.txt").write_bytes(
        BASE.replace(b"line 2\n", b"LINE 2\n").replace(b"line 3\n", b"LINE 3\n")
    )
    _git(repo, "add", "a.txt")
    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt", staged=True)

    # A range from "-line 3" to "+LINE 3" would take "+LINE 2" with it, so the
    # pair comes back out one line at a time.
    dialog.apply_lines(_line(dialog, "+LINE 3"), _line(dialog, "+LINE 3"))
    dialog.show_file("a.txt", staged=True)
    dialog.apply_lines(_line(dialog, "-line 3"), _line(dialog, "-line 3"))

    assert _index(repo, "a.txt") == BASE.replace(b"line 2\n", b"LINE 2\n")


def test_a_selection_with_no_changes_in_it_stages_nothing(qapp, repo, slot_errors):
    (repo / "a.txt").write_bytes(BASE.replace(b"line 5\n", b"LINE 5\n"))
    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")

    dialog.apply_lines(_line(dialog, " line 4"), _line(dialog, " line 4"))

    assert _index(repo, "a.txt") == BASE
    assert "no added or removed lines" in dialog.status.text()


def test_a_new_file_stages_whole_and_says_so(qapp, repo, slot_errors):
    (repo / "new.txt").write_bytes(b"fresh\n")
    dialog = StagingDialog(str(repo))
    dialog.show_file("new.txt")

    assert "whole file" in dialog.diff_title.text()
    dialog.apply_lines(0, 10)  # nothing to take apart, so nothing happens
    assert _names(dialog.staged_list) == []


def test_a_selection_git_could_not_apply_is_explained_not_raised(
    qapp, repo, slot_errors
):
    _git(repo, "rm", "-q", "--cached", "b.txt")
    (repo / "b.txt").write_bytes(b"a\nb")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "no newline at the end")
    (repo / "b.txt").write_bytes(b"a\nb\nc\n")
    dialog = StagingDialog(str(repo))
    dialog.show_file("b.txt")

    dialog.apply_lines(_line(dialog, "+c"), _line(dialog, "+c"))

    assert "last line" in dialog.status.text()
    assert _index(repo, "b.txt") == b"a\nb"


def test_normalizing_first_does_not_spoil_a_selection_of_lines(qapp, repo, slot_errors):
    """The Phase 0 invariant, through the window: CRLF on disk, eol=lf declared."""
    edited = BASE.replace(b"line 2\n", b"LINE 2\n").replace(b"line 9\n", b"LINE 9\n")
    (repo / "a.txt").write_bytes(edited.replace(b"\n", b"\r\n"))
    dialog = StagingDialog(str(repo))
    dialog.show_file("a.txt")

    dialog.apply_lines(_line(dialog, "-line 9"), _line(dialog, "+LINE 9"))

    assert _index(repo, "a.txt") == BASE.replace(b"line 9\n", b"LINE 9\n")
    assert (repo / "a.txt").read_bytes() == edited, "line endings applied on disk"
    assert git_ops.status_entries(repo)[0].unstaged, "LINE 2 is still to stage"
