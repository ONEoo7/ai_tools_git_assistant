"""The Agents tab, and the tab wiring it depends on."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant import agents  # noqa: E402
from git_assistant.agents import history  # noqa: E402
from git_assistant.agents.base import Fact, Report, Section, Table  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.providers import PROVIDERS  # noqa: E402
from git_assistant.ui.agents_panel import NO_REPOS_MESSAGE, AgentsPanel  # noqa: E402
from git_assistant.ui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Every finished run is recorded, so the store must not be the real one.

    Patched where it is imported, as tests/test_identity.py does.
    """
    monkeypatch.setattr(history, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    return s


@pytest.fixture
def with_repo(settings):
    settings.repos = [RepoEntry("/x/demo")]
    settings.active_repo = "/x/demo"
    return settings


def _report() -> Report:
    return Report(
        agent_id="size-audit",
        title="Git repository size audit",
        subtitle="demo",
        generated_at="04 August 2026 12:00",
        repo_path="/x/demo",
        sections=[
            Section(
                number="1",
                title="Summary",
                prose="It is 190.3 GiB.",
                facts=[Fact("t", "Total .git size", "190.3 GiB")],
                tables=[Table("Top paths", ["Path", "Total"], [["a.bin", "1.0 GiB"]])],
            )
        ],
    )


# ---- what the panel offers ----------------------------------------------------
def test_every_registered_agent_is_listed(qapp, settings):
    panel = AgentsPanel(settings)
    shown = [panel.agent_list.item(i).text() for i in range(panel.agent_list.count())]
    assert shown == [info.label for info in agents.infos()]


def test_run_needs_a_repository(qapp, settings):
    panel = AgentsPanel(settings)
    assert panel.status.text() == NO_REPOS_MESSAGE
    assert not panel.run_btn.isEnabled()


def test_run_is_offered_once_a_repository_exists(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    assert panel.run_btn.isEnabled()
    assert panel.status.text() == ""


def test_cancel_and_export_are_offered_only_when_they_mean_something(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    assert not panel.cancel_btn.isEnabled()
    assert not panel.copy_btn.isEnabled()
    assert not panel.export_btn.isEnabled()


def test_choosing_an_agent_describes_it_and_is_remembered(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    panel.agent_list.setCurrentRow(1)

    info = agents.infos()[1]
    assert info.description[:40] in panel.agent_description.text()
    assert info.cost_hint in panel.agent_description.text()
    assert with_repo.agent_last_id == info.id


def test_the_stored_agent_is_selected_on_open(qapp, with_repo):
    with_repo.agent_last_id = "config-audit"
    panel = AgentsPanel(with_repo)
    assert panel._agent_id() == "config-audit"


def test_fast_mode_is_only_offered_where_it_applies(qapp, with_repo):
    """The config audit has no history scan to skip."""
    panel = AgentsPanel(with_repo)
    panel.agent_list.setCurrentRow(0)  # size audit
    assert panel.fast_check.isVisible() or not panel.isVisible()
    panel.agent_list.setCurrentRow(1)  # config audit
    assert not panel.fast_check.isVisible()


def test_the_provider_can_be_chosen_here_too(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    shown = [
        panel.provider_combo.itemText(i) for i in range(panel.provider_combo.count())
    ]
    assert shown == [p.display() for p in PROVIDERS]


def test_the_stored_provider_is_shown_not_the_first_one(qapp, with_repo):
    with_repo.provider = "claude"
    panel = AgentsPanel(with_repo)
    assert panel.provider_combo.currentData() == "claude"


def test_choosing_a_provider_here_changes_the_application_setting(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    panel.provider_combo.setCurrentIndex(panel.provider_combo.findData("ollama"))
    assert with_repo.provider == "ollama"


def test_the_model_is_named_beside_the_provider(qapp, with_repo):
    """The provider is half the answer; a run uses a model too."""
    with_repo.selected_model = "qwen3.5-4b"
    panel = AgentsPanel(with_repo)
    panel.refresh_provider()
    assert "qwen3.5-4b" in panel.provider_label.text()


def test_a_provider_changed_elsewhere_is_picked_up_on_refresh(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    with_repo.provider = "ollama"  # as the Connection or Generate tab would
    panel.refresh_repos()
    assert panel.provider_combo.currentData() == "ollama"


def test_options_are_persisted(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    panel.narrate_check.setChecked(False)
    assert with_repo.agents_narrate is False


# ---- a finished run -----------------------------------------------------------
def test_a_report_fills_both_views(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    panel._on_finished(_report())

    assert "190.3 GiB" in panel.view.toHtml()
    assert panel.facts_tree.topLevelItemCount() == 1
    top = panel.facts_tree.topLevelItem(0)
    assert top.text(0) == "1 Summary"
    assert top.child(0).text(1) == "190.3 GiB"
    assert panel.copy_btn.isEnabled() and panel.export_btn.isEnabled()


def test_warnings_are_surfaced_not_swallowed(qapp, with_repo):
    report = _report()
    report.warnings.append("Section 1 was written from the measurements.")
    panel = AgentsPanel(with_repo)
    panel._on_finished(report)
    assert "written from the measurements" in panel.status.text()


def test_a_refresh_keeps_a_report_that_took_minutes(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    panel._on_finished(_report())

    panel.refresh_repos()

    assert "190.3 GiB" in panel.view.toHtml()
    assert panel.copy_btn.isEnabled()


def test_cancelling_says_so_without_a_dialog(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    panel._on_error("Cancelled.")
    assert panel.status.text() == "Cancelled."
    assert panel.run_btn.isEnabled()


# ---- the tab wiring -----------------------------------------------------------
def test_the_window_has_an_agents_tab(qapp, with_repo):
    dlg = SettingsDialog(with_repo)
    labels = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    assert "Agents" in labels


def test_switching_to_a_repo_driven_tab_refreshes_it(qapp, with_repo):
    """The regression test for the old positional `index > 1` guard."""
    dlg = SettingsDialog(with_repo)
    called: list[str] = []
    dlg.agents_panel.refresh_repos = lambda: called.append("agents")

    index = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())].index("Agents")
    dlg.tabs.setCurrentIndex(index)

    assert called == ["agents"]


def test_the_identities_tab_is_still_where_the_bar_points(qapp, with_repo):
    dlg = SettingsDialog(with_repo)
    assert dlg.tabs.tabText(dlg.identities_tab_index) == "Identities"


def test_closing_the_window_stops_a_running_agent(qapp, with_repo):
    dlg = SettingsDialog(with_repo)
    stopped: list[bool] = []
    dlg.agents_panel.cancel_running = lambda: stopped.append(True)

    dlg.close()

    assert stopped == [True]


# ---- one setting, three places that show it -----------------------------------
def test_choosing_in_the_agents_tab_reaches_the_generate_tab(qapp, with_repo):
    dlg = SettingsDialog(with_repo)
    combo = dlg.agents_panel.provider_combo

    combo.setCurrentIndex(combo.findData("ollama"))
    dlg.commit_panel.refresh_provider()

    assert with_repo.provider == "ollama"
    assert dlg.commit_panel.provider_combo.currentData() == "ollama"


def test_returning_to_connection_and_model_re_reads_the_provider(qapp, with_repo):
    """Three ways to change one setting; the tab you open must not show a stale one."""
    dlg = SettingsDialog(with_repo)
    combo = dlg.agents_panel.provider_combo
    combo.setCurrentIndex(combo.findData("ollama"))

    labels = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    dlg.tabs.setCurrentIndex(labels.index("Connection && Model"))

    selected = dlg.provider_list.currentItem().data(Qt.ItemDataRole.UserRole)
    assert selected == "ollama"


def test_choosing_in_connection_and_model_reaches_the_agents_tab(qapp, with_repo):
    dlg = SettingsDialog(with_repo)
    row = next(
        i
        for i in range(dlg.provider_list.count())
        if dlg.provider_list.item(i).data(Qt.ItemDataRole.UserRole) == "claude"
    )

    dlg.provider_list.setCurrentRow(row)

    assert dlg.agents_panel.provider_combo.currentData() == "claude"


# ---- what is on screen must describe what is selected -------------------------
def _report_for(repo="/x/demo", agent_id="size-audit", **kw):
    report = _report()
    report.repo_path = repo
    report.agent_id = agent_id
    for key, value in kw.items():
        setattr(report, key, value)
    return report


def test_switching_agent_clears_the_previous_report(qapp, with_repo):
    """The reported bug: a config audit stayed on screen titled as a size audit."""
    panel = AgentsPanel(with_repo)
    panel.agent_list.setCurrentRow(0)
    panel._on_finished(_report_for(agent_id=panel._agent_id()))
    assert "190.3 GiB" in panel.view.toHtml()

    panel.agent_list.setCurrentRow(1)

    assert panel._report is None
    assert "190.3 GiB" not in panel.view.toHtml()
    assert panel.facts_tree.topLevelItemCount() == 0
    assert not panel.copy_btn.isEnabled()


def test_switching_repository_clears_the_previous_report(qapp, with_repo):
    with_repo.repos = [RepoEntry("/x/demo"), RepoEntry("/x/other")]
    panel = AgentsPanel(with_repo)
    panel.refresh_repos()
    panel._on_finished(_report_for(repo=panel._repo_path(), agent_id=panel._agent_id()))

    tree = panel.repo_picker.repo_list
    tree.setCurrentItem(tree.topLevelItem(1))

    assert panel._report is None


def test_the_same_repository_and_agent_keeps_its_report(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    panel._on_finished(_report_for(repo=panel._repo_path(), agent_id=panel._agent_id()))

    panel._on_agent_changed()  # re-selecting the same agent changes nothing
    panel.refresh_repos()

    assert panel._report is not None


# ---- history ------------------------------------------------------------------
def test_a_finished_run_is_listed_in_previous_runs(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    panel._on_finished(_report_for(repo=panel._repo_path(), agent_id=panel._agent_id()))

    assert panel.runs_tree.topLevelItemCount() == 1
    assert "First run recorded" in panel.status.text()


def test_the_second_run_is_compared_with_the_first(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    repo, agent = panel._repo_path(), panel._agent_id()
    first = _report_for(repo=repo, agent_id=agent, head="a" * 40)
    first.sections[0].facts.append(Fact("garbage_size", "Garbage", "1.0 KiB", 1024))
    panel._on_finished(first)

    second = _report_for(repo=repo, agent_id=agent, head="a" * 40)
    second.sections[0].facts.append(Fact("garbage_size", "Garbage", "0 B", 0))
    panel._on_finished(second)

    assert panel.runs_tree.topLevelItemCount() == 2
    assert panel.tabs.isTabEnabled(panel.diff_tab)
    assert panel.tabs.tabText(panel.diff_tab).endswith("▲")
    assert "Improved since" in panel.status.text()


def test_a_finished_run_does_not_steal_the_report_tab(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    repo, agent = panel._repo_path(), panel._agent_id()
    panel._on_finished(_report_for(repo=repo, agent_id=agent))
    panel._on_finished(_report_for(repo=repo, agent_id=agent))

    assert panel.tabs.currentIndex() == 0


def test_the_comparison_tab_is_off_until_there_is_something_in_it(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    assert not panel.tabs.isTabEnabled(panel.diff_tab)


def test_opening_a_stored_run_says_it_is_stored(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    panel._on_finished(_report_for(repo=panel._repo_path(), agent_id=panel._agent_id()))
    panel._clear_report()

    panel.runs_tree.setCurrentItem(panel.runs_tree.topLevelItem(0))
    panel._on_open_run()

    assert "190.3 GiB" in panel.view.toHtml()
    assert "stored run" in panel.status.text()
    assert "(stored run)" in panel.header.text()
    assert panel.copy_btn.isEnabled() and panel.export_btn.isEnabled()


def test_the_pane_lists_only_this_repository_and_agent(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    repo = panel._repo_path()
    panel._on_finished(_report_for(repo=repo, agent_id="size-audit"))
    panel._on_finished(_report_for(repo=repo, agent_id="config-audit"))
    panel._on_finished(_report_for(repo="/x/elsewhere", agent_id="size-audit"))

    panel.agent_list.setCurrentRow(0)  # size audit
    assert panel.runs_tree.topLevelItemCount() == 1


def test_deleting_a_run_removes_it_from_the_list(qapp, with_repo, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    panel = AgentsPanel(with_repo)
    panel._on_finished(_report_for(repo=panel._repo_path(), agent_id=panel._agent_id()))
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )

    panel.runs_tree.setCurrentItem(panel.runs_tree.topLevelItem(0))
    panel._on_delete_run()

    assert panel.runs_tree.topLevelItemCount() == 0


def test_history_that_cannot_be_written_does_not_lose_the_report(qapp, with_repo, monkeypatch):
    """A five-minute audit must survive a disk that said no."""
    monkeypatch.setattr(history, "record", lambda *a, **k: (None, "disk is full"))
    panel = AgentsPanel(with_repo)

    panel._on_finished(_report_for(repo=panel._repo_path(), agent_id=panel._agent_id()))

    assert "190.3 GiB" in panel.view.toHtml()
    assert "disk is full" in panel.status.text()


def test_the_pane_says_when_there_is_no_history(qapp, with_repo):
    panel = AgentsPanel(with_repo)
    assert "No previous runs" in panel.history_note.text()


# ---- the LM Studio setup button -----------------------------------------------
def test_the_setup_button_is_offered_only_for_lm_studio(qapp, with_repo):
    """It is the only provider this machine can install."""
    dlg = SettingsDialog(with_repo)
    form = dlg._conn_form

    dlg._sync_provider_list()
    assert form.isRowVisible(dlg.setup_row_host) is (with_repo.provider == "lmstudio")

    row = next(
        i
        for i in range(dlg.provider_list.count())
        if dlg.provider_list.item(i).data(Qt.ItemDataRole.UserRole) == "claude"
    )
    dlg.provider_list.setCurrentRow(row)
    assert not form.isRowVisible(dlg.setup_row_host)


def test_declining_the_confirmation_starts_nothing(qapp, with_repo, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    dlg = SettingsDialog(with_repo)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel
    )
    started: list[bool] = []
    monkeypatch.setattr(
        "git_assistant.ui.settings_dialog.run_worker",
        lambda w: started.append(True),
    )

    dlg._on_setup_lmstudio()

    assert started == []
    assert dlg.setup_btn.isEnabled()


def test_the_confirmation_names_every_step_and_the_download(qapp, with_repo, monkeypatch):
    """Installing software and downloading gigabytes must not be a surprise."""
    from PyQt6.QtWidgets import QMessageBox

    from git_assistant import lmstudio_setup

    dlg = SettingsDialog(with_repo)
    shown: list[str] = []

    def capture(_parent, _title, text, *a, **k):
        shown.append(text)
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", capture)
    dlg._on_setup_lmstudio()

    message = shown[0]
    for step in lmstudio_setup.steps(with_repo):
        assert step.title in message
    assert "5 GB" in message
    assert lmstudio_setup.WINGET_PACKAGE in message
    assert lmstudio_setup.MODEL_REPO in message


def test_a_finished_setup_shows_what_it_configured(qapp, with_repo):
    from git_assistant import lmstudio_setup

    dlg = SettingsDialog(with_repo)
    with_repo.set_provider_model("lmstudio", "qwen3.5-4b")
    with_repo.context_window = 32768
    outcome = lmstudio_setup.SetupOutcome(
        results=[lmstudio_setup.StepResult("install", "Install LM Studio", "installed")]
    )

    dlg._on_setup_done(outcome)

    assert dlg.ctx_size_spin.value() == 32768
    assert dlg.model_combo.currentText() == "qwen3.5-4b"
    assert dlg.setup_btn.isEnabled()
    assert "completed" in dlg.setup_status.text()


def test_a_failed_setup_says_which_step_and_why(qapp, with_repo):
    from git_assistant import lmstudio_setup

    dlg = SettingsDialog(with_repo)
    outcome = lmstudio_setup.SetupOutcome(
        results=[
            lmstudio_setup.StepResult("install", "Install LM Studio", problem="no winget")
        ]
    )

    dlg._on_setup_done(outcome)

    assert "Install LM Studio" in dlg.setup_status.text()
    assert "no winget" in dlg.setup_status.text()
