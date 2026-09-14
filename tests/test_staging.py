"""Staging and unstaging part of a file: a hunk, or a few lines of one.

The unit tests pin the rules on literal diffs. The rest hand the patch to git
itself and read the index back byte for byte -- a patch is only right if git
applies it and ends up where the lines chosen say it should.
"""

import subprocess
import sys

import pytest

from git_assistant import git_ops
from git_assistant.staging import PartialPatchError, build_patch, line_numbers, parse

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "core.autocrlf", "false")
    return path


def _commit(repo, name, data):
    (repo / name).write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")


def _index(repo, name):
    return _git(repo, "show", f":{name}").stdout


def _lines_of(diff, *texts):
    """The diff line numbers of lines whose text is exactly one of ``texts``."""
    numbers = []
    for text in texts:
        found = [
            number
            for hunk in diff.hunks
            for number, line in zip(hunk.numbers(), hunk.lines)
            if line == text
        ]
        assert len(found) == 1, f"{text!r} is not exactly one line of the diff"
        numbers.append(found[0])
    return set(numbers)


def _stage(repo, name, *texts):
    diff = parse(git_ops.file_diff(repo, name))
    patch = build_patch(diff, _lines_of(diff, *texts))
    result = git_ops.apply_to_index(repo, patch)
    assert result.ok, result.stderr


def _unstage(repo, name, *texts):
    diff = parse(git_ops.file_diff(repo, name, staged=True))
    patch = build_patch(diff, _lines_of(diff, *texts), reverse=True)
    result = git_ops.apply_to_index(repo, patch, reverse=True)
    assert result.ok, result.stderr


# ---- taking a diff apart -------------------------------------------------------------
TWO_HUNKS = (
    b"diff --git a/f.txt b/f.txt\n"
    b"index 1111111..2222222 100644\n"
    b"--- a/f.txt\n"
    b"+++ b/f.txt\n"
    b"@@ -1,3 +1,3 @@\n"
    b" one\n"
    b"-two\n"
    b"+TWO\n"
    b" three\n"
    b"@@ -10,2 +10,3 @@ def later():\n"
    b" ten\n"
    b"+ten and a half\n"
    b" eleven\n"
)


def test_lines_are_numbered_as_they_appear_in_the_diff():
    diff = parse(TWO_HUNKS)
    shown = TWO_HUNKS.split(b"\n")

    assert [hunk.at for hunk in diff.hunks] == [4, 9]
    for hunk in diff.hunks:
        for number, line in zip(hunk.numbers(), hunk.lines):
            assert shown[number] == line
    assert diff.partial


def test_each_line_is_numbered_in_the_old_file_and_in_the_new():
    """Git Extensions' two margin columns: removed lines are only in the old file,
    added ones only in the new, and header and @@ lines are in neither."""
    assert line_numbers(parse(TWO_HUNKS)) == {
        5: (1, 1),  # " one"
        6: (2, None),  # "-two"
        7: (None, 2),  # "+TWO"
        8: (3, 3),  # " three"
        10: (10, 10),  # " ten"
        11: (None, 11),  # "+ten and a half"
        12: (11, 12),  # " eleven"
    }


def test_a_new_file_has_only_new_line_numbers():
    raw = (
        b"diff --git a/n.txt b/n.txt\nnew file mode 100644\n--- /dev/null\n"
        b"+++ b/n.txt\n@@ -0,0 +1,2 @@\n+first\n+second\n"
    )
    assert line_numbers(parse(raw)) == {5: (None, 1), 6: (None, 2)}


def test_the_no_newline_notice_is_not_a_line_of_either_file():
    raw = b"--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n"
    assert line_numbers(parse(raw)) == {3: (1, None), 5: (None, 1)}


def test_a_diff_with_no_hunks_has_no_line_numbers():
    raw = b"diff --git a/x.png b/x.png\nBinary files a/x.png and b/x.png differ\n"
    assert line_numbers(parse(raw)) == {}


def test_a_line_knows_its_hunk_and_a_range_knows_its_changes():
    diff = parse(TWO_HUNKS)

    assert diff.hunk_at(4) is diff.hunks[0]  # the @@ line itself
    assert diff.hunk_at(12) is diff.hunks[1]
    assert diff.hunk_at(2) is None  # a header line
    assert diff.changes_between(0, 12) == {6, 7, 11}
    assert diff.changes_between(8, 5) == {6, 7}  # dragged upwards


@pytest.mark.parametrize(
    "header",
    [
        b"new file mode 100644",
        b"deleted file mode 100644",
        b"rename from old.txt",
        b"old mode 100644",
        b"Binary files a/x.png and b/x.png differ",
    ],
)
def test_some_diffs_can_only_be_staged_whole(header):
    raw = b"diff --git a/x b/x\n" + header + b"\n@@ -1 +1 @@\n-a\n+b\n"

    diff = parse(raw)

    assert not diff.partial
    with pytest.raises(PartialPatchError):
        build_patch(diff, {4})


