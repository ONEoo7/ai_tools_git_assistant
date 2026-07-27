from git_assistant.diff_strategy import (
    FileDiff,
    build_units,
    build_units_with_coverage,
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
