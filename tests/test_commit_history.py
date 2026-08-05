"""Generated commit messages, kept so regenerating cannot lose a better one."""

import pytest

from git_assistant import commit_history
from git_assistant.commit_generator import GenerationResult


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Redirect the store; patched where it is imported, as test_identity does."""
    monkeypatch.setattr(commit_history, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


def _result(message="feat: a change\n\n- did a thing", strategy="single-shot", chunks=1):
    return GenerationResult(
        message=message,
        strategy=strategy,
        context_window=32768,
        input_budget=29492,
        input_tokens=1757,
        num_chunks=chunks,
    )


def _record(repo="/x/demo", **kw):
    return commit_history.record(repo, _result(**kw), branch="main", head="9f2a1c8" * 5)


# ---- round trip ---------------------------------------------------------------
def test_a_message_comes_back_exactly_as_it_went_in():
    stored, problem = _record()
    assert problem == ""

    back = commit_history.list_runs("/x/demo")[0]

    assert back.message == "feat: a change\n\n- did a thing"
    assert back.subject() == "feat: a change"
    assert back.branch == "main"
    assert back.run_id == stored.run_id


def test_what_produced_it_is_kept_beside_it():
    _record(strategy="map-reduce", chunks=12)
    back = commit_history.list_runs("/x/demo")[0]

    assert (back.strategy, back.num_chunks) == ("map-reduce", 12)
    assert back.context_window == 32768
    assert "map-reduce" in back.describe() and "12 chunk(s)" in back.describe()


def test_the_newest_message_is_first():
    first, _ = _record(message="feat: one")
    second, _ = _record(message="feat: two")
    assert [r.run_id for r in commit_history.list_runs("/x/demo")][0] == second.run_id


def test_another_repository_s_messages_are_not_listed_here():
    _record(repo="/x/demo")
    _record(repo="/x/other")
    assert len(commit_history.list_runs("/x/demo")) == 1


def test_two_spellings_of_one_repository_path_share_one_history():
    _record(repo="D:\\Repo\\Demo")
    assert len(commit_history.list_runs("d:/repo/demo/")) == 1


def test_a_run_that_produced_nothing_is_not_recorded():
    """An empty row in the list is worse than no row."""
    stored, problem = commit_history.record("/x/demo", _result(message="   "))
    assert (stored, problem) == (None, "")
    assert commit_history.list_runs("/x/demo") == []


def test_a_missing_store_reads_as_no_messages():
    assert commit_history.list_runs("/x/never-generated") == []


def test_a_corrupt_store_reads_as_no_messages_rather_than_raising(store):
    _record()
    commit_history.runs_path("/x/demo").write_text("{half", encoding="utf-8")
    assert commit_history.list_runs("/x/demo") == []


def test_saving_leaves_no_temporary_file_behind(store):
    _record()
    names = [p.name for p in commit_history.runs_root().iterdir()]
    assert all(not n.endswith(".tmp") for n in names)


# ---- what happened to a message -------------------------------------------------
def test_the_message_that_became_a_commit_is_marked():
    stored, _ = _record()
    assert commit_history.mark_committed(stored) is True

    back = commit_history.list_runs("/x/demo")[0]
    assert back.committed is True
    assert back.result_label().startswith("committed - ")


def test_marking_one_message_does_not_mark_the_others():
    first, _ = _record(message="feat: one")
    _record(message="feat: two")
    commit_history.mark_committed(first)

    committed = [r.subject() for r in commit_history.list_runs("/x/demo") if r.committed]
    assert committed == ["feat: one"]


# ---- retention --------------------------------------------------------------------
def test_the_newest_messages_are_kept_and_the_oldest_dropped():
    for i in range(6):
        commit_history.record("/x/demo", _result(message=f"feat: number {i}"), limit=3)

    kept = commit_history.list_runs("/x/demo")

    assert len(kept) == 3
    assert kept[0].subject() == "feat: number 5"


def test_a_pinned_message_survives_the_retention_limit():
    keeper, _ = _record(message="feat: the good one")
    commit_history.set_pinned(keeper, True)
    for i in range(6):
        commit_history.record("/x/demo", _result(message=f"feat: {i}"), limit=2)

    subjects = [r.subject() for r in commit_history.list_runs("/x/demo")]
    assert "feat: the good one" in subjects


def test_a_limit_of_zero_keeps_everything():
    for i in range(6):
        commit_history.record("/x/demo", _result(message=f"feat: {i}"), limit=0)
    assert len(commit_history.list_runs("/x/demo")) == 6


# ---- forgetting ----------------------------------------------------------------------
def test_a_deleted_message_is_gone():
    stored, _ = _record()
    assert commit_history.delete_run(stored) is True
    assert commit_history.list_runs("/x/demo") == []


def test_clearing_a_repository_forgets_all_of_its_messages():
    _record(message="feat: one")
    _record(message="feat: two")
    assert commit_history.clear_repo("/x/demo") is True
    assert commit_history.list_runs("/x/demo") == []


# ---- failure --------------------------------------------------------------------------
def test_recording_reports_a_problem_instead_of_raising_when_the_disk_refuses(monkeypatch):
    monkeypatch.setattr(
        commit_history.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("full"))
    )
    stored, problem = _record()
    assert stored is None and "full" in problem
