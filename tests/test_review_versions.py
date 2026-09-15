"""Reading a repository's own answer about which language versions it uses."""

import os
import time
from pathlib import Path

import pytest

from git_assistant.review import languages, versions
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


# ---- what is looked at, and in which order ----------------------------------------------
def _old_find(repo: Path, names, suffix=""):
    """`_find` as it was: a walk of the whole tree for every call, thrown away as it went."""
    found = []
    root_depth = len(repo.parts)
    for path in repo.rglob("*"):
        if len(path.parts) - root_depth > versions._MAX_DEPTH:
            continue
        if any(part in versions._SKIP for part in path.parts[root_depth:]):
            continue
        if not path.is_file():
            continue
        if path.name in names or (suffix and path.name.endswith(suffix)):
            found.append(path)
        if len(found) >= 8:
            break
    return found


def _tangle(root: Path):
    """Manifests at every depth, in folders that sort every way, some where nothing looks."""
    names = []
    for top in ("b", "A", "c", "node_modules", "build"):
        names.append(f"{top}/CMakeLists.txt")
        for middle in ("z", "Y", "x"):
            names.append(f"{top}/{middle}/CMakeLists.txt")
            names.append(f"{top}/{middle}/App.csproj")
            for low in ("q", "p"):
                names.append(f"{top}/{middle}/{low}/CMakeLists.txt")  # deep enough to count
                names.append(f"{top}/{middle}/{low}/deeper/CMakeLists.txt")  # too deep
    names += [
        "CMakeLists.txt",
        "dist",  # a file named for a folder nothing looks in
        ".git/CMakeLists.txt",
        "b/.git/CMakeLists.txt",
        "A/Y/dist/CMakeLists.txt",
        "tsconfig.json",
        "c/tsconfig.json",
    ]
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("set(CMAKE_CXX_STANDARD 17)\n", encoding="utf-8")
    return root


def test_the_files_looked_at_are_the_ones_rglob_found_in_the_order_it_found_them(tmp_path):
    """Which of several manifests is read first decides the answer, so the order is kept."""
    root = _tangle(tmp_path)
    depth = len(root.parts)
    expected = [
        path
        for path in root.rglob("*")
        if len(path.parts) - depth <= versions._MAX_DEPTH
        and not any(part in versions._SKIP for part in path.parts[depth:])
        and path.is_file()
    ]

    assert versions._Repository(root).files() == expected
    assert len(expected) > 8


@pytest.mark.parametrize(
    ("names", "suffix"),
    [
        (("CMakeLists.txt", "Makefile", "meson.build"), ""),
        ((), ".csproj"),
        (("tsconfig.json", ".babelrc", "babel.config.json"), ""),
        (("pom.xml",), ""),
    ],
)
def test_each_detector_is_offered_the_manifests_it_was_offered_before(tmp_path, names, suffix):
    root = _tangle(tmp_path)

    assert versions._find(versions._Repository(root), names, suffix) == _old_find(
        root, names, suffix
    )


# ---- read once --------------------------------------------------------------------------
def _settled(root: Path, seconds: int = 3600) -> None:
    """Every folder and file under ``root`` stamped as if last changed an hour ago."""
    past = time.time() - seconds
    for path in [root, *root.rglob("*")]:
        os.utime(path, (past, past))


@pytest.fixture
def walks(monkeypatch):
    """Every folder detection lists, as it lists it."""
    versions.forget()
    listed = []
    real = versions._Repository._list

    def listing(self, folder):
        listed.append(folder)
        return real(self, folder)

    monkeypatch.setattr(versions._Repository, "_list", listing)
    yield listed
    versions.forget()


def test_a_repository_is_walked_once_while_nothing_it_was_read_from_changes(tmp_path, walks):
    repo = _repo(
        tmp_path,
        {
            "pyproject.toml": 'requires-python = ">=3.12"',
            "src/app/App.csproj": "<Project><LangVersion>11</LangVersion></Project>",
        },
    )
    _settled(tmp_path)
    first = detect(repo)
    assert walks, "the first time is a walk"
    walks.clear()

    again = detect(repo)

    assert walks == []
    assert again == first
    assert set(again) == {"python", "csharp"}
    assert set(detect(repo, wanted=["csharp"])) == {"csharp"}
    assert walks == []


def test_an_edited_manifest_is_read_again(tmp_path, walks):
    repo = _repo(tmp_path, {"pyproject.toml": 'requires-python = ">=3.12"'})
    _settled(tmp_path)
    assert detect(repo)["python"].version == "py312"

    (tmp_path / "pyproject.toml").write_text('requires-python = ">=3.8"', encoding="utf-8")

    assert detect(repo)["python"].version == "py38"


def test_a_manifest_added_or_removed_in_a_folder_is_noticed(tmp_path, walks):
    repo = _repo(tmp_path, {"src/app/main.cs": "class App {}"})
    _settled(tmp_path)
    assert "csharp" not in detect(repo)

    manifest = tmp_path / "src" / "app" / "App.csproj"
    manifest.write_text("<Project><LangVersion>10</LangVersion></Project>", encoding="utf-8")
    assert detect(repo)["csharp"].version == "cs10"

    _settled(tmp_path)
    detect(repo)
    manifest.unlink()
    assert "csharp" not in detect(repo)


def test_a_repository_changed_moments_ago_is_read_again_next_time(tmp_path, walks):
    """A change in the same clock tick as the last one leaves no new stamp to see."""
    repo = _repo(tmp_path, {"Cargo.toml": '[package]\nedition = "2021"\n'})
    detect(repo)
    walks.clear()

    detect(repo)

    assert walks, "a reading of what changed a moment ago is not kept"
