"""Regression tests for the commit panel's repository state."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from git_assistant import commit_history  # noqa: E402
from git_assistant.commit_generator import GenerationResult  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui.preview_dialog import NO_REPOS_MESSAGE, CommitPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Every finished run is recorded, so the store must not be the real one.

    Patched where it is imported, as tests/test_identity.py does.
    """
    monkeypatch.setattr(commit_history, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    return s


def test_warns_when_no_repositories(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)
    assert panel.status.text() == NO_REPOS_MESSAGE
    assert not panel.regen_btn.isEnabled()


def test_warning_clears_once_repositories_are_added(qapp, settings):
    """A fresh install starts with no repos; adding some must clear the notice.

    Reproduces: install clean, add repos via 'Add folder', return to the tab and
    the stale "No repositories configured" text was still shown above a
    perfectly populated repository selector.
    """
    panel = CommitPanel(settings, auto_start=False)
    assert panel.status.text() == NO_REPOS_MESSAGE

    settings.repos = [RepoEntry("/x/a", owner="ONEoo7")]
    settings.active_repo = "/x/a"
    panel.refresh_repos()

    assert panel.repo_picker.count() == 1
    assert panel.status.text() == ""
    assert panel.regen_btn.isEnabled()


def test_refresh_keeps_a_generation_result(qapp, settings):
    """Switching tabs refreshes the panel; that must not wipe the last result."""
    settings.repos = [RepoEntry("/x/a", owner="ONEoo7")]
    settings.active_repo = "/x/a"
    panel = CommitPanel(settings, auto_start=False)

    panel.status.setText("Strategy: single-shot - ~500 input tokens")
    panel.refresh_repos()
    assert panel.status.text() == "Strategy: single-shot - ~500 input tokens"


# ---- how long the message is -------------------------------------------------
def test_the_length_is_shown_as_the_message_is_typed(qapp, settings):
    """The message is editable; a rule that judged only the model would be silent
    about the line the user typed over it."""
    panel = CommitPanel(settings, auto_start=False)

    panel.editor.setPlainText("feat: a short subject\n\nA short body.")

    assert "Subject 21/72" in panel.length_label.text()
    assert "body 13/1000" in panel.length_label.text()


def test_a_subject_over_the_hard_cap_says_what_happens_to_it(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)

    panel.editor.setPlainText("feat: " + "x" * 80)

    assert "cut by tools" in panel.length_label.text()


def test_a_message_within_the_limits_reads_as_counts_and_nothing_else(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)
    panel.editor.setPlainText("feat: fine\n\nAlso fine.")
    assert panel.length_label.text() == "Subject 10/72 - body 10/1000"


def test_an_empty_editor_says_nothing(qapp, settings):
    assert CommitPanel(settings, auto_start=False).length_label.text() == ""


def test_turning_the_rules_off_removes_the_readout(qapp, settings):
    """The limits live with the repository now; see git_assistant.repo_config."""
    from git_assistant import repo_config

    repo_config.write_text(
        repo_config.Tier.USER,
        "",
        '{"commit": {"subject_target": 0, "subject_limit": 0, "body_limit": 0}}',
    )
    panel = CommitPanel(settings, auto_start=False)

    panel.editor.setPlainText("feat: " + "x" * 200)

    assert panel.length_label.text() == ""


def test_a_message_is_never_shortened_to_fit(qapp, settings):
    """Cutting at 72 would produce the mangled subject the limit prevents."""
    panel = CommitPanel(settings, auto_start=False)
    long_one = "feat: " + "x" * 200

    panel.editor.setPlainText(long_one)

    assert panel.editor.toPlainText() == long_one


# ---- offering to ask again when it came back too long ------------------------
def _generated(message, retry=True, calls_before=1):
    from git_assistant.commit_generator import Retry

    return GenerationResult(
        message=message,
        strategy="map-reduce" if calls_before > 1 else "single-shot",
        context_window=8000,
        input_budget=7000,
        input_tokens=100,
        retry=Retry("sys", "the prompt", 512, calls_before=calls_before)
        if retry
        else None,
    )


def _answer(monkeypatch, yes: bool, seen=None):
    def confirm(parent, priced):
        if seen is not None:
            seen.append(priced)
        return yes

    monkeypatch.setattr("git_assistant.ui.preview_dialog.confirm", confirm)


def test_a_message_within_the_limits_is_never_questioned(qapp, settings, monkeypatch):
    seen = []
    _answer(monkeypatch, False, seen)
    panel = CommitPanel(settings, auto_start=False)

    panel._on_finished(_generated("feat: short enough\n\nA body."))

    assert seen == []


def test_an_overlong_message_is_priced_before_anything_is_re_sent(
    qapp, settings, monkeypatch
):
    seen = []
    _answer(monkeypatch, False, seen)
    panel = CommitPanel(settings, auto_start=False)

    panel._on_finished(_generated("feat: " + "x" * 90))

    assert len(seen) == 1
    assert seen[0].calls == 1 and seen[0].input_tokens > 0


def test_declining_keeps_the_message_that_was_generated(qapp, settings, monkeypatch):
    """A run the user declines to redo still produced something."""
    _answer(monkeypatch, False)
    panel = CommitPanel(settings, auto_start=False)
    long_one = "feat: " + "x" * 90

    panel._on_finished(_generated(long_one))

    assert panel.editor.toPlainText() == long_one
    assert "cut by tools" in panel.status.text()


def test_accepting_asks_again_with_the_reason_quoted_back(qapp, settings, monkeypatch):
    _answer(monkeypatch, True)
    started = []
    panel = CommitPanel(settings, auto_start=False)
    monkeypatch.setattr(
        panel, "_start_retry", lambda retry, note: started.append((retry, note))
    )

    panel._on_finished(_generated("feat: " + "x" * 90))

    assert len(started) == 1
    retry, note = started[0]
    assert "96 characters" in note and "72 allowed" in note
    assert retry.user == "the prompt"


def test_a_map_reduce_run_offers_one_call_not_fifteen(qapp, settings, monkeypatch):
    seen = []
    _answer(monkeypatch, False, seen)
    panel = CommitPanel(settings, auto_start=False)

    panel._on_finished(_generated("feat: " + "x" * 90, calls_before=15))

    assert seen[0].calls == 1
    assert any("15 call(s) the first time" in line for line in seen[0].lines)


def test_a_result_with_no_prompt_behind_it_is_not_offered(qapp, settings, monkeypatch):
    seen = []
    _answer(monkeypatch, False, seen)
    panel = CommitPanel(settings, auto_start=False)

    panel._on_finished(_generated("feat: " + "x" * 90, retry=False))

    assert seen == []


def test_a_second_answer_is_shown_whether_or_not_it_is_shorter(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)

    panel._on_retried("feat: " + "y" * 90)

    assert panel.editor.toPlainText().startswith("feat: y")
    assert "Still over the limit" in panel.progress.text()


def test_a_second_answer_that_fits_says_so(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)
    panel._on_retried("feat: now it fits")
    assert panel.progress.text() == "Done."


def test_it_is_not_offered_a_third_time(qapp, settings, monkeypatch):
    """A model that ignored the instruction once will ignore it again."""
    seen = []
    _answer(monkeypatch, False, seen)
    panel = CommitPanel(settings, auto_start=False)

    panel._on_retried("feat: " + "y" * 90)

    assert seen == []


# ---- which provider and model a generation will use --------------------------
def test_the_model_is_named_beside_the_provider(qapp, settings):
    """The provider is half the answer; a generation uses a model too."""
    settings.selected_model = "qwen3.5-4b"
    panel = CommitPanel(settings, auto_start=False)

    panel.refresh_provider()

    assert "qwen3.5-4b" in panel.provider_label.text()


def test_switching_provider_here_renames_the_model_too(qapp, settings):
    """The model is per provider: leaving the old one named would be a lie."""
    settings.provider = "lmstudio"
    settings.selected_model = "qwen3.5-4b"
    settings.provider_models = {"ollama": "llama3.2"}
    panel = CommitPanel(settings, auto_start=False)

    panel.provider_combo.setCurrentIndex(panel.provider_combo.findData("ollama"))

    assert "llama3.2" in panel.provider_label.text()


def test_a_provider_with_no_model_chosen_says_so(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)
    panel.refresh_provider()
    assert "no model selected" in panel.provider_label.text()


# ---- the Refresh button -----------------------------------------------------
# Staging happens outside this window, and nothing here notices it. Refresh
# re-reads it and drops the message written for the previous set of changes.


def _repo(tmp_path):
    """A real repo, since Refresh re-runs `git diff` against one."""
    import subprocess
    import sys

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    d = tmp_path / "repo"
    d.mkdir()
    for args in (["init"], ["config", "user.email", "t@e.example"], ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(d), *args], capture_output=True, creationflags=flags)
    return d


