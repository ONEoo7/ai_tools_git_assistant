"""Who wrote this and where it lives, in the four places that have to agree.

A packaged build carries no distribution metadata, so the application cannot
read its author out of ``pyproject.toml`` at runtime, and the Windows version
resource and the installer cannot import the application. Four literals, then --
which is three chances to change one and miss the rest.
"""

from pathlib import Path

import pytest

from git_assistant import AUTHOR_URL, CONTRIBUTORS, PROJECT_URL, __author__, __version__

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


def test_the_author_links_to_their_github():
    assert AUTHOR_URL == "https://github.com/ONEoo7"


# ---- what the window shows ------------------------------------------------------
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.config import Settings  # noqa: E402
from git_assistant.ui.settings_dialog import SettingsDialog, about_html  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(qapp):
    s = Settings()
    s.save = lambda: None
    return SettingsDialog(s)


# ---- the foot of the window ---------------------------------------------------------
def test_the_author_is_credited_left_of_the_version(dialog):
    from PyQt6.QtGui import QTextDocument

    shown = QTextDocument()
    shown.setHtml(dialog.credit.text())

    assert shown.toPlainText() == f"Git Assistant by {__author__}"
    row = dialog._version_row
    assert row.indexOf(dialog.credit) < row.indexOf(dialog.version_current)


def test_only_the_name_is_the_link(dialog):
    text = dialog.credit.text()
    assert f'<a href="{AUTHOR_URL}"' in text
    assert text.index("Git Assistant by") < text.index("<a ")
    assert text.endswith(f">{__author__}</a>")


def test_clicking_the_name_opens_their_github(dialog, monkeypatch):
    import git_assistant.ui.settings_dialog as dialog_mod

    opened = []
    monkeypatch.setattr(
        dialog_mod.QDesktopServices, "openUrl", staticmethod(opened.append)
    )

    dialog.credit.linkActivated.emit(AUTHOR_URL)

    assert [url.toString() for url in opened] == [AUTHOR_URL]


# ---- the About box ----------------------------------------------------------------
# The project link and the contributors are in here; the author is credited
# along the foot of the window as well.
def test_there_is_an_about_button(dialog):
    assert dialog.about_btn.text() == "About"


def test_it_names_the_author_and_the_version():
    said = about_html()
    assert __author__ in said and __version__ in said
    assert "Author" in said


def test_it_credits_what_each_contributor_did():
    """A name on its own says nothing; the credit is the contribution too."""
    said = about_html()
    for name, role in CONTRIBUTORS:
        assert name in said and role in said


def test_the_product_feedback_credit_is_there():
    assert ("Stefan Dragomir", "Product feedback") in CONTRIBUTORS


def test_the_author_is_not_listed_twice(dialog):
    """They are the author, not a contributor; one heading each."""
    assert about_html().count(__author__) == 1
    assert __author__ not in [name for name, _role in CONTRIBUTORS]


def test_the_project_link_is_a_link_in_there_too():
    assert f'href="{PROJECT_URL}"' in about_html()


def test_a_name_with_markup_in_it_could_not_break_the_box(monkeypatch):
    """It is rich text, so everything that is not a link is escaped."""
    import git_assistant.ui.settings_dialog as dialog_mod

    monkeypatch.setattr(dialog_mod, "CONTRIBUTORS", (("<b>x</b>", "Testing"),))
    assert "&lt;b&gt;x&lt;/b&gt;" in dialog_mod.about_html()


def test_opening_it_shows_the_same_text(dialog, monkeypatch):
    shown = []
    monkeypatch.setattr(
        "git_assistant.ui.settings_dialog.QMessageBox.exec",
        lambda self: shown.append(self.text()),
    )

    dialog._on_about()

    assert shown and __author__ in shown[0] and "Stefan Dragomir" in shown[0]
