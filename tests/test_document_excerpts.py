"""Un-ignoring one file, by hand, in the staged files list.

The ignore globs are a rule and are always obeyed -- a PDF that git renders as
readable text is still ignored, because a lock file renders as readable text
too and only the person committing can tell the difference. What these cover is
the exception: a file named by hand reaches the model, head first, and the run
says exactly how much of it went.
"""

import pytest

from conftest import settings_with
from git_assistant import estimate, git_ops
from git_assistant.commit_generator import CommitGenerator
from git_assistant.config import RepoEntry

#: A PDF as git renders it through a textconv driver: ordinary added lines.
PAGES = 1250
DOC = "docs/NIST.FIPS.180-4.pdf"


def _pdf(path=DOC, lines=PAGES):
    body = "".join(f"+page line {i}\n" for i in range(lines))
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{lines} @@\n" + body
    )


def _source(path="src/app.py"):
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,3 @@\n"
        " import os\n"
        "+import sys\n"
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(git_ops, "current_branch", lambda r: "main")
    monkeypatch.setattr(git_ops, "get_diffstat", lambda r, m: "2 files changed")
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: _source() + _pdf())
    return str(tmp_path)


def _settings(repo, included=(DOC,), **over):
    values = dict(
        selected_model="qwen3.5-4b",
        context_window=200_000,
        repos=[RepoEntry(repo)],
        active_repo=repo,
        ignore_globs=["*.pdf", "uv.lock"],
        include_lines=200,
    )
    values.update(over)
    bound = settings_with(**values)
    bound.set_included(repo, included)
    return bound


class _Client:
    """Answers once, and remembers what it was asked."""

    def __init__(self):
        self.prompts: list[str] = []

    def context_length_for(self, model_id):
        return None

    def list_models(self):
        return []

    def chat(self, *, model, system, user, max_tokens, **kw):
        self.prompts.append(user)
        return "docs: add the SHS standard"


def _run(settings):
    client = _Client()
    result = CommitGenerator(settings, client).generate()
    return client, result


def _coverage(result, path):
    return next(c for c in result.file_coverage if c.path == path)


# ---- the rule is always obeyed ---------------------------------------------
def test_an_ignored_file_nobody_asked_for_is_sent_nothing_of(repo):
    client, result = _run(_settings(repo, included=()))
    assert "page line" not in client.prompts[0]
    assert _coverage(result, DOC).reason == "filtered"


def test_asking_for_it_is_what_changes_that(repo):
    client, _ = _run(_settings(repo))
    assert "+page line 0\n" in client.prompts[0]


# ---- and only its head goes ------------------------------------------------
def test_only_the_head_of_it_reaches_the_model(repo):
    client, _ = _run(_settings(repo))
    [prompt] = client.prompts
    assert "+page line 100\n" in prompt
    assert "+page line 1000\n" not in prompt
    assert "further lines of this file not sent" in prompt


def test_a_limit_of_zero_sends_all_of_it(repo):
    client, _ = _run(_settings(repo, include_lines=0))
    [prompt] = client.prompts
    assert "+page line 1249\n" in prompt
    assert "further lines" not in prompt


def test_a_file_git_could_not_diff_stays_omitted_even_when_asked_for(repo, monkeypatch):
    binary = (
        "diff --git a/scan.pdf b/scan.pdf\n"
        "Binary files a/scan.pdf and b/scan.pdf differ\n"
    )
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: _source() + binary)
    _client, result = _run(_settings(repo, included=("scan.pdf",)))
    assert _coverage(result, "scan.pdf").reason == "filtered"


def test_a_commit_of_nothing_but_an_un_ignored_file_still_runs(repo, monkeypatch):
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: _pdf())
    client, _ = _run(_settings(repo))
    assert "+page line 0\n" in client.prompts[0]


# ---- what the run says about it --------------------------------------------
def test_coverage_is_measured_against_the_whole_file(repo):
    _client, result = _run(_settings(repo))
    cov = _coverage(result, DOC)
    assert cov.reason == "excerpt"
    # Five header lines plus the body; 200 of them went.
    assert len(cov.lines) == PAGES + 5
    assert cov.omitted_count == PAGES + 5 - 200
    assert not cov.fully_sent


def test_an_ordinary_file_beside_it_is_unaffected(repo):
    _client, result = _run(_settings(repo))
    cov = _coverage(result, "src/app.py")
    assert cov.reason == "sent"
    assert cov.fully_sent


def test_the_file_is_still_listed_as_dropped(repo):
    # The noise filter did drop it: the excerpt is an exception, not a keep.
    _client, result = _run(_settings(repo))
    assert DOC in result.dropped_files


# ---- what it is priced at --------------------------------------------------
def test_the_estimate_says_a_file_was_kept_by_hand(repo):
    out = estimate.for_commit(_settings(repo))
    assert any("un-ignored file(s)" in line for line in out.lines)


def test_the_estimate_counts_what_it_will_send(repo):
    asked = estimate.for_commit(_settings(repo))
    not_asked = estimate.for_commit(_settings(repo, included=()))
    assert asked.input_tokens > not_asked.input_tokens
    assert not any("un-ignored" in line for line in not_asked.lines)


# ---- the choice is remembered, per repository ------------------------------
def test_the_choice_is_kept_per_repository(tmp_path):
    from git_assistant.config import RepoEntry as Entry

    one, two = str(tmp_path / "one"), str(tmp_path / "two")
    s = settings_with(repos=[Entry(one), Entry(two)], active_repo=one)
    s.include_file(one, DOC)

    assert s.included_paths(one) == [DOC]
    assert s.included_paths(two) == []


def test_asking_twice_does_not_list_it_twice(tmp_path):
    s = settings_with(active_repo=str(tmp_path))
    s.include_file(str(tmp_path), DOC)
    s.include_file(str(tmp_path), DOC)
    assert s.included_paths(str(tmp_path)) == [DOC]


def test_going_back_to_ignoring_it_leaves_no_entry_behind(tmp_path):
    repo = str(tmp_path)
    s = settings_with(active_repo=repo)
    s.include_file(repo, DOC)
    s.ignore_file(repo, DOC)

    assert s.included_paths(repo) == []
    assert s.commit_includes == {}
