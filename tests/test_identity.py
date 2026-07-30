"""Committer identities: the store, its own config file, and the picker."""

import json
import subprocess
import sys

import pytest

from git_assistant import git_ops, identities
from git_assistant.config import RepoEntry, Settings
from git_assistant.identities import Identity, IdentityStore, is_valid

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
    """A repo that inherits nothing from the machine running the tests.

    Most of what is under test here *reads through* to the global config --
    that is the point of `get_identity` and `describe_push_auth`. Without
    pinning it, "no signing key" or "no pinned credential" would mean "the
    developer happens not to have one", and the suite would pass here and fail
    on the next machine. Git treats a missing GIT_CONFIG_GLOBAL/SYSTEM file as
    an empty one.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "absent-system"))
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init")
    return d


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Redirect both config files away from the user's real config folder.

    ``config_path`` is redirected too, not just the identities file: bootstrap
    migrates identities *out* of settings.json, so a test left pointing at the
    real one would edit the user's own settings.
    """
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setattr(identities, "user_config_dir", lambda *a, **k: str(d))
    monkeypatch.setattr(identities, "config_path", lambda: d / "settings.json")
    return d


WORK = Identity(name="Work", email="me@work.example")
PERSONAL = Identity(name="Personal", email="me@personal.example")


# ---- git plumbing ----------------------------------------------------------
def test_set_identity_pins_it_locally(repo):
    git_ops.set_identity(repo, "Personal", "me@personal.example")

    assert git_ops.get_identity(repo) == ("Personal", "me@personal.example")
    assert git_ops.get_local_identity(repo) == ("Personal", "me@personal.example")


def test_switching_identity_overwrites_rather_than_appends(repo):
    """Two selections in a row must leave one value, not a multi-valued key.

    ``git config --add`` would make ``user.email`` multi-valued, and git then
    refuses to read it back with --get. The second selection has to replace.
    """
    git_ops.set_identity(repo, "Work", "me@work.example")
    git_ops.set_identity(repo, "Personal", "me@personal.example")

    assert git_ops.get_identity(repo) == ("Personal", "me@personal.example")
    values = _git(repo, "config", "--local", "--get-all", "user.email").stdout.split()
    assert values == ["me@personal.example"]


def test_identity_is_what_a_commit_is_stamped_with(repo):
    git_ops.set_identity(repo, "Personal", "me@personal.example")
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "f.txt")

    assert git_ops.commit(repo, "feat: add f").ok
    assert _git(repo, "log", "-1", "--pretty=%ae").stdout.strip() == (
        "me@personal.example"
    )


def test_local_identity_distinguishes_pinned_from_inherited(repo):
    """An unpinned repo still commits as somebody -- report that, not nothing."""
    assert git_ops.get_local_identity(repo) == ("", "")

    git_ops.set_identity(repo, "Personal", "me@personal.example")
    assert git_ops.get_local_identity(repo)[1] == "me@personal.example"

    git_ops.clear_local_identity(repo)
    assert git_ops.get_local_identity(repo) == ("", "")


def test_clearing_an_already_absent_identity_succeeds(repo):
    """git exits 5 for unsetting a missing key; the end state is what matters."""
    assert git_ops.clear_local_identity(repo).ok


# ---- signing keys ----------------------------------------------------------
SIGNED = Identity(name="Work", email="me@work.example", signingkey="ABC123")


def test_signing_key_is_written_with_the_identity(repo):
    git_ops.set_identity(repo, SIGNED.name, SIGNED.email, SIGNED.signingkey)
    assert git_ops.get_signingkey(repo) == "ABC123"


def test_switching_to_an_unsigned_identity_clears_the_key(repo):
    """The bug this exists to prevent: a commit authored by one identity and
    signed by another's key, which forges report as unverified."""
    git_ops.set_identity(repo, SIGNED.name, SIGNED.email, SIGNED.signingkey)
    git_ops.set_identity(repo, PERSONAL.name, PERSONAL.email)

    assert git_ops.get_signingkey(repo) == ""


