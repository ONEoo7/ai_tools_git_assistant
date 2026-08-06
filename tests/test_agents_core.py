"""Formatting, figure validation and report rendering.

Pure functions: no git, no Qt, no provider.
"""

from git_assistant.agents.base import AgentInfo, Fact, Report, Section, Table
from git_assistant.config import Settings
from git_assistant.llm import ModelInfo
from git_assistant.agents.facts import (
    allowed_figures,
    facts_block,
    figures_in,
    human_bytes,
    percent,
    unsupported_figures,
)
from git_assistant.agents.report import to_html, to_markdown, to_text
from git_assistant.agents.size_audit import _fold, _matches_any, _parse_line


# ---- sizes ------------------------------------------------------------------
def test_bytes_are_base_1024_like_git_reports_them():
    """git counts in KiB; labelling the same number GB is 7% wrong at scale."""
    assert human_bytes(0) == "0 B"
    assert human_bytes(848) == "848 B"
    assert human_bytes(1024) == "1.0 KiB"
    assert human_bytes(190 * 1024**3) == "190.0 GiB"


def test_percent_of_nothing_is_not_a_division():
    assert percent(5, 0) == "n/a"
    assert percent(44, 100) == "44%"


# ---- the block the model is given --------------------------------------------
def test_facts_block_uses_labels_not_keys():
    """A model handed `git_dir_files` writes `git_dir_files` in the paragraph."""
    block = facts_block([Fact("git_dir_total", "Total .git size", "190.3 GiB")], [])
    assert "Total .git size: 190.3 GiB" in block
    assert "git_dir_total" not in block


def test_facts_block_renders_tables():
    table = Table(title="Top paths", columns=["Path", "Total"], rows=[["a.bin", "9.0 GiB"]])
    block = facts_block([], [table])
    assert "| Path | Total |" in block
    assert "| a.bin | 9.0 GiB |" in block


# ---- what the model may say ---------------------------------------------------
def test_figures_are_found_with_and_without_units():
    found = figures_in("190.3 GiB across 9,997 commits, 44% of it garbage")
    assert "190.3 GiB" in found
    assert "9,997" in found
    assert "44%" in found


def test_a_measured_figure_is_allowed_however_it_is_spaced():
    allowed = allowed_figures([Fact("k", "Total", "190.3 GiB")], [])
    assert unsupported_figures("The repository holds 190.3GiB.", allowed) == []
    assert unsupported_figures("The repository holds 190.3 gib.", allowed) == []


def test_an_invented_figure_is_caught():
    allowed = allowed_figures([Fact("k", "Total", "190.3 GiB")], [])
    assert unsupported_figures("About 42 GB of that is waste.", allowed) == ["42 GB"]


def test_a_number_inside_a_table_cell_is_quotable():
    table = Table("t", ["Path", "Versions"], [["model.eapx", "882"]])
    allowed = allowed_figures([], [table])
    assert unsupported_figures("model.eapx was committed 882 times.", allowed) == []


def test_years_and_small_counts_are_not_claims_about_the_repository():
    allowed = allowed_figures([Fact("k", "Total", "1.0 GiB")], [])
    assert unsupported_figures("In 2026 there were 3 findings.", allowed) == []


# ---- the object-scan parser ----------------------------------------------------
def test_parse_line_reads_a_blob_with_a_path():
    line = "5275313 blob 848 491 .gitattributes"
    assert _parse_line(line) == ("blob", 848, 491, ".gitattributes")


def test_parse_line_reads_a_commit_with_no_path():
    assert _parse_line("28db355 commit 858 505") == ("commit", 858, 505, "")


def test_parse_line_keeps_paths_containing_spaces():
    kind, _size, _disk, path = _parse_line("abc blob 10 5 docs/my report.md")
    assert kind == "blob" and path == "docs/my report.md"


def test_parse_line_keeps_non_ascii_paths():
    _kind, _size, _disk, path = _parse_line("abc blob 10 5 docs/relatório.md")
    assert path == "docs/relatório.md"


def test_parse_line_ignores_what_it_cannot_read():
    assert _parse_line("abc missing") is None
    assert _parse_line("abc blob notanumber 5 x") is None


def test_paths_beyond_the_cap_fold_into_their_directory():
    assert _fold("assets/scans/a.fbx") == "assets/..."
    assert _fold("README.md") == "(top level)"


# ---- gitattributes matching (an approximation, deliberately) -------------------
def test_a_pattern_without_a_slash_matches_the_basename_anywhere():
    assert _matches_any(["*.eapx"], "04_Architecture/model.eapx")
    assert not _matches_any(["*.eapx"], "04_Architecture/model.qea")


def test_a_pattern_with_a_slash_matches_the_path():
    assert _matches_any(["10_Suppliers/**"], "10_Suppliers/Vector/a.zip")
    assert not _matches_any(["10_Suppliers/**"], "src/a.zip")