def test_refresh_clears_the_generated_message(qapp, settings, tmp_path):
    repo = _repo(tmp_path)
    settings.repos = [RepoEntry(str(repo))]
    settings.active_repo = str(repo)
    panel = CommitPanel(settings, auto_start=False)

    panel.editor.setPlainText("feat: a message about changes that are no longer staged")
    panel.regen_btn.setText("Regenerate")

    panel.refresh_btn.click()

    assert panel.editor.toPlainText() == ""
    # Nothing left to regenerate, so the button says what it now does.
    assert panel.regen_btn.text() == "Generate"


def test_refresh_picks_up_files_staged_outside_the_window(qapp, settings, tmp_path):
    import subprocess
    import sys

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    repo = _repo(tmp_path)
    settings.repos = [RepoEntry(str(repo))]
    settings.active_repo = str(repo)
    panel = CommitPanel(settings, auto_start=False)
    assert panel.file_list.count() == 0

    (repo / "new.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "new.py"],
        capture_output=True,
        creationflags=flags,
    )

    panel.refresh_btn.click()

    assert panel.file_list.count() == 1


def test_refresh_is_disabled_while_generating(qapp, settings, tmp_path):
    """It clears the widgets a running generation is about to fill."""
    repo = _repo(tmp_path)
    settings.repos = [RepoEntry(str(repo))]
    settings.active_repo = str(repo)
    panel = CommitPanel(settings, auto_start=False)

    panel._set_busy(True)
    assert not panel.refresh_btn.isEnabled()
    panel._set_busy(False)
    assert panel.refresh_btn.isEnabled()


