"""Recorded reviews: kept, listed, and still readable much later."""

import os
import time

import pytest

from git_assistant.agents import history as agent_history
from git_assistant.review import history
from git_assistant.review.parse import Finding
from git_assistant.review.reviewer import FileReview, ReviewRun


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Redirect the store; patched where it is imported, as test_identity does."""
    monkeypatch.setattr(history, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


def _run(repo="/x/demo", when="2026-08-05T10:00:00Z", findings=1):
    return ReviewRun(
        repo_path=repo,
        table_name="House rules",
        table_fingerprint="abc123",
        rules_total=4,
        rules_sent=4,
        provider="lmstudio",
        model="qwen",
        started_at=when,
        head="9f2a1c8" * 5,
        branch="main",
        dirty=True,
        staged_total=2,
        files=[
            FileReview(
                path="app.py",
                findings=[
                    Finding(
                        rule_id="R-1",
                        rule_details="no bare except",
                        path="app.py",
                        line=42,
                        message="swallows the error",
                        raw_line="FINDING | R-1 | 42 | swallows the error",
                    )
                    for _ in range(findings)
                ],
                raw_reply="FINDING | R-1 | 42 | swallows the error",
                diff_truncated=True,
            ),
            FileReview(path="util.py", raw_reply="NO FINDINGS"),
        ],
    )


# ---- round trip -------------------------------------------------------------------
def test_a_review_comes_back_exactly_as_it_went_in():
    stored, problem = history.record(_run())
    assert problem == ""

    back = history.load_run(history.list_runs("/x/demo")[0]).run

    assert [f.path for f in back.files] == ["app.py", "util.py"]
    finding = back.findings()[0]
    assert (finding.rule_id, finding.line, finding.message) == (
        "R-1",
        42,
        "swallows the error",
    )
    assert back.files[0].diff_truncated is True
    assert back.table_name == "House rules"


def test_a_stored_run_still_reads_after_its_rule_table_is_deleted():
    """The rule text travels with the finding, so nothing has to be looked up."""
    history.record(_run())
    back = history.load_run(history.list_runs("/x/demo")[0]).run
    assert back.findings()[0].rule_details == "no bare except"


def test_the_list_knows_what_a_review_found_without_opening_it():
    stored, _ = history.record(_run(findings=3))
    listed = history.list_runs("/x/demo")[0]

    assert listed.run is None
    assert listed.headline["findings"] == 3
    assert "3 finding(s)" in listed.result_label()


def test_a_review_is_listed_once():
    history.record(_run())
    history.record(_run(when="2026-08-05T11:00:00Z"))
    assert len(history.list_runs("/x/demo")) == 2


def test_the_newest_review_is_first():
    history.record(_run(when="2026-08-01T10:00:00Z"))
    history.record(_run(when="2026-08-05T10:00:00Z"))
    assert history.list_runs("/x/demo")[0].started_at == "2026-08-05T10:00:00Z"


def test_another_repository_s_reviews_are_not_listed_here():
    history.record(_run(repo="/x/demo"))
    history.record(_run(repo="/x/other"))
    assert len(history.list_runs("/x/demo")) == 1


# ---- the index is only a cache ------------------------------------------------------
def test_the_index_is_rebuilt_from_the_run_files_when_it_is_lost():
    history.record(_run())
    (history.runs_dir("/x/demo") / history.INDEX_FILE).unlink()

    listed = history.list_runs("/x/demo")

    assert len(listed) == 1
    assert listed[0].headline["findings"] == 1


def test_a_torn_index_costs_a_directory_listing_not_the_history():
    history.record(_run())
    (history.runs_dir("/x/demo") / history.INDEX_FILE).write_text("{half", encoding="utf-8")
    assert len(history.list_runs("/x/demo")) == 1


def test_a_missing_run_file_loads_as_nothing_rather_than_raising():
    stored, _ = history.record(_run())
    (history.runs_dir("/x/demo") / f"{stored.run_id}.json").unlink()
    assert history.load_run(stored) is None


# ---- retention -----------------------------------------------------------------------
def test_the_newest_runs_are_kept_and_the_oldest_are_deleted():
    for day in range(1, 6):
        history.record(_run(when=f"2026-08-0{day}T10:00:00Z"), limit=3)

    kept = history.list_runs("/x/demo")

    assert [r.started_at[:10] for r in kept] == ["2026-08-05", "2026-08-04", "2026-08-03"]
    # Dropped from the list *and* from the disk, not merely hidden.
    files = [p for p in history.runs_dir("/x/demo").glob("*.json") if p.name != history.INDEX_FILE]
    assert len(files) == 3


def test_a_pinned_run_survives_the_retention_limit():
    first, _ = history.record(_run(when="2026-08-01T10:00:00Z"), limit=2)
    history.set_pinned(first, True)
    for day in (2, 3, 4):
        history.record(_run(when=f"2026-08-0{day}T10:00:00Z"), limit=2)

    kept = {r.started_at for r in history.list_runs("/x/demo")}

    assert "2026-08-01T10:00:00Z" in kept
    assert len(kept) == 3


def test_a_limit_of_zero_keeps_everything():
    for day in range(1, 6):
        history.record(_run(when=f"2026-08-0{day}T10:00:00Z"), limit=0)
    assert len(history.list_runs("/x/demo")) == 5


# ---- a scanner holding the index open ----------------------------------------------------
# On Windows `os.replace` fails with "Access is denied" while any other process
# has the destination open, and an antivirus or the search indexer reading a
# file written milliseconds ago does exactly that. Measured at roughly 0.75% of
# writes on one machine -- which surfaced as this file's retention test failing
# about one run in twenty-five, and never as anything anyone noticed in the app.


@pytest.fixture
def no_waiting(monkeypatch):
    """Skip the backoff so a give-up path costs no wall-clock time.

    Patched on the `time` module rather than through `history`, so the fixture
    does not itself depend on the retry existing -- otherwise these tests error
    on setup against a version without it instead of failing on behaviour,
    which is the difference between a regression test and a decoration.
    """
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


class Sticky:
    """An `os.replace` that denies the first `failures` calls.

    Holds the real one: `history.os` is the same module object as this file's
    `os`, so patching it and then calling `os.replace` here recurses forever.
    """

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0
        self._real = os.replace

    def __call__(self, src, dst):
        self.calls += 1
        if self.calls <= self.failures:
            raise PermissionError(13, "Access is denied")
        return self._real(src, dst)


def test_a_scanner_holding_the_index_is_waited_out(monkeypatch, no_waiting):
    """The common case: it clears in milliseconds, so the review is not lost."""
    replace = Sticky(failures=2)
    monkeypatch.setattr(history.os, "replace", replace)

    stored, problem = history.record(_run())

    assert problem == ""
    assert stored is not None
    assert replace.calls == 3  # two denials, then through
    assert len(history.list_runs("/x/demo")) == 1


def test_a_review_that_reached_the_disk_is_never_reported_as_lost(monkeypatch, no_waiting):
    """The run file is the record; the index is a cache derived from it.

    Reporting "not saved" for a review that is sitting on disk sends someone to
    re-run forty model calls they already have.
    """
    monkeypatch.setattr(history.os, "replace", Sticky(failures=99))

    stored, problem = history.record(_run())

    assert stored is not None
    assert "saved" in problem
    assert "could not be written" in problem


def test_a_review_survives_an_index_that_could_not_be_written(monkeypatch, no_waiting):
    """Dropping the stale index is what keeps the review findable.

    Left in place it describes the directory as it was *before* this review and
    is never re-derived -- a readable index is trusted -- so the run would be
    hidden for good by a scanner's timing.

    The earlier review matters: with no index at all the next read rebuilds
    from the run files anyway, so a denial on the very first write is harmless
    and proves nothing. The damage needs an index that already exists.
    """
    history.record(_run(when="2026-08-01T10:00:00Z"))
    monkeypatch.setattr(history.os, "replace", Sticky(failures=99))

    history.record(_run(when="2026-08-05T10:00:00Z"))

    assert not (history.runs_dir("/x/demo") / history.INDEX_FILE).exists()
    listed = history.list_runs("/x/demo")  # rebuilt from the run files
    assert [r.started_at[:10] for r in listed] == ["2026-08-05", "2026-08-01"]


def test_retention_still_holds_when_one_index_write_is_denied(monkeypatch, no_waiting):
    """The exact shape of the flake: a denial partway through a retention run.

    The fourth review's index write failing used to leave its run file orphaned
    on disk and the index describing the third review's state, so the fifth
    review pruned against stale data and four files survived a limit of three.
    """
    real_replace = os.replace
    seen = {"index_writes": 0}

    def replace(src, dst):
        if str(dst).endswith(history.INDEX_FILE):
            seen["index_writes"] += 1
            # The fourth, which is where it was observed. Denying the *first*
            # proves nothing: with no index on disk the next read rebuilds from
            # the run files, so that case was always safe.
            if seen["index_writes"] == 4:
                raise PermissionError(13, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(history.os, "replace", replace)
    for day in range(1, 6):
        history.record(_run(when=f"2026-08-0{day}T10:00:00Z"), limit=3)

    kept = history.list_runs("/x/demo")

    assert [r.started_at[:10] for r in kept] == ["2026-08-05", "2026-08-04", "2026-08-03"]
    files = [p for p in history.runs_dir("/x/demo").glob("*.json") if p.name != history.INDEX_FILE]
    assert len(files) == 3


def test_giving_up_leaves_no_temporary_file_behind(monkeypatch, no_waiting):
    """Otherwise the directory fills with `index.json.<hex>.tmp`, one per denial."""
    monkeypatch.setattr(history.os, "replace", Sticky(failures=99))

    history.record(_run())

    leftovers = list(history.runs_dir("/x/demo").glob("*.tmp"))
    assert leftovers == []


# ---- forgetting -------------------------------------------------------------------------
def test_a_deleted_review_is_gone_from_the_list_and_the_disk():
    stored, _ = history.record(_run())
    assert history.delete_run(stored) is True
    assert history.list_runs("/x/demo") == []


def test_clearing_a_repository_forgets_all_of_its_reviews():
    history.record(_run())
    history.record(_run(when="2026-08-06T10:00:00Z"))
    assert history.clear_repo("/x/demo") is True
    assert history.list_runs("/x/demo") == []


# ---- what is deliberately not stored --------------------------------------------------
def test_the_calls_are_not_written_to_disk():
    """Forty prompts a run, twenty runs a repository -- the findings are the record."""
    run = _run()
    run.calls = [object()]  # not even serializable
    stored, problem = history.record(run)
    assert problem == ""
    assert history.load_run(stored).run.calls == []


# ---- the two stores agree ----------------------------------------------------------------
def test_two_spellings_of_one_repository_path_share_one_history():
    history.record(_run(repo="D:\\Repo\\Demo"))
    assert len(history.list_runs("d:\\repo\\demo\\")) == 1


def test_the_review_store_and_the_agent_store_agree_on_a_repository_s_key():
    assert history.runs_dir("/x/demo").name == agent_history.runs_dir("/x/demo").name


# ---- failure ------------------------------------------------------------------------------
def test_recording_reports_a_problem_instead_of_raising_when_the_disk_refuses(monkeypatch):
    monkeypatch.setattr(
        history.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("full"))
    )
    stored, problem = history.record(_run())
    assert stored is None and "full" in problem
