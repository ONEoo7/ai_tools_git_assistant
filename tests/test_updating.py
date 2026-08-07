"""Updating through winget.

Every test drives a fake `winget` through the `runner` seam, so none of them
starts a process or touches the network. The output fixtures are real: they were
captured from winget 1.29 on Windows 11.
"""

import subprocess

import pytest

from git_assistant.updating import winget

#: What `winget search --id Git.Git --exact` actually prints. The identifier is
#: what the parser navigates by; everything around it is decoration.
SEARCH_OUTPUT = """\
Name Id      Version
---------------------
Git  Git.Git 2.55.0.3
"""

OURS = f"""\
Name          Id                           Version  Source
----------------------------------------------------------
Git Assistant {winget.PACKAGE_ID} 0.4.0    winget
"""


class Runner:
    """winget, recorded rather than run."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls: list[list[str]] = []
        self._result = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, argv):
        self.calls.append(argv)
        return self._result


@pytest.fixture
def have_winget(monkeypatch):
    monkeypatch.setattr(winget, "winget_path", lambda: r"C:\winget.exe")
    return r"C:\winget.exe"


@pytest.fixture
def installed(monkeypatch, have_winget):
    """A packaged build on Windows: the state in which updating is on."""
    monkeypatch.setattr(winget, "is_installed", lambda: True)
    monkeypatch.setattr(winget.sys, "platform", "win32")


# ---- reading winget's table --------------------------------------------------
def test_the_version_is_taken_from_beside_the_identifier():
    assert winget._version_in(SEARCH_OUTPUT, "Git.Git") == "2.55.0.3"
    assert winget._version_in(OURS, winget.PACKAGE_ID) == "0.4.0"


def test_a_table_without_our_package_reads_as_absent():
    assert winget._version_in(SEARCH_OUTPUT, winget.PACKAGE_ID) is None


def test_the_localised_header_is_not_what_is_matched():
    """The header is translated; the package identifier never is.

    Parsing by the `Version` column heading would work on an English Windows
    and quietly find nothing on any other, which is the worst shape a bug of
    this kind can take -- it cannot be reproduced by whoever wrote it.
    """
    german = "Name          ID                    Version   Quelle\n" + (
        f"Git Assistant {winget.PACKAGE_ID} 0.4.0     winget"
    )
    assert winget._version_in(german, winget.PACKAGE_ID) == "0.4.0"


def test_a_version_winget_could_not_read_is_not_a_version():
    """`winget list` prints "Unknown" for an entry with no readable version."""
    unknown = f"Git Assistant {winget.PACKAGE_ID} Unknown"
    assert winget._version_in(unknown, winget.PACKAGE_ID) is None


# ---- asking ------------------------------------------------------------------
def test_the_search_is_exact_and_names_the_source(have_winget):
    runner = Runner(stdout=OURS)

    assert winget.available_version(runner) == "0.4.0"

    argv = runner.calls[0]
    assert argv[1:4] == ["search", "--id", winget.PACKAGE_ID]
    assert "--exact" in argv  # or a package whose id merely contains ours matches
    assert argv[argv.index("--source") + 1] == "winget"
    # Without this, a machine that has never run winget blocks on a prompt.
    assert "--disable-interactivity" in argv


def test_a_package_winget_has_never_heard_of_is_not_an_error(have_winget):
    """Which is the state today: the manifest is not merged yet.

    On a five-minute timer, treating this as a failure would be an error toast
    every five minutes for as long as the package is unpublished.
    """
    runner = Runner(returncode=20, stdout="No package found matching input criteria.")
    assert winget.available_version(runner) is None


def test_a_real_winget_failure_is_reported_in_its_own_words(have_winget):
    runner = Runner(returncode=1, stderr="Failed when searching source; results will not be included: winget")

    with pytest.raises(winget.UpdateUnavailableError, match="Failed when searching"):
        winget.available_version(runner)


# ---- comparing ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("published", "running", "expected"),
    [
        ("0.4.0", "0.3.18", True),
        ("0.3.19", "0.3.18", True),
        ("0.3.18", "0.3.18", False),
        ("0.3.9", "0.3.18", False),  # 9 < 18: not a string comparison
        ("0.4", "0.4.0", False),  # padded, so these are equal
        ("0.4.0", "0.4", False),
        ("1.0.0", "0.99.99", True),
        ("", "0.3.18", False),  # unparseable reads as "not newer"
        ("nonsense", "0.3.18", False),
    ],
)
def test_which_versions_are_worth_offering(published, running, expected):
    assert winget.is_newer(published, running) is expected


# ---- the check ---------------------------------------------------------------
def test_nothing_newer_is_no_result(installed, monkeypatch):
    monkeypatch.setattr(winget, "current_version", lambda: "0.4.0")
    assert winget.check_for_update(Runner(stdout=OURS)) is None


def test_something_newer_carries_both_versions(installed, monkeypatch):
    monkeypatch.setattr(winget, "current_version", lambda: "0.3.18")

    result = winget.check_for_update(Runner(stdout=OURS))

    assert result.version == "0.4.0"
    assert result.current == "0.3.18"


def test_a_source_checkout_is_refused_before_anything_runs(have_winget, monkeypatch):
    """A developer should not be told about a release every five minutes."""
    monkeypatch.setattr(winget, "is_installed", lambda: False)
    monkeypatch.setattr(winget.sys, "platform", "win32")
    runner = Runner(stdout=OURS)

    with pytest.raises(winget.UpdateUnavailableError, match="source checkout"):
        winget.check_for_update(runner)

    assert runner.calls == []


def test_no_winget_says_where_to_get_one(monkeypatch):
    monkeypatch.setattr(winget, "winget_path", lambda: None)
    monkeypatch.setattr(winget.sys, "platform", "win32")
    assert "App Installer" in winget.unavailable_reason()


def test_updating_is_off_where_winget_does_not_exist(monkeypatch):
    monkeypatch.setattr(winget.sys, "platform", "linux")
    assert "Windows-only" in winget.unavailable_reason()


# ---- installing --------------------------------------------------------------
def test_a_package_winget_installed_is_upgraded(installed):
    result = winget.UpdateResult(version="0.4.0", current="0.3.18", installed=True)
    runner = Runner()

    winget.upgrade(result, runner)

    assert runner.calls[0][1] == "upgrade"


def test_a_package_winget_did_not_install_is_installed_over(installed):
    """`winget upgrade` on an install winget never made does nothing at all.

    This application also ships an NSIS installer and a portable zip, so the
    package being absent from `winget list` is an ordinary state -- and the
    installer replacing an existing install in place is how it has always been
    upgraded.
    """
    result = winget.UpdateResult(version="0.4.0", current="0.3.18", installed=False)
    runner = Runner()

    winget.upgrade(result, runner)

    assert runner.calls[0][1] == "install"


def test_installing_is_silent_and_agrees_to_the_terms(installed):
    result = winget.UpdateResult(version="0.4.0", current="0.3.18")
    runner = Runner()

    winget.upgrade(result, runner)

    argv = runner.calls[0]
    # Without --silent the installer's own window opens behind the tray.
    assert "--silent" in argv
    assert "--accept-package-agreements" in argv
    assert "--accept-source-agreements" in argv


def test_a_declined_uac_prompt_is_reported_not_swallowed(installed):
    """A per-machine install needs elevation, and declining it is a real answer.

    Reported rather than silent because the user pressed Install now: silence
    after that is indistinguishable from a dead button.
    """
    result = winget.UpdateResult(version="0.4.0", current="0.3.18")
    runner = Runner(returncode=1, stderr="Cancelled by user")

    with pytest.raises(winget.UpdateUnavailableError, match="Cancelled by user"):
        winget.upgrade(result, runner)


def test_the_command_the_dialog_names_is_the_one_that_runs(installed):
    """The consent dialog shows `result.command`; anything else is a lie."""
    result = winget.UpdateResult(version="0.4.0", current="0.3.18", installed=True)
    runner = Runner()

    winget.upgrade(result, runner)

    assert runner.calls[0][1 : 1 + len(result.command)] == result.command


# ---- what winget is asked about ------------------------------------------------
def test_the_package_identifier_matches_the_published_manifest():
    """It is the primary key in winget-pkgs and cannot change once published.

    A mismatch does not error anywhere: the check simply never finds anything,
    forever, which looks exactly like the feature being broken.
    """
    from pathlib import Path

    manifest = (
        Path(__file__).resolve().parent.parent
        / "installer"
        / "winget"
        / f"{winget.PACKAGE_ID}.yaml"
    )
    assert manifest.is_file(), f"no manifest named for {winget.PACKAGE_ID}"
    assert f"PackageIdentifier: {winget.PACKAGE_ID}" in manifest.read_text(encoding="utf-8")


def test_the_check_interval_is_five_minutes():
    assert winget.CHECK_MINUTES == 5


# ---- the tray, which is what actually asks --------------------------------------
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.ui import tray as tray_module  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def quiet_tray(monkeypatch, tmp_path):
    """A TrayApp that touches no config, no repos and no winget of its own."""
    from git_assistant.config import Settings

    settings = Settings()
    settings.save = lambda: None
    monkeypatch.setattr(Settings, "load", staticmethod(lambda: settings))
    monkeypatch.setattr(tray_module.TrayApp, "_backfill_owners", lambda self: None)
    monkeypatch.setattr(tray_module.TrayApp, "_refresh_watcher", lambda self: None)
    return settings


def test_updating_is_checked_at_startup_and_then_every_five_minutes(
    qapp, quiet_tray, monkeypatch
):
    """The two halves of the requirement, and the only place they are both true."""
    checks: list[str] = []
    monkeypatch.setattr(tray_module, "unavailable_reason", lambda: None)
    monkeypatch.setattr(
        tray_module.TrayApp, "_check_for_update", lambda self: checks.append("asked")
    )

    app = tray_module.TrayApp(qapp)

    assert checks == ["asked"]  # at startup
    assert app._update_timer.isActive()
    assert app._update_timer.interval() == 5 * 60_000


def test_nothing_is_checked_when_winget_cannot_update_this_install(
    qapp, quiet_tray, monkeypatch
):
    """A source checkout must not start a timer that can only ever fail."""
    checks: list[str] = []
    monkeypatch.setattr(tray_module, "unavailable_reason", lambda: "a source checkout")
    monkeypatch.setattr(
        tray_module.TrayApp, "_check_for_update", lambda self: checks.append("asked")
    )

    app = tray_module.TrayApp(qapp)

    assert checks == []
    assert not app._update_timer.isActive()


def test_a_version_already_declined_is_not_offered_again_five_minutes_later(
    qapp, quiet_tray, monkeypatch
):
    """At this interval that would be twelve modal dialogs an hour."""
    offered: list[str] = []
    monkeypatch.setattr(tray_module, "unavailable_reason", lambda: None)
    monkeypatch.setattr(tray_module.TrayApp, "_check_for_update", lambda self: None)
    monkeypatch.setattr(
        tray_module.TrayApp, "offer_install", lambda self, r: offered.append(r.version)
    )
    monkeypatch.setattr(tray_module.TrayApp, "_notify", lambda self, *a: None)
    app = tray_module.TrayApp(qapp)
    result = winget.UpdateResult(version="0.4.0", current="0.3.18")

    app._on_update_found(result)
    app._on_update_found(result)  # the next tick, five minutes later

    assert offered == ["0.4.0"]


def test_declining_the_consent_dialog_runs_no_winget(qapp, quiet_tray, monkeypatch):
    ran: list[object] = []
    monkeypatch.setattr(tray_module, "unavailable_reason", lambda: None)
    monkeypatch.setattr(tray_module.TrayApp, "_check_for_update", lambda self: None)
    monkeypatch.setattr(tray_module, "ask_to_install", lambda result: False)
    monkeypatch.setattr(tray_module, "UpgradeWorker", lambda r: ran.append(r))
    app = tray_module.TrayApp(qapp)

    app.offer_install(winget.UpdateResult(version="0.4.0", current="0.3.18"))

    assert ran == []
