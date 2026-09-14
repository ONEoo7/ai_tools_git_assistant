"""Staging and line endings, against real repositories.

Nothing is mocked. What gets staged, what a diff is byte for byte, and what
.gitattributes turns a file into are all git's to decide, and a stub would agree
with whatever the code under test already assumed.
"""

import os
import subprocess
import sys

import pytest

from git_assistant import git_ops

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

CRLF = b"one\r\ntwo\r\n"
LF = b"one\ntwo\n"


def _git(repo, *args, stdin=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin,
        capture_output=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A repository whose line endings answer to its own config and nothing else."""
    path = tmp_path / "work"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    # Set here, where it outranks the machine's own settings, so this machine's
    # autocrlf cannot change what these tests see.
    _git(path, "config", "core.autocrlf", "false")
    _git(path, "config", "core.safecrlf", "false")
    return path


def _commit(repo, files, message="base"):
    for name, data in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _by_path(repo):
    return {entry.path: entry for entry in git_ops.status_entries(repo)}


# ---- what has changed ------------------------------------------------------------
def test_a_clean_repository_has_nothing_to_report(repo):
    _commit(repo, {"a.txt": b"a\n"})
    assert git_ops.status_entries(repo) == []


def test_every_kind_of_change_is_reported(repo):
    _commit(
        repo,
        {
            "old name.txt": b"rename me\n" * 5,
            "gone.txt": b"g\n",
            "part.txt": b"a\nb\nc\n",
            "ünïcode.txt": b"u\n",
        },
    )
    _git(repo, "mv", "old name.txt", "new name.txt")
    (repo / "gone.txt").unlink()
    (repo / "part.txt").write_bytes(b"A\nb\nc\n")
    _git(repo, "add", "part.txt")
    (repo / "part.txt").write_bytes(b"A\nb\nC\n")
    (repo / "ünïcode.txt").write_bytes(b"U\n")
    (repo / "new dir").mkdir()
    (repo / "new dir" / "fresh.txt").write_bytes(b"f\n")

    entries = _by_path(repo)

    assert set(entries) == {
        "new name.txt",
        "gone.txt",
        "part.txt",
        "ünïcode.txt",
        "new dir/fresh.txt",
    }
    renamed = entries["new name.txt"]
    assert renamed.orig_path == "old name.txt"
    assert renamed.staged and not renamed.unstaged
    assert renamed.paths == ["new name.txt", "old name.txt"]
    assert entries["gone.txt"].worktree == "D" and entries["gone.txt"].unstaged
    assert entries["part.txt"].staged and entries["part.txt"].unstaged
    assert entries["ünïcode.txt"].unstaged and not entries["ünïcode.txt"].staged
    fresh = entries["new dir/fresh.txt"]
    assert fresh.untracked and fresh.unstaged and not fresh.staged


def test_a_partly_staged_file_counts_once_as_changed_and_once_as_unstaged(repo):
    _commit(repo, {"part.txt": b"a\nb\n", "staged.txt": b"s\n"})
    (repo / "part.txt").write_bytes(b"A\nb\n")
    _git(repo, "add", "part.txt")
    (repo / "part.txt").write_bytes(b"A\nB\n")
    (repo / "staged.txt").write_bytes(b"S\n")
    _git(repo, "add", "staged.txt")
    (repo / "new.txt").write_bytes(b"n\n")

    # part.txt and new.txt have unstaged work; all three have changed.
    assert git_ops.unstaged_counts(git_ops.status_entries(repo)) == (2, 3)


def test_a_submodule_is_marked_as_one(repo, tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    _git(inner, "init", "-q")
    _git(inner, "config", "user.email", "test@example.com")
    _git(inner, "config", "user.name", "Test")
    _commit(inner, {"s.txt": b"s\n"})
    _commit(repo, {"a.txt": b"a\n"})
    _git(
        repo, "-c", "protocol.file.allow=always",
        "submodule", "add", "-q", str(inner), "libs/inner",
    )

    entries = _by_path(repo)

    assert entries["libs/inner"].submodule
    assert not entries[".gitmodules"].submodule


# ---- staging and unstaging whole files ----------------------------------------------
def test_staging_takes_edits_new_files_and_deletions_alike(repo):
    _commit(repo, {"edit.txt": b"a\n", "gone.txt": b"g\n"})
    (repo / "edit.txt").write_bytes(b"b\n")
    (repo / "gone.txt").unlink()
    (repo / "new.txt").write_bytes(b"n\n")

    assert git_ops.stage_paths(repo, ["edit.txt", "gone.txt", "new.txt"]).ok

    entries = _by_path(repo)
    assert set(entries) == {"edit.txt", "gone.txt", "new.txt"}
    assert all(e.staged and not e.unstaged for e in entries.values())


def test_a_name_that_looks_like_a_pattern_is_taken_literally(repo):
    """As a pattern, "[a].txt" matches a.txt -- which must not be swept up."""
    _commit(repo, {"a.txt": b"a\n"})
    (repo / "[a].txt").write_bytes(b"x\n")
    (repo / "a.txt").write_bytes(b"changed\n")

    assert git_ops.stage_paths(repo, ["[a].txt"]).ok

    entries = _by_path(repo)
    assert entries["[a].txt"].staged
    assert not entries["a.txt"].staged


def test_unstaging_leaves_the_file_on_disk_alone(repo):
    _commit(repo, {"edit.txt": b"a\n"})
    (repo / "edit.txt").write_bytes(b"b\n")
    git_ops.stage_paths(repo, ["edit.txt"])

    assert git_ops.unstage_paths(repo, ["edit.txt"]).ok

    entry = _by_path(repo)["edit.txt"]
    assert entry.unstaged and not entry.staged
    assert (repo / "edit.txt").read_bytes() == b"b\n"


def test_a_new_file_can_be_unstaged_before_the_first_commit(repo):
    (repo / "first.txt").write_bytes(b"f\n")
    git_ops.stage_paths(repo, ["first.txt"])
    assert not git_ops.has_head(repo)

    assert git_ops.unstage_paths(repo, ["first.txt"]).ok

    assert _by_path(repo)["first.txt"].untracked


def test_a_staged_rename_is_unstaged_whole(repo):
    _commit(repo, {"old.txt": b"x\n" * 5})
    _git(repo, "mv", "old.txt", "new.txt")
    renamed = _by_path(repo)["new.txt"]

    assert git_ops.unstage_paths(repo, renamed.paths).ok

    entries = _by_path(repo)
    assert entries["old.txt"].worktree == "D" and not entries["old.txt"].staged
    assert entries["new.txt"].untracked


# ---- one file's diff -----------------------------------------------------------------
def test_a_diff_keeps_its_carriage_returns(repo):
    _commit(repo, {"crlf.txt": CRLF})
    (repo / "crlf.txt").write_bytes(b"one\r\nTWO\r\n")

    diff = git_ops.file_diff(repo, "crlf.txt")

    assert b"-two\r\n" in diff
    assert b"+TWO\r\n" in diff


def test_staged_and_unstaged_diffs_are_the_two_halves_of_a_change(repo):
    _commit(repo, {"f.txt": b"a\nb\n"})
    (repo / "f.txt").write_bytes(b"A\nb\n")
    git_ops.stage_paths(repo, ["f.txt"])
    (repo / "f.txt").write_bytes(b"A\nB\n")

    staged = git_ops.file_diff(repo, "f.txt", staged=True)
    unstaged = git_ops.file_diff(repo, "f.txt")

    assert b"+A\n" in staged and b"+B\n" not in staged
    assert b"+B\n" in unstaged and b"+A\n" not in unstaged


def test_an_untracked_file_diffs_as_all_added(repo):
    _commit(repo, {"base.txt": b"b\n"})
    (repo / "new dir").mkdir()
    (repo / "new dir" / "fresh.txt").write_bytes(b"one\ntwo\n")

    diff = git_ops.file_diff(repo, "new dir/fresh.txt", untracked=True)

    assert b"+one\n+two\n" in diff


def test_a_diff_applies_whatever_the_diff_config_says(repo):
    """noprefix would leave `git apply` nothing to strip; colour, nothing to parse."""
    _commit(repo, {"f.txt": b"a\n"})
    _git(repo, "config", "diff.noprefix", "true")
    _git(repo, "config", "color.diff", "always")
    (repo / "f.txt").write_bytes(b"b\n")

    patch = git_ops.file_diff(repo, "f.txt")

    assert git_ops.apply_to_index(repo, patch).ok
    assert _by_path(repo)["f.txt"].staged


def test_applying_in_reverse_takes_a_staged_change_back_out(repo):
    _commit(repo, {"f.txt": b"a\n"})
    (repo / "f.txt").write_bytes(b"b\n")
    git_ops.stage_paths(repo, ["f.txt"])
    staged = git_ops.file_diff(repo, "f.txt", staged=True)

    assert git_ops.apply_to_index(repo, staged, reverse=True).ok

    entry = _by_path(repo)["f.txt"]
    assert entry.unstaged and not entry.staged


def test_a_patch_to_a_crlf_file_applies(repo):
    """The whole reason diffs are kept as bytes: text mode would strip the CRs."""
    _commit(repo, {"crlf.txt": b"a\r\nb\r\nc\r\n"})
    (repo / "crlf.txt").write_bytes(b"A\r\nb\r\nC\r\n")

    assert git_ops.apply_to_index(repo, git_ops.file_diff(repo, "crlf.txt")).ok
    assert _git(repo, "cat-file", "-p", ":crlf.txt").stdout == b"A\r\nb\r\nC\r\n"


# ---- line endings --------------------------------------------------------------------
def test_which_files_have_a_line_ending_rule(repo):
    (repo / ".gitattributes").write_bytes(
        b"*.txt text\n"
        b"*.auto text=auto\n"
        b"*.lf eol=lf\n"
        b"*.bin binary\n"
        b"*.raw -text\n"
        b"*.big filter=lfs diff=lfs merge=lfs -text\n"
        b"*.lfstext filter=lfs text\n"
    )
    names = [
        "a.txt", "a.auto", "a.lf", "a.bin", "a.raw", "a.big", "a.lfstext", "a.none"
    ]

    assert git_ops.line_ending_rules(repo, names) == {"a.txt", "a.auto", "a.lf"}


@pytest.mark.parametrize(
    "rule, name, on_disk, expected",
    [
        ("*.txt text eol=lf", "a.txt", CRLF, LF),
        ("*.bat text eol=crlf", "run.bat", LF, CRLF),
        ("* text=auto eol=lf", "with space ü.txt", CRLF, LF),
    ],
)
def test_line_endings_become_what_gitattributes_says(
    repo, rule, name, on_disk, expected
):
    (repo / ".gitattributes").write_bytes(rule.encode() + b"\n")
    (repo / name).write_bytes(on_disk)

    done = git_ops.normalize_line_endings(repo, [name])

    assert done.changed == [name]
    assert (repo / name).read_bytes() == expected


def test_a_binary_file_under_text_auto_is_left_as_it_is(repo):
    (repo / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    blob = b"\x00\x01\r\n\x02\n"
    (repo / "image.dat").write_bytes(blob)

    done = git_ops.normalize_line_endings(repo, ["image.dat"])

    assert done.unchanged == ["image.dat"]
    assert (repo / "image.dat").read_bytes() == blob


def test_files_without_a_rule_are_not_touched(repo):
    """Including one a filter such as Git LFS owns, whatever else it declares."""
    (repo / ".gitattributes").write_bytes(b"*.raw -text\n*.big filter=lfs text\n")
    names = ["a.raw", "a.big", "a.none"]
    for name in names:
        (repo / name).write_bytes(CRLF)

    done = git_ops.normalize_line_endings(repo, names)

    assert sorted(done.skipped) == sorted(names)
    assert all((repo / name).read_bytes() == CRLF for name in names)


def test_a_file_already_right_is_not_rewritten(repo):
    (repo / ".gitattributes").write_bytes(b"*.txt text eol=lf\n")
    (repo / "a.txt").write_bytes(LF)
    before = os.stat(repo / "a.txt").st_mtime_ns

    done = git_ops.normalize_line_endings(repo, ["a.txt"])

    assert done.unchanged == ["a.txt"]
    assert os.stat(repo / "a.txt").st_mtime_ns == before


def test_a_file_that_is_not_on_disk_is_skipped(repo):
    (repo / ".gitattributes").write_bytes(b"*.txt text eol=lf\n")

    assert git_ops.normalize_line_endings(repo, ["gone.txt"]).skipped == ["gone.txt"]


def test_normalizing_never_changes_what_git_diff_shows(repo):
    """Which is what lets a selection of lines survive being normalized under."""
    _commit(
        repo, {".gitattributes": b"*.txt text eol=lf\n", "a.txt": b"one\ntwo\nthree\n"}
    )
    (repo / "a.txt").write_bytes(b"one\r\nTWO\r\nthree\r\nfour\r\n")
    before = git_ops.file_diff(repo, "a.txt")

    git_ops.normalize_line_endings(repo, ["a.txt"])

    assert git_ops.file_diff(repo, "a.txt") == before
    assert (repo / "a.txt").read_bytes() == b"one\nTWO\nthree\nfour\n"


def test_endings_committed_before_the_rule_are_fixed_by_normalizing_then_staging(
    repo,
):
    _commit(repo, {"a.txt": CRLF})
    (repo / ".gitattributes").write_bytes(b"*.txt text eol=lf\n")

    git_ops.normalize_line_endings(repo, ["a.txt"])
    git_ops.stage_paths(repo, ["a.txt"])

    assert _git(repo, "cat-file", "-p", ":a.txt").stdout == LF
    assert (repo / "a.txt").read_bytes() == LF


def test_safecrlf_does_not_stop_a_conversion_that_was_asked_for(repo):
    _git(repo, "config", "core.safecrlf", "true")
    (repo / ".gitattributes").write_bytes(b"*.txt text eol=lf\n")
    (repo / "mixed.txt").write_bytes(b"one\r\ntwo\nthree\r\n")

    done = git_ops.normalize_line_endings(repo, ["mixed.txt"])

    assert done.changed == ["mixed.txt"]
    assert (repo / "mixed.txt").read_bytes() == b"one\ntwo\nthree\n"


def test_many_files_are_normalized_together(repo):
    (repo / ".gitattributes").write_bytes(b"*.txt text eol=lf\n")
    names = [f"f{i:02}.txt" for i in range(20)]
    for name in names:
        (repo / name).write_bytes(CRLF)

    done = git_ops.normalize_line_endings(repo, names)

    assert done.changed == names
    assert all((repo / name).read_bytes() == LF for name in names)


# ---- reading a file's line endings ---------------------------------------------------
def test_each_copy_of_a_file_has_its_own_line_endings(repo):
    """Git normalized the index when it was committed; the editor did not."""
    _commit(repo, {".gitattributes": b"*.txt text eol=lf\n", "a.txt": LF})
    (repo / "a.txt").write_bytes(CRLF)

    endings = git_ops.line_endings(repo, "a.txt")

    assert endings == git_ops.LineEndings("lf", "crlf", "text eol=lf")
    assert endings.declared == "lf"


def test_a_new_file_has_no_index_copy_and_a_deleted_one_no_file(repo):
    _commit(repo, {"gone.txt": LF})
    (repo / "gone.txt").unlink()
    (repo / "new file.txt").write_bytes(CRLF)

    assert git_ops.line_endings(repo, "gone.txt") == git_ops.LineEndings("lf", "", "")
    assert git_ops.line_endings(repo, "new file.txt") == git_ops.LineEndings(
        "", "crlf", ""
    )


@pytest.mark.parametrize(
    "data, word",
    [(b"a\r\nb\n", "mixed"), (b"no line break", "none"), (b"\x00\x01\r\n", "-text")],
)
def test_git_says_mixed_none_or_binary(repo, data, word):
    _commit(repo, {"f.dat": data})

    endings = git_ops.line_endings(repo, "f.dat")

    assert (endings.index, endings.worktree) == (word, word)
    assert endings.declared == ""


def test_a_rule_that_leaves_the_ending_to_the_machine_declares_none(repo):
    _commit(repo, {".gitattributes": b"* text=auto\n", "a.txt": LF})

    assert git_ops.line_endings(repo, "a.txt").declared == ""


def test_a_path_git_does_not_know_has_no_line_endings(repo):
    _commit(repo, {"a.txt": LF})

    assert git_ops.line_endings(repo, "nothing-here.txt") is None


def test_many_paths_are_asked_about_in_command_lines_that_fit(repo, monkeypatch):
    names = [f"file-{n:02}.txt" for n in range(12)]
    _commit(repo, {name: LF for name in names})
    monkeypatch.setattr(git_ops, "_ARGUMENTS_BUDGET", 40)  # two or three names a call

    found = git_ops.line_endings_of(repo, names)

    assert sorted(found) == names


# ---- what applying .gitattributes would rewrite --------------------------------------
def test_a_file_on_disk_against_its_rule_is_one_to_rewrite(repo):
    _commit(repo, {".gitattributes": b"*.txt text eol=lf\n", "a.txt": LF, "b.txt": LF})
    (repo / "a.txt").write_bytes(CRLF)

    changes = git_ops.line_ending_changes(repo, ["a.txt", "b.txt"])

    assert changes == {"a.txt": git_ops.EndingChange("crlf", "lf")}


def test_what_would_be_rewritten_is_exactly_what_normalizing_rewrites(repo):
    """Every kind of file at once, then the real thing, and the two compared.

    Includes rules that name no ending, where this machine's config decides and
    git's words for the file cannot, and a file whose index copy already has
    carriage returns from before its rule was declared.
    """
    _git(repo, "config", "core.eol", "lf")  # what "text" with no eol means here
    _commit(repo, {"legacy.auto": CRLF})  # carriage returns in the index
    _commit(
        repo,
        {
            ".gitattributes": (
                b"*.lf text eol=lf\n*.crlf text eol=crlf\n*.auto text=auto\n"
                b"*.bin binary\n"
            ),
            "ok.lf": LF,
            "bad.lf": LF,
            "ok.crlf": CRLF,
            "bad.crlf": CRLF,
            "fresh.auto": LF,
            "raw.dat": LF,
            "blob.bin": b"\x00\x01\n",
        },
    )
    edits = {
        "bad.lf": CRLF,
        "bad.crlf": LF,
        "fresh.auto": CRLF,
        "legacy.auto": CRLF + b"three\r\n",
        "raw.dat": CRLF,
        "blob.bin": b"\x00\x02\r\n",
        "new.lf": b"new\r\nfile\r\n",
    }
    for name, data in edits.items():
        (repo / name).write_bytes(data)
    paths = sorted({*edits, "ok.lf", "ok.crlf"})

    predicted = git_ops.line_ending_changes(repo, paths)
    done = git_ops.normalize_line_endings(repo, paths)

    assert set(predicted) == set(done.changed)
    assert {"bad.lf", "bad.crlf", "fresh.auto", "new.lf"} <= set(predicted)
    assert not {"ok.lf", "ok.crlf", "raw.dat", "blob.bin"} & set(predicted)
    assert predicted["bad.crlf"] == git_ops.EndingChange("lf", "crlf")
