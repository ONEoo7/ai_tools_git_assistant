"""Comparing two runs: what counts as better, and what is just movement."""

from git_assistant.agents import compare
from git_assistant.agents.base import CheckResult, Fact, Report, Section, Status
from git_assistant.agents.compare import Direction, Polarity


def _run(agent_id, head, facts, checks=None, repo="/x/demo", dirty=False):
    """A stand-in for history.StoredRun: the fields compare.diff reads."""
    report = Report(
        agent_id=agent_id,
        title="t",
        subtitle="s",
        generated_at="04 August 2026 12:00",
        repo_path=repo,
        head=head,
        branch="main",
        dirty=dirty,
        sections=[
            Section(
                number="1",
                title="Summary",
                facts=[Fact(k, k.replace("_", " ").title(), v[0], v[1]) for k, v in facts.items()],
            )
        ],
        checks=checks or [],
    )

    class _Stored:
        def __init__(self):
            self.agent_id = agent_id
            self.repo_path = repo
            self.head = head
            self.dirty = dirty
            self.report = report

        def when_label(self):
            return "4 Aug 12:00"

    return _Stored()


def _check(check_id, status, title="A check", headline="what happened"):
    return CheckResult(check_id, title, status, headline, weight=3)


def _delta(diff, key):
    return next(m for m in diff.metrics if m.key == key)


# ---- direction ----------------------------------------------------------------
def test_fewer_failures_is_better():
    before = _run("config-audit", "a", {"checks_failed": ("3", 3)})
    after = _run("config-audit", "b", {"checks_failed": ("1", 1)})
    assert _delta(compare.diff(before, after), "checks_failed").direction is Direction.BETTER


def test_more_failures_is_worse():
    before = _run("config-audit", "a", {"checks_failed": ("1", 1)})
    after = _run("config-audit", "b", {"checks_failed": ("3", 3)})
    assert _delta(compare.diff(before, after), "checks_failed").direction is Direction.WORSE


def test_more_passes_is_better():
    before = _run("config-audit", "a", {"checks_passed": ("5", 5)})
    after = _run("config-audit", "b", {"checks_passed": ("8", 8)})
    assert _delta(compare.diff(before, after), "checks_passed").direction is Direction.BETTER


def test_an_unchanged_measurement_is_the_same():
    before = _run("config-audit", "a", {"checks_failed": ("3", 3)})
    after = _run("config-audit", "b", {"checks_failed": ("3", 3)})
    assert _delta(compare.diff(before, after), "checks_failed").direction is Direction.SAME


# ---- growth is not a regression -------------------------------------------------
def test_more_commits_is_not_an_improvement_or_a_regression():
    before = _run("size-audit", "a", {"commits": ("100", 100)})
    after = _run("size-audit", "b", {"commits": ("140", 140)})
    assert _delta(compare.diff(before, after), "commits").direction is Direction.NEUTRAL


def test_a_repository_that_grew_because_work_happened_is_not_worse():
    """Different commits: the size moved because the history did."""
    before = _run("size-audit", "aaa", {"git_dir_total": ("1.0 GiB", 1073741824)})
    after = _run("size-audit", "bbb", {"git_dir_total": ("1.2 GiB", 1288490188)})

    delta = _delta(compare.diff(before, after), "git_dir_total")

    assert delta.direction is Direction.NEUTRAL
    assert delta.polarity is Polarity.NEUTRAL
    assert delta.delta > 0  # still reported, just not scored


def test_shrinking_at_the_same_commit_is_an_improvement():
    """Same content, less disk: that is housekeeping, and it worked."""
    before = _run("size-audit", "aaa", {"git_dir_total": ("1.0 GiB", 1073741824)})
    after = _run("size-audit", "aaa", {"git_dir_total": ("600.0 MiB", 629145600)})

    diff = compare.diff(before, after)

    assert diff.same_head is True
    assert _delta(diff, "git_dir_total").direction is Direction.BETTER
    assert diff.verdict() is Direction.BETTER


