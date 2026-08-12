from git_assistant.diff_strategy import (
    BINARY,
    FileDiff,
    build_units,
    build_units_with_coverage,
    drop_reason,
    excerpt_included,
    filter_files,
    pack_units,
    split_diff,
    split_into_hunks,
    split_into_hunks_indexed,
    truncate_indexed,
    truncate_to_budget,
)

SAMPLE_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 import os
+import sys

 def main():
@@ -10,2 +11,3 @@ def main():
     run()
+    cleanup()
diff --git a/uv.lock b/uv.lock
index aaa..bbb 100644
--- a/uv.lock
+++ b/uv.lock
@@ -1,2 +1,2 @@
-version = 1
+version = 2
diff --git a/logo.png b/logo.png
index ccc..ddd 100644
Binary files a/logo.png and b/logo.png differ
"""


def _count(text: str) -> int:
    # Deterministic token proxy for tests: 1 token per whitespace word.
    return max(1, len(text.split()))


def test_split_diff_counts_and_paths():
    files = split_diff(SAMPLE_DIFF)
    assert [f.path for f in files] == ["src/app.py", "uv.lock", "logo.png"]


def test_filter_drops_lockfiles_and_binaries():
    files = split_diff(SAMPLE_DIFF)
    kept, dropped = filter_files(files, ["*.lock", "uv.lock"])
    assert [f.path for f in kept] == ["src/app.py"]
    assert set(dropped) == {"uv.lock", "logo.png"}


def test_split_into_hunks():
    files = split_diff(SAMPLE_DIFF)
    app = next(f for f in files if f.path == "src/app.py")
    hunks = split_into_hunks(app)
    assert len(hunks) == 2
    # Each hunk piece carries the file header.
    for h in hunks:
        assert h.startswith("diff --git a/src/app.py")
        assert "@@" in h


def test_build_units_splits_oversized_file():
    files = split_diff(SAMPLE_DIFF)
    app = next(f for f in files if f.path == "src/app.py")
    # Budget smaller than whole file but larger than a single hunk -> 2 units.
    whole = _count(app.text)
    units = build_units([app], budget=whole - 1, count_tokens=_count)
    assert len(units) >= 2


def test_pack_units_respects_budget():
    units = ["a a a", "b b b", "c c c"]  # 3 tokens each with _count
    chunks = pack_units(units, budget=6, count_tokens=_count)
    # Two units (6 tokens) per chunk max.
    assert len(chunks) == 2


def test_truncate_to_budget_inserts_marker():
    text = "\n".join(f"line {i}" for i in range(50)) + "\n"
    out = truncate_to_budget(text, budget=10, count_tokens=_count)
    assert "lines truncated" in out
    assert _count(out) <= _count(text)


def test_truncate_noop_when_small():
    text = "diff --git a/x b/x\n@@\n+one\n"
    assert truncate_to_budget(text, budget=1000, count_tokens=_count) == text


def test_split_into_hunks_indexed_covers_all_lines():
    files = split_diff(SAMPLE_DIFF)
    app = next(f for f in files if f.path == "src/app.py")
    pieces = split_into_hunks_indexed(app)
    assert len(pieces) == 2
    n = len(app.text.splitlines(keepends=True))
    covered = set()
    for text, idxs in pieces:
        covered |= set(idxs)
        # the piece text is exactly the lines at those indices
        lines = app.text.splitlines(keepends=True)
        assert text == "".join(lines[i] for i in idxs)
    assert covered == set(range(n))  # every original line appears somewhere


def test_truncate_indexed_reports_kept_lines():
    text = "".join(f"line {i}\n" for i in range(50))
    out, kept = truncate_indexed(text, budget=10, count_tokens=_count)
    assert "lines truncated" in out
    n = len(text.splitlines(keepends=True))
    assert kept[-1] == n - 1          # tail line preserved
    assert len(kept) < n              # something was dropped


def test_truncate_indexed_keeps_all_when_small():
    text = "a\nb\nc\n"
    out, kept = truncate_indexed(text, budget=1000, count_tokens=_count)
    assert out == text
    assert kept == [0, 1, 2]


def test_coverage_empty_when_everything_fits():
    files, _ = filter_files(split_diff(SAMPLE_DIFF), ["*.lock"])
    units, omitted = build_units_with_coverage(files, budget=10_000, count_tokens=_count)
    assert units
    assert all(not v for v in omitted.values())


def test_coverage_reports_omitted_lines_when_truncating():
    big = FileDiff(
        path="big.py",
        text="diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n"
        + "@@ -1,1 +1,1 @@\n"
        + "".join(f"+line {i}\n" for i in range(200)),
    )
    units, omitted = build_units_with_coverage([big], budget=20, count_tokens=_count)
    assert units
    # Some lines could not be sent, and they are real indices of the file.
    n = len(big.text.splitlines(keepends=True))
    assert omitted["big.py"]
    assert all(0 <= i < n for i in omitted["big.py"])


# ---- un-ignoring one file --------------------------------------------------
#
# The ignore globs are a rule and are always obeyed. What comes back is only
# what somebody named by hand, and only its head. These cover both halves --
# what is taken back, and, just as much, what is not.


def _doc(path: str, body_lines: int) -> FileDiff:
    return FileDiff(
        path=path,
        text=(
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{body_lines} @@\n"
            + "".join(f"+page line {i}\n" for i in range(body_lines))
        ),
    )


def _one(doc: FileDiff, limit: int, included=None):
    asked = [doc.path] if included is None else included
    return excerpt_included([doc], [doc.path], asked, limit)


def test_an_un_ignored_file_is_cut_down_to_its_head():
    doc = _doc("docs/NIST.FIPS.180-4.pdf", 1250)
    [excerpt] = _one(doc, limit=200)
    assert excerpt.kept == 200
    assert excerpt.total == len(doc.text.splitlines(keepends=True))
    # The limit counts lines of the diff segment, headers included: five of
    # them here, so the body stops five short of the limit.
    assert "+page line 0\n" in excerpt.file.text
    assert "+page line 194\n" in excerpt.file.text
    assert "+page line 195\n" not in excerpt.file.text
    assert excerpt.source is doc


def test_the_cut_says_how_much_was_left_behind():
    # Without this the hunk header promises 1250 lines, 200 arrive, and the
    # file reads as stopping mid-sentence.
    doc = _doc("spec.pdf", 1250)
    [excerpt] = _one(doc, limit=200)
    total = len(doc.text.splitlines(keepends=True))
    assert (
        f"[... {total - 200} further lines of this file not sent ...]"
        in excerpt.file.text
    )


def test_a_short_file_is_kept_whole_and_unmarked():
    doc = _doc("note.pdf", 10)
    [excerpt] = _one(doc, limit=200)
    assert excerpt.file is doc
    assert excerpt.kept == excerpt.total
    assert "further lines" not in excerpt.file.text


def test_a_limit_of_zero_sends_the_whole_file():
    # Un-ignoring is already the opt-in, so there is nothing left for 0 to
    # switch off; it is the no-cap spelling every other limit here uses.
    doc = _doc("spec.pdf", 1250)
    for limit in (0, -1):
        [excerpt] = _one(doc, limit=limit)
        assert excerpt.file is doc
        assert excerpt.kept == excerpt.total


def test_a_file_nobody_asked_for_is_left_ignored():
    # The globs are a rule; this is the whole point of the redesign.
    doc = _doc("spec.pdf", 1250)
    assert excerpt_included([doc], ["spec.pdf"], [], 200) == []
    assert excerpt_included([doc], ["spec.pdf"], ["other.pdf"], 200) == []


def test_nothing_is_inferred_from_the_file_type():
    # A PDF nobody asked for is as ignored as a lock file, and a lock file
    # somebody did ask for comes back. Only the person committing decides.
    lock = _doc("uv.lock", 1250)
    [excerpt] = excerpt_included([lock], ["uv.lock"], ["uv.lock"], 200)
    assert excerpt.kept == 200


def test_a_file_git_could_not_diff_comes_back_empty_handed():
    # No text to take: the two lines git does emit say nothing the diffstat
    # has not, so asking for it changes nothing.
    binary = FileDiff(
        path="scan.pdf",
        text=(
            "diff --git a/scan.pdf b/scan.pdf\n"
            "Binary files a/scan.pdf and b/scan.pdf differ\n"
        ),
    )
    assert _one(binary, limit=200) == []


def test_a_file_that_survived_the_filter_is_not_excerpted():
    # It is already going in whole; excerpting it would send it twice.
    kept = _doc("keep.pdf", 1250)
    assert excerpt_included([kept], [], ["keep.pdf"], 200) == []


# ---- which rule dropped a file ---------------------------------------------
#
# "Omitted" is not something anyone can act on. Which glob matched is.


def test_the_glob_that_matched_is_the_one_reported():
    files = split_diff(SAMPLE_DIFF)
    lock = next(f for f in files if f.path == "uv.lock")
    assert drop_reason(lock, ["*.min.js", "*.lock", "uv.lock"]) == "*.lock"


def test_a_basename_match_counts_as_a_match():
    doc = _doc("deep/nested/spec.pdf", 3)
    assert drop_reason(doc, ["*.pdf"]) == "*.pdf"


def test_git_refusing_to_diff_is_reported_as_that_rather_than_a_glob():
    files = split_diff(SAMPLE_DIFF)
    png = next(f for f in files if f.path == "logo.png")
    # Even with a glob that would also have matched: binary is why nothing
    # could have been sent, and no edit to the ignore list changes it.
    assert drop_reason(png, ["*.png"]) == BINARY


def test_a_file_no_rule_touched_reports_nothing():
    files = split_diff(SAMPLE_DIFF)
    app = next(f for f in files if f.path == "src/app.py")
    assert drop_reason(app, ["*.lock"]) == ""


def test_the_reason_agrees_with_the_filter():
    """They apply the same two tests in the same order, and must not drift."""
    globs = ["*.lock", "*.png"]
    files = split_diff(SAMPLE_DIFF)
    _kept, dropped = filter_files(files, globs)
    for f in files:
        assert bool(drop_reason(f, globs)) == (f.path in dropped)