# ---- submodules are nested under the repository that contains them ----------
def _rows(picker):
    """(label, depth) for every row of the picker, top to bottom."""
    out = []

    def rec(item, depth):
        out.append((item.text(0), depth))
        for i in range(item.childCount()):
            rec(item.child(i), depth + 1)

    tree = picker.repo_list
    for i in range(tree.topLevelItemCount()):
        rec(tree.topLevelItem(i), 0)
    return out


def test_picker_nests_submodules_under_their_repo(qapp, settings):
    settings.repos = [
        RepoEntry("/x/alpha"),
        RepoEntry("/x/alpha/libs/inner"),
        RepoEntry("/x/beta"),
    ]
    settings.active_repo = "/x/alpha"
    panel = CommitPanel(settings, auto_start=False)

    assert _rows(panel.repo_picker) == [("alpha", 0), ("inner", 1), ("beta", 0)]
    # A submodule is a repository in its own right, so it counts as one.
    assert panel.repo_picker.count() == 3


def test_picker_can_select_a_submodule(qapp, settings):
    settings.repos = [RepoEntry("/x/alpha"), RepoEntry("/x/alpha/libs/inner")]
    settings.active_repo = "/x/alpha/libs/inner"
    panel = CommitPanel(settings, auto_start=False)

    assert panel.repo_picker.current_path() == "/x/alpha/libs/inner"


def test_picker_filter_keeps_the_parent_of_a_matching_submodule(qapp, settings):
    settings.repos = [
        RepoEntry("/x/alpha"),
        RepoEntry("/x/alpha/libs/inner"),
        RepoEntry("/x/beta"),
    ]
    settings.active_repo = "/x/alpha"
    panel = CommitPanel(settings, auto_start=False)
    tree = panel.repo_picker.repo_list

    panel.repo_picker.filter_edit.setText("inner")

    alpha = tree.topLevelItem(0)
    assert not alpha.isHidden()  # a match must not be stranded out of its tree
    assert not alpha.child(0).isHidden()
    assert tree.topLevelItem(1).isHidden()  # beta matches nothing


# ---- the branch selector ----------------------------------------------------
# Picking a branch checks it out: the diff being described is the work tree's,
# and so is the commit, so the choice cannot mean anything else.
def _run_git(repo, *args):
    import subprocess
    import sys

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, creationflags=flags
    )


