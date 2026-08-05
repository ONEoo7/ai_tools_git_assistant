"""Reviewing files: what is sent, what is cut, and what survives a failure."""

import pytest

from git_assistant import git_ops
from git_assistant.config import Settings
from git_assistant.llm import ModelInfo
from git_assistant.parallel import CancelledError
from git_assistant.review import reviewer
from git_assistant.review.rules import Rule, RuleTable

TABLE = RuleTable(
    name="House rules",
    rules=[Rule("R-1", "no bare except"), Rule("R-12", "log at the boundary")],
)


class _Client:
    """A provider client that answers, and remembers what it was asked."""

    def __init__(self, *answers, context=32768):
        self.answers = list(answers)
        self.seen: list[dict] = []
        self._context = context
        self.phase = ""

    def chat(self, model, system, user, max_tokens, temperature=0.2):
        self.seen.append({"system": system, "user": user, "max_tokens": max_tokens})
        return self.answers.pop(0) if self.answers else "NO FINDINGS"

    def list_models(self):
        return [ModelInfo(id="m", max_context_length=self._context, loaded=True)]

    def context_length_for(self, model_id):
        return self._context


def _settings(**kw):
    s = Settings(selected_model="m", context_window=kw.pop("context", 32768))
    s.save = lambda: None
    for key, value in kw.items():
        setattr(s, key, value)
    return s


def _diff(path, lines=3):
    body = "".join(f"+line {i}\n" for i in range(lines))
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


@pytest.fixture
def repo(monkeypatch):
    """A repository whose diff and file contents are ours to choose."""
    files = {"app.py": _diff("app.py"), "util.py": _diff("util.py")}
    contents = {"app.py": "print('app')\n", "util.py": "print('util')\n"}
    monkeypatch.setattr(
        git_ops, "get_diff", lambda r, m: "".join(files[p] for p in sorted(files))
    )
    monkeypatch.setattr(git_ops, "file_content", lambda r, p, m: contents.get(p, ""))
    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")
    monkeypatch.setattr(git_ops, "has_uncommitted_changes", lambda r: True)
    monkeypatch.setattr(
        git_ops, "_run", lambda r, a, **k: git_ops.GitResult(True, "abc123", "", 0)
    )
    return {"files": files, "contents": contents}


def _run(client, paths=("app.py", "util.py"), settings=None, **kw):
    return reviewer.review(
        settings or _settings(),
        client,
        repo="/x/demo",
        paths=list(paths),
        table=TABLE,
        **kw,
    )


# ---- which files are offered, and which are sent -----------------------------------
def test_a_file_filtered_as_noise_is_listed_but_not_reviewable(monkeypatch):
    monkeypatch.setattr(
        git_ops, "get_diff", lambda r, m: _diff("app.py") + _diff("uv.lock")
    )
    found = reviewer.staged_files("/x/demo", "cached", ["*.lock"])

    assert [(c.path, c.reviewable) for c in found] == [
        ("app.py", True),
        ("uv.lock", False),
    ]


def test_one_call_is_made_for_each_selected_file(repo):
    client = _Client()
    run = _run(client)

    assert len(client.seen) == 2
    assert [r.path for r in run.files] == ["app.py", "util.py"]


def test_a_file_that_was_not_marked_is_never_sent(repo):
    client = _Client()
    run = _run(client, paths=["app.py"])

    assert len(client.seen) == 1
    assert "util.py" not in client.seen[0]["user"]
    assert [r.path for r in run.files] == ["app.py"]


def test_reviewing_nothing_says_so_rather_than_calling_the_model(repo):
    client = _Client()
    with pytest.raises(ValueError, match="marked for review"):
        _run(client, paths=[])
    assert client.seen == []


def test_a_table_with_no_rules_is_refused_before_any_call(repo):
    client = _Client()
    with pytest.raises(ValueError, match="no rules"):
        reviewer.review(
            _settings(), client, repo="/x/demo", paths=["app.py"], table=RuleTable("Empty")
        )
    assert client.seen == []


# ---- what reaches the prompt ---------------------------------------------------------
def test_the_rules_the_diff_and_the_file_content_all_reach_the_prompt(repo):
    client = _Client()
    _run(client, paths=["app.py"])

    sent = client.seen[0]["user"]
    assert "R-1: no bare except" in sent
    assert "line 0" in sent  # the diff
    assert "print('app')" in sent  # the file after the change
    assert "app.py" in sent


def test_the_answer_is_asked_for_in_the_form_the_parser_reads(repo):
    client = _Client()
    _run(client, paths=["app.py"])
    assert "FINDING |" in client.seen[0]["user"]
    assert "NO FINDINGS" in client.seen[0]["user"]


def test_every_call_is_labelled_so_the_calls_pane_can_name_it(repo):
    client = _Client()
    _run(client, paths=["app.py"])
    assert client.phase == "reviewing a file"


# ---- budgeting -----------------------------------------------------------------------
def test_the_rules_are_kept_whole_when_they_have_to_be_shortened():
    table = RuleTable("big", [Rule(f"R-{i}", "x" * 200) for i in range(20)])
    text, sent = reviewer.fit_rules(table, 200)

    assert 0 < sent < 20
    assert f"{20 - sent} of 20 rules omitted" in text
    # Every rule that was sent is whole: no line ends mid-sentence.
    for line in text.splitlines()[:sent]:
        assert line.endswith("x" * 200)


