"""What a run says it will cost, before it is allowed to start."""

import pytest

from git_assistant import estimate, git_ops, usage
from git_assistant.commit_generator import CommitGenerator
from git_assistant.config import RepoEntry, Settings
from git_assistant.llm import ModelInfo
from git_assistant.review.rules import Rule, RuleTable

TABLE = RuleTable(
    name="House rules",
    rules=[Rule("R-1", "no bare except"), Rule("R-12", "log at the boundary")],
)


@pytest.fixture
def settings(tmp_path):
    s = Settings(selected_model="qwen3.5-4b", context_window=32768, parallel_calls=4)
    s.save = lambda: None
    s.repos = [RepoEntry(str(tmp_path))]
    s.active_repo = str(tmp_path)
    return s


def _file(path, lines=3):
    body = "".join(f"+line {i} of {path}\n" for i in range(lines))
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


@pytest.fixture
def small_diff(monkeypatch):
    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")
    monkeypatch.setattr(git_ops, "get_diffstat", lambda r, m: "2 files changed")
    monkeypatch.setattr(
        git_ops, "get_diff", lambda r, m: _file("app.py") + _file("util.py")
    )
    monkeypatch.setattr(git_ops, "file_content", lambda r, p, m: f"print('{p}')\n")


@pytest.fixture
def huge_diff(monkeypatch):
    big = "".join(_file(f"f{i}.py", lines=400) for i in range(12))
    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")
    monkeypatch.setattr(git_ops, "get_diffstat", lambda r, m: "12 files changed")
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: big)
    monkeypatch.setattr(git_ops, "file_content", lambda r, p, m: "x\n" * 50)
    return big


# ---- a commit message -----------------------------------------------------------
def test_a_diff_that_fits_is_one_call(settings, small_diff):
    out = estimate.for_commit(settings)

    assert out.calls == 1
    assert out.input_tokens > 0
    assert out.output_tokens > 0
    assert out.feature == usage.COMMIT
    assert "fits" in " ".join(out.lines)


def test_the_estimate_names_what_will_answer_it(settings, small_diff):
    out = estimate.for_commit(settings)
    assert out.model == "qwen3.5-4b"
    assert out.provider == "lmstudio"


def test_a_diff_too_large_is_a_call_per_chunk_plus_one(settings, huge_diff):
    out = estimate.for_commit(settings)

    assert out.calls > 2
    assert "map" in " ".join(out.lines).lower() or "pieces" in " ".join(out.lines)
    assert "chunk(s)" in " ".join(out.lines)


def test_the_promised_number_of_calls_is_the_number_a_run_makes(settings, huge_diff):
    """The estimate copies the generator's arithmetic; this is what stops it drifting."""

    class _Client:
        def __init__(self):
            self.calls = 0

        def chat(self, model, system, user, max_tokens, temperature=0.2):
            self.calls += 1
            return "a note"

        def list_models(self):
            return [ModelInfo(id="qwen3.5-4b", max_context_length=32768, loaded=True)]

        def context_length_for(self, model_id):
            return 32768

    predicted = estimate.for_commit(settings)
    client = _Client()
    CommitGenerator(settings, client).generate()

    assert client.calls == predicted.calls


def test_the_estimate_says_when_there_is_nothing_to_describe(settings, monkeypatch):
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: "")
    monkeypatch.setattr(git_ops, "get_diffstat", lambda r, m: "")
    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")

    out = estimate.for_commit(settings)

    assert out.calls == 0
    assert "No staged changes" in out.problem


def test_no_repository_is_a_problem_not_a_number(settings):
    settings.active_repo = ""
    assert "No repository" in estimate.for_commit(settings).problem


def test_a_repository_git_cannot_read_is_reported_rather_than_raised(
    settings, monkeypatch
):
    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")

    def refuse(repo, mode):
        raise git_ops.GitError("dubious ownership")

    monkeypatch.setattr(git_ops, "get_diffstat", refuse)
    assert "dubious ownership" in estimate.for_commit(settings).problem