def test_switching_between_signed_identities_replaces_the_key(repo):
    git_ops.set_identity(repo, "Work", "me@work.example", "ABC123")
    git_ops.set_identity(repo, "Personal", "me@personal.example", "DEF456")

    assert git_ops.get_signingkey(repo) == "DEF456"


def test_clearing_the_identity_also_clears_the_key(repo):
    git_ops.set_identity(repo, SIGNED.name, SIGNED.email, SIGNED.signingkey)
    git_ops.clear_local_identity(repo)

    assert git_ops.get_signingkey(repo) == ""


def test_signing_enabled_reads_commit_gpgsign(repo):
    _git(repo, "config", "commit.gpgsign", "false")
    assert not git_ops.signing_enabled(repo)
    _git(repo, "config", "commit.gpgsign", "true")
    assert git_ops.signing_enabled(repo)


def test_signing_key_round_trips_through_the_file(config_dir):
    IdentityStore([SIGNED]).save()
    assert IdentityStore.bootstrap().identities == [SIGNED]


def test_files_without_a_signing_key_still_load(config_dir):
    (config_dir / "committer_identities.json").write_text(
        json.dumps({"identities": [{"name": "Work", "email": "me@work.example"}]}),
        encoding="utf-8",
    )
    assert IdentityStore.bootstrap().identities == [WORK]


# ---- validation ------------------------------------------------------------
@pytest.mark.parametrize(
    "identity",
    [
        Identity(name="No email", email=""),
        Identity(name="Not an address", email="nope"),
        Identity(name="New\nline", email="me@x.example"),
        Identity(name="ok", email="me@x.example\nuser.name=someone"),
        Identity(name="ok", email="me@x.example", signingkey="A\nB"),
    ],
)
def test_unusable_identities_are_rejected(identity):
    assert not is_valid(identity)


def test_ordinary_identity_is_accepted():
    assert is_valid(WORK)


def test_duplicate_emails_collapse_case_insensitively():
    merged = identities.dedupe(
        [WORK, Identity(name="Work again", email="ME@WORK.EXAMPLE")]
    )
    assert merged == [WORK]


# ---- the file --------------------------------------------------------------
def test_identities_round_trip_through_their_own_file(config_dir):
    IdentityStore([WORK, PERSONAL]).save()

    assert (config_dir / "committer_identities.json").exists()
    assert IdentityStore.bootstrap().identities == [WORK, PERSONAL]


def test_file_is_versioned(config_dir):
    IdentityStore([WORK]).save()
    data = json.loads(
        (config_dir / "committer_identities.json").read_text(encoding="utf-8")
    )
    assert data["version"] == identities.SCHEMA_VERSION
    assert data["identities"] == [
        {"name": "Work", "email": "me@work.example", "signingkey": ""}
    ]


def test_a_bare_list_still_loads(config_dir):
    """Hand-written or third-party files should not need the envelope."""
    (config_dir / "committer_identities.json").write_text(
        json.dumps([{"name": "Work", "email": "me@work.example"}]), encoding="utf-8"
    )
    assert IdentityStore.bootstrap().identities == [WORK]


def test_corrupt_file_does_not_crash_the_app(config_dir):
    (config_dir / "committer_identities.json").write_text("{ not json", encoding="utf-8")
    assert IdentityStore.bootstrap().identities == []


# ---- first run -------------------------------------------------------------
def test_first_run_seeds_from_git(config_dir, monkeypatch):
    monkeypatch.setattr(
        identities.git_ops, "get_global_identity", lambda: ("Global", "me@global.example")
    )
    store = IdentityStore.bootstrap()

    assert store.identities == [Identity(name="Global", email="me@global.example")]
    assert (config_dir / "committer_identities.json").exists()


def test_first_run_happens_once_even_when_git_knows_nothing(config_dir, monkeypatch):
    """An emptied list must not be refilled from git config on every start."""
    monkeypatch.setattr(identities.git_ops, "get_global_identity", lambda: ("", ""))
    assert IdentityStore.bootstrap().identities == []
    assert (config_dir / "committer_identities.json").exists()

    monkeypatch.setattr(
        identities.git_ops, "get_global_identity", lambda: ("Late", "late@x.example")
    )
    assert IdentityStore.bootstrap().identities == []


