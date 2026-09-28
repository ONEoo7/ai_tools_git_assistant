"""Writing a commit message from the names of the changed files alone.

Offered on the dialog that prices a run, ticked there to begin with when the changes
come to more than `estimate.NAMES_ONLY_ABOVE` tokens: one call, however large the
change, carrying what happened to which file and none of the lines.
"""

import pytest

from conftest import settings_with
from git_assistant import estimate, git_ops, llm_log, prompts
from git_assistant.commit_generator import (
    ADDED,
    COPIED,
    DELETED,
    KINDS,
    MODIFIED,
    NAMES_ONLY,
    NAMES_ONLY_NOTE,
    RENAMED,
    CommitGenerator,
    file_kind,
    names_prompt,
)
from git_assistant.config import RepoEntry
from git_assistant.diff_strategy import split_diff
from git_assistant.llm import ModelInfo
from git_assistant.llm_log import RecordingClient
from git_assistant.tokenizer import estimate_tokens

ADDED_DIFF = (
    "diff --git a/docs/new.md b/docs/new.md\nnew file mode 100644\n"
    "index 0000000..3b18e51\n--- /dev/null\n+++ b/docs/new.md\n@@ -0,0 +1 @@\n+hello\n"
)
DELETED_DIFF = (
    "diff --git a/old/gone.py b/old/gone.py\ndeleted file mode 100644\n"
    "index 3b18e51..0000000\n--- a/old/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
)
RENAMED_DIFF = (
    "diff --git a/src/before.py b/src/after.py\nsimilarity index 100%\n"
    "rename from src/before.py\nrename to src/after.py\n"
)
COPIED_DIFF = (
    "diff --git a/lib/a.c b/lib/b.c\nsimilarity index 90%\ncopy from lib/a.c\n"
    "copy to lib/b.c\n--- a/lib/a.c\n+++ b/lib/b.c\n@@ -1 +1 @@\n-a\n+b\n"
)
#: A modified file whose changes say "new file mode" and "deleted file mode": what
#: happened to it is in git's header, not in whatever its lines happen to say.
MODIFIED_DIFF = (
    "diff --git a/src/app.py b/src/app.py\nindex 1111111..2222222 100644\n"
    "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n-new file mode 100644\n"
    "+deleted file mode 100644\n"
)
EVERY_KIND = MODIFIED_DIFF + ADDED_DIFF + RENAMED_DIFF + DELETED_DIFF + COPIED_DIFF
STAT = (
    " docs/new.md    | 1 +\n old/gone.py    | 1 -\n src/app.py     | 2 +-\n"
    " 5 files changed, 3 insertions(+), 2 deletions(-)\n"
)


def _files(text=EVERY_KIND):
    return split_diff(text)


# ---- what happened to which file ------------------------------------------------------
def test_what_happened_to_each_file_is_read_off_git_s_header_for_it():
    kinds = {file.path: file_kind(file) for file in _files()}

    assert kinds == {
        "docs/new.md": (ADDED, "docs/new.md"),
        "old/gone.py": (DELETED, "old/gone.py"),
        "src/after.py": (RENAMED, "src/before.py -> src/after.py"),
        "lib/b.c": (COPIED, "lib/a.c -> lib/b.c"),
        "src/app.py": (MODIFIED, "src/app.py"),  # whatever its lines say
    }


def test_the_names_are_grouped_by_what_happened_the_rarer_kinds_first():
    named = names_prompt("{diff}", branch="main", diffstat="", files=_files(), budget=10_000)

    assert named.user == (
        f"{NAMES_ONLY_NOTE}\n\n"
        "Added (1):\n  docs/new.md\n\n"
        "Deleted (1):\n  old/gone.py\n\n"
        "Renamed (1):\n  src/before.py -> src/after.py\n\n"
        "Copied (1):\n  lib/a.c -> lib/b.c\n\n"
        "Modified (1):\n  src/app.py"
    )
    assert KINDS == (ADDED, DELETED, RENAMED, COPIED, MODIFIED)
    assert (named.listed, named.total) == (5, 5)


