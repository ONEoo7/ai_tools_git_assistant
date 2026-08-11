"""The Languages tab: what the reviewer understands, said out loud."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.review import languages, rule_files  # noqa: E402
from git_assistant.ui.languages_tab import (  # noqa: E402
    LanguagesTab,
    file_types,
    version_span,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _row(tab, label):
    for index in range(tab.tree.topLevelItemCount()):
        item = tab.tree.topLevelItem(index)
        if item.text(0) == label:
            return item
    raise AssertionError(f"no row for {label}")


def test_every_supported_language_is_listed(qapp):
    tab = LanguagesTab()
    listed = [tab.tree.topLevelItem(i).text(0) for i in range(tab.tree.topLevelItemCount())]
    assert listed == [one.label for one in languages.LANGUAGES]


def test_a_language_shows_the_file_types_that_reach_it(qapp):
    tab = LanguagesTab()
    assert _row(tab, "Python").text(1) == ".py, .pyi, .pyw, #!python, #!python3"


def test_a_shebang_is_listed_because_it_is_how_a_file_with_no_extension_is_reviewed():
    """`bin/deploy` with `#!/bin/bash` is a shell script and gets reviewed."""
    shell = languages.get("bash")
    assert "#!bash" in file_types(shell)


def test_a_language_with_no_shebang_lists_only_extensions():
    assert file_types(languages.get("rust")) == ".rs"


def test_the_collapsed_row_says_the_span_not_a_count():
    """A count would sit directly above the versions and read as one of them."""
    assert version_span(languages.get("c")) == "C89/90 – C23"


def test_each_version_is_a_child_row(qapp):
    tab = LanguagesTab()
    row = _row(tab, "C++")
    labels = [row.child(i).text(2) for i in range(row.childCount())]
    assert labels == list(languages.get("cpp").version_labels)


def test_an_older_version_gets_fewer_rules_than_a_newer_one(qapp):
    """The whole point of the column: `since: c++20` is absent below C++20."""
    tab = LanguagesTab()
    row = _row(tab, "C++")
    oldest = int(row.child(0).text(3))
    newest = int(row.child(row.childCount() - 1).text(3))

    assert oldest < newest
    assert newest == int(row.text(3))  # the newest gets every rule there is


def test_the_rule_count_matches_what_a_review_would_be_sent(qapp):
    tab = LanguagesTab()
    row = _row(tab, "Python")
    table = rule_files.table_for("python", "py36")
    shown = [row.child(i) for i in range(row.childCount())]
    at_py36 = [one for one in shown if one.text(2) == "Python 3.6"][0]

    assert at_py36.text(3) == str(len(table.rules))


def test_the_note_says_which_extension_two_languages_claim(qapp):
    tab = LanguagesTab()
    assert ".h is C or C++" in tab.note.text()


def test_the_note_says_an_unknown_language_is_skipped_not_guessed(qapp):
    """The rule this whole table exists to keep."""
    tab = LanguagesTab()
    assert "skipped, never guessed" in tab.note.text()


def test_nothing_here_is_editable(qapp):
    """A read-out, not a setting: every one of these facts is what the build is."""
    tab = LanguagesTab()
    row = _row(tab, "Python")
    from PyQt6.QtCore import Qt

    assert not row.flags() & Qt.ItemFlag.ItemIsEditable
    assert not row.flags() & Qt.ItemFlag.ItemIsUserCheckable
