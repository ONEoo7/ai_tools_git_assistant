"""Reading a repository's own answer about which language versions it uses."""

import pytest

from git_assistant.review import languages
from git_assistant.review.versions import detect, from_content


def _repo(tmp_path, files: dict[str, str]):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return str(tmp_path)


# ---- what a project declares --------------------------------------------------------
@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ('requires-python = ">=3.13"', "py312"),
        ('requires-python = ">=3.12"', "py312"),
        ('requires-python = ">=3.10"', "py310"),
        ('requires-python = ">=3.8"', "py38"),
        ('requires-python = ">=3.6"', "py36"),
    ],
)
def test_python_is_read_from_pyproject(tmp_path, declared, expected):
    repo = _repo(tmp_path, {"pyproject.toml": f"[project]\n{declared}\n"})
    found = detect(repo)["python"]
    assert found.version == expected
    assert "pyproject.toml" in found.source


def test_python_is_read_from_a_python_version_file(tmp_path):
    repo = _repo(tmp_path, {".python-version": "3.12.1\n"})
    assert detect(repo)["python"].version == "py312"


def test_rust_is_read_from_its_edition(tmp_path):
    repo = _repo(tmp_path, {"Cargo.toml": '[package]\nedition = "2021"\n'})
    found = detect(repo)["rust"]
    assert found.version == "rust2021"
    assert "edition" in found.source


def test_an_edition_this_build_does_not_know_is_not_invented(tmp_path):
    repo = _repo(tmp_path, {"Cargo.toml": '[package]\nedition = "2030"\n'})
    assert "rust" not in detect(repo)


def test_typescript_is_read_from_the_pinned_compiler(tmp_path):
    repo = _repo(
        tmp_path,
        {
            "tsconfig.json": "{}",
            "package.json": '{"devDependencies": {"typescript": "^5.4.2"}}',
        },
    )
    found = detect(repo)["typescript"]
    assert found.version == "ts5"
    assert "typescript" in found.source


def test_javascript_is_read_from_the_compile_target(tmp_path):
    repo = _repo(tmp_path, {"tsconfig.json": '{"compilerOptions": {"target": "ES2020"}}'})
    assert detect(repo)["javascript"].version == "es2020"


def test_csharp_is_read_from_an_explicit_language_version(tmp_path):
    repo = _repo(tmp_path, {"App.csproj": "<Project><LangVersion>11.0</LangVersion></Project>"})
    found = detect(repo)["csharp"]
    assert found.version == "cs11"
    assert "LangVersion" in found.source


def test_csharp_falls_back_to_what_the_framework_defaults_to(tmp_path):
    repo = _repo(
        tmp_path, {"App.csproj": "<Project><TargetFramework>net8.0</TargetFramework></Project>"}
    )
    assert detect(repo)["csharp"].version == "cs12"


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("<maven.compiler.release>21</maven.compiler.release>", "java21"),
        ("<maven.compiler.source>17</maven.compiler.source>", "java17"),
        ("<maven.compiler.release>8</maven.compiler.release>", "java8"),
    ],
)
def test_java_is_read_from_the_pom(tmp_path, declared, expected):
    repo = _repo(tmp_path, {"pom.xml": f"<project><properties>{declared}</properties></project>"})
    assert detect(repo)["java"].version == expected


def test_java_is_read_from_gradle_too(tmp_path):
    repo = _repo(tmp_path, {"build.gradle": "java { sourceCompatibility = 17 }"})
    assert detect(repo)["java"].version == "java17"


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("set(CMAKE_CXX_STANDARD 20)", "c++20"),
        ("set(CMAKE_CXX_STANDARD 17)", "c++17"),
        ("target_compile_features(app PRIVATE cxx_std_23)", "c++23"),
    ],
)
def test_cpp_is_read_from_cmake(tmp_path, declared, expected):
    repo = _repo(tmp_path, {"CMakeLists.txt": declared})
    assert detect(repo)["cpp"].version == expected


