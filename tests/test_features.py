"""The no-update build: the flag, and the UI's behaviour without an updater."""

import re
from pathlib import Path

import pytest

from git_assistant import features

ROOT = Path(__file__).resolve().parent.parent
NOUPDATE_SPEC = ROOT / "git-assistant-onedir-noupdate.spec"


def test_source_checkout_has_the_updater():
    """A developer checkout contains everything; only the build drops it."""
    assert features.UPDATES_SUPPORTED is True


def test_flag_is_derived_not_declared():
    """It must follow the package's presence, so the two cannot disagree.

    A module-level constant would be a second place to be wrong: excluding the
    package while leaving the constant True produces a build that offers
    updates it cannot perform.
    """
    source = (ROOT / "src" / "git_assistant" / "features.py").read_text(encoding="utf-8")
    assert "find_spec" in source
    assert not re.search(r"^UPDATES_SUPPORTED\s*=\s*(True|False)\s*$", source, re.M)


def test_missing_updater_is_reported_as_absent(monkeypatch):
    monkeypatch.setattr(features, "find_spec", lambda name: None)
    assert features._has_updater() is False


def test_broken_import_counts_as_no_updater(monkeypatch):
    """A build that cannot answer the question does not have a usable updater."""

    def boom(name):
        raise ImportError("no")

    monkeypatch.setattr(features, "find_spec", boom)
    assert features._has_updater() is False


# ---- the spec ---------------------------------------------------------------
def test_noupdate_spec_excludes_the_updater_and_its_reach():
    """The capability has to be absent from the bundle, not merely unused."""
    code = _spec_code()
    for module in (
        "git_assistant.updating",
        "git_assistant.ui.update_prompt",
        "dist_client",
    ):
        assert f'"{module}"' in code, f"{module} should be excluded"


def test_noupdate_spec_keeps_httpx():
    """httpx is not the updater's alone -- lmstudio_client is built on it.

    Excluding it produced a build that died with ModuleNotFoundError on launch,
    before the tray icon appeared: the app cannot reach LM Studio without it.
    Regression guard for exactly that.
    """
    assert '"httpx"' not in _spec_code()


#: Source that only the no-update build drops. Anything outside this is code the
#: no-update build still runs, so its imports must survive the exclusions.
UPDATER_SOURCE = ("git_assistant/updating/", "git_assistant/ui/update_prompt.py")


def _excluded_third_party() -> set[str]:
    block = re.search(r"EXCLUDED\s*=\s*\[(.*?)\]", _spec_code(), re.S)
    assert block, "EXCLUDED list not found in the spec"
    names = set(re.findall(r'"([^"]+)"', block.group(1)))
    return {n for n in names if not n.startswith("git_assistant")}


def test_nothing_the_app_still_needs_is_excluded():
    """The general form of the httpx mistake.

    An exclusion is only safe when the module is reachable *only* from the
    updater. httpx looked like the updater's HTTP client and is also how the
    application talks to LM Studio, so dropping it produced a build that died
    on launch. Verifying that by eye is exactly what failed; this checks every
    exclusion against every module the no-update build still runs.
    """
    excluded = _excluded_third_party()
    offenders: dict[str, list[str]] = {}

    for path in (ROOT / "src").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if any(part in rel for part in UPDATER_SOURCE):
            continue  # not in the no-update build, so its imports do not matter
        text = path.read_text(encoding="utf-8")
        for module in excluded:
            pattern = rf"^\s*(?:import|from)\s+{re.escape(module)}\b"
            if re.search(pattern, text, re.M):
                offenders.setdefault(module, []).append(rel)

    assert not offenders, (
        "these modules are excluded from the no-update build but still imported "
        f"by code it runs: {offenders}"
    )


def _spec_code() -> str:
    """The spec with comments stripped -- prose about root.json is not shipping it."""
    return "\n".join(
        line
        for line in NOUPDATE_SPEC.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_noupdate_spec_ships_no_update_metadata():
    """root.json and update_url.txt only mean something to the updater."""
    code = _spec_code()
    assert "root.json" not in code
    assert "update_url.txt" not in code


def test_noupdate_spec_collects_to_its_own_directory():
    """Otherwise the two installers would package each other's build."""
    spec = NOUPDATE_SPEC.read_text(encoding="utf-8")
    assert 'name="GitAssistant-noupdate"' in spec


# ---- the UI -----------------------------------------------------------------
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.config import Settings  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None
    return s


def test_window_says_there_is_no_updater(qapp, settings, monkeypatch):
    """Not "updates are off", which invites hunting for the switch."""
    import git_assistant.ui.settings_dialog as sd

    monkeypatch.setattr(sd, "UPDATES_SUPPORTED", False)
    dlg = sd.SettingsDialog(settings)

    assert dlg.version_online.text() == "no updater"
    assert "no self-updater" in dlg.version_online.toolTip()


def test_window_does_not_offer_to_edit_a_config_that_does_nothing(
    qapp, settings, monkeypatch
):
    import git_assistant.ui.settings_dialog as sd

    monkeypatch.setattr(sd, "UPDATES_SUPPORTED", False)
    dlg = sd.SettingsDialog(settings)

    assert "no self-updater" in dlg.update_source.text()
    buttons = dlg.findChildren(type(dlg.trust_btn))
    assert not any(b.text() == "Edit..." for b in buttons)
