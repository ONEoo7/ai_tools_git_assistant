"""The winget manifests, as text assertions over files nothing else reads.

`wingetcreate update` regenerates the version, the URL and the hash on every
release and carries everything else forward from the last published version.
So a field that is wrong here is wrong in winget-pkgs until somebody notices,
and a field that is *missing* is not something a later release puts back.

The one these exist for is `Dependencies`. 0.3.16 shipped without it, onto
machines with no git, and this application is a front end for git.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "installer" / "winget"
IDENTIFIER = "StefanGhitescu.GitAssistant"

INSTALLER = MANIFESTS / f"{IDENTIFIER}.installer.yaml"
LOCALE = MANIFESTS / f"{IDENTIFIER}.locale.en-US.yaml"
VERSION = MANIFESTS / f"{IDENTIFIER}.yaml"
ALL_THREE = (INSTALLER, LOCALE, VERSION)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _field(path: Path, name: str) -> str:
    for line in _read(path).splitlines():
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip()
    return ""


# ---- the dependency ---------------------------------------------------------------
def test_git_is_declared_as_a_dependency():
    """The whole reason this directory exists. Without it winget installs an
    application that can only tell you to go and install something else."""
    text = _read(INSTALLER)
    assert "Dependencies:" in text
    assert "PackageDependencies:" in text
    assert "PackageIdentifier: Git.Git" in text


def test_the_dependency_is_on_the_installer_manifest():
    """`Dependencies` is an installer-manifest field; winget ignores it in the
    locale manifest rather than complaining, which is the worse failure."""
    assert "Git.Git" not in _read(LOCALE)
    assert "Git.Git" not in _read(VERSION)


# ---- the primary key --------------------------------------------------------------
@pytest.mark.parametrize("path", ALL_THREE, ids=lambda p: p.name)
def test_every_file_names_the_same_package(path):
    """The identifier is the primary key in winget-pkgs and cannot be changed
    once published. A mismatch does not error -- it publishes a second package."""
    assert _field(path, "PackageIdentifier") == IDENTIFIER


@pytest.mark.parametrize("path", ALL_THREE, ids=lambda p: p.name)
def test_every_file_agrees_on_the_version(path):
    versions = {_field(p, "PackageVersion") for p in ALL_THREE}
    assert len(versions) == 1, f"mixed versions across the set: {versions}"


@pytest.mark.parametrize("path", ALL_THREE, ids=lambda p: p.name)
def test_every_file_agrees_on_the_schema(path):
    """A mixed set is a validation failure, not a warning."""
    schemas = {_field(p, "ManifestVersion") for p in ALL_THREE}
    assert len(schemas) == 1, f"mixed schema versions: {schemas}"


def test_the_three_manifests_declare_their_own_types():
    assert _field(INSTALLER, "ManifestType") == "installer"
    assert _field(LOCALE, "ManifestType") == "defaultLocale"
    assert _field(VERSION, "ManifestType") == "version"


# ---- what is published, and what is deliberately not -------------------------------
def test_only_the_per_user_installer_is_published():
    """One installer, and the per-user one.

    Two nullsoft installers of one architecture are duplicates to validation
    unless each declares a scope, and this is the package the application's own
    update check upgrades -- per-machine would raise a UAC prompt from a
    process the user did not start.
    """
    url = _read(INSTALLER)
    assert "-user-windows-x64-setup.exe" in url
    for excluded in ("-machine-", ".zip", "noupdate"):
        assert excluded not in url


def test_the_installer_url_matches_the_regex_the_workflow_submits():
    """The workflow picks the asset by regex; a manifest naming a different one
    would be replaced by that asset on the next automatic release."""
    workflow = _read(ROOT / ".github" / "workflows" / "release.yml")
    assert "-user-windows-x64-setup\\.exe$" in workflow
    assert "-user-windows-x64-setup.exe" in _read(INSTALLER)


def test_the_identifier_matches_the_one_the_workflow_submits():
    """A mismatch opens a PR for a second, unrelated package rather than failing."""
    assert f"identifier: {IDENTIFIER}" in _read(
        ROOT / ".github" / "workflows" / "release.yml"
    )


def test_the_scope_is_declared():
    """Two nullsoft installers of one architecture are duplicates to validation
    unless each says which scope it is."""
    assert _field(INSTALLER, "Scope") == "user"


def test_the_hash_is_a_sha256():
    # `startswith` after stripping, not `in`: the field is mentioned in a
    # comment further up, and matching that gave a line with no colon in it.
    lines = [
        line.strip()
        for line in _read(INSTALLER).splitlines()
        if line.strip().startswith("InstallerSha256:")
    ]
    assert len(lines) == 1, "one installer, one hash"
    digest = lines[0].split(":", 1)[1].strip()
    assert len(digest) == 64 and digest == digest.upper()
    assert all(c in "0123456789ABCDEF" for c in digest)


# ---- the things a human reads ---------------------------------------------------------
def test_the_author_matches_the_application():
    from git_assistant import __author__

    assert _field(LOCALE, "Author") == __author__
    assert _field(LOCALE, "Publisher") == __author__


def test_the_description_says_git_is_needed():
    """Someone reading `winget show` before installing should learn it there."""
    assert "Requires Git for Windows" in _read(LOCALE)


def test_the_licence_matches_the_repository():
    assert _field(LOCALE, "License") == "MIT"
    assert "MIT License" in _read(ROOT / "LICENSE")
