"""Recording what was sent to the model, and what came back."""

import pytest

from git_assistant import llm_log
from git_assistant.commit_generator import CommitGenerator
from conftest import settings_with
from git_assistant.config import Settings
from git_assistant.llm import ModelInfo
from git_assistant.llm_log import RecordingClient


class _Client:
    """A provider client that answers, and remembers what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen: list[dict] = []

    def chat(self, model, system, user, max_tokens, temperature=0.2):
        self.seen.append(
            {"model": model, "system": system, "user": user, "max_tokens": max_tokens}
        )
        return self.answers.pop(0) if self.answers else "ok"

    def list_models(self):
        return [ModelInfo(id="m", max_context_length=32768, loaded=True)]

    def context_length_for(self, model_id):
        return 32768

    def ping(self):
        return self.list_models()


def _chat(recorder, user="hello", system="sys", max_tokens=100):
    return recorder.chat(
        model="m", system=system, user=user, max_tokens=max_tokens
    )


# ---- it records without changing anything ------------------------------------
def test_the_request_reaches_the_provider_unchanged():
    inner = _Client("answer")
    recorder = RecordingClient(inner)

    assert _chat(recorder, user="the diff", system="be terse") == "answer"

    assert inner.seen == [
        {"model": "m", "system": "be terse", "user": "the diff", "max_tokens": 100}
    ]


def test_both_halves_of_the_exchange_are_kept():
    recorder = RecordingClient(_Client("a message"))
    _chat(recorder, user="the diff")

    call = recorder.calls[0]
    assert call.user == "the diff"
    assert call.response == "a message"
    assert call.seconds >= 0
    assert call.ok


def test_calls_are_numbered_in_order():
    recorder = RecordingClient(_Client("1", "2", "3"))
    for _ in range(3):
        _chat(recorder)
    assert [c.index for c in recorder.calls] == [1, 2, 3]


def test_a_failing_call_is_recorded_and_still_raises():
    class Broken(_Client):
        def chat(self, *a, **kw):
            raise RuntimeError("the provider said no")

    recorder = RecordingClient(Broken())
    with pytest.raises(RuntimeError):
        _chat(recorder)

    assert recorder.calls[0].ok is False
    assert "the provider said no" in recorder.calls[0].error


def test_everything_else_is_the_client_underneath():
    recorder = RecordingClient(_Client())
    assert recorder.context_length_for("m") == 32768
    assert recorder.list_models()[0].id == "m"
    assert recorder.ping()


def test_each_finished_call_is_announced():
    seen = []
    recorder = RecordingClient(_Client("a", "b"), on_call=seen.append)
    _chat(recorder)
    _chat(recorder)
    assert [c.index for c in seen] == [1, 2]


def test_a_failed_call_is_announced_too():
    class Broken(_Client):
        def chat(self, *a, **kw):
            raise RuntimeError("no")

    seen = []
    with pytest.raises(RuntimeError):
        _chat(RecordingClient(Broken(), on_call=seen.append))
    assert seen and seen[0].ok is False


def test_concurrent_calls_all_get_their_own_number():
    """Map chunks run on several threads; numbering must not collide."""
    import threading

    recorder = RecordingClient(_Client(*[str(i) for i in range(20)]))
    threads = [threading.Thread(target=lambda: _chat(recorder)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(c.index for c in recorder.calls) == list(range(1, 21))


# ---- what it shows ---------------------------------------------------------------
def test_the_transcript_holds_the_whole_exchange():
    recorder = RecordingClient(_Client("the answer"))
    _chat(recorder, user="the question", system="the rules")

    text = recorder.calls[0].transcript()

    assert "--- SYSTEM ---" in text and "the rules" in text
    assert "--- USER" in text and "the question" in text
    assert "--- RESPONSE ---" in text and "the answer" in text


def test_a_failed_call_shows_the_error_where_the_answer_would_be():
    class Broken(_Client):
        def chat(self, *a, **kw):
            raise RuntimeError("context length exceeded")

    recorder = RecordingClient(Broken())
    with pytest.raises(RuntimeError):
        _chat(recorder)

    assert "context length exceeded" in recorder.calls[0].transcript()


def test_the_summary_says_what_the_call_cost():
    recorder = RecordingClient(_Client("a b c"))
    _chat(recorder, user="x " * 500)
    summary = recorder.calls[0].summary()
    assert "in /" in summary and "out" in summary


# ---- the phases a generation goes through ------------------------------------------
class _Stub(_Client):
    """Enough of a client to drive a whole generation."""

    def context_length_for(self, model_id):
        return 32768


def _settings(tmp_path, context=32768):
    s = settings_with(selected_model="m", context_window=context, parallel_calls=2)
    s.active_repo = str(tmp_path)
    return s


def test_a_single_shot_run_records_one_call(tmp_path, monkeypatch):
    from git_assistant import git_ops

    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")
    monkeypatch.setattr(git_ops, "get_diffstat", lambda r, m: "a.py | 2 +-")
    monkeypatch.setattr(
        git_ops, "get_diff", lambda r, m: "diff --git a/a.py b/a.py\n+one line\n"
    )
    recorder = RecordingClient(_Stub("feat: a change"))

    result = CommitGenerator(_settings(tmp_path), recorder).generate()

    assert result.strategy == "single-shot"
    assert [c.phase for c in recorder.calls] == [llm_log.SINGLE]


def test_a_map_reduce_run_names_the_phase_of_every_call(tmp_path, monkeypatch):
    """Which call to blame for a poor message is the whole point of recording."""
    from git_assistant import git_ops

    big = "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n"
        + "".join(f"+line {j} of file {i}\n" for j in range(400))
        for i in range(6)
    )
    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")
    monkeypatch.setattr(git_ops, "get_diffstat", lambda r, m: "6 files changed")
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: big)
    recorder = RecordingClient(_Stub(*[f"note {i}" for i in range(40)]))

    result = CommitGenerator(_settings(tmp_path, context=8192), recorder).generate()

    phases = [c.phase for c in recorder.calls]
    assert result.strategy == "map-reduce"
    assert phases.count(llm_log.MAP) == result.num_chunks
    assert phases[-1] == llm_log.FINAL, "the last call is the one that writes it"


def test_the_final_call_carries_the_notes_the_chunks_produced(tmp_path, monkeypatch):
    from git_assistant import git_ops

    big = "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n" + "".join(f"+line {j}\n" for j in range(400))
        for i in range(6)
    )
    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")
    monkeypatch.setattr(git_ops, "get_diffstat", lambda r, m: "6 files changed")
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: big)
    recorder = RecordingClient(_Stub(*[f"NOTE-{i}" for i in range(40)]))

    CommitGenerator(_settings(tmp_path, context=8192), recorder).generate()

    final = [c for c in recorder.calls if c.phase == llm_log.FINAL][-1]
    assert "NOTE-0" in final.user
    assert "Changes:" in final.user


def test_the_result_carries_the_calls(tmp_path, monkeypatch):
    """So a finished run can be inspected, not just watched while it happens."""
    from git_assistant import git_ops

    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")
    monkeypatch.setattr(git_ops, "get_diffstat", lambda r, m: "a.py | 1 +")
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: "diff --git a/a.py b/a.py\n+x\n")
    recorder = RecordingClient(_Stub("feat: x"))
    generator = CommitGenerator(_settings(tmp_path), recorder)

    result = generator.generate()
    result.calls = list(recorder.calls)  # as the worker does

    assert len(result.calls) == 1


def test_a_chunk_that_returns_nothing_is_dropped_and_counted(tmp_path, monkeypatch):
    """An empty note is a chunk whose changes reached the message through nothing."""
    from git_assistant import git_ops

    big = "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n" + "".join(f"+line {j}\n" for j in range(400))
        for i in range(6)
    )
    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")
    monkeypatch.setattr(git_ops, "get_diffstat", lambda r, m: "6 files changed")
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: big)

    class Blanking(_Stub):
        """Every other chunk comes back empty."""

        def __init__(self):
            super().__init__()
            self.n = 0

        def chat(self, model, system, user, max_tokens, temperature=0.2):
            self.n += 1
            if "Summarize the following" in user:
                return "" if self.n % 2 else f"note {self.n}"
            return "feat: something"

    recorder = RecordingClient(Blanking())
    result = CommitGenerator(_settings(tmp_path, context=8192), recorder).generate()

    assert result.blank_notes > 0
    final = [c for c in recorder.calls if c.phase == llm_log.FINAL][-1]
    # Blank lines where a note should be would hide the loss; nothing empty is
    # passed on.
    notes = final.user.split("Changes:\n", 1)[-1]
    assert "\n\n\n" not in notes
