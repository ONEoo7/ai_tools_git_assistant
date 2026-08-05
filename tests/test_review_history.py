"""Recorded reviews: kept, listed, and still readable much later."""

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
