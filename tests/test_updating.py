"""The rules that decide whether self-update is offered at all.

Nothing here reaches the network. What is being asserted is the set of
conditions under which this application will *not* try to update itself, which
is the part worth pinning: a bug that fails to offer an update is an
inconvenience, and a bug that unpacks a release over somebody's working tree is
not.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

from git_assistant import __version__
from git_assistant.updating import client

REPO_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------- source vs installed


def test_a_source_checkout_is_not_an_installed_build() -> None:
    """The test suite runs from a checkout, so this is the real condition."""
    assert client.is_installed() is False


def test_updating_is_off_in_a_checkout_even_with_a_url_configured() -> None:
    """Configuration is not enough; it also has to be somewhere to install to.

    Self-update replaces the files it runs from. In a checkout those files are
    a working tree.
    """
    config = client.UpdateConfig(base_url="https://updates.example")
    assert config.enabled is False


def test_the_reason_given_is_the_checkout_not_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every disabled case used to report "no update URL is configured".

    Which sends someone editing environment variables when the answer is that
    they are running from source.
    """
    monkeypatch.setattr(client, "verifier_available", lambda: True)
    config = client.UpdateConfig(base_url="https://updates.example")

    reason = config.unavailable_reason()
    assert reason is not None
    assert "source checkout" in reason


def test_an_unconfigured_url_still_reports_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client, "is_installed", lambda: True)
    monkeypatch.setattr(client, "verifier_available", lambda: True)

    reason = client.UpdateConfig(base_url="").unavailable_reason()
    assert reason is not None
    assert "update URL" in reason


def test_a_packaged_build_without_the_verifier_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client, "is_installed", lambda: True)
    monkeypatch.setattr(client, "verifier_available", lambda: False)

    reason = client.UpdateConfig(base_url="https://updates.example").unavailable_reason()
    assert reason is not None
    assert "verifier" in reason