def test_the_repository_s_template_is_used_with_only_the_totals_of_the_stat():
    """The rest of --stat is the same names again, a line each."""
    named = names_prompt(
        "on {branch}\nstat: {diffstat}\nchanges: {diff}",
        branch="dev/x",
        diffstat=STAT,
        files=_files(),
        budget=10_000,
    )

    assert named.user.startswith(
        "on dev/x\nstat: 5 files changed, 3 insertions(+), 2 deletions(-)\nchanges: "
        + NAMES_ONLY_NOTE
    )
    assert "| 1 +" not in named.user
    assert "hello" not in named.user and "-bye" not in named.user  # no changes at all


def test_what_it_carries_is_counted_as_every_estimate_counts_it():
    named = names_prompt("{diff}", branch="", diffstat=STAT, files=_files(), budget=10_000)

    assert named.tokens == estimate_tokens(prompts.COMMIT_SYSTEM) + estimate_tokens(named.user)


def test_names_that_do_not_fit_are_left_out_modified_ones_first_and_counted():
    many = "".join(
        f"diff --git a/src/m{i}.py b/src/m{i}.py\n--- a/src/m{i}.py\n+++ b/src/m{i}.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
        for i in range(400)
    )
    files = _files(many + ADDED_DIFF + DELETED_DIFF)
    whole = names_prompt("{diff}", branch="", diffstat="", files=files, budget=100_000)

    named = names_prompt("{diff}", branch="", diffstat="", files=files, budget=whole.tokens // 3)

    assert named.tokens <= whole.tokens // 3
    # As many as fit, and no fewer: the room left over is less than one more name.
    assert whole.tokens // 3 - named.tokens < estimate_tokens("\n  src/m100.py")
    assert 2 < named.listed < named.total == 402
    assert "Added (1):\n  docs/new.md" in named.user
    assert "Deleted (1):\n  old/gone.py" in named.user
    assert "Modified (400):" in named.user  # how many there were, all the same
    assert f"... and {402 - named.listed} more file(s), not named" in named.user
    assert "src/m0.py" in named.user and "src/m399.py" not in named.user


def test_a_budget_the_names_fit_whole_cuts_nothing():
    named = names_prompt("{diff}", branch="", diffstat="", files=_files(), budget=10_000)
    exact = names_prompt("{diff}", branch="", diffstat="", files=_files(), budget=named.tokens)

    assert exact.listed == exact.total and "more file(s)" not in exact.user


# ---- a run from the names ----------------------------------------------------------------
@pytest.fixture
def settings(tmp_path):
    return settings_with(
        selected_model="qwen3.5-4b",
        context_window=32768,
        parallel_calls=4,
        ignore_globs=["*.lock"],
        repos=[RepoEntry(str(tmp_path))],
        active_repo=str(tmp_path),
    )


LOCK = "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1 +1 @@\n-1\n+2\n"


@pytest.fixture
def staged(monkeypatch):
    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")
    monkeypatch.setattr(git_ops, "get_diffstat", lambda r, m: STAT)
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: EVERY_KIND + LOCK)


class _Client:
    def __init__(self):
        self.asked = []

    def chat(self, model, system, user, max_tokens, temperature=0.2):
        self.asked.append((system, user, max_tokens))
        return "chore: move things around"

    def list_models(self):
        return [ModelInfo(id="qwen3.5-4b", max_context_length=32768, loaded=True)]

    def context_length_for(self, model_id):
        return 32768


def test_a_run_from_the_names_is_one_call_carrying_them_and_no_changes(settings, staged):
    client = _Client()

    result = CommitGenerator(settings, client).generate(names_only=True)

    ((system, user, _answer),) = client.asked
    assert system == prompts.COMMIT_SYSTEM
    assert NAMES_ONLY_NOTE in user and "src/before.py -> src/after.py" in user
    assert "+hello" not in user and "uv.lock" not in user  # no lines; noise stays noise
    assert result.message == "chore: move things around"
    assert result.strategy == NAMES_ONLY and result.num_chunks == 1
    assert result.retry.user == user