def _repo_with_branches(tmp_path):
    """A repo with a commit and a second branch, so there is something to pick."""
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _run_git(repo, "add", "a.txt")
    _run_git(repo, "commit", "-m", "first")
    _run_git(repo, "branch", "feature")
    return repo


def _panel_for(settings, repo):
    settings.repos = [RepoEntry(str(repo))]
    settings.active_repo = str(repo)
    return CommitPanel(settings, auto_start=False)


def test_branch_selector_lists_branches_and_shows_the_current_one(qapp, settings, tmp_path):
    from git_assistant import git_ops

    repo = _repo_with_branches(tmp_path)
    panel = _panel_for(settings, repo)

    shown = [panel.branch_combo.itemText(i) for i in range(panel.branch_combo.count())]
    assert set(shown) == {git_ops.current_branch(repo), "feature"}
    assert panel.branch_combo.currentData() == git_ops.current_branch(repo)


def test_choosing_a_branch_checks_it_out(qapp, settings, tmp_path):
    from git_assistant import git_ops

    repo = _repo_with_branches(tmp_path)
    panel = _panel_for(settings, repo)

    panel.branch_combo.setCurrentIndex(panel.branch_combo.findData("feature"))

    assert git_ops.current_branch(repo) == "feature"
    assert "feature" in panel.status.text()


def test_switching_branch_drops_the_message_written_for_the_other_one(
    qapp, settings, tmp_path
):
    repo = _repo_with_branches(tmp_path)
    panel = _panel_for(settings, repo)
    panel.editor.setPlainText("feat: something about the other branch")

    panel.branch_combo.setCurrentIndex(panel.branch_combo.findData("feature"))

    assert panel.editor.toPlainText() == ""


def test_declining_the_confirmation_leaves_the_branch_alone(
    qapp, settings, tmp_path, monkeypatch
):
    """Uncommitted work follows you across, so the switch is never silent."""
    from PyQt6.QtWidgets import QMessageBox

    from git_assistant import git_ops

    repo = _repo_with_branches(tmp_path)
    (repo / "a.txt").write_text("edited\n", encoding="utf-8")  # now dirty
    panel = _panel_for(settings, repo)
    start = git_ops.current_branch(repo)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel
    )

    panel.branch_combo.setCurrentIndex(panel.branch_combo.findData("feature"))

    assert git_ops.current_branch(repo) == start
    assert panel.branch_combo.currentData() == start, "the box must not lie"