def test_waste_is_scored_whatever_the_commit_is():
    before = _run("size-audit", "aaa", {"garbage_size": ("84.0 GiB", 90194313216)})
    after = _run("size-audit", "bbb", {"garbage_size": ("0 B", 0)})
    assert _delta(compare.diff(before, after), "garbage_size").direction is Direction.BETTER


# ---- the shape of a delta --------------------------------------------------------
def test_a_percentage_is_not_computed_from_zero():
    before = _run("size-audit", "a", {"garbage_size": ("0 B", 0)})
    after = _run("size-audit", "a", {"garbage_size": ("1.0 KiB", 1024)})
    assert _delta(compare.diff(before, after), "garbage_size").percent is None


def test_a_size_change_is_read_in_bytes_not_digits():
    before = _run("size-audit", "aaa", {"git_dir_total": ("1.0 GiB", 1073741824)})
    after = _run("size-audit", "aaa", {"git_dir_total": ("512.0 MiB", 536870912)})
    text = _delta(compare.diff(before, after), "git_dir_total").change_text()
    assert text.startswith("down 512.0 MiB")
    assert "-50.0%" in text


def test_a_measurement_only_one_run_has_is_not_an_improvement():
    before = _run("size-audit", "a", {"garbage_size": ("1.0 KiB", 1024)})
    after = _run("size-audit", "a", {"tmp_packs": ("2", 2)})
    diff = compare.diff(before, after)
    assert _delta(diff, "tmp_packs").direction is Direction.NEUTRAL
    assert _delta(diff, "garbage_size").after == "—"


def test_a_value_with_no_number_is_context_only_when_it_changed():
    before = _run("size-audit", "a", {"repo_branch": ("main", None), "git_version": ("2.55", None)})
    after = _run("size-audit", "b", {"repo_branch": ("release", None), "git_version": ("2.55", None)})

    diff = compare.diff(before, after)

    assert [c.key for c in diff.context] == ["repo_branch"]
    assert not [m for m in diff.metrics if m.key == "repo_branch"]


# ---- checks -----------------------------------------------------------------------
def test_a_failing_check_that_passes_is_a_fix():
    before = _run("config-audit", "a", {}, [_check("EOL-02", Status.FAIL)])
    after = _run("config-audit", "b", {}, [_check("EOL-02", Status.PASS)])
    assert compare.diff(before, after).checks[0].fixed()


def test_a_passing_check_that_fails_is_a_regression():
    before = _run("config-audit", "a", {}, [_check("EOL-02", Status.PASS)])
    after = _run("config-audit", "b", {}, [_check("EOL-02", Status.FAIL)])
    assert compare.diff(before, after).checks[0].regressed()


def test_a_warning_that_becomes_a_failure_is_a_regression():
    before = _run("config-audit", "a", {}, [_check("LFS-03", Status.WARN)])
    after = _run("config-audit", "b", {}, [_check("LFS-03", Status.FAIL)])
    assert compare.diff(before, after).checks[0].regressed()


def test_a_check_that_stopped_applying_was_not_fixed():
    before = _run("config-audit", "a", {}, [_check("LFS-04", Status.FAIL)])
    after = _run("config-audit", "b", {}, [_check("LFS-04", Status.SKIP)])
    assert compare.diff(before, after).checks[0].direction is Direction.NEUTRAL


def test_unchanged_checks_are_not_listed():
    before = _run("config-audit", "a", {}, [_check("A", Status.PASS), _check("B", Status.FAIL)])
    after = _run("config-audit", "b", {}, [_check("A", Status.PASS), _check("B", Status.PASS)])
    assert [c.id for c in compare.diff(before, after).checks] == ["B"]


def test_regressions_are_listed_before_fixes():
    before = _run("config-audit", "a", {}, [_check("A", Status.PASS), _check("B", Status.FAIL)])
    after = _run("config-audit", "b", {}, [_check("A", Status.FAIL), _check("B", Status.PASS)])
    assert [c.direction for c in compare.diff(before, after).checks] == [
        Direction.WORSE,
        Direction.BETTER,
    ]