def test_choosing_no_change_builds_no_patch():
    diff = parse(TWO_HUNKS)
    assert build_patch(diff, set()) is None
    assert build_patch(diff, {5, 8}) is None  # context lines are not changes


def test_staging_keeps_removals_not_chosen_and_drops_additions_not_chosen():
    raw = (
        b"--- a/f\n+++ b/f\n"
        b"@@ -1,4 +1,4 @@\n"
        b" keep\n-gone one\n-gone two\n+new one\n+new two\n keep\n"
    )
    diff = parse(raw)

    patch = build_patch(diff, _lines_of(diff, b"-gone one", b"+new one"))

    # "new one" replaces "gone one" where it stood, ahead of the line kept.
    assert patch == (
        b"--- a/f\n+++ b/f\n"
        b"@@ -1,4 +1,4 @@\n"
        b" keep\n-gone one\n+new one\n gone two\n keep\n"
    )


def test_unstaging_swaps_which_kind_is_kept_and_which_goes():
    raw = (
        b"--- a/f\n+++ b/f\n"
        b"@@ -1,4 +1,4 @@\n"
        b" keep\n-gone one\n-gone two\n+new one\n+new two\n keep\n"
    )
    diff = parse(raw)

    patch = build_patch(diff, _lines_of(diff, b"-gone one", b"+new one"), reverse=True)

    assert patch == (
        b"--- a/f\n+++ b/f\n"
        b"@@ -1,4 +1,4 @@\n"
        b" keep\n-gone one\n+new one\n new two\n keep\n"
    )


def test_a_missing_newline_mid_file_is_refused_rather_than_written():
    """Only the added line chosen: the old last line would stay unterminated."""
    raw = (
        b"--- a/f\n+++ b/f\n"
        b"@@ -1,2 +1,3 @@\n"
        b" a\n-b\n\\ No newline at end of file\n+b\n+c\n"
    )
    diff = parse(raw)

    with pytest.raises(PartialPatchError):
        build_patch(diff, _lines_of(diff, b"+c"))


# ---- what git makes of it ------------------------------------------------------------
BASE = b"".join(b"line %d\n" % n for n in range(1, 21))


def test_one_changed_line_of_two_is_staged(repo):
    _commit(repo, "f.txt", BASE)
    edited = BASE.replace(b"line 2\n", b"LINE 2\n").replace(b"line 3\n", b"LINE 3\n")
    (repo / "f.txt").write_bytes(edited)

    _stage(repo, "f.txt", b"-line 2", b"+LINE 2")

    assert _index(repo, "f.txt") == BASE.replace(b"line 2\n", b"LINE 2\n")
    assert (repo / "f.txt").read_bytes() == edited, "the file on disk is left alone"


REPLACED = BASE.replace(b"line 2\nline 3\nline 4\n", b"LINE 2\nLINE 3\nLINE 4\n")


@pytest.mark.parametrize("which", [2, 3, 4])
def test_any_one_line_of_a_replaced_run_is_staged_in_its_place(repo, which):
    """Git lists a replaced run as all removals then all additions; staging one
    pair from it must not move the new line past the old ones left alone."""
    _commit(repo, "f.txt", BASE)
    (repo / "f.txt").write_bytes(REPLACED)

    _stage(repo, "f.txt", b"-line %d" % which, b"+LINE %d" % which)

    expected = BASE.replace(b"line %d\n" % which, b"LINE %d\n" % which)
    assert _index(repo, "f.txt") == expected


@pytest.mark.parametrize("which", [2, 3, 4])
def test_any_one_line_of_a_staged_replaced_run_is_unstaged_in_its_place(repo, which):
    _commit(repo, "f.txt", BASE)
    (repo / "f.txt").write_bytes(REPLACED)
    git_ops.stage_paths(repo, ["f.txt"])

    _unstage(repo, "f.txt", b"-line %d" % which, b"+LINE %d" % which)

    expected = REPLACED.replace(b"LINE %d\n" % which, b"line %d\n" % which)
    assert _index(repo, "f.txt") == expected


def _two(n):
    return b"line %d" % n, b"LINE %d" % n


# A selection in the view is one unbroken range, so inside a replaced run the two
# halves of a pair are chosen a step apart. Each sequence below is how that goes,
# and where the lines must end up at every step.
def test_staging_a_removal_then_its_addition(repo):
    _commit(repo, "f.txt", BASE)
    (repo / "f.txt").write_bytes(REPLACED)

    _stage(repo, "f.txt", b"-line 2")
    _stage(repo, "f.txt", b"+LINE 2")

    assert _index(repo, "f.txt") == BASE.replace(b"line 2\n", b"LINE 2\n")


def test_staging_an_addition_then_its_removal(repo):
    _commit(repo, "f.txt", BASE)
    (repo / "f.txt").write_bytes(REPLACED)

    _stage(repo, "f.txt", b"+LINE 3")
    assert _index(repo, "f.txt") == BASE.replace(b"line 3\n", b"LINE 3\nline 3\n")
    _stage(repo, "f.txt", b"-line 3")

    assert _index(repo, "f.txt") == BASE.replace(b"line 3\n", b"LINE 3\n")


