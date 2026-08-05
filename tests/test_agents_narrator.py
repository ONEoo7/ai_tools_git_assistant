"""Narration: the model writes the prose, and only about what was measured."""

import pytest

from git_assistant.agents import narrator, prompts
from git_assistant.agents.base import AgentContext, Fact, Report, Section, Table
from git_assistant.agents.facts import facts_block
from git_assistant.model_runtime import ModelRuntime
from git_assistant.config import Settings
from git_assistant.llm import LLMError, ModelInfo


class _Client:
    """Answers with whatever it was given, in order."""

    def __init__(self, *answers, context=32768):
        self.answers = list(answers)
        self.prompts: list[str] = []
        self._context = context

    def list_models(self):
        return [ModelInfo(id="m", max_context_length=self._context, loaded=True)]

    def context_length_for(self, model_id):
        return self._context

    def chat(self, model, system, user, max_tokens, temperature=0.2):
        self.prompts.append(user)
        return self.answers.pop(0) if self.answers else "..."


class _Broken:
    def list_models(self):
        return []

    def context_length_for(self, model_id):
        return 32768

    def chat(self, *a, **kw):
        raise LLMError("the provider is not reachable")


def _report(rows=3) -> Report:
    section = Section(
        number="1",
        title="Executive summary",
        slot="exec_summary",
        facts=[
            Fact("git_dir_total", "Total .git size", "190.3 GiB"),
            Fact("reachable_commits", "Reachable commits", "9,997"),
        ],
        tables=[
            Table("Top paths", ["Path", "Total"], [[f"f{i}.bin", "1.0 GiB"] for i in range(rows)])
        ],
    )
    return Report(
        agent_id="size-audit",
        title="Git repository size audit",
        subtitle="demo",
        generated_at="04 August 2026 12:00",
        repo_path="/repos/demo",
        sections=[section],
    )


def _runtime(client, **kw):
    settings = Settings(selected_model="m", **kw)
    return ModelRuntime(settings, client)


def _ctx():
    return AgentContext(repo="/repos/demo", settings=Settings())


# ---- the happy path -----------------------------------------------------------
def test_prose_that_quotes_the_measurements_is_kept():
    report = _report()
    client = _Client("The .git directory holds 190.3 GiB across 9,997 commits.")

    narrator.narrate(report, _runtime(client), _ctx())

    assert report.find("1").prose.startswith("The .git directory holds")
    assert report.find("1").prose_verified is True
    assert report.warnings == []


def test_the_facts_reach_the_model():
    report = _report()
    client = _Client("190.3 GiB.")
    narrator.narrate(report, _runtime(client), _ctx())
    assert "Total .git size: 190.3 GiB" in client.prompts[0]
    assert "COVER, IN THIS ORDER" in client.prompts[0]


# ---- fabrication ---------------------------------------------------------------
def test_an_invented_figure_is_sent_back_once():
    report = _report()
    client = _Client("About 42 GB is waste.", "The .git directory holds 190.3 GiB.")

    narrator.narrate(report, _runtime(client), _ctx())

    assert len(client.prompts) == 2
    assert "42 GB" in client.prompts[1]  # the correction names it
    assert report.find("1").prose == "The .git directory holds 190.3 GiB."
    assert report.find("1").prose_verified is True


def test_a_model_that_keeps_inventing_loses_its_paragraph():
    report = _report()
    client = _Client("About 42 GB is waste.", "Nearer 43 GB, actually.")

    narrator.narrate(report, _runtime(client), _ctx())

    section = report.find("1")
    assert section.prose_verified is False
    assert "190.3 GiB" in section.prose  # the measurements, rendered plainly
    assert "43 GB" not in section.prose
    assert section.draft == "Nearer 43 GB, actually."
    assert any("not measured" in w for w in report.warnings)


# ---- the provider being unavailable ---------------------------------------------
def test_a_provider_failure_leaves_the_report_intact():
    report = _report()
    narrator.fill_deterministic(report)

    narrator.narrate(report, _runtime(_Broken()), _ctx())

    assert "190.3 GiB" in report.find("1").prose
    assert any("not reachable" in w for w in report.warnings)


def test_deterministic_prose_states_the_measurements():
    report = _report()
    narrator.fill_deterministic(report)
    prose = report.find("1").prose
    assert "total .git size: 190.3 GiB" in prose
    assert "reachable commits: 9,997" in prose


# ---- context budget ------------------------------------------------------------
def test_the_facts_fit_a_small_window_by_dropping_table_rows():
    report = _report(rows=400)
    client = _Client("190.3 GiB.")
    runtime = _runtime(client, context_window=4096)

    narrator.narrate(report, runtime, _ctx())

    sent = client.prompts[0]
    assert sent.count("f0.bin") <= 1
    assert len(sent.splitlines()) < 400  # trimmed to fit
    # The report itself keeps every row; only the prompt was shortened.
    assert len(report.find("1").tables[0].rows) == 400


def test_a_section_with_no_outline_is_left_alone():
    report = _report()
    report.sections[0].slot = "not-a-slot"
    client = _Client("should not be called")

    narrator.narrate(report, _runtime(client), _ctx())

    assert client.prompts == []


def test_cancelling_stops_before_the_next_section():
    report = _report()
    report.sections.append(Section(number="2", title="More", slot="next_steps"))
    ctx = AgentContext(repo="/r", settings=Settings(), is_cancelled=lambda: True)

    with pytest.raises(Exception):
        narrator.narrate(report, _runtime(_Client("x")), ctx)


# ---- prompt shape ---------------------------------------------------------------
def test_the_system_prompt_demands_digits_and_forbids_arithmetic():
    assert "as digits" in prompts.AGENT_SYSTEM
    assert "Never compute" in prompts.AGENT_SYSTEM


def test_every_outline_has_points_and_a_sane_answer_length():
    for agent_id, slots in prompts.OUTLINES.items():
        for slot, outline in slots.items():
            assert outline.points, f"{agent_id}/{slot}"
            assert 192 <= outline.max_tokens() <= 640


def test_facts_block_is_small_enough_to_be_worth_sending():
    from git_assistant.tokenizer import estimate_tokens

    report = _report(rows=10)
    section = report.find("1")
    assert estimate_tokens(facts_block(section.facts, section.tables)) < 1000
