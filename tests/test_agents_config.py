"""The configuration checks, one repository per thing they can catch."""

import subprocess
import sys

import pytest

from git_assistant.agents import checks, config_audit, probe as probe_mod
from git_assistant.agents.base import AgentContext, Status
from git_assistant.agents.report import to_markdown
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
    (tmp_path / "a.txt").write_bytes(b"hello\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-m", "first")
    return tmp_path


def _probe(repo):
    return probe_mod.collect(AgentContext(repo=str(repo), settings=Settings()))


def _result(repo, check):
    return check(_probe(repo))


def _attributes(repo, text):
    (repo / ".gitattributes").write_text(text, encoding="utf-8")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-m", "attributes")


# ---- line endings ------------------------------------------------------------
def test_no_attributes_means_line_endings_are_undeclared(repo):
    result = _result(repo, checks.eol_declared)
    assert result.status is Status.FAIL
    assert "text=auto" in result.remediation


def test_a_catch_all_rule_passes(repo):
    _attributes(repo, "* text=auto eol=lf\n")
    assert _result(repo, checks.eol_declared).status is Status.PASS


def test_rules_without_a_catch_all_only_warn(repo):
    _attributes(repo, "*.py text\n")
    assert _result(repo, checks.eol_declared).status is Status.WARN


def test_local_autocrlf_instead_of_attributes_is_a_failure(repo):
    """The user's question: this answer lives on one machine, not in the repo."""
    _git(repo, "config", "--local", "core.autocrlf", "true")

    result = _result(repo, checks.eol_config_scope)

    assert result.status is Status.FAIL
    assert any("core.autocrlf = true" in e and "local" in e for e in result.evidence)


def test_declaring_it_in_attributes_settles_it(repo):
    _git(repo, "config", "--local", "core.autocrlf", "true")
    _attributes(repo, "* text=auto eol=lf\n")

    result = _result(repo, checks.eol_config_scope)

    assert result.status is Status.PASS
    assert any("local" in e for e in result.evidence)  # still reported, not hidden


def test_a_repo_with_no_line_ending_config_at_all_passes(repo):
    assert _result(repo, checks.eol_config_scope).status in (Status.PASS, Status.WARN)