def test_cpp_is_read_from_a_makefile_flag(tmp_path):
    repo = _repo(tmp_path, {"Makefile": "CXXFLAGS = -O2 -std=c++14 -Wall\n"})
    assert detect(repo)["cpp"].version == "c++14"


def test_c_is_read_separately_from_cpp(tmp_path):
    repo = _repo(tmp_path, {"CMakeLists.txt": "set(CMAKE_C_STANDARD 11)"})
    found = detect(repo)
    assert found["c"].version == "c11"
    assert "cpp" not in found


# ---- when nothing says --------------------------------------------------------------
def test_a_repository_that_declares_nothing_answers_nothing(tmp_path):
    """Absent, never guessed: a version that is too new quietly adds rules."""
    assert detect(_repo(tmp_path, {"readme.md": "hello"})) == {}


def test_a_repository_that_is_not_there_answers_nothing(tmp_path):
    assert detect(str(tmp_path / "gone")) == {}


def test_only_the_languages_asked_about_are_looked_for(tmp_path):
    repo = _repo(
        tmp_path,
        {"pyproject.toml": 'requires-python = ">=3.12"', "Cargo.toml": 'edition = "2021"'},
    )
    assert set(detect(repo, wanted=["rust"])) == {"rust"}


def test_a_manifest_deep_in_the_tree_is_not_taken_as_the_answer(tmp_path):
    repo = _repo(tmp_path, {"a/b/c/d/e/App.csproj": "<Project><LangVersion>7</LangVersion></Project>"})
    assert "csharp" not in detect(repo)


def test_a_manifest_in_node_modules_is_not_the_project_s_own(tmp_path):
    repo = _repo(
        tmp_path,
        {"node_modules/dep/tsconfig.json": '{"compilerOptions": {"target": "ES5"}}'},
    )
    assert "javascript" not in detect(repo)


def test_the_source_of_every_answer_can_be_shown_to_the_user(tmp_path):
    repo = _repo(tmp_path, {"Cargo.toml": '[package]\nedition = "2018"\n'})
    assert detect(repo)["rust"].describe().startswith("from ")


# ---- what a single file says about itself ---------------------------------------------
def test_a_doctype_says_which_html_this_is():
    assert from_content("html", "<!DOCTYPE html>\n<html>").version == "html5"
    old = '<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01//EN">'
    assert from_content("html", old).version == "html4"


def test_a_shebang_says_which_shell_this_is():
    assert from_content("bash", "#!/bin/bash\nset -e\n").version == "bash5"
    assert from_content("bash", "#!/bin/sh\n").version == "posix"


def test_a_requires_line_says_which_powershell_this_is():
    assert from_content("powershell", "#Requires -Version 7.2\n").version == "ps7"
    assert from_content("powershell", "#Requires -Version 5.1\n").version == "ps5"


def test_a_file_that_says_nothing_about_itself_answers_nothing():
    assert from_content("html", "<html>") is None
    assert from_content("python", "import os\n") is None


def test_every_version_a_detector_can_return_is_a_real_one(tmp_path):
    """A version this build cannot filter on would silently keep every rule."""
    cases = {
        "pyproject.toml": 'requires-python = ">=3.12"',
        "Cargo.toml": '[package]\nedition = "2021"',
        "tsconfig.json": '{"compilerOptions": {"target": "ES2020"}}',
        "App.csproj": "<Project><LangVersion>11</LangVersion></Project>",
        "pom.xml": "<project><properties><maven.compiler.release>21</maven.compiler.release></properties></project>",
        "CMakeLists.txt": "set(CMAKE_CXX_STANDARD 20)\nset(CMAKE_C_STANDARD 11)",
    }
    for language, found in detect(_repo(tmp_path, cases)).items():
        lang = languages.get(language)
        assert found.version in lang.versions, f"{language}: {found.version}"