def test_a_clean_repo_switches_without_asking(qapp, settings, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from git_assistant import git_ops

    repo = _repo_with_branches(tmp_path)
    panel = _panel_for(settings, repo)

    def refuse(*_a, **_k):
        raise AssertionError("nothing to warn about in a clean work tree")

    monkeypatch.setattr(QMessageBox, "question", refuse)
    panel.branch_combo.setCurrentIndex(panel.branch_combo.findData("feature"))

    assert git_ops.current_branch(repo) == "feature"


def test_a_repo_without_commits_offers_nothing_to_switch_to(qapp, settings, tmp_path):
    """An unborn branch has no ref; the box shows the state and stays inert."""
    panel = _panel_for(settings, _repo(tmp_path))

    assert panel.branch_combo.isEnabled() is False


def test_the_branch_box_is_disabled_while_generating(qapp, settings, tmp_path):
    """Switching mid-run changes the diff the worker is describing."""
    panel = _panel_for(settings, _repo_with_branches(tmp_path))

    panel._set_busy(True)
    assert not panel.branch_combo.isEnabled()
    panel._set_busy(False)
    assert panel.branch_combo.isEnabled()


# ---- View LLM Calls -----------------------------------------------------------
def _call(index=1, phase="summarizing a chunk", response="a note"):
    from git_assistant.llm_log import LlmCall

    return LlmCall(
        index=index,
        phase=phase,
        model="qwen3.5-4b",
        system="be terse",
        user="diff fragment here",
        max_tokens=384,
        response=response,
        seconds=1.5,
    )


def test_the_calls_pane_starts_empty(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)
    assert panel.calls_list.count() == 0
    assert panel.calls_label.text() == "View LLM Calls"
    assert not panel.copy_all_calls_btn.isEnabled()


def test_each_call_appears_as_it_finishes(qapp, settings):
    """A map-reduce run is minutes long; the calls should not arrive all at once."""
    panel = CommitPanel(settings, auto_start=False)

    panel._on_call(_call(1))
    panel._on_call(_call(2, phase="writing the message", response="feat: a change"))

    assert panel.calls_list.count() == 2
    assert "2" in panel.calls_label.text()
    assert panel.copy_all_calls_btn.isEnabled()


def test_selecting_a_call_shows_exactly_what_was_sent_and_returned(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)
    panel._on_call(_call(response="the model's answer"))

    panel.calls_list.setCurrentRow(0)
    shown = panel.call_view.toPlainText()

    assert "be terse" in shown  # the system prompt
    assert "diff fragment here" in shown  # the user prompt, verbatim
    assert "the model's answer" in shown
    assert "qwen3.5-4b" in shown and "384" in shown


def test_the_final_synthesis_call_is_listed_like_the_rest(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)
    panel._on_call(_call(1, phase="summarizing a chunk"))
    panel._on_call(_call(2, phase="writing the message"))

    assert "writing the message" in panel.calls_list.item(1).text()


def test_a_failed_call_is_marked(qapp, settings):
    from git_assistant.llm_log import LlmCall

    panel = CommitPanel(settings, auto_start=False)
    panel._on_call(
        LlmCall(1, "summarizing a chunk", "m", "s", "u", 384, error="HTTP 500")
    )

    assert "[failed]" in panel.calls_list.item(0).text()
    panel.calls_list.setCurrentRow(0)
    assert "HTTP 500" in panel.call_view.toPlainText()


def test_starting_a_run_clears_the_previous_run_s_calls(qapp, settings, tmp_path):
    """They describe the diff that was, which is exactly the confusing case."""
    repo = _repo(tmp_path)
    settings.repos = [RepoEntry(str(repo))]
    settings.active_repo = str(repo)
    panel = CommitPanel(settings, auto_start=False)
    panel._on_call(_call())
    assert panel.calls_list.count() == 1

    panel._reset_calls()

    assert panel.calls_list.count() == 0
    assert panel.call_view.toPlainText() == ""
    assert not panel.copy_all_calls_btn.isEnabled()


def test_the_commit_tab_uses_the_shared_calls_pane(qapp, settings):
    """The same widget the Code Review tab shows, so the two cannot drift."""
    from git_assistant.ui.calls_pane import CallsPane

    panel = CommitPanel(settings, auto_start=False)

    assert isinstance(panel.calls_pane, CallsPane)
    assert panel.calls_list is panel.calls_pane.calls_list


def test_copying_a_call_says_so_in_the_panel_s_own_status_line(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)
    panel._on_call(_call())

    panel.calls_pane._on_copy_all()

    assert "copied to the clipboard" in panel.progress.text()


# ---- Previous Runs ------------------------------------------------------------
def _result(message="feat: a change", strategy="single-shot"):
    return GenerationResult(
        message=message,
        strategy=strategy,
        context_window=32768,
        input_budget=29492,
        input_tokens=1757,
    )


def _panel_with_repo(settings, tmp_path):
    repo = _repo(tmp_path)
    settings.repos = [RepoEntry(str(repo))]
    settings.active_repo = str(repo)
    return CommitPanel(settings, auto_start=False)


def test_the_right_hand_pane_shows_previous_runs_first(qapp, settings):
    from git_assistant.ui.side_panel import CALLS_TAB, HISTORY_TAB

    panel = CommitPanel(settings, auto_start=False)

    assert panel.side_panel.tabs.tabText(0) == HISTORY_TAB
    assert panel.side_panel.tabs.tabText(1) == CALLS_TAB
    assert panel.side_panel.tabs.currentIndex() == 0


def test_a_generated_message_is_recorded_and_listed(qapp, settings, tmp_path):
    """Regenerating is normal, and the second message is often worse."""
    panel = _panel_with_repo(settings, tmp_path)

    panel._on_finished(_result("feat: the first one"))

    assert panel.runs_tree.topLevelItemCount() == 1
    assert "feat: the first one" in panel.runs_tree.topLevelItem(0).text(1)


def test_regenerating_keeps_the_earlier_message(qapp, settings, tmp_path):
    panel = _panel_with_repo(settings, tmp_path)

    panel._on_finished(_result("feat: the first one"))
    panel._on_finished(_result("feat: the second one"))

    subjects = [
        panel.runs_tree.topLevelItem(i).text(1)
        for i in range(panel.runs_tree.topLevelItemCount())
    ]
    assert any("the first one" in s for s in subjects)
    assert any("the second one" in s for s in subjects)


def test_opening_an_earlier_message_puts_it_back_in_the_editor(qapp, settings, tmp_path):
    panel = _panel_with_repo(settings, tmp_path)
    panel._on_finished(_result("feat: the first one"))
    panel._on_finished(_result("feat: the second one"))

    for i in range(panel.runs_tree.topLevelItemCount()):
        item = panel.runs_tree.topLevelItem(i)
        if "the first one" in item.text(1):
            panel.runs_tree.setCurrentItem(item)
    panel._on_open_run()

    assert panel.editor.toPlainText() == "feat: the first one"


def _with_calls(message, *phases):
    """A finished run that recorded an exchange per phase."""
    from git_assistant.llm_log import LlmCall

    result = _result(message)
    result.calls = [
        LlmCall(
            index=i,
            phase=phase,
            model="m",
            system="sys",
            user=f"the prompt for {phase}",
            max_tokens=512,
            response=f"the answer from {phase}",
        )
        for i, phase in enumerate(phases, start=1)
    ]
    return result


def _open_first_run(panel):
    panel.runs_tree.setCurrentItem(panel.runs_tree.topLevelItem(0))
    panel._on_open_run()


def test_opening_an_earlier_message_shows_the_calls_that_wrote_it(
    qapp, settings, tmp_path
):
    """The reported gap: the message came back but the calls pane did not."""
    panel = _panel_with_repo(settings, tmp_path)
    panel._on_finished(_with_calls("feat: the one", "summarizing a chunk", "writing the message"))
    panel._reset_calls()  # as reopening the window, or running something else, does

    _open_first_run(panel)

    assert panel.calls_list.count() == 2
    assert [c.phase for c in panel._calls] == [
        "summarizing a chunk",
        "writing the message",
    ]
    assert "the prompt for writing the message" in panel._calls[1].transcript()
    assert "2 call(s)" in panel.progress.text()


def test_opening_a_run_replaces_the_calls_of_the_one_before_it(qapp, settings, tmp_path):
    """One run's prompts under another run's message explain nothing."""
    panel = _panel_with_repo(settings, tmp_path)
    panel._on_finished(_with_calls("feat: the first", "single-shot"))
    panel._on_finished(
        _with_calls("feat: the second", "summarizing a chunk", "writing the message")
    )

    for i in range(panel.runs_tree.topLevelItemCount()):
        item = panel.runs_tree.topLevelItem(i)
        if "the first" in item.text(1):
            panel.runs_tree.setCurrentItem(item)
    panel._on_open_run()

    assert panel.editor.toPlainText() == "feat: the first"
    assert [c.phase for c in panel._calls] == ["single-shot"]


def test_opening_a_message_from_before_the_calls_were_kept_says_so(
    qapp, settings, tmp_path
):
    """Silence would read as "this run made no calls", which is a lie."""
    panel = _panel_with_repo(settings, tmp_path)
    panel._on_finished(_result("feat: no calls recorded"))  # no calls on the result

    _open_first_run(panel)

    assert panel.calls_list.count() == 0
    assert "before the calls were kept" in panel.call_view.toPlainText()


def test_open_and_delete_are_offered_only_when_a_run_is_selected(qapp, settings, tmp_path):
    panel = _panel_with_repo(settings, tmp_path)
    panel._on_finished(_result())
    panel.runs_tree.clearSelection()

    panel._on_run_selection()
    assert not panel.open_run_btn.isEnabled()

    panel.runs_tree.setCurrentItem(panel.runs_tree.topLevelItem(0))
    assert panel.open_run_btn.isEnabled() and panel.delete_run_btn.isEnabled()


def test_deleting_a_message_removes_it_from_the_list(qapp, settings, tmp_path, monkeypatch):
    panel = _panel_with_repo(settings, tmp_path)
    panel._on_finished(_result())
    panel.runs_tree.setCurrentItem(panel.runs_tree.topLevelItem(0))
    # Deleting asks; a test that does not answer waits for a dialog nobody can
    # see, for as long as the run is given.
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )

    panel._on_delete_run()

    assert panel.runs_tree.topLevelItemCount() == 0