def test_crlf_in_the_index_is_reported(repo):
    (repo / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    _git(repo, "-c", "core.autocrlf=false", "add", "crlf.txt")
    _git(repo, "commit", "-m", "crlf")
    _attributes(repo, "* text=auto eol=lf\n")

    result = _result(repo, checks.eol_index_clean)

    assert result.status is Status.FAIL
    assert any("crlf.txt" in e for e in result.evidence)
    assert "renormalize" in result.remediation


def test_a_shell_script_without_eol_lf_is_flagged(repo):
    (repo / "run.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    _git(repo, "add", "run.sh")
    _git(repo, "commit", "-m", "script")

    result = _result(repo, checks.eol_scripts)

    assert result.status is Status.WARN
    assert any("run.sh" in e for e in result.evidence)


def test_scripts_pass_once_their_endings_are_declared(repo):
    (repo / "run.sh").write_bytes(b"#!/bin/sh\n")
    (repo / "go.bat").write_bytes(b"@echo off\r\n")
    _git(repo, "add", "run.sh", "go.bat")
    _git(repo, "commit", "-m", "scripts")
    _attributes(repo, "* text=auto eol=lf\n*.bat text eol=crlf\n")

    assert _result(repo, checks.eol_scripts).status is Status.PASS


# ---- LFS ---------------------------------------------------------------------
def test_a_raw_file_at_an_lfs_path_is_the_retroactivity_finding(repo):
    _attributes(repo, "*.bin filter=lfs diff=lfs merge=lfs -text\n")
    (repo / "model.bin").write_bytes(b"z" * 300_000)
    _git(repo, "-c", "filter.lfs.process=", "-c", "filter.lfs.clean=",
         "-c", "filter.lfs.smudge=", "-c", "filter.lfs.required=false",
         "add", "model.bin")
    _git(repo, "commit", "-m", "raw at an lfs path")

    result = _result(repo, checks.lfs_pointers_are_pointers)

    assert result.status is Status.FAIL
    assert any("model.bin" in e for e in result.evidence)
    assert "lfs migrate import" in result.remediation


def test_a_big_file_outside_lfs_is_flagged(repo):
    (repo / "huge.bin").write_bytes(b"0" * (51 * 1024 * 1024))
    _git(repo, "add", "huge.bin")
    _git(repo, "commit", "-m", "huge")

    result = _result(repo, checks.lfs_coverage)

    assert result.status is Status.FAIL
    assert any("huge.bin" in e for e in result.evidence)


def test_a_medium_binary_only_warns(repo):
    (repo / "art.png").write_bytes(b"0" * (6 * 1024 * 1024))
    _git(repo, "add", "art.png")
    _git(repo, "commit", "-m", "art")

    assert _result(repo, checks.lfs_coverage).status is Status.WARN


def test_a_repository_without_lfs_rules_skips_the_lfs_checks(repo):
    assert _result(repo, checks.lfs_pointers_are_pointers).status is Status.SKIP
    assert _result(repo, checks.lfs_settings).status is Status.SKIP


def test_untracked_attributes_apply_to_nobody_else(repo):
    (repo / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    result = _result(repo, checks.lfs_attributes_tracked)
    assert result.status is Status.FAIL


# ---- secrets, hygiene, portability -------------------------------------------
def test_a_password_in_a_remote_url_never_reaches_the_report(repo):
    _git(repo, "remote", "add", "origin", "https://user:hunter2@example.com/x.git")

    result = _result(repo, checks.credentials_in_remote)
    report = config_audit.ConfigAuditAgent().collect(
        AgentContext(repo=str(repo), settings=Settings())
    )

    assert result.status is Status.FAIL
    assert "hunter2" not in " ".join(result.evidence)
    assert "hunter2" not in to_markdown(report)
    assert "***" in " ".join(result.evidence)


def test_redaction_keeps_enough_to_identify_the_remote():
    assert (
        checks.redact_url("https://bob:secret@git.example.com/org/x.git")
        == "https://bob:***@git.example.com/org/x.git"
    )
    assert checks.redact_url("https://git.example.com/org/x.git") == (
        "https://git.example.com/org/x.git"
    )


def test_a_tracked_file_that_is_also_ignored_is_reported(repo):
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_bytes(b"x")
    _git(repo, "add", "-f", ".gitignore", "build/out.o")
    _git(repo, "commit", "-m", "ignored but tracked")

    result = _result(repo, checks.ignored_but_tracked)

    assert result.status is Status.WARN
    assert any("build/out.o" in e for e in result.evidence)


def test_paths_differing_only_by_case_cannot_both_check_out(repo):
    # Written straight into the index: a case-insensitive filesystem cannot
    # hold both names, so this is the only way to reproduce the state.
    proc = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input="x\n", capture_output=True, text=True, creationflags=_NO_WINDOW,
    )
    sha = proc.stdout.strip()
    for name in ("File.TXT", "file.txt"):
        _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{sha},{name}")

    result = _result(repo, checks.case_collisions)

    assert result.status is Status.FAIL
    assert any("File.TXT" in e for e in result.evidence)


# ---- the report --------------------------------------------------------------
def test_the_report_lists_every_check_worst_first(repo):
    _git(repo, "config", "--local", "core.autocrlf", "true")

    report = config_audit.ConfigAuditAgent().collect(
        AgentContext(repo=str(repo), settings=Settings())
    )

    assert [s.number for s in report.sections] == ["1", "2", "3", "4"]
    table = report.find("1").tables[0]
    assert len(table.rows) == len(checks.CHECKS)
    statuses = [row[1] for row in table.rows]
    assert statuses == sorted(statuses, key=["FAIL", "WARN", "PASS", "n/a"].index)
    assert any(row[0] == "EOL-02" and row[1] == "FAIL" for row in table.rows)


def test_every_check_reports_something_on_a_plain_repository(repo):
    results = checks.run_all(_probe(repo))
    assert len(results) == len(checks.CHECKS)
    assert all(r.headline for r in results)
    assert all(r.id and r.title for r in results)
