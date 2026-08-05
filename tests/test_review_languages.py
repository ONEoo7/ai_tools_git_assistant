"""Which language a file is in."""

import pytest

from git_assistant.review import languages
from git_assistant.review.languages import LANGUAGES, UNKNOWN, detect, hint_from


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/app.py", "python"),
        ("src/app.pyi", "python"),
        ("main.rs", "rust"),
        ("Program.cs", "csharp"),
        ("index.ts", "typescript"),
        ("component.tsx", "typescript"),
        ("index.js", "javascript"),
        ("bundle.mjs", "javascript"),
        ("Main.java", "java"),
        ("site.css", "css"),
        ("index.html", "html"),
        ("legacy.htm", "html"),
        ("engine.cpp", "cpp"),
        ("engine.hpp", "cpp"),
        ("engine.cc", "cpp"),
        ("driver.c", "c"),
        ("build.sh", "bash"),
        ("Deploy.ps1", "powershell"),
    ],
)
def test_the_extension_says_which_language_it_is(path, expected):
    assert detect(path) == expected


def test_the_case_of_the_extension_does_not_matter():
    assert detect("SRC/APP.PY") == "python"


@pytest.mark.parametrize("path", ["notes.md", "data.json", "photo.png", "Makefile"])
def test_a_file_no_language_claims_is_unknown(path):
    """Reviewing it against some other language's rules is worse than skipping it."""
    assert detect(path) == UNKNOWN


# ---- the header problem ------------------------------------------------------------
def test_a_header_beside_cpp_sources_is_a_cpp_header():
    assert detect("engine.h", repo_hint="cpp") == "cpp"


def test_a_header_in_a_c_repository_is_a_c_header():
    assert detect("driver.h", repo_hint="c") == "c"


def test_a_header_that_says_cpp_in_its_own_first_lines_is_believed():
    """The diff is already in memory; no second look at the file is needed."""
    head = "#pragma once\n#include <vector>\nnamespace engine {\n"
    assert detect("engine.h", head=head, repo_hint="c") == "cpp"


def test_a_header_with_nothing_to_go_on_is_c():
    assert detect("thing.h") == "c"


def test_an_answer_already_given_wins_over_any_guess(monkeypatch):
    head = "namespace engine {\n"
    assert detect("engine.h", head=head, overrides={".h": "c"}) == "c"


def test_an_override_naming_a_language_that_does_not_exist_is_not_obeyed():
    assert detect("engine.h", overrides={".h": "brainfuck"}) == UNKNOWN


def test_the_repository_hint_is_taken_from_the_files_that_are_not_ambiguous():
    assert hint_from(["a.h", "b.h", "c.cpp"]) == "cpp"
    assert hint_from(["a.h", "b.c"]) == "c"
    assert hint_from(["a.h", "readme.md"]) == ""


# ---- files with no extension ---------------------------------------------------------
@pytest.mark.parametrize(
    ("shebang", "expected"),
    [
        ("#!/bin/bash\n", "bash"),
        ("#!/bin/sh\n", "bash"),
        ("#!/usr/bin/env python3\n", "python"),
        ("#!/usr/bin/env node\n", "javascript"),
        ("#!/usr/bin/pwsh\n", "powershell"),
    ],
)
def test_a_file_with_no_extension_is_read_from_its_shebang(shebang, expected):
    assert detect("scripts/deploy", head=shebang) == expected


def test_a_file_with_no_extension_and_no_shebang_is_unknown():
    """Never the repository's main language: that is how a .txt gets reviewed."""
    assert detect("LICENSE", head="Copyright...", repo_hint="cpp") == UNKNOWN


# ---- the version lists -----------------------------------------------------------------
def test_every_language_the_user_asked_for_is_here():
    assert {lang.id for lang in LANGUAGES} == {
        "c",
        "cpp",
        "csharp",
        "css",
        "html",
        "java",
        "javascript",
        "python",
        "rust",
        "typescript",
        "bash",
        "powershell",
    }


def test_versions_are_ordered_oldest_first_and_that_order_is_the_mechanism():
    cpp = languages.get("cpp")
    assert cpp.versions[0] == "c++98"
    assert cpp.index_of("c++14") < cpp.index_of("c++20")
    assert cpp.index_of("nonsense") == -1


def test_every_version_has_a_label_a_person_would_recognise():
    for lang in LANGUAGES:
        assert len(lang.version_labels) == len(lang.versions), lang.id
    assert languages.get("cpp").label_for("c++17") == "C++17"
    assert languages.get("python").label_for("py312") == "Python 3.12+"


def test_a_version_nobody_set_has_no_label():
    assert languages.get("cpp").label_for("") == ""


def test_no_two_languages_claim_the_same_extension_by_accident():
    seen: dict[str, str] = {}
    for lang in LANGUAGES:
        for ext in lang.extensions:
            if ext in seen:
                assert ext in languages.AMBIGUOUS, f"{ext}: {seen[ext]} and {lang.id}"
            seen[ext] = lang.id


def test_the_any_language_row_has_a_readable_name():
    assert languages.label_of(languages.ANY) == "Any language"
    assert languages.label_of("python") == "Python"
    assert languages.label_of("klingon") == "Unknown"
