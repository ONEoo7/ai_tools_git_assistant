"""The Metrics audit: what it counts, and what it refuses to score."""

import subprocess
import sys

import pytest

from git_assistant import agents
from git_assistant.agents import compare
from git_assistant.agents.base import AgentContext
from git_assistant.agents.metrics_audit import TOP_TYPES, MetricsAgent
from git_assistant.config import Settings

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    return tmp_path


def _add(repo, name, text):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", name)
    return path


def _collect(repo):
    settings = Settings()
    settings.save = lambda: None
    return MetricsAgent().collect(AgentContext(repo=str(repo), settings=settings))


def _facts(report) -> dict:
    return {f.key: f.value for f in report.facts_by_key().values()} | {
        key: fact.raw for key, fact in report.facts_by_key().items()
    }


def _raw(report, key):
    return report.facts_by_key()[key].raw


# ---- what is counted -----------------------------------------------------------
def test_lines_blank_lines_and_files_are_counted(repo):
    _add(repo, "a.py", "one\n\ntwo\n")  # 3 lines, 1 blank
    _add(repo, "b.py", "x\n")
    _git(repo, "commit", "-m", "add")

    report = _collect(repo)

    assert _raw(report, "files") == 2
    assert _raw(report, "lines") == 4
    assert _raw(report, "blank_lines") == 1
    assert _raw(report, "code_lines") == 3


def test_only_tracked_files_are_counted(repo):
    """Build output is not somebody's work, and neither is anything ignored."""
    _add(repo, "kept.py", "one\n")
    _git(repo, "commit", "-m", "add")
    (repo / "untracked.py").write_text("a\nb\nc\n", encoding="utf-8")
    (repo / "build.py").write_text("x\n" * 50, encoding="utf-8")
    _add(repo, ".gitignore", "build.py\n")
    _git(repo, "commit", "-m", "ignore")

    report = _collect(repo)

    assert _raw(report, "lines") == 2  # kept.py and .gitignore
    assert "untracked" not in str(report.sections[1].tables[0].rows)


def test_a_binary_file_is_skipped_rather_than_counted_as_one_long_line(repo):
    _add(repo, "a.py", "one\n")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02" * 400)
    _git(repo, "add", "--", "blob.bin")
    _git(repo, "commit", "-m", "add")

    report = _collect(repo)

    assert _raw(report, "files") == 1
    assert ".bin" not in str(report.sections[1].tables[0].rows)


# ---- how it is presented -------------------------------------------------------
def test_the_types_are_listed_largest_first_with_their_share(repo):
    _add(repo, "big.py", "x\n" * 90)
    _add(repo, "small.md", "y\n" * 10)
    _git(repo, "commit", "-m", "add")

    table = _collect(repo).sections[1].tables[0]

    assert [row[0] for row in table.rows] == [".py", ".md"]
    assert table.columns == ["Type", "Files", "Lines", "Code", "Share"]
    assert table.rows[0][4] == "90%"


def test_a_long_tail_of_types_is_summed_rather_than_dropped_in_silence(repo):
    """A table that stops at twenty rows without saying so reads as twenty types."""
    for i in range(TOP_TYPES + 5):
        _add(repo, f"f{i}.t{i}", "line\n" * (100 - i))
    _git(repo, "commit", "-m", "add")

    table = _collect(repo).sections[1].tables[0]

    assert len(table.rows) == TOP_TYPES
    assert "5 further type(s)" in table.note


def test_an_empty_repository_is_a_report_and_not_a_crash(repo):
    _git(repo, "commit", "--allow-empty", "-m", "nothing")

    report = _collect(repo)

    assert _raw(report, "files") == 0
    assert _raw(report, "lines") == 0
    assert report.sections[1].tables[0].rows == []


def test_a_repository_git_cannot_read_is_reported_as_a_warning(tmp_path):
    report = _collect(tmp_path / "not-a-repo")

    assert report.warnings
    assert _raw(report, "files") == 0


# ---- how it sits beside the other audits ---------------------------------------
def test_it_is_one_of_the_audits(repo):
    assert "metrics" in [info.id for info in agents.infos()]
    assert agents.get("metrics") is not None


def test_every_audit_is_named_the_same_way():
    """One word each, so the list reads as one set of things."""
    labels = [info.label for info in agents.infos()]

    assert labels == ["Size", "Configuration", "Consistency", "Metrics"]
    assert all(" " not in label for label in labels)


def test_it_runs_through_the_ordinary_audit_path(repo):
    _add(repo, "a.py", "one\n")
    _git(repo, "commit", "-m", "add")
    settings = Settings()
    settings.save = lambda: None

    report = agents.run("metrics", settings, repo=str(repo), narrate=False)

    assert report.agent_id == "metrics"
    assert report.head and report.branch  # the point it describes


def test_a_line_count_is_reported_and_never_scored():
    """More code is not better code, and less is not worse."""
    from git_assistant.agents.compare import Polarity

    assert compare.headline_keys("metrics")
    for key in ("lines", "code_lines", "files", "blank_lines"):
        metric = compare.metric_for("metrics", key)
        assert metric.polarity is Polarity.NEUTRAL, key


def test_two_runs_can_be_compared(repo):
    """Which is most of why this is an audit rather than a window of its own."""
    _add(repo, "a.py", "one\n")
    _git(repo, "commit", "-m", "one")
    before = _collect(repo)
    _add(repo, "b.py", "two\nthree\n")
    _git(repo, "commit", "-m", "two")
    after = _collect(repo)

    difference = compare.diff(_stored(before), _stored(after))

    assert difference is not None
    assert difference.summary()


def _stored(report):
    from git_assistant.agents.history import StoredRun

    return StoredRun(
        run_id=report.generated_at,
        agent_id=report.agent_id,
        repo_path=report.repo_path,
        started_at=report.generated_at,
        head=report.head,
        branch=report.branch,
        dirty=report.dirty,
        report=report,
    )
