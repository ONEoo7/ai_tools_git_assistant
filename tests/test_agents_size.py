"""The size audit, against real repositories built for the purpose."""

import os
import subprocess
import sys

import pytest

from git_assistant.agents import size_audit
from git_assistant.agents.base import AgentContext, CancelledError
from git_assistant.config import Settings

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=check,
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@e.example")
    _git(tmp_path, "config", "user.name", "T")
    return tmp_path


def _ctx(repo, **kw):
    return AgentContext(repo=str(repo), settings=Settings(), **kw)


def _commit(repo, name, content):
    (repo / name).write_bytes(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add {name}")


def test_every_version_of_a_file_is_counted(repo):
    """The point of the report: one path, summed across its whole history."""
    sizes = [200_000, 210_000, 220_000]
    for i, size in enumerate(sizes):
        _commit(repo, "big.bin", bytes([i]) * size)
    _commit(repo, "small.txt", b"hello\n")

    scan = size_audit._history(_ctx(repo), {}, [], [])

    assert scan.by_path["big.bin"].versions == 3
    assert scan.by_path["big.bin"].size == sum(sizes)
    assert scan.by_path["big.bin"].largest == max(sizes)
    assert scan.by_ext[".bin"].size == sum(sizes)
    assert scan.by_ext[".txt"].versions == 1


def test_history_covers_every_branch_not_just_the_checked_out_one(repo):
    _commit(repo, "a.txt", b"a\n")
    _git(repo, "checkout", "-b", "side")
    _commit(repo, "only-on-side.txt", b"side\n")
    _git(repo, "checkout", "-")

    scan = size_audit._history(_ctx(repo), {}, [], [])

    assert "only-on-side.txt" in scan.by_path


def test_fast_mode_reports_totals_without_the_breakdown(repo):
    _commit(repo, "a.bin", b"x" * 5000)
    warnings: list[str] = []

    scan = size_audit._history(_ctx(repo, fast=True), {}, [], warnings)

    assert scan.blob_size >= 5000
    assert scan.by_path == {}
    assert any("Fast mode" in w for w in warnings)


def test_cancelling_stops_the_scan(repo):
    _commit(repo, "a.txt", b"a\n")
    ctx = _ctx(repo, is_cancelled=lambda: True)
    with pytest.raises(CancelledError):
        SizeAgent = size_audit.SizeAuditAgent()
        SizeAgent.collect(ctx)


def test_reachability_counts_match_git(repo):
    _commit(repo, "a.txt", b"a\n")
    _commit(repo, "b.txt", b"b\n")
    _git(repo, "branch", "feature")
    _git(repo, "tag", "v1")

    reach = size_audit._reachability(_ctx(repo))

    assert reach["commits"] == 2
    assert reach["branches"] == 2
    assert reach["tags"] == 1
    assert reach["first_commit"] and reach["last_commit"]


def test_leftover_temporary_packs_are_found_and_measured(repo):
    _commit(repo, "a.txt", b"a\n")
    pack_dir = repo / ".git" / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "tmp_pack_deadbeef").write_bytes(b"0" * 4096)

    found: list = []
    tree = size_audit._measure_git_dir(_ctx(repo), str(repo / ".git"), found)

    assert [name for name, _s, _m in found] == ["objects/pack/tmp_pack_deadbeef"]
    assert found[0][1] == 4096
    assert tree.buckets["objects/pack"] >= 4096


def test_the_report_has_the_reference_structure(repo):
    _commit(repo, "a.txt", b"a\n")

    report = size_audit.SizeAuditAgent().collect(_ctx(repo))

    assert [s.number for s in report.sections] == ["1", "2", "3", "4", "5"]
    assert [s.number for s in report.find("2").sections] == ["2.1", "2.2"]
    assert report.find("2.2").tables  # top paths and extensions
    assert "gc --prune=now" in report.find("4.1").commands[0][1]
    assert "lfs migrate import" in report.find("4.2").commands[0][1]


def test_content_at_lfs_paths_is_reported_as_stored_raw(repo):
    """The reference report's finding: a rule added later converts nothing."""
    (repo / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8"
    )
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-m", "track bin with lfs")
    # Added with the clean filter neutered, which is what a commit made before
    # the rule existed looks like afterwards: full content at an LFS path.
    (repo / "model.bin").write_bytes(b"z" * 300_000)
    # `process` is the filter git-lfs actually installs, and it outranks
    # clean/smudge -- neutering only those would still store a pointer.
    _git(repo, "-c", "filter.lfs.process=", "-c", "filter.lfs.clean=",
         "-c", "filter.lfs.smudge=", "-c", "filter.lfs.required=false",
         "add", "model.bin")
    _git(repo, "commit", "-m", "add model.bin")

    ctx = _ctx(repo)
    patterns, rules = size_audit._lfs_patterns(ctx)
    scan = size_audit._history(ctx, {}, patterns, [])

    assert rules == 1
    assert scan.lfs_raw_blobs == 1
    assert scan.lfs_raw_size == 300_000


def test_an_unreadable_path_is_refused_clearly(tmp_path):
    with pytest.raises(RuntimeError, match="not a readable git repository"):
        size_audit.SizeAuditAgent().collect(_ctx(tmp_path / "nope"))


def test_bucketing_separates_pack_from_loose_objects_and_submodules():
    assert size_audit._bucket_of(("objects", "pack", "x.pack")) == "objects/pack"
    assert size_audit._bucket_of(("objects", "ab", "cdef")) == "objects"
    assert size_audit._bucket_of(("modules", "sub", "config")) == "modules"
    assert size_audit._bucket_of(("lfs", "objects", "aa")) == "lfs"
    assert size_audit._bucket_of(("config",)) == "other"


def test_count_objects_reports_bytes_not_kibibytes(repo):
    """git answers in KiB; a report that mixed the two would be 1024x wrong."""
    _commit(repo, "a.txt", b"a" * 40_000)
    raw = {
        line.split(":")[0]: line.split(":")[1].strip()
        for line in _git(repo, "count-objects", "-v").stdout.splitlines()
    }
    counts = size_audit._count_objects(_ctx(repo))

    assert counts["size"] == int(raw["size"]) * 1024
    assert counts["count"] == int(raw["count"])
    assert "garbage" in counts
