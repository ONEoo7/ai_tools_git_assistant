"""The starter files: what they say, and how they meet a repository that has some.

Merging is tested against real repositories wherever git's reading of the result
is the point -- a .gitattributes is only right if `git check-attr` agrees.
"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from git_assistant import git_ops
from git_assistant import starter_files as sf
from git_assistant.agents import checks
from git_assistant.agents import probe as probe_mod
from git_assistant.agents.base import AgentContext, Status
from git_assistant.config import Settings
from git_assistant.review import languages

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
ROOT = Path(__file__).resolve().parents[1]


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    _git(path, "init", "-q", "--initial-branch=main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "core.autocrlf", "false")
    return path


def _only(planned, name):
    (item,) = [p for p in planned if p.name == name]
    return item


def _attr(repo, attribute, path):
    out = _git(repo, "check-attr", attribute, "--", path).stdout
    return out.rsplit(": ", 1)[-1].strip()


# ---- the shipped templates -----------------------------------------------------------
def test_every_language_code_review_knows_has_a_gitignore_decision():
    assert set(sf.GITIGNORE_TEMPLATE) == set(languages.ids())


def test_every_template_named_is_one_this_build_ships():
    shipped = sf.bundled()
    assert shipped is not None
    named = {name for name in sf.GITIGNORE_TEMPLATE.values() if name}
    assert named <= set(shipped.templates)


def test_the_resource_is_pinned_and_written_with_lf_only():
    raw = (ROOT / "src/git_assistant/resources/starter_templates.json").read_bytes()
    data = json.loads(raw)

    assert b"\r" not in raw
    assert re.fullmatch(r"[0-9a-f]{40}", data["gitignore"]["commit"])
    assert re.fullmatch(r"[0-9a-f]{40}", data["licenses"]["commit"])
    assert data["gitignore"]["license"] == "CC0-1.0"


def test_a_missing_resource_means_nothing_to_offer_not_a_crash(monkeypatch):
    monkeypatch.setattr(sf, "data_file", lambda *parts: None)
    sf.bundled.cache_clear()
    try:
        assert sf.bundled() is None
        assert sf.ignore_sections(["python"]) == []
        choices = sf.Choices(gitignore=("python",), license="MIT", holder="A", year=1)
        assert {p.action for p in sf.plan(None, choices)} == {"skip"}
    finally:
        sf.bundled.cache_clear()


# ---- .gitignore ----------------------------------------------------------------------
def test_javascript_and_typescript_share_one_node_section():
    sections = sf.ignore_sections(["typescript", "python", "javascript", "c"])

    assert [s.template for s in sections] == ["C", "Node", "Python"]
    assert sections[1].languages == ("JavaScript", "TypeScript")


@pytest.mark.parametrize("language", ["css", "html", "bash", "powershell"])
def test_a_language_with_no_template_adds_nothing(language):
    assert not sf.has_template(language)
    assert sf.ignore_sections([language]) == []


def test_a_new_gitignore_marks_each_section():
    (item,) = sf.plan(None, sf.Choices(gitignore=("python", "rust")))
    text = item.content.decode()

    assert item.action == "create"
    assert text.startswith(
        "# >>> git-assistant: Python (Python), from github/gitignore @"
    )
    assert "# <<< git-assistant: Python\n" in text
    assert "# >>> git-assistant: Rust (Rust)" in text
    assert "__pycache__/" in text


def test_appending_keeps_every_byte_that_was_there(repo):
    before = b"# mine\r\n.env.local\r\n/out"  # CRLF, and no final newline
    (repo / ".gitignore").write_bytes(before)

    (item,) = sf.plan(repo, sf.Choices(gitignore=("python",)))

    assert item.action == "append"
    assert item.content.startswith(before + b"\r\n\r\n# >>> git-assistant: Python")
    assert b"\n" not in item.content.replace(b"\r\n", b""), "one line ending throughout"


def test_adding_the_same_languages_again_adds_nothing(repo):
    choices = sf.Choices(gitignore=("python", "javascript"))
    sf.write(repo, sf.plan(repo, choices))
    written = (repo / ".gitignore").read_bytes()

    (again,) = sf.plan(repo, choices)

    assert again.action == "skip"
    assert (repo / ".gitignore").read_bytes() == written
    assert again.kept == (
        "Already has the Node section.",
        "Already has the Python section.",
    )


def test_only_the_missing_sections_are_added(repo):
    sf.write(repo, sf.plan(repo, sf.Choices(gitignore=("python",))))

    (item,) = sf.plan(repo, sf.Choices(gitignore=("python", "cpp")))

    assert item.action == "append"
    assert item.added.startswith("# >>> git-assistant: C++")
    assert "git-assistant: Python" not in item.added


def test_a_utf16_gitignore_is_left_alone(repo):
    (repo / ".gitignore").write_bytes("*.log\n".encode("utf-16"))

    (item,) = sf.plan(repo, sf.Choices(gitignore=("python",)))

    assert item.action == "skip"
    assert "UTF-16" in item.summary


# ---- .gitattributes ------------------------------------------------------------------
ATTRIBUTES = sf.Attributes(
    default="lf", rules=(sf.EolRule("*.bat", "crlf"), sf.EolRule("*.png", "binary"))
)


def test_a_new_gitattributes_decides_every_file_first():
    (item,) = sf.plan(None, sf.Choices(attributes=ATTRIBUTES))
    rules = [
        line for line in item.content.decode().splitlines() if not line.startswith("#")
    ]

    assert rules == ["* text=auto eol=lf", "*.bat text eol=crlf", "*.png binary"]


@pytest.mark.parametrize(
    ("ending", "line"),
    [
        ("lf", "*.x text eol=lf"),
        ("crlf", "*.x text eol=crlf"),
        ("native", "*.x text"),
        ("binary", "*.x binary"),
    ],
)
def test_each_ending_is_written_as_git_spells_it(ending, line):
    assert sf.EolRule("*.x", ending).line() == line


def test_the_rule_for_every_file_goes_above_the_rules_already_there(repo):
    """Appended last, `* text=auto eol=lf` would override `*.cmd eol=crlf`."""
    (repo / ".gitattributes").write_bytes(b"# ours\n*.cmd text eol=crlf\n")

    sf.write(repo, sf.plan(repo, sf.Choices(attributes=ATTRIBUTES)))

    lines = (repo / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert lines[:3] == ["# ours", "* text=auto eol=lf", "*.cmd text eol=crlf"]
    assert _attr(repo, "eol", "run.cmd") == "crlf"
    assert _attr(repo, "eol", "run.bat") == "crlf"
    assert _attr(repo, "eol", "notes.txt") == "lf"


def test_a_rule_already_there_is_kept_and_reported(repo):
    (repo / ".gitattributes").write_bytes(
        b"* text=auto\n*.bat text eol=lf\n*.png binary\n"
    )

    (item,) = sf.plan(repo, sf.Choices(attributes=ATTRIBUTES))

    assert item.action == "skip", "nothing left to add: the rest is already decided"
    assert item.kept == (
        "Kept your rule for all files: Native (CRLF on Windows, LF elsewhere), not LF.",
        "Kept your rule for *.bat: LF, not CRLF.",
    )


def test_only_the_rules_missing_are_appended(repo):
    (repo / ".gitattributes").write_bytes(b"* text=auto eol=lf\r\n*.png binary\r\n")

    (item,) = sf.plan(repo, sf.Choices(attributes=ATTRIBUTES))

    assert item.action == "append"
    assert item.added == "*.bat text eol=crlf\n"
    assert item.content == (
        b"* text=auto eol=lf\r\n*.png binary\r\n*.bat text eol=crlf\r\n"
    )


@pytest.mark.parametrize(
    ("pattern", "ok"),
    [
        ("*.bat", True),
        ("docs/**", True),
        ("", False),
        ("my file.txt", False),
        ("!*.bat", False),
        ("#notes", False),
        ("docs/", False),
    ],
)
def test_patterns_git_would_misread_are_refused(pattern, ok):
    assert (sf.pattern_problem(pattern) == "") is ok


def test_a_bad_pattern_stops_the_file_rather_than_being_written():
    attributes = sf.Attributes(rules=(sf.EolRule("docs/", "lf"),))

    (item,) = sf.plan(None, sf.Choices(attributes=attributes))

    assert item.action == "skip"
    assert "docs/" in item.summary


def test_a_repository_given_the_defaults_passes_its_own_audit(repo):
    """What the Audit tab asks of line endings, a repository started here has."""
    choices = sf.Choices(attributes=sf.Attributes("native", sf.DEFAULT_RULES))
    sf.write(repo, sf.plan(repo, choices))
    for name in ("build.sh", "build.bat", "notes.txt"):
        (repo / name).write_text("echo\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "start")

    probe = probe_mod.collect(AgentContext(repo=str(repo), settings=Settings()))

    assert checks.eol_declared(probe).status is Status.PASS
    assert checks.eol_scripts(probe).status is Status.PASS


# ---- LICENSE -------------------------------------------------------------------------
def test_mit_is_the_text_this_project_is_licensed_under():
    ours = (ROOT / "LICENSE").read_bytes().replace(b"\r\n", b"\n").decode()

    assert sf.license_text("MIT", holder="Stefan Ghitescu", year=2026) == ours


def test_apache_is_kept_word_for_word():
    text = sf.license_text("Apache-2.0", holder="ignored", year=2026)

    assert hashlib.sha256(text.encode()).hexdigest() == (
        "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
    )
    assert "[yyyy] [name of copyright owner]" in text


def test_mit_without_a_holder_is_not_written(repo):
    (item,) = sf.plan(repo, sf.Choices(license="MIT", holder="  ", year=2026))

    assert item.action == "skip"


def test_an_existing_license_is_replaced_only_when_asked(repo):
    (repo / "LICENSE.md").write_bytes(b"All rights reserved.\n")
    planned = sf.plan(repo, sf.Choices(license="MIT", holder="A", year=2026))
    (item,) = planned

    assert (item.name, item.action) == ("LICENSE.md", "replace")
    sf.write(repo, planned)
    assert (repo / "LICENSE.md").read_bytes() == b"All rights reserved.\n"
    assert not (repo / "LICENSE").exists(), "not a second license beside the first"

    sf.write(repo, planned, replace=True)
    assert (repo / "LICENSE.md").read_text(encoding="utf-8").startswith("MIT License")


def test_the_same_license_again_is_not_a_replacement(repo):
    choices = sf.Choices(license="Apache-2.0")
    sf.write(repo, sf.plan(repo, choices))

    (item,) = sf.plan(repo, choices)

    assert item.action == "skip"


# ---- README.md -----------------------------------------------------------------------
def test_a_new_readme_is_empty(repo):
    report = sf.write(repo, sf.plan(repo, sf.Choices(readme=True)))

    assert (repo / "README.md").read_bytes() == b""
    assert report.done == {"create": ["README.md"]}
    assert report.summary() == "Created README.md."


def test_the_report_says_what_was_done_to_which_files():
    report = sf.Written(
        done={
            "append": [".gitattributes"],
            "create": ["README.md", ".gitignore", "LICENSE"],
        }
    )

    assert report.summary() == (
        "Created README.md, .gitignore and LICENSE. Added to .gitattributes."
    )


@pytest.mark.parametrize("name", ["README.md", "readme.md", "README", "Readme.rst"])
def test_any_readme_already_there_is_never_overwritten(repo, name):
    (repo / name).write_bytes(b"# Mine\n")

    sf.write(repo, sf.plan(repo, sf.Choices(readme=True)), replace=True)

    assert (repo / name).read_bytes() == b"# Mine\n"


# ---- writing -------------------------------------------------------------------------
def test_new_files_get_the_endings_the_new_rules_give_them(repo):
    choices = sf.Choices(
        gitignore=("python",),
        attributes=sf.Attributes("crlf", ()),
        license="MIT",
        holder="A",
        year=2026,
        readme=True,
    )

    report = sf.write(repo, sf.plan(repo, choices))

    assert report.problems == []
    for name in (".gitattributes", ".gitignore", "LICENSE"):
        data = (repo / name).read_bytes()
        assert data.count(b"\r\n") == data.count(b"\n"), f"{name} is not CRLF"


def test_native_means_whatever_this_machine_checks_out(repo):
    _git(repo, "config", "core.eol", "crlf")
    choices = sf.Choices(
        attributes=sf.Attributes("native", ()), license="MIT", holder="A", year=1
    )

    sf.write(repo, sf.plan(repo, choices))

    assert b"\r\n" in (repo / "LICENSE").read_bytes()


def test_nothing_is_staged(repo):
    choices = sf.Choices(
        gitignore=("rust",), attributes=ATTRIBUTES, license="MIT", holder="A", year=1
    )

    sf.write(repo, sf.plan(repo, choices))

    assert _git(repo, "status", "--porcelain").stdout.split("\n")[0].startswith("??")
    assert _git(repo, "diff", "--cached", "--name-only").stdout == ""


def test_a_failure_to_fix_line_endings_is_reported_not_raised(repo, monkeypatch):
    def fail(*args, **kwargs):
        raise git_ops.GitError("hash-object failed")

    monkeypatch.setattr(git_ops, "normalize_line_endings", fail)

    report = sf.write(repo, sf.plan(repo, sf.Choices(license="Apache-2.0")))

    assert (repo / "LICENSE").exists()
    assert report.problems == ["Line endings were not applied: hash-object failed"]