def test_unstaging_a_removal_puts_the_line_back_where_it_was(repo):
    _commit(repo, "f.txt", BASE)
    (repo / "f.txt").write_bytes(REPLACED)
    git_ops.stage_paths(repo, ["f.txt"])

    _unstage(repo, "f.txt", b"-line 3")

    assert _index(repo, "f.txt") == REPLACED.replace(b"LINE 3\n", b"line 3\nLINE 3\n")


def test_unstaging_an_addition_then_its_removal(repo):
    _commit(repo, "f.txt", BASE)
    (repo / "f.txt").write_bytes(REPLACED)
    git_ops.stage_paths(repo, ["f.txt"])

    _unstage(repo, "f.txt", b"+LINE 3")
    _unstage(repo, "f.txt", b"-line 3")

    assert _index(repo, "f.txt") == REPLACED.replace(b"LINE 3\n", b"line 3\n")


def test_a_later_hunk_is_staged_on_its_own(repo):
    """With the hunk before it left out, its position has to come out right."""
    _commit(repo, "f.txt", BASE)
    edited = (
        BASE.replace(b"line 2\n", b"line 2\nextra A\nextra B\n")
        .replace(b"line 18\n", b"LINE 18\n")
    )
    (repo / "f.txt").write_bytes(edited)

    _stage(repo, "f.txt", b"-line 18", b"+LINE 18")

    assert _index(repo, "f.txt") == BASE.replace(b"line 18\n", b"LINE 18\n")


def test_both_hunks_staged_one_after_the_other_add_up_to_the_file(repo):
    _commit(repo, "f.txt", BASE)
    edited = (
        BASE.replace(b"line 2\n", b"line 2\nextra A\n")
        .replace(b"line 18\n", b"LINE 18\n")
    )
    (repo / "f.txt").write_bytes(edited)

    _stage(repo, "f.txt", b"+extra A")
    _stage(repo, "f.txt", b"-line 18", b"+LINE 18")

    assert _index(repo, "f.txt") == edited


def test_some_added_and_removed_lines_of_one_hunk_are_staged(repo):
    _commit(repo, "f.txt", BASE)
    edited = BASE.replace(b"line 5\nline 6\n", b"new five\nnew six\nnew seven\n")
    (repo / "f.txt").write_bytes(edited)

    _stage(repo, "f.txt", b"-line 6", b"+new seven")

    assert _index(repo, "f.txt") == BASE.replace(b"line 6\n", b"new seven\n")


def test_lines_are_added_at_the_very_top_of_a_file(repo):
    _commit(repo, "f.txt", BASE)
    (repo / "f.txt").write_bytes(b"first\nsecond\n" + BASE)

    _stage(repo, "f.txt", b"+second")

    assert _index(repo, "f.txt") == b"second\n" + BASE


def test_one_staged_line_is_unstaged_and_the_rest_stays(repo):
    _commit(repo, "f.txt", BASE)
    edited = BASE.replace(b"line 2\n", b"LINE 2\n").replace(b"line 3\n", b"LINE 3\n")
    (repo / "f.txt").write_bytes(edited)
    git_ops.stage_paths(repo, ["f.txt"])

    _unstage(repo, "f.txt", b"-line 3", b"+LINE 3")

    assert _index(repo, "f.txt") == BASE.replace(b"line 2\n", b"LINE 2\n")


def test_a_later_staged_hunk_is_unstaged_on_its_own(repo):
    _commit(repo, "f.txt", BASE)
    edited = (
        BASE.replace(b"line 2\n", b"line 2\nextra A\nextra B\n")
        .replace(b"line 18\n", b"LINE 18\n")
    )
    (repo / "f.txt").write_bytes(edited)
    git_ops.stage_paths(repo, ["f.txt"])

    _unstage(repo, "f.txt", b"-line 18", b"+LINE 18")

    expected = BASE.replace(b"line 2\n", b"line 2\nextra A\nextra B\n")
    assert _index(repo, "f.txt") == expected


def test_a_crlf_file_keeps_its_carriage_returns_through_a_partial_stage(repo):
    crlf = BASE.replace(b"\n", b"\r\n")
    _commit(repo, "f.txt", crlf)
    edited = (
        crlf.replace(b"line 2\r\n", b"LINE 2\r\n")
        .replace(b"line 9\r\n", b"LINE 9\r\n")
    )
    (repo / "f.txt").write_bytes(edited)

    _stage(repo, "f.txt", b"-line 9\r", b"+LINE 9\r")

    assert _index(repo, "f.txt") == crlf.replace(b"line 9\r\n", b"LINE 9\r\n")


def test_a_last_line_without_a_newline_is_staged_with_the_lines_it_needs(repo):
    _commit(repo, "f.txt", b"a\nb")
    (repo / "f.txt").write_bytes(b"a\nb\nc\n")

    _stage(repo, "f.txt", b"-b", b"+b", b"+c")

    assert _index(repo, "f.txt") == b"a\nb\nc\n"
