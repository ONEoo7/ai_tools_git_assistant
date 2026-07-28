"""The rules that decide whether self-update is offered at all.

Nothing here reaches the network. What is being asserted is the set of
conditions under which this application will *not* try to update itself, which
is the part worth pinning: a bug that fails to offer an update is an
inconvenience, and a bug that unpacks a release over somebody's working tree is
not.
"""

from __future__ import annotations

import json
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


def test_a_build_with_neither_source_has_no_url(no_user_config: Path) -> None:
    """A checkout: no `update_url.txt`, no `update.json`."""
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
