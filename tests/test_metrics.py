import subprocess
import sys

import pytest

from git_assistant import metrics

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


def test_count_text():
    assert metrics.count_text("") == (0, 0)
    assert metrics.count_text("a\nb\nc\n") == (3, 0)
    assert metrics.count_text("a\n\n  \nb") == (4, 2)


def test_ext_of():
    assert metrics.ext_of("src/app.py") == ".py"
    assert metrics.ext_of("README.MD") == ".md"
    assert metrics.ext_of("Makefile") == "Makefile"
    assert metrics.ext_of("a/b/Dockerfile") == "Dockerfile"


def test_is_binary():
    assert metrics.is_binary(b"text\x00more")
    assert not metrics.is_binary(b"just text\n")


def test_aggregate_sums_across_repos():
    a = metrics.RepoMetrics(path="/a")
    a.by_ext[".py"] = metrics.LangStat(files=1, lines=10, blank=2)
    b = metrics.RepoMetrics(path="/b")
    b.by_ext[".py"] = metrics.LangStat(files=2, lines=5, blank=1)
    b.by_ext[".md"] = metrics.LangStat(files=1, lines=3, blank=0)
    agg = metrics.aggregate([a, b])
    assert agg[".py"].files == 3 and agg[".py"].lines == 15 and agg[".py"].blank == 3
    assert agg[".py"].code == 12
    assert agg[".md"].lines == 3


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@e.com")
    _git(tmp_path, "config", "user.name", "T")
    return tmp_path


def test_analyze_repo_counts_tracked_files(repo):
    (repo / "app.py").write_text("import os\n\nprint('hi')\n", encoding="utf-8")
    (repo / "notes.md").write_text("# Title\n\ntext\n", encoding="utf-8")
    (repo / "ignored.log").write_text("noise\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (repo / "logo.bin").write_bytes(b"\x00\x01\x02binary")
    _git(repo, "add", "app.py", "notes.md", ".gitignore", "logo.bin")

    m = metrics.analyze_repo(str(repo))
    assert m.ok
    # .log is gitignored & untracked -> excluded; binary skipped
    assert ".log" not in m.by_ext
    assert m.by_ext[".py"].files == 1
    assert m.by_ext[".py"].lines == 3
    assert m.by_ext[".py"].blank == 1
    assert m.by_ext[".py"].code == 2
    assert m.by_ext[".md"].lines == 3
    assert m.totals.files == 3  # app.py, notes.md, .gitignore (binary skipped)


def test_icon_resource_bundled():
    """The generated multi-resolution .ico ships with the package."""
    from git_assistant.ui.icon import icon_file

    path = icon_file()
    assert path.is_file(), f"missing {path}; run: uv run python tools/make_icon.py"
    # ICO magic: reserved=0, type=1 (icon)
    assert path.read_bytes()[:4] == b"\x00\x00\x01\x00"