def test_a_run_that_produced_nothing_is_not_listed(qapp, settings, tmp_path):
    panel = _panel_with_repo(settings, tmp_path)
    panel._on_finished(_result(message="   "))
    assert panel.runs_tree.topLevelItemCount() == 0


def test_another_repository_s_messages_are_not_shown_here(qapp, settings, tmp_path):
    panel = _panel_with_repo(settings, tmp_path)
    panel._on_finished(_result())
    assert panel.runs_tree.topLevelItemCount() == 1

    (tmp_path / "other").mkdir()
    other = _repo(tmp_path / "other")
    settings.repos.append(RepoEntry(str(other)))
    settings.active_repo = str(other)
    panel.repo_picker.refresh()
    panel._on_repo_selected(str(other))

    assert panel.runs_tree.topLevelItemCount() == 0


# ---- asked before anything is sent ------------------------------------------------
def test_generating_asks_what_it_will_send_first(qapp, settings, tmp_path, monkeypatch):
    """The estimate is shown after the button is pressed and before the first call."""
    asked = []
    monkeypatch.setattr(
        "git_assistant.ui.preview_dialog.confirm",
        lambda parent, est: asked.append(est) or True,
    )
    started = []
    monkeypatch.setattr(
        "git_assistant.ui.preview_dialog.run_worker", lambda w: started.append(w)
    )
    panel = _panel_with_repo(settings, tmp_path)

    panel._start()

    assert asked and asked[0].feature == "Commit message"
    assert started, "agreeing runs it"