def test_all_three_present_enables_updating(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the tests above would pass with the feature simply broken."""
    monkeypatch.setattr(client, "is_installed", lambda: True)
    monkeypatch.setattr(client, "verifier_available", lambda: True)

    config = client.UpdateConfig(base_url="https://updates.example")
    assert config.unavailable_reason() is None
    assert config.enabled is True


def test_checking_from_a_checkout_raises_rather_than_reaching_the_network() -> None:
    # `base_url` points at a host that does not resolve: if the guard were
    # missing this would fail with a connection error instead, so the test
    # distinguishes "refused" from "tried and failed".
    config = client.UpdateConfig(base_url="https://updates.invalid")

    with pytest.raises(client.UpdateUnavailableError, match="source checkout"):
        client.check_for_update(config)


# ------------------------------------------------------------- the version


def test_the_reported_version_is_the_one_written_in_the_source() -> None:
    assert client.current_version() == __version__


def test_the_version_is_written_in_exactly_one_place() -> None:
    """The drift this prevents was real and silent.

    `pyproject.toml` said 0.2.0 while `__init__.py` said 0.1.0. A frozen build
    carries no distribution metadata, so the application reported the literal —
    meaning a 0.2.0 build would have seen the published 0.2.0 as newer than
    itself and offered to install it, every time, forever.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "version" not in pyproject["project"], (
        "pyproject.toml declares its own version; it must derive from __init__.py"
    )
    assert "version" in pyproject["project"]["dynamic"]
    assert pyproject["tool"]["hatch"]["version"]["path"] == "src/git_assistant/__init__.py"


# ------------------------------------------------- where the build looks


@pytest.fixture
def no_user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point `update.json` at an empty directory, so the real one is untouched."""
    path = tmp_path / client.UPDATE_CONFIG_FILE
    monkeypatch.setattr(client, "update_config_path", lambda: path)
    return path


def test_the_address_is_committed_so_every_build_carries_one(
    no_user_config: Path,
) -> None:
    """The regression that cost two release cycles.

    This used to be written only at package time from a CI variable, so an
    unset variable shipped a build whose updater silently did nothing — which
    from the outside is indistinguishable from the feature being broken. It is
    committed now, so a build without an address requires someone to delete a
    tracked file, and the release workflow refuses to build in that state.
    """
    committed = REPO_ROOT / "src" / "git_assistant" / "updating" / client.UPDATE_URL_FILE
    assert committed.is_file(), f"{client.UPDATE_URL_FILE} must be committed, not generated"

    url = client.packaged_update_url()
    assert url.startswith(("http://", "https://"))
    assert client.UpdateConfig.load().base_url == url.rstrip("/")


def test_a_build_missing_the_address_has_no_url(
    monkeypatch: pytest.MonkeyPatch, no_user_config: Path
) -> None:
    monkeypatch.setattr(client, "_packaged_file", lambda name: None)
    assert client.packaged_update_url() == ""
    assert client.UpdateConfig.load().base_url == ""


def test_the_packaged_url_is_the_default(
    monkeypatch: pytest.MonkeyPatch, no_user_config: Path
) -> None:
    """The case that made a packaged default necessary.

    An installed application is launched from the Start Menu and inherits the
    user environment, not a shell's, so an environment variable never reached
    it. A fresh install has to work with no configuration at all.
    """
    monkeypatch.setattr(client, "packaged_update_url", lambda: "https://updates.example/")

    config = client.UpdateConfig.load()
    assert config.base_url == "https://updates.example"
    assert "published with" in config.origin


def test_the_user_file_overrides_the_packaged_url(
    monkeypatch: pytest.MonkeyPatch, no_user_config: Path
) -> None:
    """The whole point: a build whose only address is compiled in cannot
    recover when that address goes down."""
    monkeypatch.setattr(client, "packaged_update_url", lambda: "https://updates.example")
    no_user_config.write_text(json.dumps({"url": "http://127.0.0.1:8080"}), encoding="utf-8")

    config = client.UpdateConfig.load()
    assert config.base_url == "http://127.0.0.1:8080"
    assert config.origin == str(no_user_config)


def test_the_user_file_can_set_the_channel(
    monkeypatch: pytest.MonkeyPatch, no_user_config: Path
) -> None:
    no_user_config.write_text(
        json.dumps({"url": "https://updates.example", "channel": "beta"}), encoding="utf-8"
    )
    assert client.UpdateConfig.load().channel == "beta"


def test_an_absent_channel_stays_on_stable(no_user_config: Path) -> None:
    no_user_config.write_text(json.dumps({"url": "https://updates.example"}), encoding="utf-8")
    assert client.UpdateConfig.load().channel == client.DEFAULT_CHANNEL


@pytest.mark.parametrize(
    "written",
    ["{ not json", '["a", "list"]', '{"url": "file:///etc/passwd"}', '{"url": "updates.example"}'],
)
def test_an_unusable_user_file_is_reported_not_raised(
    no_user_config: Path, written: str
) -> None:
    """A JSON typo must not stop the application starting.

    It must not look like "no override" either, or someone edits a file and
    watches nothing happen with no idea why.
    """
    no_user_config.write_text(written, encoding="utf-8")

    config = client.UpdateConfig.load()
    assert config.base_url == ""
    assert config.problem
    assert config.unavailable_reason() == config.problem


def test_a_broken_user_file_does_not_silently_fall_back(
    monkeypatch: pytest.MonkeyPatch, no_user_config: Path
) -> None:
    """Falling back to the packaged address would hide the mistake.

    Someone who edited this file did so because the packaged address was not
    working; quietly using it anyway is the least useful thing to do.
    """
    monkeypatch.setattr(client, "packaged_update_url", lambda: "https://updates.example")
    no_user_config.write_text('{"url": "ftp://mirror.example"}', encoding="utf-8")

    config = client.UpdateConfig.load()
    assert config.base_url == ""
    assert "not http(s)" in config.problem


def test_the_application_never_writes_the_update_config(no_user_config: Path) -> None:
    """The reason this is not a key in `settings.json`.

    That file is rewritten by the application, so a hand-edited address could
    be clobbered — and a code path that can change where updates come from is
    one more thing that has to be right.
    """
    source = (REPO_ROOT / "src" / "git_assistant").rglob("*.py")
    for path in source:
        text = path.read_text(encoding="utf-8")
        assert f'"{client.UPDATE_CONFIG_FILE}"' not in text or path.name == "client.py"

    client.UpdateConfig.load()
    assert not no_user_config.exists(), "reading the config must not create it"


@pytest.mark.parametrize(
    "written, expected",
    [
        ("https://updates.example/", "https://updates.example/"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("  https://updates.example  \n", "https://updates.example"),
        # Plain http is allowed on purpose: TUF signs the metadata and pins the
        # target hashes, so a loopback deployment is a normal way to run this.
        ("", ""),
        ("file:///etc/passwd", ""),
        ("javascript:alert(1)", ""),
        ("updates.example", ""),
    ],
)
def test_a_packaged_url_must_look_like_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, written: str, expected: str
) -> None:
    """A malformed build-time constant must not stop the application starting.

    It reads as "no default", which disables the updater, rather than raising
    out of `load` on the path that builds the tray menu.
    """
    packaged = tmp_path / client.UPDATE_URL_FILE
    packaged.write_text(written, encoding="utf-8")
    monkeypatch.setattr(client, "_packaged_file", lambda name: packaged)

    assert client.packaged_update_url() == expected


def test_a_missing_root_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shared file lookup must not have made the root optional.
    monkeypatch.setattr(client, "_packaged_file", lambda name: None)

    with pytest.raises(client.UpdateUnavailableError, match="not bundled"):
        client._trusted_root()


def test_the_template_overrides_nothing(
    monkeypatch: pytest.MonkeyPatch, no_user_config: Path
) -> None:
    """Creating the file must not change where updates come from.

    Pre-filling `url` with the address currently in use would pin this
    installation to whatever the build shipped with, so a later build pointing
    somewhere else would be quietly ignored.
    """
    monkeypatch.setattr(client, "packaged_update_url", lambda: "https://updates.example")

    before = client.UpdateConfig.load()
    created = client.ensure_update_config()
    after = client.UpdateConfig.load()

    assert created == no_user_config
    assert created.is_file()
    assert after.base_url == before.base_url == "https://updates.example"
    assert not after.problem


def test_the_template_is_the_documented_shape(no_user_config: Path) -> None:
    client.ensure_update_config()
    data = json.loads(no_user_config.read_text(encoding="utf-8"))

    assert data["url"] == ""
    assert data["channel"] == client.DEFAULT_CHANNEL
    assert "url" in data["_comment"]


def test_an_existing_file_is_never_overwritten(no_user_config: Path) -> None:
    """It holds a hand-edited address. Replacing it with a template would be
    the worst possible time to lose it -- the service is already down."""
    no_user_config.write_text('{"url": "https://mirror.example"}', encoding="utf-8")

    client.ensure_update_config()

    assert client.UpdateConfig.load().base_url == "https://mirror.example"


def test_an_unknown_key_is_ignored_not_rejected(no_user_config: Path) -> None:
    # `_comment` is in the template, so parsing must tolerate it -- otherwise
    # the file this application writes is one it refuses to read.
    no_user_config.write_text(
        json.dumps({"_comment": "hi", "url": "https://updates.example", "future": 1}),
        encoding="utf-8",
    )

    config = client.UpdateConfig.load()
    assert config.base_url == "https://updates.example"
    assert not config.problem


# --------------------------------------------------- how often it checks


@pytest.mark.parametrize(
    "written, expected",
    [
        (None, client.DEFAULT_CHECK_MINUTES),
        (60, 60),
        (0, client.DEFAULT_CHECK_MINUTES),
        (-5, client.DEFAULT_CHECK_MINUTES),
        # Clamped, not rejected. Wanting faster checks is a preference, and
        # refusing the file over it would turn that into "updating is off".
        (0.1, client.MIN_CHECK_MINUTES),
        ("often", client.DEFAULT_CHECK_MINUTES),
        (True, client.DEFAULT_CHECK_MINUTES),
    ],
)
def test_the_check_interval_is_clamped_not_trusted(
    no_user_config: Path, written: object, expected: int
) -> None:
    body: dict[str, object] = {"url": "https://updates.example"}
    if written is not None:
        body["check_interval_minutes"] = written
    no_user_config.write_text(json.dumps(body), encoding="utf-8")

    config = client.UpdateConfig.load()
    assert config.check_minutes == expected
    assert not config.problem


def test_a_bad_interval_never_disables_updating(no_user_config: Path) -> None:
    no_user_config.write_text(
        json.dumps({"url": "https://updates.example", "check_interval_minutes": -1}),
        encoding="utf-8",
    )
    assert client.UpdateConfig.load().base_url == "https://updates.example"


def test_the_floor_is_low_enough_to_test_with() -> None:
    # If the minimum were an hour, watching a release land would mean editing
    # source. One minute is fast enough to observe and slow enough to leave on.
    assert client.MIN_CHECK_MINUTES <= 1
    assert client.DEFAULT_CHECK_MINUTES >= 60


# ------------------------------------------------------------- installing


def a_result(target: str, version: str = "0.4.0") -> client.UpdateResult:
    return client.UpdateResult(
        version=version,
        target_path=target,
        length=10,
        sha256_hex="ab" * 32,
        mandatory=False,
    )


def test_a_non_executable_release_is_refused_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check that stops a zip being executed.

    The channel published the portable zip for several releases. Handing that
    to the installer path would mean either replacing a running directory in
    place -- which this does not do -- or executing whatever the archive
    happened to contain.
    """
    called = []
    monkeypatch.setattr(client, "download_update", lambda *a: called.append(a))
    monkeypatch.setattr(client, "_launch_installer", lambda p: called.append(p))

    result = a_result("git-assistant/stable/windows-amd64/0.4.0/app-0.4.0.zip")

    with pytest.raises(client.UpdateUnavailableError, match="cannot install"):
        client.install_update(client.UpdateConfig(base_url="https://x"), result)

    assert called == [], "nothing may be downloaded or run for an unsupported artifact"


def test_the_staged_name_comes_from_the_signed_target_path() -> None:
    # Not from anything the server said in an unsigned response, and not from a
    # Content-Disposition header.
    result = a_result("git-assistant/stable/windows-amd64/0.4.0/setup-0.4.0.exe")
    assert client.staged_path(result).name == "setup-0.4.0.exe"


def test_the_staging_directory_is_the_per_user_config_dir() -> None:
    # Not %TEMP%: an installer is executed from here, and a directory routinely
    # written by other software is a poor place to keep something about to run.
    staged = client.staged_path(a_result("a/b/c/setup.exe"))
    assert staged.parent == client.update_config_path().parent / "updates"


def test_the_bytes_are_verified_again_immediately_before_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verifying only at download time leaves a window on disk.

    `download_update` checks what arrived; this checks what is about to be
    executed, and between the two the file simply sits there.
    """
    order: list[str] = []
    staged = tmp_path / "setup.exe"

    def fake_download(_config, _result, destination):
        staged.write_bytes(b"payload")
        order.append("download")
        return destination

    monkeypatch.setattr(client, "staged_path", lambda r: staged)
    monkeypatch.setattr(client, "download_update", fake_download)
    monkeypatch.setattr(client, "_verify_staged", lambda r, p: order.append("verify"))
    monkeypatch.setattr(client, "_launch_installer", lambda p: order.append("launch"))

    client.install_update(client.UpdateConfig(base_url="https://x"), a_result("a/setup.exe"))

    assert order == ["download", "verify", "launch"]


def test_previous_downloads_are_cleared_before_a_new_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each staged installer is tens of megabytes and nothing else removes them."""
    staged = tmp_path / "setup-0.4.0.exe"
    stale = tmp_path / "setup-0.3.9.exe"
    stale.write_bytes(b"an old release")

    monkeypatch.setattr(client, "staged_path", lambda r: staged)
    monkeypatch.setattr(
        client, "download_update", lambda *a: staged.write_bytes(b"new") or staged
    )
    monkeypatch.setattr(client, "_verify_staged", lambda r, p: None)
    monkeypatch.setattr(client, "_launch_installer", lambda p: None)

    client.install_update(client.UpdateConfig(base_url="https://x"), a_result("a/setup.exe"))

    assert not stale.exists()
    assert staged.exists(), "the one about to be installed must survive the sweep"


def test_an_unremovable_leftover_does_not_stop_the_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Reclaiming disk space must never be the reason an update fails.
    staged = tmp_path / "setup-0.4.0.exe"
    (tmp_path / "locked.exe").write_bytes(b"x")
    monkeypatch.setattr(client, "staged_path", lambda r: staged)

    def boom(_self):
        raise OSError("in use")

    monkeypatch.setattr(Path, "unlink", boom)
    monkeypatch.setattr(
        client, "download_update", lambda *a: staged.write_bytes(b"new") or staged
    )
    monkeypatch.setattr(client, "_verify_staged", lambda r, p: None)
    launched: list[Path] = []
    monkeypatch.setattr(client, "_launch_installer", launched.append)

    client.install_update(client.UpdateConfig(base_url="https://x"), a_result("a/setup.exe"))

    assert launched == [staged]


def test_every_build_path_bundles_the_updater_data_files() -> None:
    """Three build paths must ship the same files, and did not.

    The release workflow bundled `update_url.txt`; both PyInstaller specs
    bundled only `root.json`. A locally built application therefore had an
    updater with no address in it, which looks exactly like a broken updater
    rather than an unconfigured one.
    """
    needed = [client.ROOT_FILENAME, client.UPDATE_URL_FILE]

    for spec in ("git-assistant-onedir.spec", "git-assistant.spec"):
        text = (REPO_ROOT / spec).read_text(encoding="utf-8")
        for name in needed:
            assert name in text, f"{spec} does not bundle {name}"

    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for name in needed:
        assert name in workflow, f"the release workflow does not bundle {name}"


def test_startup_sweeps_the_directory_completely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The relaunched build removes the installer that produced it.

    Sparing nothing, unlike the pre-download sweep: at startup there is no file
    that is about to be needed, and the one sitting there is the installer this
    very build was made by.
    """
    monkeypatch.setattr(client, "staged_dir", lambda: tmp_path)
    (tmp_path / "setup-0.3.5.exe").write_bytes(b"the installer that just ran")

    client.clear_staged_updates()

    assert list(tmp_path.iterdir()) == []


def test_a_locked_installer_does_not_stop_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An installer that has not quite exited still holds its own file open.

    That is the ordinary case at startup, since the installer relaunches the
    application before it finishes. It must not raise on the path that builds
    the tray.
    """
    monkeypatch.setattr(client, "staged_dir", lambda: tmp_path)
    (tmp_path / "setup.exe").write_bytes(b"x")

    def boom(_self):
        raise OSError("in use by another process")

    monkeypatch.setattr(Path, "unlink", boom)
    client.clear_staged_updates()  # must not raise


def test_sweeping_a_directory_that_is_not_there_is_fine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A fresh install has never staged anything.
    monkeypatch.setattr(client, "staged_dir", lambda: tmp_path / "never-created")
    client.clear_staged_updates()


# ------------------------------------------------------------ launching it
# `subprocess.Popen` used to start the installer, and `CreateProcess` beneath it
# does not elevate: an installer manifested requireAdministrator fails with
# ERROR_ELEVATION_REQUIRED (740) rather than prompting. The launch goes through
# ShellExecuteEx now, which uses the elevation broker.


def test_declining_the_elevation_prompt_is_explained_as_a_choice() -> None:
    """Not "error 1223". The user dismissed a dialog and nothing was changed."""
    message = client._launch_error_message(1223)
    assert "declined" in message
    assert "nothing has been changed" in message
    assert "1223" not in message


def test_elevation_required_is_reported_as_a_bug() -> None:
    """740 means the launch did not elevate, which is the thing that was fixed.

    If it reappears it is a regression, not a condition to explain away, so the
    message says so rather than reading like ordinary bad luck.
    """
    message = client._launch_error_message(740)
    assert "bug" in message


def test_an_unrecognised_failure_still_names_the_code() -> None:
    assert "Windows error 3" in client._launch_error_message(3)


@pytest.mark.skipif(sys.platform != "win32", reason="ShellExecuteEx is Windows-only")
def test_a_failed_launch_raises_rather_than_failing_silently(tmp_path: Path) -> None:
    """Exercises the real ShellExecuteExW call: struct layout, call, GetLastError.

    A missing file is the cheapest way to reach the failure path without
    starting anything. Getting the structure wrong would surface here as a
    crash or a bogus code rather than ERROR_FILE_NOT_FOUND.
    """
    with pytest.raises(client.UpdateUnavailableError) as caught:
        client._launch_installer(tmp_path / "was-never-downloaded.exe")

    assert "Windows error 2" in str(caught.value)  # ERROR_FILE_NOT_FOUND


# --------------------------------------------- which installer to update into
# A per-user and a per-machine install are the same bundle in different
# installers, so nothing about which one ran can be baked in at package time.
# It matters anyway: offered the per-user installer, a Program Files install
# would not upgrade -- it would put a second copy under %LOCALAPPDATA% and
# leave itself stale.


def _frozen_at(monkeypatch: pytest.MonkeyPatch, executable: Path) -> None:
    executable.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))