def test_at_least_one_rule_is_sent_however_small_the_budget():
    table = RuleTable("big", [Rule("R-1", "x" * 5000), Rule("R-2", "y")])
    text, sent = reviewer.fit_rules(table, 1)
    assert sent == 1 and "R-1" in text


def test_the_file_content_is_dropped_before_the_diff_is():
    built = reviewer.build_prompt(
        path="a.py",
        diff="+line\n" * 400,
        content="print(1)\n" * 400,
        table=TABLE,
        budget=1400,
    )
    assert built.diff_truncated is False
    assert built.content_sent is False


def test_a_diff_larger_than_the_budget_is_truncated_and_says_where(repo):
    built = reviewer.build_prompt(
        path="a.py", diff="+line\n" * 4000, content="x\n", table=TABLE, budget=400
    )
    assert built.diff_truncated is True
    assert "truncated" in built.user


def test_the_prompt_tells_the_model_what_it_is_not_being_shown():
    built = reviewer.build_prompt(
        path="a.py", diff="+line\n" * 4000, content="x\n" * 4000, table=TABLE, budget=400
    )
    assert "Note:" in built.user and "Judge only what you can see" in built.user


def test_a_file_that_fits_is_sent_whole_and_says_nothing_about_truncation():
    built = reviewer.build_prompt(
        path="a.py", diff="+one line\n", content="print(1)\n", table=TABLE, budget=4000
    )
    assert not built.diff_truncated and not built.content_truncated
    assert "Note:" not in built.user


def test_a_review_of_two_files_does_not_split_the_context_four_ways(repo):
    """Slots are only worth reserving for requests that will actually be made."""
    client = _Client()
    _run(client, paths=["app.py"], settings=_settings(parallel_calls=4, context=32768))

    # One file, so the whole window is its share: the file content survives.
    assert "print('app')" in client.seen[0]["user"]
    assert "Note:" not in client.seen[0]["user"]


def test_the_run_reports_the_rules_of_its_most_cramped_call(repo):
    table = RuleTable("big", [Rule(f"R-{i}", "x" * 400) for i in range(40)])
    run = reviewer.review(
        _settings(context=4096), _Client(), repo="/x/demo", paths=["app.py"], table=table
    )
    assert run.rules_truncated()
    assert run.rules_sent < 40
    assert f"only {run.rules_sent} of 40 rules sent" in run.summary()


# ---- what came back --------------------------------------------------------------------
def test_findings_are_attributed_to_the_file_that_was_being_reviewed(repo):
    client = _Client("FINDING | R-1 | 12 | swallows the error", "NO FINDINGS")
    run = _run(client)

    assert [f.path for f in run.findings()] == ["app.py"]
    assert run.files[1].clean


def test_an_empty_reply_is_reported_rather_than_treated_as_a_clean_file(repo):
    run = _run(_Client("", "NO FINDINGS"), paths=["app.py"])

    assert run.files[0].error
    assert run.files[0].clean is False
    assert run.headline()["failed"] == 1


def test_an_unreadable_reply_is_asked_again_before_it_is_given_up_on(repo):
    client = _Client("a wall of prose", "FINDING | R-1 | 3 | swallows the error")
    run = _run(client, paths=["app.py"])

    assert len(client.seen) == 2
    assert "could not be read" in client.seen[1]["user"]
    assert run.files[0].retried
    assert [f.rule_id for f in run.findings()] == ["R-1"]


def test_a_reply_that_stays_unreadable_becomes_a_visible_finding(repo):
    run = _run(_Client("prose", "more prose"), paths=["app.py"])

    finding = run.findings()[0]
    assert finding.parsed is False
    assert "more prose" in finding.raw_line


def test_a_file_whose_call_fails_does_not_lose_the_other_files_findings(repo):
    class Flaky(_Client):
        def chat(self, model, system, user, max_tokens, temperature=0.2):
            if "app.py" in user:
                raise RuntimeError("Server error '500 Internal Server Error'")
            return "FINDING | R-12 | 4 | no logging here"

    run = _run(Flaky())

    assert "500" in run.files[0].error
    assert [f.rule_id for f in run.findings()] == ["R-12"]


def test_the_run_records_what_it_was_run_with(repo):
    run = _run(_Client(), settings=_settings(provider="lmstudio"))

    assert run.table_name == "House rules"
    assert run.table_fingerprint == TABLE.fingerprint()
    assert (run.model, run.provider) == ("m", "lmstudio")
    assert run.branch == "main" and run.head == "abc123"
    assert run.staged_total == 2


def test_the_model_s_own_words_are_kept_for_every_file(repo):
    run = _run(_Client("NO FINDINGS", "NO FINDINGS"))
    assert all(r.raw_reply == "NO FINDINGS" for r in run.files)


# ---- stopping ----------------------------------------------------------------------------
def test_cancelling_stops_before_the_next_call(repo):
    client = _Client()
    with pytest.raises(CancelledError):
        _run(client, is_cancelled=lambda: True)
    assert client.seen == []
