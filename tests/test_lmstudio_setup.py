"""The one-press LM Studio setup.

Nothing here installs, downloads or launches anything: every command goes
through a fake runner that records what it was asked to do.
"""

import json
import sys

import pytest

from git_assistant import lmstudio_setup as setup
from git_assistant import repo_config
from git_assistant.config import Settings

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the setup is written for Windows"
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point ~/.lmstudio at a temp directory: this test writes real files."""
    monkeypatch.setattr(setup.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".lmstudio" / ".internal").mkdir(parents=True)
    return tmp_path / ".lmstudio"


class Runner:
    """Stands in for every subprocess; records the argv it was given."""

    def __init__(self, output=None, fail=None):
        self.calls: list[list[str]] = []
        self._output = output or {}
        self._fail = fail or {}

    def __call__(self, args, ok_codes=(0,)):
        self.calls.append(args)
        key = next((k for k in self._fail if k in " ".join(args)), None)
        if key:
            raise setup.SetupError(self._fail[key])
        for pattern, lines in self._output.items():
            if pattern in " ".join(args):
                return iter(lines)
        return iter([])

    def ran(self, fragment: str) -> bool:
        return any(fragment in " ".join(call) for call in self.calls)


def _ctx(runner=None, cancelled=False):
    said: list[str] = []
    ctx = setup.SetupContext(
        progress=said.append,
        is_cancelled=lambda: cancelled,
        runner=runner or Runner(),
    )
    ctx.said = said
    return ctx


# ---- installing ---------------------------------------------------------------
def test_the_app_is_installed_through_its_publishers_package(home):
    runner = Runner()
    setup.install_app(_ctx(runner))

    assert runner.ran(f"winget install --id {setup.WINGET_PACKAGE}")
    assert runner.ran("--silent")
    assert runner.ran("--accept-package-agreements")
    assert runner.ran("--disable-interactivity")


def test_nothing_newer_to_install_is_a_success_not_a_failure(home):
    """winget says "no applicable upgrade" with a non-zero code."""
    runner = Runner(output={"winget": ["No applicable upgrade found."]})
    assert setup.install_app(_ctx(runner)) == "already at the latest version"


def test_winget_is_allowed_to_report_its_no_update_code(home):
    """Both the unsigned and the signed spelling of the same code."""
    codes = []

    def runner(args, ok_codes=(0,)):
        codes.append(ok_codes)
        return iter([])

    setup.install_app(_ctx(runner))
    assert setup._WINGET_NO_UPDATE in codes[0]
    assert setup._WINGET_NO_UPDATE_SIGNED in codes[0]


# ---- LM Studio's own settings ---------------------------------------------------
def test_developer_mode_and_the_service_are_turned_on(home):
    (home / "settings.json").write_text(
        json.dumps({"language": "en", "developerMode": False}), encoding="utf-8"
    )

    setup.enable_service_and_developer_mode(_ctx())

    data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert data["developerMode"] is True
    assert data["enableLocalService"] is True
    assert data["language"] == "en", "every other setting must survive"


def test_the_api_server_is_set_to_start_with_the_app(home):
    (home / ".internal" / "http-server-config.json").write_text(
        json.dumps({"autoStartOnLaunch": False, "port": 4321, "cors": True}),
        encoding="utf-8",
    )

    setup.enable_service_and_developer_mode(_ctx())

    data = json.loads(
        (home / ".internal" / "http-server-config.json").read_text(encoding="utf-8")
    )
    assert data["autoStartOnLaunch"] is True
    assert data["port"] == 1234
    assert data["cors"] is True, "unrelated server settings must survive"


def test_settings_that_are_already_right_are_left_alone(home):
    (home / "settings.json").write_text(
        json.dumps({"developerMode": True, "enableLocalService": True}), encoding="utf-8"
    )
    (home / ".internal" / "http-server-config.json").write_text(
        json.dumps({"autoStartOnLaunch": True, "port": 1234}), encoding="utf-8"
    )

    assert setup.enable_service_and_developer_mode(_ctx()) == "already configured"


def test_a_settings_file_that_is_not_json_is_reported_not_overwritten(home):
    (home / "settings.json").write_text("{ broken", encoding="utf-8")

    with pytest.raises(setup.SetupError, match="not readable JSON"):
        setup.enable_service_and_developer_mode(_ctx())

    assert (home / "settings.json").read_text(encoding="utf-8") == "{ broken"


def test_a_missing_settings_file_is_created(home):
    setup.enable_service_and_developer_mode(_ctx())
    assert json.loads((home / "settings.json").read_text(encoding="utf-8"))[
        "developerMode"
    ]


# ---- the model ------------------------------------------------------------------
def test_the_model_is_fetched_by_repository_and_quantization(home):
    (home / "bin").mkdir()
    (home / "bin" / "lms.exe").write_text("", encoding="utf-8")
    runner = Runner(output={"ls": ["You have 0 models"]})

    assert setup.download_model(_ctx(runner)) == "downloaded"
    assert runner.ran(f"get {setup.MODEL_REPO}@{setup.MODEL_QUANT}")
    assert runner.ran("--yes")


def test_a_model_already_on_disk_is_not_downloaded_again(home):
    (home / "bin").mkdir()
    (home / "bin" / "lms.exe").write_text("", encoding="utf-8")
    runner = Runner(output={"ls": ["qwen3.5-4b    4B    qwen35    5.16 GB"]})

    assert setup.download_model(_ctx(runner)) == "already downloaded"
    assert not runner.ran("get ")


def test_the_model_step_says_so_when_the_cli_is_missing(home):
    with pytest.raises(setup.SetupError, match="CLI is not at"):
        setup.download_model(_ctx())


def test_the_model_is_configured_the_way_lm_studio_stores_it(home):
    note = setup.configure_model(_ctx())

    written = json.loads(setup.model_config_file().read_text(encoding="utf-8"))
    assert written["load"]["fields"] == [
        {"key": "llm.load.contextLength", "value": 32768}
    ]
    assert written["operation"]["fields"] == [
        {"key": "llm.prediction.reasoning.enableThinking", "value": False}
    ]
    assert "32,768" in note
    assert setup.model_config_file().parent.name == "Qwen3.5-4B-GGUF"
    assert setup.model_config_file().name == "Qwen3.5-4B-Q8_0.gguf.json"


def test_a_model_already_configured_is_left_alone(home):
    setup.configure_model(_ctx())
    assert setup.configure_model(_ctx()) == "already configured"


# ---- this application ------------------------------------------------------------
def test_the_app_is_pointed_at_what_was_just_installed(home):
    settings = Settings()
    settings.save = lambda: None

    setup.point_app_at_it(_ctx(), settings)

    assert settings.provider == "lmstudio"
    assert settings.active_model() == "qwen3.5-4b"
    assert settings.lmstudio_port == 1234
    # The window belongs to the model, so it lands in the User tier -- where
    # every run reads it from. Setting the field on `settings` would leave it
    # somewhere nothing reads.
    assert repo_config.defaults().model.context_window == setup.CONTEXT_LENGTH


def test_pointing_the_app_at_it_leaves_a_repositorys_own_window_alone(home, tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    repo_config.write_text(
        repo_config.Tier.REPO, str(repo), '{"model": {"context_window": 8000}}'
    )
    settings = Settings()
    settings.save = lambda: None

    setup.point_app_at_it(_ctx(), settings)

    assert repo_config.resolve(repo).model.context_window == 8000


def test_pointing_the_app_at_it_keeps_the_rest_of_the_user_tier(home):
    repo_config.write_text(
        repo_config.Tier.USER, "", '{"commit": {"diff_mode": "working"}}'
    )
    settings = Settings()
    settings.save = lambda: None

    setup.point_app_at_it(_ctx(), settings)

    written = repo_config.defaults()
    assert written.model.context_window == setup.CONTEXT_LENGTH
    assert written.commit.diff_mode == "working"  # not erased by the write


# ---- the whole sequence -----------------------------------------------------------
def _all_ready(home):
    """A machine where everything is already done except the app's own settings."""
    (home / "bin").mkdir(exist_ok=True)
    (home / "bin" / "lms.exe").write_text("", encoding="utf-8")
    (home / ".internal" / "app-install-location.json").write_text(
        json.dumps({"path": str(home / "bin" / "lms.exe")}), encoding="utf-8"
    )
    return Runner(output={"ls": ["qwen3.5-4b  4B"]})