def test_every_file_is_marked_as_named_and_the_noise_as_filtered(settings, staged):
    result = CommitGenerator(settings, _Client()).generate(names_only=True)

    reasons = {c.path: c.reason for c in result.file_coverage}
    assert reasons == {
        "src/app.py": "named",
        "docs/new.md": "named",
        "src/after.py": "named",
        "old/gone.py": "named",
        "lib/b.c": "named",
        "uv.lock": "filtered",
    }
    # None of a named file's lines reached the model, and it says so.
    named = next(c for c in result.file_coverage if c.path == "src/app.py")
    assert named.omitted_count == len(named.lines) > 0


def test_the_call_is_logged_as_one_from_file_names(settings, staged):
    recorder = RecordingClient(_Client())

    CommitGenerator(settings, recorder).generate(names_only=True)

    assert [call.phase for call in recorder.calls] == [llm_log.NAMES]


def test_without_being_asked_a_run_still_sends_the_changes(settings, staged):
    client = _Client()

    result = CommitGenerator(settings, client).generate()

    assert "+hello" in client.asked[0][1] and result.strategy == "single-shot"


def test_the_worker_hands_the_choice_on_to_the_run(tmp_path, staged, monkeypatch):
    pytest.importorskip("PyQt6.QtCore")
    from git_assistant.config import Settings
    from git_assistant.ui import workers

    raw = Settings()
    raw.repos = [RepoEntry(str(tmp_path))]
    raw.active_repo = str(tmp_path)
    raw.selected_model = "qwen3.5-4b"
    monkeypatch.setattr(workers, "build_client", lambda settings, feature: _Client())
    results = []
    for names_only in (True, False):
        worker = workers.GeneratorWorker(raw, names_only=names_only)
        worker.finished.connect(results.append)
        worker.error.connect(results.append)
        worker.run()

    assert [result.strategy for result in results] == [NAMES_ONLY, "single-shot"]


# ---- what the dialog is told ------------------------------------------------------------
def test_the_estimate_offers_the_names_at_what_the_run_will_send(settings, staged):
    offered = estimate.for_commit(settings).names_only
    client = _Client()
    CommitGenerator(settings, client).generate(names_only=True)
    ((system, user, answer),) = client.asked

    assert offered.calls == 1
    assert offered.input_tokens == estimate_tokens(system) + estimate_tokens(user)
    assert offered.output_tokens == answer
    assert "The names of 5 file(s)" in offered.lines[0]


def test_a_change_under_the_threshold_is_sent_whole_unless_ticked(settings, staged):
    priced = estimate.for_commit(settings)

    assert priced.only_names is False and priced.chosen() is priced
    assert not any("Ticked" in line for line in priced.names_only.lines)


def test_a_change_over_the_threshold_is_ticked_to_send_only_names(
    settings, staged, monkeypatch
):
    assert estimate.NAMES_ONLY_ABOVE == 128_000  # what was asked for
    monkeypatch.setattr(estimate, "NAMES_ONLY_ABOVE", 100)

    priced = estimate.for_commit(settings)

    assert priced.only_names is True and priced.chosen() is priced.names_only
    assert any("Ticked to begin with" in line for line in priced.names_only.lines)


def test_the_threshold_is_the_whole_change_not_what_the_window_holds(
    settings, staged, monkeypatch
):
    """At it, not over it: sent whole."""
    at = estimate.for_commit(settings)
    whole = at.input_tokens if at.calls == 1 else None
    assert whole is not None
    monkeypatch.setattr(estimate, "NAMES_ONLY_ABOVE", whole)

    assert estimate.for_commit(settings).only_names is False
    monkeypatch.setattr(estimate, "NAMES_ONLY_ABOVE", whole - 1)
    assert estimate.for_commit(settings).only_names is True


def test_nothing_to_describe_offers_no_names_either(settings, monkeypatch):
    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")
    monkeypatch.setattr(git_ops, "get_diffstat", lambda r, m: "")
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: "")

    priced = estimate.for_commit(settings)

    assert priced.problem and priced.names_only is None