def test_identities_migrate_out_of_settings_json(config_dir, tmp_path, monkeypatch):
    """An older build kept them in settings.json; move them and drop the key."""
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "lmstudio_port": 1234,
                "identities": [{"name": "Work", "email": "me@work.example"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(identities, "config_path", lambda: settings_file)

    assert IdentityStore.bootstrap().identities == [WORK]

    leftover = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "identities" not in leftover
    assert leftover["lmstudio_port"] == 1234  # nothing else disturbed


def test_migration_wins_over_git_seeding(config_dir, tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"identities": [{"name": "Work", "email": "me@work.example"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(identities, "config_path", lambda: settings_file)
    monkeypatch.setattr(
        identities.git_ops, "get_global_identity", lambda: ("Global", "g@x.example")
    )

    assert IdentityStore.bootstrap().identities == [WORK]


# ---- export / import -------------------------------------------------------
def test_export_then_import_on_another_machine(config_dir, tmp_path):
    out = tmp_path / "exported.json"
    IdentityStore([WORK, PERSONAL]).export_to(out)

    fresh = IdentityStore()
    added, skipped = fresh.import_from(out)

    assert (added, skipped) == (2, 0)
    assert fresh.identities == [WORK, PERSONAL]


def test_import_merges_rather_than_replaces(config_dir, tmp_path):
    """Importing on a machine that already has identities must not delete them."""
    out = tmp_path / "exported.json"
    IdentityStore([PERSONAL]).export_to(out)

    store = IdentityStore([WORK])
    added, skipped = store.import_from(out)

    assert (added, skipped) == (1, 0)
    assert store.identities == [WORK, PERSONAL]


def test_import_leaves_an_existing_email_alone(config_dir, tmp_path):
    """A stale export must not silently rename the identity in use."""
    out = tmp_path / "exported.json"
    IdentityStore([Identity(name="Old name", email="me@work.example")]).export_to(out)

    store = IdentityStore([WORK])
    added, skipped = store.import_from(out)

    assert (added, skipped) == (0, 1)
    assert store.identities == [WORK]


def test_importing_junk_adds_nothing(config_dir, tmp_path):
    junk = tmp_path / "junk.json"
    junk.write_text(json.dumps({"lmstudio_ip": "127.0.0.1"}), encoding="utf-8")

    store = IdentityStore([WORK])
    assert store.import_from(junk) == (0, 0)
    assert store.identities == [WORK]


def test_importing_a_missing_file_adds_nothing(config_dir, tmp_path):
    store = IdentityStore([WORK])
    assert store.import_from(tmp_path / "nope.json") == (0, 0)
    assert store.identities == [WORK]


# ---- the picker ------------------------------------------------------------
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.ui.identities_panel import IdentitiesPanel  # noqa: E402
from git_assistant.ui.identity_bar import (  # noqa: E402
    INFO_STYLE,
    UNSAVED,
    WARN_STYLE,
    IdentityBar,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(repo):
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    s.repos = [RepoEntry(str(repo))]
    s.active_repo = str(repo)
    return s


@pytest.fixture
def store(config_dir):
    return IdentityStore([WORK, PERSONAL])


def test_picker_selects_the_repos_current_identity(qapp, settings, store, repo):
    git_ops.set_identity(repo, "Personal", "me@personal.example")
    bar = IdentityBar(settings, store)

    assert bar.combo.currentText() == "me@personal.example"
    assert bar.status.text() == "set for this repository"


def test_choosing_an_identity_writes_it_to_the_repo(qapp, settings, store, repo):
    bar = IdentityBar(settings, store)
    bar.combo.setCurrentIndex(0)  # "Work"

    assert git_ops.get_local_identity(repo) == ("Work", "me@work.example")
    assert bar.combo.currentText() == "me@work.example"


def test_unlisted_identity_is_shown_not_replaced(qapp, settings, store, repo):
    """Git's answer wins the display, even when it is not one of the stored set."""
    git_ops.set_identity(repo, "Someone", "stray@example.com")
    bar = IdentityBar(settings, store)

    assert bar.combo.currentData() == UNSAVED
    assert "stray@example.com" in bar.combo.currentText()


def test_picker_is_inert_without_a_repository(qapp, settings, store):
    settings.active_repo = ""
    settings.repos = []
    bar = IdentityBar(settings, store)

    assert not bar.combo.isEnabled()
    assert bar.status.text() == "No repository selected"


def test_manage_entry_asks_for_the_tab_instead_of_selecting(qapp, settings, store, repo):
    bar = IdentityBar(settings, store)
    asked = []
    bar.manageRequested.connect(lambda: asked.append(True))

    bar.combo.setCurrentIndex(bar.combo.count() - 1)  # "Manage identities..."

    assert asked == [True]
    assert bar.combo.currentData() != "__manage__"  # not left showing


def test_selecting_a_signed_identity_sets_the_key(qapp, settings, store, repo):
    store.replace([SIGNED])
    bar = IdentityBar(settings, store)
    bar.combo.setCurrentIndex(0)

    assert git_ops.get_signingkey(repo) == "ABC123"


def test_bar_warns_when_signing_is_on_with_no_key(qapp, settings, store, repo):
    _git(repo, "config", "commit.gpgsign", "true")
    git_ops.set_identity(repo, PERSONAL.name, PERSONAL.email)
    bar = IdentityBar(settings, store)

    assert "signing key missing" in bar.status.text()
    assert "user.signingkey" in bar.status.toolTip()


def test_bar_is_quiet_when_signing_is_on_and_a_key_resolves(qapp, settings, store, repo):
    _git(repo, "config", "commit.gpgsign", "true")
    git_ops.set_identity(repo, SIGNED.name, SIGNED.email, SIGNED.signingkey)
    bar = IdentityBar(settings, store)

    assert "signing key missing" not in bar.status.text()


def test_bar_reports_what_will_authenticate_a_push(qapp, settings, store, repo):
    _git(repo, "remote", "add", "origin", "https://ONEoo7@github.com/ONEoo7/x.git")
    bar = IdentityBar(settings, store)

    assert bar.auth_status.text() == "push: github.com as ONEoo7"
    assert bar.auth_status.styleSheet() == INFO_STYLE


def test_bar_flags_a_credential_shared_across_accounts(qapp, settings, store, repo):
    """Committing as one account does not log you in as it -- say so."""
    _git(repo, "remote", "add", "origin", "https://github.com/ONEoo7/x.git")
    bar = IdentityBar(settings, store)

    assert "github.com" in bar.auth_status.text()
    assert bar.auth_status.styleSheet() == WARN_STYLE
    assert "does not log you in" in bar.auth_status.toolTip()


# ---- the tab ---------------------------------------------------------------
def test_tab_edits_persist_to_the_file(qapp, store, config_dir):
    panel = IdentitiesPanel(store)
    panel.table.item(0, 1).setText("changed@work.example")

    assert store.identities[0].email == "changed@work.example"
    assert IdentityStore.bootstrap().identities[0].email == "changed@work.example"


def test_tab_removal_persists(qapp, store, config_dir):
    panel = IdentitiesPanel(store)
    panel.table.selectRow(0)
    panel._on_remove()

    assert store.identities == [PERSONAL]
    assert IdentityStore.bootstrap().identities == [PERSONAL]


def test_half_typed_row_is_not_stored_but_stays_on_screen(qapp, store, config_dir):
    """Adding a row then typing a name must not save an identity with no email."""
    panel = IdentitiesPanel(store)
    panel._on_add()
    panel.table.item(2, 0).setText("Half")

    assert len(store.identities) == 2  # not stored yet
    assert panel.table.rowCount() == 3  # but not thrown away either
    assert "incomplete" in panel.status.text()


def test_tab_reports_the_file_it_writes(qapp, store, config_dir):
    panel = IdentitiesPanel(store)
    assert "committer_identities.json" in panel.status.text()
