"""The rules that decide whether self-update is offered at all.

Nothing here reaches the network. What is being asserted is the set of
conditions under which this application will *not* try to update itself, which
is the part worth pinning: a bug that fails to offer an update is an
inconvenience, and a bug that unpacks a release over somebody's working tree is
not.
"""

from __future__ import annotations

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
