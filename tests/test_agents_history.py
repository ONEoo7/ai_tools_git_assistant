"""Recording runs: what is kept, what is thrown away, and what survives damage."""

import json

import pytest

from git_assistant.agents import history
from git_assistant.agents.base import CheckResult, Fact, Report, Section, Status, Table


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Redirect the store; patched where it is imported, as test_identity does."""
    monkeypatch.setattr(history, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


def _report(agent_id="size-audit", repo="/x/demo", head="a" * 40, **facts) -> Report:
    values = {"git_dir_total": ("Total .git size", "1.0 GiB", 1073741824)}
    values.update(facts)
    section = Section(
        number="1",
        title="Summary",
        slot="exec_summary",
        prose="The repository is 1.0 GiB.",
        facts=[Fact(k, v[0], v[1], v[2]) for k, v in values.items()],
        tables=[Table("Top paths", ["Path", "Total"], [["a.bin", "1.0 GiB"]], note="n")],
        commands=[("Run:", "git gc")],
        sections=[Section(number="1.1", title="Nested", prose="Deeper.")],
    )
    return Report(
        agent_id=agent_id,
        title="Git repository size audit",
        subtitle="demo",
        generated_at="04 August 2026 12:00",
        repo_path=repo,
        head=head,
        branch="main",
        dirty=True,
        sections=[section],
        warnings=["something to say"],
    )


# ---- round trip ---------------------------------------------------------------
def test_a_run_comes_back_exactly_as_it_went_in():
    run, problem = history.record(_report())
    assert problem == ""

    loaded = history.load_run(history.list_runs("/x/demo")[0])
    report = loaded.report

    assert report.title == "Git repository size audit"
    assert report.head == "a" * 40 and report.branch == "main" and report.dirty
    assert report.warnings == ["something to say"]
    section = report.sections[0]
    assert section.prose == "The repository is 1.0 GiB."
    assert section.facts[0].raw == 1073741824
    assert section.tables[0].rows == [["a.bin", "1.0 GiB"]]
    assert section.sections[0].number == "1.1"


def test_commands_come_back_as_tuples_not_lists():
    """JSON has no tuples; a rehydrated report must not be subtly different."""
    history.record(_report())
    loaded = history.load_run(history.list_runs("/x/demo")[0])
    assert loaded.report.sections[0].commands == [("Run:", "git gc")]


def test_check_verdicts_survive():
    report = _report(agent_id="config-audit")
    report.checks = [
        CheckResult("EOL-02", "Line endings", Status.FAIL, "set per machine", weight=3)
    ]
    history.record(report)

    loaded = history.load_run(history.list_runs("/x/demo")[0])

    assert loaded.report.checks[0].status is Status.FAIL
    assert loaded.report.checks[0].id == "EOL-02"


def test_nothing_is_written_until_a_run_is_recorded(store):
    assert history.list_runs("/x/demo") == []
    assert not (store / "agent_runs").exists()


# ---- the index is small -----------------------------------------------------
def test_the_index_does_not_carry_the_prose(store):
    """The list must be drawable without reading every report."""
    history.record(_report())
    index = next((store / "agent_runs").rglob("index.json"))

    text = index.read_text(encoding="utf-8")

    assert "The repository is 1.0 GiB." not in text
    assert "git_dir_total" in text  # the headline number is cached
    assert len(text) < 2000


def test_the_headline_is_cached_for_the_list():
    history.record(_report())
    run = history.list_runs("/x/demo")[0]
    assert run.headline["git_dir_total"]["raw"] == 1073741824


# ---- listing ------------------------------------------------------------------
def test_runs_are_listed_newest_first():
    first, _ = history.record(_report())
    second, _ = history.record(_report())
    listed = history.list_runs("/x/demo")
    assert [r.run_id for r in listed] == [second.run_id, first.run_id]


def test_listing_can_be_filtered_to_one_agent():
    history.record(_report(agent_id="size-audit"))
    history.record(_report(agent_id="config-audit"))

    assert len(history.list_runs("/x/demo")) == 2
    assert len(history.list_runs("/x/demo", "config-audit")) == 1


def test_another_repository_has_its_own_history():
    history.record(_report(repo="/x/demo"))
    assert history.list_runs("/x/other") == []


def test_one_repository_however_its_path_is_written():
    """The same answer the repository tree gives: normalized paths are one repo."""
    history.record(_report(repo="D:\\Repo"))
    history.record(_report(repo="d:\\repo\\"))
    assert len(history.list_runs("D:/repo")) == 2


def test_a_path_full_of_awkward_characters_still_gets_a_directory(store):
    history.record(_report(repo="D:\\my repos\\pro:ject ünïcode\\"))
    directories = [p.name for p in (store / "agent_runs").iterdir()]
    assert len(directories) == 1
    assert "/" not in directories[0] and ":" not in directories[0]


# ---- appending and pruning ------------------------------------------------------
def test_recording_does_not_rewrite_an_existing_run_file(store):
    first, _ = history.record(_report())
    path = next((store / "agent_runs").rglob(f"{first.run_id}.json"))
    before = path.read_bytes()

    history.record(_report())

    assert path.read_bytes() == before


def test_the_cap_removes_the_oldest_from_disk(store):
    kept = [history.record(_report(), limit=3)[0] for _ in range(5)]

    listed = history.list_runs("/x/demo")
    files = {p.stem for p in (store / "agent_runs").rglob("*.json") if p.stem != "index"}

    assert len(listed) == 3
    assert [r.run_id for r in listed] == [r.run_id for r in reversed(kept[2:])]
    assert files == {r.run_id for r in kept[2:]}


def test_the_cap_is_per_agent():
    for _ in range(3):
        history.record(_report(agent_id="size-audit"), limit=2)
        history.record(_report(agent_id="config-audit"), limit=2)

    assert len(history.list_runs("/x/demo", "size-audit")) == 2
    assert len(history.list_runs("/x/demo", "config-audit")) == 2


def test_a_pinned_run_outlives_the_cap():
    """The baseline you want to measure against is the one furthest back."""
    baseline, _ = history.record(_report(), limit=2)
    history.set_pinned(baseline, True)
    for _ in range(4):
        history.record(_report(), limit=2)

    assert baseline.run_id in {r.run_id for r in history.list_runs("/x/demo")}


def test_a_limit_of_zero_keeps_everything():
    for _ in range(6):
        history.record(_report(), limit=0)
    assert len(history.list_runs("/x/demo")) == 6


def test_deleting_a_run_removes_it_from_disk_and_the_list(store):
    run, _ = history.record(_report())
    history.record(_report())

    assert history.delete_run(run) is True

    assert run.run_id not in {r.run_id for r in history.list_runs("/x/demo")}
    assert not list((store / "agent_runs").rglob(f"{run.run_id}.json"))


def test_clearing_forgets_one_repository(store):
    history.record(_report(repo="/x/demo"))
    history.record(_report(repo="/x/other"))

    history.clear_repo("/x/demo")

    assert history.list_runs("/x/demo") == []
    assert len(history.list_runs("/x/other")) == 1


# ---- damage -------------------------------------------------------------------
def test_a_corrupt_run_file_is_skipped_not_fatal(store):
    good, _ = history.record(_report())
    bad, _ = history.record(_report())
    path = next((store / "agent_runs").rglob(f"{bad.run_id}.json"))
    path.write_text("{not json", encoding="utf-8")

    assert history.load_run(bad) is None
    assert history.unreadable_count("/x/demo") == 1
    # The index still lists it; the good one still opens.
    assert history.load_run(good).report is not None


def test_a_corrupt_index_is_rebuilt_from_the_run_files(store):
    history.record(_report())
    history.record(_report())
    index = next((store / "agent_runs").rglob("index.json"))
    index.write_text("[]not json", encoding="utf-8")

    listed = history.list_runs("/x/demo")

    assert len(listed) == 2
    assert listed[0].headline  # rebuilt from the reports, not lost


def test_an_index_entry_whose_file_is_gone_does_not_raise(store):
    run, _ = history.record(_report())
    next((store / "agent_runs").rglob(f"{run.run_id}.json")).unlink()

    assert history.list_runs("/x/demo")  # still listed
    assert history.load_run(run) is None  # and says so when opened


def test_a_record_from_a_future_schema_is_still_read(store):
    run, _ = history.record(_report())
    path = next((store / "agent_runs").rglob(f"{run.run_id}.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 99
    data["something_new"] = {"unknown": True}
    path.write_text(json.dumps(data), encoding="utf-8")

    assert history.load_run(history.list_runs("/x/demo")[0]).report is not None


def test_a_run_that_cannot_be_saved_is_reported_not_raised(monkeypatch):
    """A five-minute audit must not be lost because the disk said no."""
    monkeypatch.setattr(
        history.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("full"))
    )
    run, problem = history.record(_report())
    assert run is None
    assert "full" in problem


# ---- labels -------------------------------------------------------------------
def test_a_run_says_when_and_where_it_ran():
    run, _ = history.record(_report())
    assert run.commit_label() == "main @ aaaaaaa*"  # * means uncommitted changes
    assert run.when_label()


def test_an_unparseable_timestamp_does_not_crash_the_label():
    run = history.StoredRun(run_id="x", agent_id="a", repo_path="/x", started_at="soon")
    assert run.when_label() == "soon"
