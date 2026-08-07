"""Counting what each provider was asked to do, and showing it."""

import json
from datetime import datetime, timezone

import pytest

from git_assistant import usage


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Redirect the store; patched where it is imported, as test_identity does."""
    monkeypatch.setattr(usage, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


# ---- recording ------------------------------------------------------------------
def test_a_call_comes_back_exactly_as_it_went_in():
    usage.record("lmstudio", "qwen3.5-4b", 1200, 340)

    loaded = usage.load()

    event = loaded.events[0]
    assert (event.provider, event.model) == ("lmstudio", "qwen3.5-4b")
    assert (event.input_tokens, event.output_tokens, event.total) == (1200, 340, 1540)
    assert event.when.endswith("Z")
    assert event.estimated is False


def test_the_totals_add_up_across_calls():
    for _ in range(3):
        usage.record("lmstudio", "qwen3.5-4b", 100, 10)

    total = usage.load().totals[0]

    assert (total.calls, total.input_tokens, total.output_tokens) == (3, 300, 30)
    assert total.total == 330


def test_each_model_of_each_provider_is_counted_separately():
    usage.record("lmstudio", "qwen3.5-4b", 100, 10)
    usage.record("lmstudio", "another-model", 5, 1)
    usage.record("claude", "claude-opus-5", 7, 2)

    loaded = usage.load()

    assert len(loaded.totals) == 3
    assert {t.model for t in loaded.for_provider("lmstudio")} == {
        "qwen3.5-4b",
        "another-model",
    }
    assert loaded.grand_total() == (3, 112, 13)


def test_the_newest_call_is_first():
    usage.record("lmstudio", "a", 1, 1)
    usage.record("lmstudio", "b", 2, 2)
    assert usage.load().events[0].model == "b"


def test_an_estimated_call_says_so_and_is_counted_apart():
    usage.record("openai", "gpt-4o", 10, 2, estimated=True)
    usage.record("openai", "gpt-4o", 10, 2)

    total = usage.load().totals[0]

    assert total.calls == 2
    assert total.estimated_calls == 1


def test_a_call_without_a_provider_is_not_recorded():
    assert usage.record("", "m", 1, 1) is None
    assert usage.load().totals == []


def test_a_model_that_was_not_named_still_has_a_row():
    usage.record("lmstudio", "", 1, 1)
    assert usage.load().totals[0].model == "(unnamed)"


# ---- the file ---------------------------------------------------------------------
def test_a_missing_file_reads_as_nothing_recorded():
    assert usage.load().totals == []
    assert usage.load().grand_total() == (0, 0, 0)


def test_a_corrupt_file_reads_as_nothing_rather_than_raising(store):
    (store / usage.USAGE_FILE).write_text("{half", encoding="utf-8")
    assert usage.load().totals == []


def test_a_hand_edited_file_keeps_what_can_be_read(store):
    (store / usage.USAGE_FILE).write_text(
        json.dumps(
            {
                "totals": [{"provider": "lmstudio", "model": "m", "calls": 4}],
                "events": [{"provider": "lmstudio", "model": "m", "when": "2026-01-01T00:00:00Z"}],
            }
        ),
        encoding="utf-8",
    )
    loaded = usage.load()
    assert loaded.totals[0].calls == 4
    assert loaded.events[0].model == "m"


def test_saving_leaves_no_temporary_file_behind(store):
    usage.record("lmstudio", "m", 1, 1)
    assert [p.name for p in store.iterdir()] == [usage.USAGE_FILE]


def test_the_detail_is_capped_but_the_totals_are_not():
    """"How much has this cost" must not change when the log is trimmed."""
    for _ in range(12):
        usage.record("lmstudio", "m", 10, 1, limit=5)

    loaded = usage.load()

    assert len(loaded.events) == 5
    assert loaded.totals[0].calls == 12
    assert loaded.totals[0].input_tokens == 120


def test_clearing_forgets_everything():
    usage.record("lmstudio", "m", 1, 1)
    assert usage.clear() is True
    assert usage.load().totals == []


def test_recording_from_several_threads_loses_nothing():
    """A review fans out; every thread finishes here."""
    import threading

    threads = [
        threading.Thread(target=lambda: usage.record("lmstudio", "m", 10, 1))
        for _ in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert usage.load().totals[0].calls == 20


def test_a_disk_that_refuses_is_silent_rather_than_losing_the_answer(monkeypatch):
    """A failure here must never be the reason a generated message is lost."""
    monkeypatch.setattr(
        usage.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("full"))
    )
    assert usage.record("lmstudio", "m", 1, 1) is None


# ---- what a provider reports --------------------------------------------------------
def test_an_openai_shaped_answer_is_read_from_its_own_usage_block():
    payload = {"usage": {"prompt_tokens": 1200, "completion_tokens": 340}}
    assert usage.from_openai_payload(payload) == (1200, 340)


@pytest.mark.parametrize("payload", [{}, {"usage": None}, {"usage": {}}, "nope", None])
def test_an_answer_without_usage_reports_none(payload):
    assert usage.from_openai_payload(payload) is None


def test_a_provider_that_reports_nothing_is_counted_here_and_marked():
    usage.record_openai_response(
        "lmstudio", "m", {}, system="be terse", user="the diff", reply="a message"
    )

    event = usage.load().events[0]

    assert event.estimated is True
    assert event.input_tokens > 0 and event.output_tokens > 0


def test_the_provider_s_own_count_is_preferred_over_ours():
    usage.record_openai_response(
        "lmstudio",
        "m",
        {"usage": {"prompt_tokens": 9999, "completion_tokens": 1}},
        system="be terse",
        user="the diff",
        reply="a message",
    )

    event = usage.load().events[0]

    assert (event.input_tokens, event.output_tokens) == (9999, 1)
    assert event.estimated is False


# ---- reading it back ----------------------------------------------------------------
def test_providers_are_listed_most_recently_used_first():
    usage.record("claude", "opus", 1, 1)
    usage.record("lmstudio", "qwen", 1, 1)
    assert usage.load().providers()[0] == "lmstudio"


def test_two_calls_in_one_millisecond_are_still_ordered(monkeypatch):
    """This test used to fail about half the time, and was right to.

    Recording twice takes well under a millisecond, which is all the stamp
    records -- so the two providers tied, and a tie was resolved by whichever
    was seen *first*: the answer backwards.
    """

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(usage, "datetime", _Frozen)

    usage.record("claude", "opus", 1, 1)
    usage.record("lmstudio", "qwen", 1, 1)

    loaded = usage.load()
    stamps = {t.provider: t.last for t in loaded.totals}
    assert stamps["claude"] == stamps["lmstudio"], "the tie this pins is not real"
    assert loaded.providers() == ["lmstudio", "claude"]


def test_a_provider_older_than_the_kept_events_is_still_listed_last():
    """Ordered by its stamp, which is the one thing a stamp can be trusted for."""
    usage.record("claude", "opus", 1, 1, limit=1)
    usage.record("lmstudio", "qwen", 1, 1, limit=1)

    loaded = usage.load()

    assert [e.provider for e in loaded.events] == ["lmstudio"]  # claude aged out
    assert loaded.providers() == ["lmstudio", "claude"]


def test_a_time_is_shown_in_local_time_not_utc():
    usage.record("lmstudio", "m", 1, 1)
    label = usage.load().events[0].when_label()
    assert ":" in label and "T" not in label


# ---- what the tokens were spent on ------------------------------------------------
def test_a_call_records_what_it_was_for():
    usage.record("lmstudio", "qwen3.5-4b", 100, 10, feature=usage.REVIEW)
    assert usage.load().events[0].feature == "Code review"


def test_one_model_used_for_two_things_is_two_totals():
    usage.record("lmstudio", "m", 100, 10, feature=usage.COMMIT)
    usage.record("lmstudio", "m", 400, 20, feature=usage.REVIEW)

    totals = {t.feature: t.input_tokens for t in usage.load().totals}

    assert totals == {"Commit message": 100, "Code review": 400}


def test_a_call_that_says_nothing_is_marked_as_a_gap_not_as_a_category():
    usage.record("lmstudio", "m", 1, 1)
    assert usage.load().totals[0].feature == usage.UNATTRIBUTED


def test_a_file_written_before_features_existed_still_loads(store):
    """Its rows read as unattributed rather than disappearing."""
    (store / usage.USAGE_FILE).write_text(
        json.dumps(
            {
                "totals": [{"provider": "lmstudio", "model": "m", "calls": 4}],
                "events": [{"provider": "lmstudio", "model": "m", "when": "2026-01-01T00:00:00Z"}],
            }
        ),
        encoding="utf-8",
    )
    loaded = usage.load()
    assert loaded.totals[0].feature == usage.UNATTRIBUTED
    assert loaded.events[0].feature == usage.UNATTRIBUTED


def test_the_client_a_run_builds_carries_what_the_run_is_for():
    """Set where it is known: a completion knows only a provider and a model."""
    from git_assistant.config import Settings
    from git_assistant.llm import build_client

    settings = Settings(provider="lmstudio")
    assert build_client(settings, feature=usage.AUDIT).feature == "Repository audit"
    assert build_client(settings).feature == ""


def test_a_completion_is_filed_under_the_feature_its_client_was_built_with():
    from git_assistant.lmstudio_client import LMStudioClient

    client = LMStudioClient("http://x", feature=usage.COMMIT)
    usage.record_openai_response(
        client.provider_key,
        "m",
        {"usage": {"prompt_tokens": 10, "completion_tokens": 2}},
        system="s",
        user="u",
        reply="r",
        feature=client.feature,
    )

    assert usage.load().events[0].feature == "Commit message"