def test_declining_sends_nothing(qapp, settings, tmp_path, monkeypatch):
    monkeypatch.setattr("git_assistant.ui.preview_dialog.confirm", lambda *a: False)
    started = []
    monkeypatch.setattr(
        "git_assistant.ui.preview_dialog.run_worker", lambda w: started.append(w)
    )
    panel = _panel_with_repo(settings, tmp_path)

    panel._start()

    assert started == []
    assert panel.regen_btn.isEnabled(), "the panel is not left looking busy"


# ---- selecting several previous messages ------------------------------------------
def _select_all_runs(panel):
    panel.runs_tree.selectAll()
    return panel._selected_runs()


def test_several_messages_can_be_selected_at_once(qapp, settings, tmp_path):
    """Tidying up is the thing anybody does to a list of twenty."""
    panel = _panel_with_repo(settings, tmp_path)
    panel._on_finished(_result("feat: one"))
    panel._on_finished(_result("feat: two"))

    assert len(_select_all_runs(panel)) == 2


def test_open_is_withdrawn_while_more_than_one_is_selected(qapp, settings, tmp_path):
    """Opening is a question about one message; two is not an answer."""
    panel = _panel_with_repo(settings, tmp_path)
    panel._on_finished(_result("feat: one"))
    panel._on_finished(_result("feat: two"))

    _select_all_runs(panel)
    assert not panel.open_run_btn.isEnabled()
    assert panel.delete_run_btn.isEnabled()

    panel.runs_tree.setCurrentItem(panel.runs_tree.topLevelItem(0))
    assert panel.open_run_btn.isEnabled()


def test_deleting_removes_every_selected_message(qapp, settings, tmp_path, monkeypatch):
    panel = _panel_with_repo(settings, tmp_path)
    for i in range(3):
        panel._on_finished(_result(f"feat: number {i}"))
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )

    _select_all_runs(panel)
    panel._on_delete_run()

    assert panel.runs_tree.topLevelItemCount() == 0


def test_deleting_several_asks_first_and_says_how_many(qapp, settings, tmp_path, monkeypatch):
    """A stray Ctrl+A must not put twenty messages behind one click."""
    panel = _panel_with_repo(settings, tmp_path)
    for i in range(3):
        panel._on_finished(_result(f"feat: number {i}"))
    asked = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Cancel,
    )

    _select_all_runs(panel)
    panel._on_delete_run()

    assert asked and "3" in asked[0]
    assert panel.runs_tree.topLevelItemCount() == 3  # declined, so nothing went
