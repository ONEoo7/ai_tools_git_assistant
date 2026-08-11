"""A judged review, end to end: the second pass, its cost, and what it records."""

import pytest

from git_assistant import estimate, git_ops, repo_config
from git_assistant.config import RepoEntry, Settings
from git_assistant.review import plan as plan_mod
from git_assistant.review import reviewer
from git_assistant.review.judge import JudgeConfig
from git_assistant.review.rules import Rule, RuleTable

JUDGED = JudgeConfig(provider="claude", model="sonnet-5", temperature=0.0, prompt="")


class Fake:
    """A client that reviews and judges, and remembers which it was asked."""

    phase = ""

    def __init__(self, judge_reply="SCORE | 7.5 | missed one"):
        self.reviews: list[str] = []
        self.judgements: list[str] = []
        self._judge_reply = judge_reply

    def chat(self, model, system, user, max_tokens, temperature=None):
        if "grade the work" in system:
            self.judgements.append(user)
            return self._judge_reply
        self.reviews.append(user)
        return "FINDING | R-1 | 4 | uses a bare except"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = Settings()
    s.save = lambda: None
    s.repos = [RepoEntry("/x/demo")]
    s.active_repo = "/x/demo"
    s.provider = "lmstudio"
    s.selected_model = "qwen-small"
    monkeypatch.setattr(
        git_ops,
        "get_diff",
        lambda r, m: "".join(
            f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n+x = 1\n"
            for p in ("a.py", "b.py")
        ),
    )
    monkeypatch.setattr(git_ops, "file_content", lambda r, p, m: "x = 1\n")
    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")
    monkeypatch.setattr(git_ops, "has_uncommitted_changes", lambda r: False)
    return s


def _plan(judge=JUDGED):
    table = RuleTable("House", [Rule("R-1", "no bare except")])
    candidates = reviewer.staged_files("/x/demo", "cached", [])
    plan = plan_mod.ReviewPlan.of_table("/x/demo", candidates, table)
    plan.judge = judge
    return plan


def _run(settings, plan, client):
    return reviewer.review(
        repo_config.bind(settings, "/x/demo"), client, plan=plan, judge_client=client
    )


# ---- the second pass ----------------------------------------------------------------
def test_every_reviewed_file_is_scored(settings):
    client = Fake()

    run = _run(settings, _plan(), client)

    assert len(client.reviews) == 2
    assert len(client.judgements) == 2
    assert run.judge_scores == [7.5, 7.5]
    assert run.judge_score == 7.5


def test_the_judge_is_shown_the_exchange_it_is_scoring(settings):
    """The whole prompt and the whole reply, which is what was asked for."""
    client = Fake()

    _run(settings, _plan(), client)

    shown = client.judgements[0]
    assert "no bare except" in shown, "the rules the reviewer was given"
    assert "x = 1" in shown, "the code it was shown"
    assert "FINDING | R-1 | 4 | uses a bare except" in shown, "the answer it gave"


def test_nothing_is_judged_when_the_box_is_off(settings):
    client = Fake()

    run = _run(settings, _plan(judge=None), client)

    assert client.judgements == []
    assert run.judged() is False
    assert run.judge_model == ""


def test_a_judge_with_no_model_scores_nothing(settings):
    """A ticked box is not a judge, and must not look like one."""
    client = Fake()

    run = _run(settings, _plan(judge=JudgeConfig(provider="claude")), client)

    assert client.judgements == []
    assert run.judged() is False


def test_the_findings_are_untouched_by_the_judge(settings):
    """This is a measurement harness, not a filter."""
    judged = _run(settings, _plan(), Fake())
    plain = _run(settings, _plan(judge=None), Fake())

    assert [f.rule_id for f in judged.findings()] == [f.rule_id for f in plain.findings()]
    assert len(judged.findings()) == 2


def test_a_file_that_could_not_be_reviewed_is_not_scored(settings, monkeypatch):
    """There is no answer to grade, and a zero would file the reviewer's crash
    as the judge's opinion of it."""

    class Broken(Fake):
        def chat(self, model, system, user, max_tokens, temperature=None):
            if "grade the work" in system:
                return super().chat(model, system, user, max_tokens, temperature)
            raise RuntimeError("the model fell over")

    run = _run(settings, _plan(), Broken())

    assert all(f.error for f in run.files)
    assert run.judge_scores == []


def test_a_judge_that_answers_nonsense_does_not_score_zero(settings):
    """It is counted as unscored, and said so, rather than averaged in as 0."""
    run = _run(settings, _plan(), Fake(judge_reply="I cannot assess this."))

    assert run.judge_scores == []
    assert run.judge_failed == 2
    assert run.judge_score == 0.0
    assert run.judged() is False  # nothing to put on a leaderboard


def test_the_judge_is_asked_at_its_own_temperature(settings):
    seen = []

    class Watching(Fake):
        def chat(self, model, system, user, max_tokens, temperature=None):
            seen.append((model, temperature))
            return super().chat(model, system, user, max_tokens, temperature)

    _run(settings, _plan(), Watching())

    assert ("sonnet-5", 0.0) in seen
    assert any(m == "qwen-small" for m, _ in seen)


# ---- what it costs ------------------------------------------------------------------
def test_judging_is_priced_before_it_is_spent(settings):
    """This dialog is where somebody decides whether to spend it."""
    bound = repo_config.bind(settings, "/x/demo")

    plain = estimate.for_review(bound, _plan(judge=None))
    judged = estimate.for_review(bound, _plan())

    assert judged.calls == plain.calls * 2
    assert judged.output_tokens > plain.output_tokens
    assert judged.input_tokens > plain.input_tokens


def test_the_estimate_says_who_is_judging(settings):
    judged = estimate.for_review(repo_config.bind(settings, "/x/demo"), _plan())
    assert any("sonnet-5" in line for line in judged.lines)


def test_an_unusable_judge_costs_nothing(settings):
    bound = repo_config.bind(settings, "/x/demo")

    priced = estimate.for_review(bound, _plan(judge=JudgeConfig(provider="claude")))

    assert priced.calls == estimate.for_review(bound, _plan(judge=None)).calls


# ---- how long it took ----------------------------------------------------------------
def test_the_time_recorded_covers_the_files_that_were_scored(settings, monkeypatch):
    """Time and score have to be about the same files, or the two columns on
    the leaderboard describe different things."""
    import time as time_mod

    ticks = iter([0.0, 1.5, 10.0, 13.5, 100.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(time_mod, "monotonic", lambda: next(ticks, 100.0))

    run = _run(settings, _plan(), Fake())

    assert len(run.judge_scores) == 2
    assert run.judged_seconds == 5.0  # 1.5 + 3.5, the two reviewed files


def test_a_run_nobody_could_score_records_no_time(settings):
    run = _run(settings, _plan(), Fake(judge_reply="not a score"))

    assert run.judge_scores == []
    assert run.judged_seconds == 0.0


def test_an_unjudged_run_records_no_time(settings):
    run = _run(settings, _plan(judge=None), Fake())
    assert run.judged_seconds == 0.0