def test_every_step_runs_in_order(home, monkeypatch):
    settings = Settings()
    settings.save = lambda: None
    runner = _all_ready(home)
    monkeypatch.setattr(setup, "_launch_detached", lambda *a, **k: None)

    outcome = setup.run(settings, _ctx(runner))

    assert [r.key for r in outcome.results] == [
        "install", "configure", "service", "model", "model-config", "app",
    ]
    assert outcome.ok
    assert "set up" in outcome.summary()


def test_a_failing_step_stops_the_rest(home):
    settings = Settings()
    settings.save = lambda: None
    runner = Runner(fail={"winget": "winget is not installed"})

    outcome = setup.run(settings, _ctx(runner))

    assert [r.key for r in outcome.results] == ["install"]
    assert not outcome.ok
    assert "winget is not installed" in outcome.summary()
    # lmstudio is the default provider, so the witness for "no later step ran"
    # is the model, which nothing else sets.
    assert settings.active_model() == "", "later steps must not have run"


def test_the_run_can_be_stopped(home):
    settings = Settings()
    settings.save = lambda: None
    ctx = setup.SetupContext(is_cancelled=lambda: True, runner=Runner())

    with pytest.raises(setup.Cancelled):
        setup.run(settings, ctx)


def test_the_steps_are_named_for_the_confirmation(home):
    titles = [s.title for s in setup.steps(Settings())]
    assert len(titles) == 6
    assert any("Install" in t for t in titles)
    assert any(setup.MODEL_REPO in t for t in titles)


def test_the_state_summary_says_what_is_already_there(home):
    lines = "\n".join(setup.describe_state())
    assert "LM Studio:" in lines
    assert "CLI:" in lines
    assert "developer mode:" in lines
