from git_assistant.diff_strategy import (
    FileDiff,
    build_units,
    filter_files,
    pack_units,
    split_diff,
    split_into_hunks,
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
