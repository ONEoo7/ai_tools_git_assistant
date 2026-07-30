"""What will authenticate a push -- the half `user.email` does not decide."""

import subprocess
import sys

import pytest

from git_assistant import git_ops

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo that inherits no credential config from the host machine.

    `describe_push_auth` reads through to the global config on purpose, so
    without this "no pinned credential" would really mean "the developer has
    not pinned one". Git treats a missing GIT_CONFIG_GLOBAL/SYSTEM file as
    empty.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "absent-system"))
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init")
    return d


def _with_remote(repo, url):
    _git(repo, "remote", "add", "origin", url)
    return git_ops.describe_push_auth(repo)


def test_no_remote_is_reported_as_such(repo):
    auth = git_ops.describe_push_auth(repo)
    assert auth.kind == ""
    assert auth.summary() == "no remote"
    assert auth.warning() == ""


# ---- HTTPS -----------------------------------------------------------------
def test_plain_https_shares_one_credential_per_host(repo):
    """The case that makes 'commit as personal' misleading on a work machine."""
    auth = _with_remote(repo, "https://github.com/ONEoo7/thing.git")

    assert (auth.kind, auth.host, auth.account) == ("https", "github.com", "")
    assert auth.shared
    assert "github.com" in auth.summary()
    assert "useHttpPath" in auth.warning()


def test_username_in_the_url_pins_the_account(repo):
    auth = _with_remote(repo, "https://ONEoo7@github.com/ONEoo7/thing.git")

    assert auth.account == "ONEoo7"
    assert not auth.shared
    assert auth.summary() == "push: github.com as ONEoo7"
    assert auth.warning() == ""


def test_password_in_the_url_is_not_carried_around(repo):
    auth = _with_remote(repo, "https://user:hunter2@github.com/ONEoo7/thing.git")

    assert auth.account == "user"
    assert "hunter2" not in auth.summary()
    assert "hunter2" not in auth.warning()


def test_credential_username_config_pins_the_account(repo):
    _git(repo, "config", "credential.https://github.com.username", "ONEoo7")
    auth = _with_remote(repo, "https://github.com/ONEoo7/thing.git")

    assert auth.account == "ONEoo7"
    assert not auth.shared


def test_path_scoped_credentials_are_not_shared(repo):
    """`useHttpPath` gives each org its own entry, so one host serves several."""
    _git(repo, "config", "credential.https://github.com.useHttpPath", "true")
    auth = _with_remote(repo, "https://github.com/ONEoo7/thing.git")

    assert not auth.shared
    assert auth.warning() == ""


def test_generic_credential_keys_are_honoured(repo):
    _git(repo, "config", "credential.useHttpPath", "true")
    auth = _with_remote(repo, "https://gitlab.com/someone/thing.git")

    assert not auth.shared


# ---- SSH -------------------------------------------------------------------
def test_scp_style_remote_on_a_real_host_uses_the_default_key(repo):
    auth = _with_remote(repo, "git@github.com:ONEoo7/thing.git")

    assert (auth.kind, auth.host) == ("ssh", "github.com")
    assert auth.shared
    assert "default key" in auth.summary()
    assert "IdentityFile" in auth.warning()


def test_ssh_host_alias_is_treated_as_a_separated_key(repo):
    """A non-canonical host is an ~/.ssh/config alias -- the way keys differ."""
    auth = _with_remote(repo, "git@github-personal:ONEoo7/thing.git")

    assert (auth.kind, auth.host) == ("ssh", "github-personal")
    assert not auth.shared
    assert "key from SSH config" in auth.summary()
    assert auth.warning() == ""


def test_ssh_scheme_urls_are_understood(repo):
    auth = _with_remote(repo, "ssh://git@github-work/ONEoo7/thing.git")

    assert (auth.kind, auth.host) == ("ssh", "github-work")
    assert not auth.shared


def test_the_git_user_is_not_mistaken_for_an_account(repo):
    """`git@` is the protocol's user; reporting it as the account would mislead."""
    auth = _with_remote(repo, "git@github.com:ONEoo7/thing.git")
    assert auth.account == ""
    assert "git" not in auth.summary().split()
