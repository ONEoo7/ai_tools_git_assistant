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


# ---- the calls behind a message ------------------------------------------------------
def _call(index=1, phase="single-shot", user="the diff", response="feat: a change"):
    from git_assistant.llm_log import LlmCall

    return LlmCall(
        index=index,
        phase=phase,
        model="m",
        system="you are a commit message writer",
        user=user,
        max_tokens=512,
        response=response,
        seconds=1.5,
    )


def test_the_calls_come_back_with_the_message_they_produced():
    """Opening a stored run should answer "what was actually sent", too."""
    result = _result()
    result.calls = [_call(1), _call(2, phase="writing the message")]

    stored, _ = commit_history.record("/x/demo", result)
    back = commit_history.list_runs("/x/demo")[0]
    calls = commit_history.load_calls(back)

    assert back.num_calls == 2
    assert [c.index for c in calls] == [1, 2]
    assert [c.phase for c in calls] == ["single-shot", "writing the message"]
    assert calls[0].user == "the diff"
    assert calls[0].response == "feat: a change"
    assert calls[0].seconds == 1.5
    assert calls[0].transcript()  # the pane renders this and must not fail


def test_a_message_with_no_calls_recorded_says_so_rather_than_lying():
    """A run from a build that kept none is not a run that made none."""
    stored, _ = _record()
    back = commit_history.list_runs("/x/demo")[0]

    assert back.num_calls == 0
    assert commit_history.load_calls(back) == []


def test_the_transcript_is_kept_out_of_the_list_of_messages():
    """The file that draws the list must stay small; see the module docstring."""
    result = _result()
    result.calls = [_call(1, user="x" * 50_000)]
    commit_history.record("/x/demo", result)

    index = commit_history.runs_path("/x/demo").read_text(encoding="utf-8")

    assert "x" * 1_000 not in index
    assert len(index) < 5_000


def test_an_enormous_run_keeps_what_fits_and_the_end_of_it():
    """The last call is the one that wrote the message; the rest fed it."""
    result = _result(strategy="map-reduce", chunks=40)
    result.calls = [
        _call(i, user="x" * 40_000, response=f"note {i}") for i in range(1, 41)
    ]
    result.calls[-1] = _call(40, phase="writing the message", response="feat: it")

    stored, _ = commit_history.record("/x/demo", result)
    back = commit_history.list_runs("/x/demo")[0]
    calls = commit_history.load_calls(back)

    assert back.num_calls == 40, "what it did is recorded even when not all is kept"
    assert 0 < len(calls) < 40
    assert calls[-1].phase == "writing the message"
    assert [c.index for c in calls] == sorted(c.index for c in calls)
    size = commit_history.calls_path("/x/demo", stored.run_id).stat().st_size
    assert size <= commit_history.MAX_CALL_BYTES * 1.1


def test_one_huge_call_is_kept_rather_than_nothing_at_all():
    result = _result()
    result.calls = [_call(1, user="x" * (commit_history.MAX_CALL_BYTES * 2))]

    stored, _ = commit_history.record("/x/demo", result)

    assert len(commit_history.load_calls(stored)) == 1


def test_deleting_a_message_takes_its_transcript_with_it():
    result = _result()
    result.calls = [_call(1)]
    stored, _ = commit_history.record("/x/demo", result)
    assert commit_history.calls_path("/x/demo", stored.run_id).exists()

    commit_history.delete_run(stored)

    assert not commit_history.calls_path("/x/demo", stored.run_id).exists()


def test_a_message_pruned_by_the_limit_takes_its_transcript_with_it():
    """Otherwise the prompts of every run ever made stay on disk, unreachable."""
    kept = []
    for i in range(4):
        result = _result(message=f"feat: number {i}")
        result.calls = [_call(1, user=f"diff {i}")]
        stored, _ = commit_history.record("/x/demo", result, limit=2)
        kept.append(stored)

    alive = {r.run_id for r in commit_history.list_runs("/x/demo")}
    for stored in kept:
        exists = commit_history.calls_path("/x/demo", stored.run_id).exists()
        assert exists is (stored.run_id in alive), stored.run_id


def test_clearing_a_repository_forgets_the_prompts_too():
    """They are the part of this store that holds what was in the diff."""
    result = _result()
    result.calls = [_call(1, user="a secret in a diff")]
    stored, _ = commit_history.record("/x/demo", result)

    commit_history.clear_repo("/x/demo")

    assert not commit_history.calls_path("/x/demo", stored.run_id).exists()


def test_a_transcript_that_cannot_be_written_does_not_lose_the_message(monkeypatch):
    """The message is what the run was for; the transcript is what it said.

    `record` promises never to raise -- it is called from a worker thread with a
    message in hand -- so a disk that refuses the prompts must cost the prompts.
    """
    result = _result()
    result.calls = [_call(1)]
    real_mkdir = commit_history.Path.mkdir

    def refuse_the_calls_dir(self, *args, **kwargs):
        if commit_history.CALLS_DIR in self.parts:
            raise OSError("no room for transcripts")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(commit_history.Path, "mkdir", refuse_the_calls_dir)

    stored, problem = commit_history.record("/x/demo", result)

    assert stored is not None and problem == ""
    assert commit_history.list_runs("/x/demo")[0].message == result.message
    assert commit_history.load_calls(stored) == []