def test_verdicts_are_read_back_out_of_the_report_when_a_run_predates_them():
    """A run recorded before reports carried their checks must still compare."""
    def _finding(check_id, status):
        return Section(
            number="2.1",
            title=f"{check_id} — Line endings",
            facts=[
                Fact(f"{check_id}_status", "Result", status),
                Fact(f"{check_id}_finding", "Finding", "what happened"),
            ],
        )

    before = _run("config-audit", "a", {})
    after = _run("config-audit", "b", {})
    before.report.sections.append(_finding("EOL-02", "FAIL"))
    after.report.sections.append(_finding("EOL-02", "PASS"))

    checks = compare.diff(before, after).checks

    assert checks[0].id == "EOL-02" and checks[0].fixed()
    assert checks[0].title == "Line endings"


# ---- the summary ------------------------------------------------------------------
def test_the_summary_says_improved_and_names_the_fix():
    before = _run("config-audit", "a", {"checks_failed": ("3", 3)}, [_check("EOL-02", Status.FAIL)])
    after = _run("config-audit", "b", {"checks_failed": ("2", 2)}, [_check("EOL-02", Status.PASS)])

    summary = compare.diff(before, after).summary()

    assert summary.startswith("Improved since 4 Aug 12:00")
    assert "EOL-02" in summary


def test_the_summary_says_regressed():
    before = _run("config-audit", "a", {"checks_failed": ("1", 1)})
    after = _run("config-audit", "b", {"checks_failed": ("4", 4)})
    assert compare.diff(before, after).summary().startswith("Regressed")


def test_the_summary_says_mixed_when_both_happened():
    before = _run(
        "config-audit", "a", {"checks_failed": ("2", 2)},
        [_check("A", Status.PASS), _check("B", Status.FAIL)],
    )
    after = _run(
        "config-audit", "b", {"checks_failed": ("2", 2)},
        [_check("A", Status.FAIL), _check("B", Status.PASS)],
    )
    assert compare.diff(before, after).summary().startswith("Mixed")


def test_the_summary_says_nothing_moved():
    before = _run("config-audit", "a", {"checks_failed": ("2", 2)})
    after = _run("config-audit", "a", {"checks_failed": ("2", 2)})
    assert compare.diff(before, after).summary().startswith("No change")


def test_the_summary_names_the_commits_when_they_moved():
    before = _run("size-audit", "aaaaaaaaaa", {"garbage_size": ("1.0 KiB", 1024)})
    after = _run("size-audit", "bbbbbbbbbb", {"garbage_size": ("0 B", 0)})
    assert "→" in compare.diff(before, after).summary()


def test_the_summary_says_when_the_work_tree_was_dirty():
    """Otherwise the comparison quietly credits uncommitted edits to a commit."""
    before = _run("size-audit", "aaa", {"garbage_size": ("1.0 KiB", 1024)}, dirty=True)
    after = _run("size-audit", "aaa", {"garbage_size": ("0 B", 0)})
    assert "uncommitted changes" in compare.diff(before, after).summary()


# ---- refusing to compare ------------------------------------------------------------
def test_two_different_agents_are_not_comparable():
    before = _run("size-audit", "a", {})
    after = _run("config-audit", "a", {})
    assert compare.diff(before, after) is None


# ---- rendering ------------------------------------------------------------------------
def test_a_comparison_renders_as_markdown_and_html():
    before = _run("config-audit", "a", {"checks_failed": ("3", 3)}, [_check("EOL-02", Status.FAIL)])
    after = _run("config-audit", "b", {"checks_failed": ("2", 2)}, [_check("EOL-02", Status.PASS)])
    diff = compare.diff(before, after)

    md = compare.to_markdown(diff)
    html = compare.to_html(diff)

    assert "| EOL-02 A check | FAIL | PASS |" in md
    assert "Checks that changed" in html
    assert "<table" in html


def test_a_check_that_did_not_apply_in_either_run_is_not_a_change():
    before = _run("config-audit", "a", {}, [_check("LFS-04", Status.SKIP)])
    after = _run("config-audit", "b", {}, [_check("LFS-04", Status.SKIP)])
    assert compare.diff(before, after).checks == []