# ---- rendering ------------------------------------------------------------------
def _report() -> Report:
    child = Section(
        number="2.1",
        title="Orphaned data",
        prose="Nothing to reclaim.",
        facts=[Fact("g", "Garbage", "0 B")],
        commands=[("Run:", "git gc")],
    )
    parent = Section(
        number="2",
        title="Where it goes",
        tables=[Table("Locations", ["Location", "Size"], [["objects/pack", "1.0 GiB"]])],
        sections=[child],
    )
    return Report(
        agent_id="size-audit",
        title="Git repository size audit",
        subtitle="demo — findings",
        generated_at="04 August 2026 12:00",
        repo_path="D:\\repos\\demo",
        sections=[Section(number="1", title="Summary", prose="It is fine."), parent],
    )


def test_markdown_numbers_headings_by_depth():
    md = to_markdown(_report())
    assert "## 1 Summary" in md
    assert "## 2 Where it goes" in md
    assert "### 2.1 Orphaned data" in md
    assert "| objects/pack | 1.0 GiB |" in md
    assert "```bash\ngit gc\n```" in md


def test_html_escapes_content():
    report = _report()
    report.sections[0].prose = "a < b & c"
    html = to_html(report)
    assert "a &lt; b &amp; c" in html
    assert "<table" in html


def test_unverified_prose_says_so_in_both_renderings():
    report = _report()
    report.sections[0].slot = "exec_summary"
    report.sections[0].prose_verified = False
    assert "measurements" in to_markdown(report)
    assert "measurements" in to_html(report)


def test_text_output_drops_the_markup():
    text = to_text(_report())
    assert "```" not in text
    assert "git gc" in text


def test_walk_visits_nested_sections():
    numbers = [s.number for s in _report().walk()]
    assert numbers == ["1", "2", "2.1"]


def test_a_quantity_written_as_a_word_cannot_be_checked_so_it_is_rejected():
    """The model is asked again for digits: "sixty-nine" traces to nothing."""
    allowed = allowed_figures([Fact("k", "Scripts", "69")], [])
    assert unsupported_figures("Sixty-nine scripts are at risk.", allowed) == [
        "Sixty-nine"
    ]
    assert unsupported_figures("69 scripts are at risk.", allowed) == []


def test_small_words_are_left_alone_because_they_are_prose():
    allowed = allowed_figures([Fact("k", "Total", "1.0 GiB")], [])
    assert unsupported_figures("One file dominates, for two reasons.", allowed) == []


def test_scale_words_are_caught_however_they_are_written():
    allowed = allowed_figures([Fact("k", "Files", "2,680")], [])
    bad = unsupported_figures("Two thousand six hundred eighty files.", allowed)
    assert "thousand" in [b.lower() for b in bad]


# ---- every call the narration makes -----------------------------------------------
class _Narratable:
    """An agent that collects instantly and leaves one paragraph to write."""

    info = AgentInfo("stub", "Stub", "collects nothing", "free")

    def collect(self, ctx):
        return Report(
            # A real agent id, so the narrator has an outline to write to.
            agent_id="size-audit",
            title="Stub",
            subtitle="demo",
            generated_at="05 August 2026 12:00",
            repo_path=ctx.repo,
            sections=[
                Section(
                    number="1",
                    title="Executive summary",
                    slot="exec_summary",
                    facts=[Fact("total", "Total", "1.0 GiB")],
                )
            ],
        )


class _Client:
    def chat(self, model, system, user, max_tokens, temperature=0.2):
        return "The repository holds 1.0 GiB."

    def list_models(self):
        return [ModelInfo(id="m", max_context_length=32768, loaded=True)]

    def context_length_for(self, model_id):
        return 32768


def _stub_run(monkeypatch, **kw):
    import git_assistant.agents as agents_pkg

    monkeypatch.setattr(agents_pkg, "get", lambda agent_id: _Narratable())
    monkeypatch.setattr(agents_pkg, "_sample_point", lambda repo: ("abc", "main", False))
    settings = Settings(selected_model="m")
    settings.save = lambda: None
    return agents_pkg.run("stub", settings, repo="/x/demo", narrate=True, **kw)


def test_a_listener_is_handed_every_call_the_narration_makes(monkeypatch):
    """The Audit tab shows them, so a poor paragraph traces to one exchange."""
    import git_assistant.agents as agents_pkg

    # Same signature as the real one: a run says what it is spending on.
    monkeypatch.setattr(
        agents_pkg, "build_client", lambda s, feature="": _Client()
    )
    seen = []

    report = _stub_run(monkeypatch, on_call=seen.append)

    assert seen, "the tab has nothing to show without these"
    assert all(call.phase == "writing a section" for call in seen)
    assert report.sections[0].prose


def test_nothing_pays_for_a_recorder_it_will_not_read(monkeypatch):
    import git_assistant.agents as agents_pkg
    from git_assistant.llm_log import RecordingClient

    given = []
    # Same signature as the real one: a run says what it is spending on.
    monkeypatch.setattr(
        agents_pkg, "build_client", lambda s, feature="": _Client()
    )
    real = agents_pkg.ModelRuntime

    class Spy(real):
        def __init__(self, settings, client):
            given.append(isinstance(client, RecordingClient))
            super().__init__(settings, client)

    monkeypatch.setattr(agents_pkg, "ModelRuntime", Spy)

    _stub_run(monkeypatch)

    assert given == [False]
