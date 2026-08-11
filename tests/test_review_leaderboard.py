"""The leaderboard: what accumulates across runs, and what must not be mixed."""

import json

import pytest

from git_assistant.review import leaderboard, rule_files


@pytest.fixture(autouse=True)
def board_dir(tmp_path, monkeypatch):
    """The board lives beside the rule files, so redirecting those moves it."""
    monkeypatch.setattr(rule_files, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


def _record(model="qwen", judge="sonnet", scores=(7.0,), provider="lmstudio"):
    return leaderboard.record(
        provider=provider,
        model=model,
        judge_provider="claude",
        judge_model=judge,
        scores=list(scores),
    )


# ---- where it lives ----------------------------------------------------------------
def test_it_sits_beside_the_rule_files(board_dir):
    assert leaderboard.path().parent == rule_files.rules_dir()
    assert leaderboard.path().name == "leaderboard.json"


def test_a_board_nobody_has_written_reads_as_empty():
    assert leaderboard.load().rows == []


# ---- folding a run in ----------------------------------------------------------------
def test_a_run_becomes_a_row():
    _record(scores=[7.0, 8.0])

    rows = leaderboard.load().rows

    assert len(rows) == 1
    assert (rows[0].runs, rows[0].files) == (1, 2)
    assert rows[0].mean == 7.5


def test_a_second_run_folds_into_the_same_row():
    _record(scores=[7.0, 8.0])
    _record(scores=[6.0])

    rows = leaderboard.load().rows

    assert len(rows) == 1
    assert (rows[0].runs, rows[0].files) == (2, 3)
    assert rows[0].mean == 7.0  # (7 + 8 + 6) / 3


def test_the_running_total_is_kept_so_the_mean_survives_pruned_history():
    """Reviews are pruned as they age; the board must not need them."""
    _record(scores=[10.0])
    _record(scores=[0.0])

    row = leaderboard.load().rows[0]

    assert row.total == 10.0
    assert row.mean == 5.0


def test_a_run_that_scored_nothing_is_not_recorded():
    """Counting it would move the runs column without moving the mean, which
    reads as a model having been measured when it was not."""
    _record(scores=[])
    assert leaderboard.load().rows == []


# ---- what makes a row its own ----------------------------------------------------
def test_a_different_judge_starts_a_different_row():
    """A 7 from Opus and a 7 from a 4B model are not the same measurement."""
    _record(judge="sonnet", scores=[7.0])
    _record(judge="opus", scores=[9.0])

    rows = leaderboard.load().rows

    assert len(rows) == 2
    assert {r.judge_model for r in rows} == {"sonnet", "opus"}
    assert {r.mean for r in rows} == {7.0, 9.0}  # neither moved the other


def test_a_different_reviewed_model_starts_a_different_row():
    _record(model="qwen", scores=[7.0])
    _record(model="phi", scores=[4.0])
    assert len(leaderboard.load().rows) == 2


def test_the_same_model_name_on_two_providers_is_two_rows():
    """`llama3` on Ollama and on a hosted endpoint are not the same thing."""
    _record(provider="ollama", scores=[7.0])
    _record(provider="lmstudio", scores=[3.0])
    assert len(leaderboard.load().rows) == 2


# ---- reading it back ---------------------------------------------------------------
def test_the_best_comes_first():
    _record(model="middling", scores=[5.0])
    _record(model="good", scores=[9.0])
    _record(model="poor", scores=[1.0])

    assert [r.model for r in leaderboard.load().ranked()] == ["good", "middling", "poor"]


def test_a_tie_is_broken_by_which_was_measured_more():
    _record(model="barely-tested", scores=[8.0])
    _record(model="well-tested", scores=[8.0, 8.0, 8.0])

    assert leaderboard.load().ranked()[0].model == "well-tested"


def test_a_damaged_board_reads_as_empty_rather_than_raising():
    """Losing the board must never be able to fail the review being recorded."""
    _record()
    leaderboard.path().write_text("{ not json", encoding="utf-8")

    assert leaderboard.load().rows == []


def test_a_row_that_makes_no_sense_is_dropped_and_the_rest_kept():
    _record(model="real")
    data = json.loads(leaderboard.path().read_text(encoding="utf-8"))
    data["rows"].append({"model": "", "runs": "lots"})
    leaderboard.path().write_text(json.dumps(data), encoding="utf-8")

    rows = leaderboard.load().rows

    assert [r.model for r in rows] == ["real"]


def test_clearing_leaves_nothing_behind():
    _record()
    assert leaderboard.clear() == ""
    assert leaderboard.load().rows == []


def test_clearing_a_board_that_is_not_there_is_not_an_error():
    assert leaderboard.clear() == ""


# ---- how long the model took ---------------------------------------------------------
def test_the_time_is_kept_and_divided_by_the_files_it_covers():
    leaderboard.record(
        provider="lmstudio",
        model="qwen",
        judge_provider="claude",
        judge_model="sonnet",
        scores=[7.0, 8.0],
        seconds=5.0,
    )

    row = leaderboard.load().rows[0]

    assert row.seconds == 5.0
    assert row.secs_per_file == 2.5


def test_the_time_accumulates_like_the_score_does():
    for scores, seconds in (([7.0, 8.0], 4.0), ([6.0], 8.0)):
        leaderboard.record(
            provider="lmstudio",
            model="qwen",
            judge_provider="claude",
            judge_model="sonnet",
            scores=scores,
            seconds=seconds,
        )

    row = leaderboard.load().rows[0]

    assert row.seconds == 12.0
    assert row.secs_per_file == 4.0  # 12s over 3 files


def test_a_run_recorded_without_a_time_leaves_the_column_empty():
    """Runs stored before timing was kept, and anything that forgets to say."""
    _record()
    row = leaderboard.load().rows[0]
    assert row.seconds == 0.0
    assert row.secs_per_file == 0.0


def test_a_nonsense_time_cannot_drag_the_average_backwards():
    leaderboard.record(
        provider="p",
        model="m",
        judge_provider="j",
        judge_model="k",
        scores=[7.0],
        seconds=-30.0,
    )
    assert leaderboard.load().rows[0].seconds == 0.0


def test_an_old_board_without_times_still_reads():
    """A board written before this column existed must not be lost over it."""
    _record()
    data = json.loads(leaderboard.path().read_text(encoding="utf-8"))
    for row in data["rows"]:
        row.pop("seconds", None)
        row.pop("secs_per_file", None)
    leaderboard.path().write_text(json.dumps(data), encoding="utf-8")

    rows = leaderboard.load().rows

    assert len(rows) == 1
    assert rows[0].secs_per_file == 0.0
