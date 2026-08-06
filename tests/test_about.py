"""Who wrote this and where it lives, in the four places that have to agree.

A packaged build carries no distribution metadata, so the application cannot
read its author out of ``pyproject.toml`` at runtime, and the Windows version
resource and the installer cannot import the application. Four literals, then --
which is three chances to change one and miss the rest.
"""

from pathlib import Path

import pytest

from git_assistant import PROJECT_URL, __author__

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    ["pyproject.toml", "tools/win_version_info.py", "installer/git-assistant.nsi"],
)
def test_the_author_is_the_same_everywhere(name):
    assert __author__ in _read(name)


def test_the_project_link_is_the_repository_this_is():
    assert PROJECT_URL == "https://github.com/ONEoo7/ai_tools_git_assistant"


def test_the_link_is_https():
    """It is handed to a browser; http would be a downgrade nobody asked for."""
    assert PROJECT_URL.startswith("https://")


# ---- what the window shows ------------------------------------------------------
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.config import Settings  # noqa: E402
from git_assistant.ui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(qapp):
    s = Settings()
    s.save = lambda: None
    return SettingsDialog(s)


def test_the_link_is_shown_and_points_at_the_project(dialog):
    assert "Project link" in dialog.project_link.text()
    assert PROJECT_URL in dialog.project_link.text()


def test_the_link_is_clickable(dialog):
    """A plain label would render the anchor and do nothing when it was clicked."""
    assert dialog.project_link.openExternalLinks()


def test_the_whole_address_is_available_without_clicking(dialog):
    assert dialog.project_link.toolTip() == PROJECT_URL


def test_the_author_is_named(dialog):
    assert dialog.author_label.text() == f"Author: {__author__}"