# ---- a code review ---------------------------------------------------------------
def test_a_review_is_one_call_per_marked_file(settings, small_diff):
    out = estimate.for_review(settings, settings.active_repo, ["app.py", "util.py"], TABLE)

    assert out.calls == 2
    assert out.feature == usage.REVIEW
    assert "One call per marked file" in out.lines[0]


def test_a_file_that_was_not_marked_is_not_counted(settings, small_diff):
    both = estimate.for_review(settings, settings.active_repo, ["app.py", "util.py"], TABLE)
    one = estimate.for_review(settings, settings.active_repo, ["app.py"], TABLE)

    assert one.calls == 1
    assert one.input_tokens < both.input_tokens


def test_the_rules_the_diff_and_the_file_are_all_counted(settings, small_diff):
    small = estimate.for_review(settings, settings.active_repo, ["app.py"], TABLE)
    big = estimate.for_review(
        settings,
        settings.active_repo,
        ["app.py"],
        RuleTable("many", [Rule(f"R-{i}", "x" * 100) for i in range(10)]),
    )
    assert big.input_tokens > small.input_tokens


def test_a_review_says_which_files_will_not_fit_whole(settings, huge_diff):
    settings.context_window = 4096
    out = estimate.for_review(settings, settings.active_repo, ["f0.py", "f1.py"], TABLE)
    assert "cut to the budget" in " ".join(out.lines)


def test_reviewing_nothing_is_a_problem_not_a_number(settings, small_diff):
    assert "No files are marked" in estimate.for_review(
        settings, settings.active_repo, [], TABLE
    ).problem


def test_a_table_with_no_rules_is_a_problem(settings, small_diff):
    out = estimate.for_review(settings, settings.active_repo, ["app.py"], RuleTable("empty"))
    assert "no rules" in out.problem


# ---- an audit ---------------------------------------------------------------------
def test_an_audit_is_one_call_per_section_of_the_report(settings):
    out = estimate.for_audit(settings, "config-audit", narrate=True)

    assert out.calls == 2  # the config audit narrates two sections
    assert out.feature == usage.AUDIT
    assert out.output_tokens > 0


def test_the_bigger_audit_costs_more_sections(settings):
    size = estimate.for_audit(settings, "size-audit", narrate=True)
    config = estimate.for_audit(settings, "config-audit", narrate=True)
    assert size.calls > config.calls


def test_an_audit_without_narration_sends_nothing(settings):
    out = estimate.for_audit(settings, "size-audit", narrate=False)

    assert out.calls == 0
    assert out.total == 0
    assert "Nothing is sent" in out.lines[0]


def test_an_audit_does_not_invent_an_input_figure(settings):
    """The facts each section is handed do not exist until the scan has run.

    Six calls times the whole window would be a "ceiling" nobody reaches, and a
    number that frightens rather than informs.
    """
    out = estimate.for_audit(settings, "size-audit", narrate=True)

    assert out.input_unknown
    assert out.input_tokens == 0
    assert out.input_cap > 0
    assert "not known until" in out.summary()
    assert "capped at" in out.summary()


# ---- how it reads ------------------------------------------------------------------
def test_the_summary_is_calls_in_and_out(settings, small_diff):
    text = estimate.for_commit(settings).summary()
    assert "call(s)" in text and "tokens in" in text and "out" in text


def test_the_total_is_both_halves(settings, small_diff):
    out = estimate.for_commit(settings)
    assert out.total == out.input_tokens + out.output_tokens


def test_marked_files_that_are_not_in_the_diff_are_not_calls(settings, small_diff):
    """A path staged a moment ago and unstaged since is not a call, and not a cost."""
    out = estimate.for_review(
        settings, settings.active_repo, ["app.py", "gone.py"], TABLE
    )
    assert out.calls == 1


def test_a_review_of_only_missing_files_says_so_instead_of_a_number(settings, small_diff):
    out = estimate.for_review(settings, settings.active_repo, ["gone.py"], TABLE)
    assert out.calls == 0
    assert "None of the marked files" in out.problem


def test_a_review_never_promises_calls_it_cannot_price(settings, small_diff):
    out = estimate.for_review(settings, settings.active_repo, ["app.py", "util.py"], TABLE)
    assert out.calls == 2
    assert out.input_tokens > 0, "a call with no content is a number nobody can trust"