def test_a_source_checkout_is_not_a_per_machine_install() -> None:
    """Nothing is installed anywhere; the question does not arise."""
    assert client.is_per_machine_install() is False
    assert client.default_channel() == client.DEFAULT_CHANNEL


@pytest.mark.skipif(sys.platform != "win32", reason="Program Files is Windows-only")
def test_a_per_user_install_follows_the_default_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    _frozen_at(monkeypatch, tmp_path / "Local" / "Programs" / "GitAssistant" / "app.exe")

    assert client.is_per_machine_install() is False
    assert client.default_channel() == client.DEFAULT_CHANNEL


@pytest.mark.skipif(sys.platform != "win32", reason="Program Files is Windows-only")
def test_a_per_machine_install_follows_its_own_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    program_files = tmp_path / "Program Files"
    monkeypatch.setenv("ProgramFiles", str(program_files))
    _frozen_at(monkeypatch, program_files / "GitAssistant" / "app.exe")

    assert client.is_per_machine_install() is True
    assert client.default_channel() == f"{client.DEFAULT_CHANNEL}-machine"


@pytest.mark.skipif(sys.platform != "win32", reason="Program Files is Windows-only")
def test_the_32_bit_program_files_counts_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 32-bit install lands under Program Files (x86) and is no less machine-wide."""
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    x86 = tmp_path / "Program Files (x86)"
    monkeypatch.setenv("ProgramFiles(x86)", str(x86))
    _frozen_at(monkeypatch, x86 / "GitAssistant" / "app.exe")

    assert client.is_per_machine_install() is True


def test_an_explicit_channel_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """update.json exists so an operator can override what the build decided."""
    monkeypatch.setattr(
        client,
        "user_update_config",
        lambda: client.UserUpdateConfig(url="https://example.test", channel="beta"),
    )
    assert client.UpdateConfig.load().channel == "beta"
