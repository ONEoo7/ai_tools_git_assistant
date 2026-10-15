"""Rules that hold for every test here.

The only one so far is about modal dialogs, and it exists because of what
happens without it: a test that reaches ``QMessageBox.question`` and answers
nothing does not fail. It waits, for about thirty seconds, and then Windows
returns a default and the test carries on and passes. Thirty seconds bought
nothing, said nothing, and the suite quietly got slower every time another one
was added.

So a modal that nobody has arranged an answer for is an error, named and
immediate. A test that means to open one says what it answers -- which every
test here that opens one already does.

The rate-limit backoff is the same problem wearing different clothes: a test
that meets a 429 and honours the wait *does* honour it, for a minute and a
half, and passes. So sleeping is recorded rather than lived.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# `pytest --testmon` learns which tests ran which lines from coverage's
# per-test contexts. On a Python with sys.monitoring, coverage measures with
# that unless told otherwise, and it cannot keep contexts apart: the map it
# builds is missing tests, and an edit then reruns some of the tests that
# reach it rather than all of them. The C tracer keeps them. Set here because
# this file is read before any plugin starts measuring, and xdist's workers
# inherit it.
os.environ.setdefault("COVERAGE_CORE", "ctrace")

#: What ``git init`` copies into every repository it makes, less the fourteen
#: sample hooks git ships: files nothing runs, and half of everything a run of
#: the suite leaves in its temporary folder. pytest keeps three runs' folders and
#: deletes the oldest at the end of each run, one file at a time -- ten seconds
#: of the run, spent after the last test. Every repository a test makes, and
#: every clone, is made from this. ``info/exclude`` stays, since a test writes
#: to it, and so does ``description``: what is left is what git would make.
_GIT_TEMPLATE = {
    "description": "Unnamed repository; edit this file 'description' to name the repository.\n",
    "info/exclude": (
        "# git ls-files --others --exclude-from=.git/info/exclude\n"
        "# Lines that start with '#' are comments.\n"
        "# For a project mostly in C, the following would be a good set of\n"
        "# exclude patterns (uncomment them if you want to use them):\n"
        "# *.[oa]\n"
        "# *~\n"
    ),
}


def _git_template() -> Path:
    """The template, written where it is missing or different, and nowhere else:
    another run of the suite may be reading it."""
    import tempfile

    root = Path(tempfile.gettempdir()) / "git-assistant-tests-template"
    for name, text in _GIT_TEMPLATE.items():
        file = root / name
        try:
            if file.read_text(encoding="utf-8") == text:
                continue
        except OSError:
            file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8", newline="\n")
    (root / "hooks").mkdir(exist_ok=True)
    return root


# Set once, by the process that starts the run; its workers inherit it.
if "PYTEST_XDIST_WORKER" not in os.environ:
    os.environ["GIT_TEMPLATE_DIR"] = str(_git_template())

#: Every store that writes into the user's config directory. A test that runs
#: against the real one leaves files in it -- which happened, and left two
#: settings directories in AppData named after repositories that never existed.
#: Each module here reads `user_config_dir` at call time, which is what makes
#: redirecting it enough.
_STORES = (
    "git_assistant.config",
    "git_assistant.repo_config",
    "git_assistant.settings_backup",
    "git_assistant.commit_history",
    "git_assistant.usage",
    "git_assistant.review.rule_files",
    "git_assistant.review.rules",
    "git_assistant.review.history",
    "git_assistant.agents.history",
    "git_assistant.identities",
)


@pytest.fixture(autouse=True)
def _config_dir_is_never_the_real_one(tmp_path, monkeypatch):
    """Point every store at a directory this test owns.

    Tests that want to look at what was written patch this themselves and are
    unaffected; this is the floor, so that forgetting to is a test that writes
    somewhere harmless rather than into the user's settings.
    """
    import importlib

    home = tmp_path / "config-root"
    for name in _STORES:
        try:
            module = importlib.import_module(name)
        except ImportError:  # an optional dependency this run does not have
            continue
        if hasattr(module, "user_config_dir"):
            monkeypatch.setattr(
                module, "user_config_dir", lambda *a, **k: str(home)
            )

@pytest.fixture(autouse=True)
def _global_git_config_is_never_the_real_one(monkeypatch, tmp_path_factory):
    """Mark nothing safe in the user's own global git config.

    Adding a repository marks it safe there, and settings have that on by
    default: every test that adds one would leave a safe.directory line in the
    real ~/.gitconfig. A test that points ``GIT_CONFIG_GLOBAL`` at a file of its
    own marks for real, in that file; any other marks nothing.
    """
    from pathlib import Path

    from git_assistant import git_ops

    real = git_ops.add_safe_lines
    base = Path(tmp_path_factory.getbasetemp()).resolve()

    def add_safe_lines(lines):
        target = os.environ.get("GIT_CONFIG_GLOBAL", "")
        if not target or not Path(target).resolve().is_relative_to(base):
            return git_ops.Marked()
        return real(lines)

    monkeypatch.setattr(git_ops, "add_safe_lines", add_safe_lines)
    git_ops.forget_safe_directories()
    yield
    git_ops.forget_safe_directories()


#: ``subprocess.run`` as it was before any test stood in for it.
_REAL_RUN = subprocess.run

#: What git said to each read of its own config this session, and the
#: environment it was asked in. See `_global_reads_are_asked_once`.
_global_answers: dict[tuple[str, ...], object] = {}
_session_environment: dict[str, str] | None = None


def _environment() -> dict[str, str]:
    """``os.environ``, less what pytest changes for every test of its own accord."""
    return {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}


def _reads_git_config(args) -> bool:
    """Whether ``args`` only reads git's own config -- no file named, no repository's."""
    return (
        args[:1] == ["config"]
        and bool({"--get", "--get-all"} & set(args))
        and not {"--file", "-f", "--blob", "--local", "--worktree"} & set(args)
    )


@pytest.fixture(autouse=True, scope="session")
def _session_git_environment():
    """Note the environment the session started in, before any test changes it."""
    global _session_environment
    _session_environment = _environment()
    yield
    _session_environment = None
    _global_answers.clear()


@pytest.fixture(autouse=True)
def _global_reads_are_asked_once(monkeypatch, _session_git_environment):
    """Ask git what its own config says once a session rather than once a test.

    Every test that runs a git command through `git_ops` has it read the
    safe.directory list first, which the fixture above makes it read again for
    each test; and every one that draws the branches tab or the settings window
    has it read the global identity, since each test's identities file is new.
    Four processes, seven hundred tests, and one answer: no test writes the
    global config it runs under, which is what the fixture above is for.

    So where the environment is the one the session began in and
    ``subprocess.run`` is the real one, a read is answered as git answered it the
    first time. A test that points git at a config of its own, or stands in for
    ``subprocess.run`` to see what is started or to have git missing, gets git
    itself; so does anything that is not a read, and a write forgets every answer.
    """
    from dataclasses import replace

    from git_assistant import git_ops

    real = git_ops._run_global

    def run_global(args):
        if not _reads_git_config(args):
            if args[:1] == ["config"] and not {"--file", "-f", "--blob"} & set(args):
                _global_answers.clear()
            return real(args)
        if git_ops.subprocess.run is not _REAL_RUN or _environment() != _session_environment:
            return real(args)
        key = tuple(args)
        if key not in _global_answers:
            _global_answers[key] = real(args)
        return replace(_global_answers[key])

    monkeypatch.setattr(git_ops, "_run_global", run_global)


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Record backoff waits instead of taking them, and yield the seconds.

    Autouse because the cost of forgetting is a suite that passes slowly rather
    than one that fails: ask for ``slept`` by name to assert on the waits.
    Limiters are dropped either side, since they are shared per account and
    would otherwise carry one test's pacing into the next.

    The clock advances by whatever was "slept", which is not decoration. A
    recorded wait that does not move the clock leaves the limiter's next-allowed
    moment still in the future, so the next caller waits the same interval again
    and the recorded total comes out well over what was actually asked for --
    a fake that made the retry budget look breached when it was not.
    """
    from git_assistant import ratelimit

    waits: list[float] = []
    now = [0.0]

    def pass_the_time(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(ratelimit, "sleep", pass_the_time)
    monkeypatch.setattr(ratelimit, "monotonic", lambda: now[0])
    ratelimit.forget_all()
    yield waits
    ratelimit.forget_all()


#: The blocking entry points. Each returns a value the caller acts on, which is
#: exactly why a test that does not choose one is not testing anything.
_MODALS = ("question", "warning", "critical", "information", "about")


@pytest.fixture(autouse=True)
def _no_unanswered_dialogs(monkeypatch):
    """Make an unanswered modal fail the test that opened it."""
    widgets = pytest.importorskip("PyQt6.QtWidgets", reason="Qt is optional here")
    box = widgets.QMessageBox

    def refuse(name):
        def opened(*args, **kwargs):
            raise AssertionError(
                f"QMessageBox.{name} was opened with nothing to answer it. "
                "A test that expects this dialog should say what it answers:\n"
                f"    monkeypatch.setattr(QMessageBox, {name!r}, "
                "lambda *a, **k: QMessageBox.StandardButton.Yes)"
            )

        return opened

    for name in _MODALS:
        if hasattr(box, name):
            monkeypatch.setattr(box, name, staticmethod(refuse(name)))


class _Clipboard:
    """A clipboard of the test's own: what it last copied, and nobody else's."""

    def __init__(self):
        self._text = ""

    def setText(self, text, *_mode):  # noqa: N802 - Qt's name
        self._text = text

    def text(self, *_mode):
        return self._text


@pytest.fixture(autouse=True)
def _clipboard_is_the_tests_own(monkeypatch):
    """Stand in for the desktop's clipboard, which every process on it shares.

    Whoever copied last owns what is on it, and the next copy waits for the
    owner to let go -- which a worker in the middle of a test, with no event
    loop running, does not do: Windows gives up on it after five seconds, and
    only then lets the copy through. Two dozen workers copying meant one test
    of a copy took five seconds whenever another had copied before it, and a
    run left what the tests copied on the clipboard of whoever started it.

    Tests that look at what was copied stand in for it themselves, and still do.
    """
    try:
        from PyQt6.QtGui import QGuiApplication
    except ImportError:  # Qt is optional here
        return
    clipboard = _Clipboard()
    monkeypatch.setattr(QGuiApplication, "clipboard", staticmethod(lambda: clipboard))


@pytest.fixture(autouse=True, scope="session")
def _one_application():
    """One QApplication for the session, and nothing still running when it goes.

    Each Qt test module asks for ``QApplication.instance() or QApplication([])``
    in a fixture of its own, and the first to ask makes it. When that module is
    done, nothing else may be holding it, and it is destroyed -- with any
    worker a test left running on a QThread, which then writes into freed
    memory and ends the process in whichever test runs next. A settings dialog
    closed while its history was still being read did exactly that, and took
    an xdist worker with it whenever the next test came from another module.
    """
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:  # Qt is optional here
        yield None
        return
    import time

    from git_assistant.ui import workers

    app = QApplication.instance() or QApplication([])
    yield app
    # A worker's finish reaches its thread's quit() through this thread's event
    # loop, so waiting on the thread alone would wait forever.
    deadline = time.monotonic() + 30
    while any(thread.isRunning() for thread, _ in list(workers._IN_FLIGHT)):
        if time.monotonic() > deadline:
            break
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def _worker_threads_are_measured(monkeypatch):
    """Let a tracer follow work onto the QThreads that ``run_worker`` starts.

    ``pytest --testmon`` maps each test to the code it runs by tracing it, and a
    tracer only follows a thread that installs it: ``threading`` does that for
    its own threads and Qt does not for its. Nor can it be installed as the
    thread starts, since every slot PyQt calls there gets a Python thread state
    of its own, so it is installed by the worker's ``run`` itself. Without
    this, an edit to code a test runs only on a worker thread does not rerun
    that test. In a run nothing is tracing there is nothing to install, and
    nothing here changes.
    """
    if threading.gettrace() is None:
        return
    try:
        from git_assistant.ui import workers
    except ImportError:  # Qt is optional here
        return
    real = workers.run_worker

    def run_worker(worker):
        run = worker.run.__func__

        def measured(self):
            trace = threading.gettrace()
            if trace is not None:
                sys.settrace(trace)
            return run(self)

        # Bound to the worker, so PyQt still calls it on the worker's thread,
        # and only while run_worker connects it. Left on the worker, it would
        # hold the worker in a cycle, and a QObject freed by the cycle collector
        # is freed on whichever thread that runs -- a crash this must not add.
        worker.run = types.MethodType(measured, worker)
        try:
            return real(worker)
        finally:
            del worker.run

    # Each module took its own reference when it imported the function.
    for name, module in list(sys.modules.items()):
        if name.startswith("git_assistant") and getattr(module, "run_worker", None) is real:
            monkeypatch.setattr(module, "run_worker", run_worker)


def user_tier(**values):
    """Set repo-scoped values in the user tier, for settings that already exist.

    The names are the ones deep code reads -- ``langfuse_host``,
    ``context_window`` -- which are keys in the settings a repository carries
    and no longer fields on ``Settings``. ``endpoints`` is spelled out because
    it is a map rather than one value.
    """
    from git_assistant import repo_config

    rules: dict = {}
    for name, value in values.items():
        if name == "endpoints":
            rules.setdefault("model", {})["endpoints"] = value
            continue
        section, key = repo_config._BOUND[name]
        rules.setdefault(section, {})[key] = value
    data = repo_config.source_dict(repo_config.Tier.USER)
    for section, entries in rules.items():
        data.setdefault(section, {}).update(entries)
    repo_config.write_text(repo_config.Tier.USER, "", repo_config.text_from(data))


def settings_with(settings=None, **values):
    """Settings for a test, with the repo-scoped half written to the user tier.

    These names -- ``context_window``, ``diff_mode``, ``ignore_globs`` and the
    rest -- used to be fields on ``Settings`` and are now the settings a
    repository carries. Production reads them through ``repo_config.bind``, so
    a test that wants to set one has to say where it lives; this is the one
    line that says it.

    Anything not repo-scoped is passed straight to ``Settings``, so a single
    call still sets a provider and a context window together the way the old
    constructor did.

    Returns the bound view, which is what every consumer of these is handed.
    """
    from git_assistant import repo_config
    from git_assistant.config import Settings

    rules: dict = {}
    for name in list(values):
        where = repo_config._BOUND.get(name)
        if where is None:
            continue
        section, key = where
        rules.setdefault(section, {})[key] = values.pop(name)
    stale = values.pop("stale", None)
    if stale is not None:
        rules.setdefault("audit", {})["stale"] = stale
    endpoints = values.pop("endpoints", None)
    if endpoints is not None:
        rules.setdefault("model", {})["endpoints"] = endpoints

    if rules:
        data = repo_config.source_dict(repo_config.Tier.USER)
        for section, entries in rules.items():
            data.setdefault(section, {}).update(entries)
        repo_config.write_text(repo_config.Tier.USER, "", repo_config.text_from(data))

    base = settings if settings is not None else Settings(**values)
    if settings is not None:
        for name, value in values.items():
            setattr(base, name, value)
    base.save = lambda: None
    return repo_config.bind(base, base.active_repo)


#: Where this worker keeps what ``git init`` made, and the environment it was
#: made in. A copy of it is what ``git init`` would make again only while git
#: would read the same config, which is why the environment is kept with it.
_init_templates: Path | None = None
_init_environment: tuple | None = None


def _git_environment() -> tuple:
    """Everything in the environment that can change what ``git init`` makes."""
    return tuple(
        sorted(
            (key, value)
            for key, value in os.environ.items()
            if key.startswith("GIT_") or key in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME")
        )
    )


@pytest.fixture(autouse=True, scope="session")
def _git_init_templates(tmp_path_factory):
    """Note the environment the session started in, before any test changes it.

    Nothing is built here, so a run that makes no repository runs no git.
    """
    global _init_templates, _init_environment
    _init_templates = tmp_path_factory.mktemp("git-init")
    _init_environment = _git_environment()
    yield
    _init_templates = _init_environment = None
    _committed.clear()


def _init_command(path, bare, branch):
    command = ["git", "init", "-q"]
    if bare:
        command.append("--bare")
    if branch is not None:
        command.append(f"--initial-branch={branch}")
    return [*command, str(path)]


def _init_template(bare, branch):
    """The git directory ``git init`` makes here, or None if it must be run."""
    if _init_templates is None or _git_environment() != _init_environment:
        return None
    made = _init_templates / f"{'bare' if bare else 'work'}-{(branch or '').replace('/', '%')}"
    if not made.exists():
        try:
            subprocess.run(
                _init_command(made, bare, branch),
                capture_output=True,
                creationflags=_NO_WINDOW,
                check=True,
            )
        except BaseException:
            shutil.rmtree(made, ignore_errors=True)
            raise
    return made if bare else made / ".git"


def _config_value(value: str) -> str:
    """``value`` as ``git config`` writes it into the file."""
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    )
    if value != value.strip() or "#" in value or ";" in value:
        return f'"{escaped}"'
    return escaped


def _write_config(file: Path, values: dict[str, str]) -> None:
    """Each of ``values`` where ``git config`` would put it: last in its
    section, or in a new section at the end of the file."""
    lines = file.read_text(encoding="utf-8").splitlines(keepends=True)
    for name, value in values.items():
        section, _, key = name.rpartition(".")
        head, _, sub = section.partition(".")
        header = f'[{head} "{sub}"]' if sub else f"[{head}]"
        entry = f"\t{key} = {_config_value(value)}\n"
        starts = [i for i, line in enumerate(lines) if line.strip() == header]
        if not starts:
            lines += [f"{header}\n", entry]
            continue
        end = starts[-1] + 1
        while end < len(lines) and not lines[end].lstrip().startswith("["):
            end += 1
        lines.insert(end, entry)
    file.write_text("".join(lines), encoding="utf-8", newline="")


def git_init(path, *, bare=False, branch=None, config=None):
    """``git init`` at ``path``, then ``git config`` for each of ``config``.

    Neither is run. The repository is a copy of what ``git init`` made the first
    time it was asked for one like it, and config is written into the file the
    way git writes it. On a machine that scans every process as it starts, the
    three commands cost three tenths of a second, and the suite makes a thousand
    repositories; the copy costs a hundredth. A test that has changed where git
    reads its config gets a real ``git init``, since that config can change what
    it makes.

    ``config`` maps ``section.key`` -- or ``section.subsection.key`` -- to a
    value, set in order, for keys a new repository does not already have.
    Returns ``path``.
    """
    path = Path(path)
    template = _init_template(bare, branch)
    if template is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            _init_command(path, bare, branch),
            capture_output=True,
            creationflags=_NO_WINDOW,
            check=True,
        )
    else:
        shutil.copytree(template, path if bare else path / ".git", dirs_exist_ok=bare)
    if config:
        _write_config(path / "config" if bare else path / ".git" / "config", config)
    return path


#: What `committed` made, by what it was asked for, in this worker's folder.
_committed: dict[tuple, Path] = {}


def _commit_files(path: Path, files, message, config, branch) -> None:
    git_init(path, branch=branch, config=config)
    for name, content in files.items():
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            file.write_bytes(content)
        else:
            file.write_text(content, encoding="utf-8")
    for args in (["add", "-A"], ["commit", "-q", "-m", message]):
        subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            creationflags=_NO_WINDOW,
            check=True,
        )


def committed(path, files, *, message="initial", config=None, branch=None):
    """`git_init` at ``path``, and ``files`` -- names to text or bytes -- committed.

    The first repository asked for with these arguments is made at the side and
    copied, and so is every one after it: the commit is the same commit each
    time, made at the time the first was. Its two git commands were most of the
    setup of a test that wants a repository with something in it, which is most
    of the tests here. As with `git_init`, a test that has changed where git
    reads its config gets its repository made where it asked, for real.

    Returns ``path``.
    """
    path = Path(path)
    if _init_templates is None or _git_environment() != _init_environment:
        _commit_files(path, files, message, config, branch)
        return path
    key = (repr(sorted(files.items())), message, repr(config), branch)
    made = _committed.get(key)
    if made is None:
        made = _init_templates / f"committed-{len(_committed)}"
        try:
            _commit_files(made, files, message, config, branch)
        except BaseException:
            shutil.rmtree(made, ignore_errors=True)
            raise
        _committed[key] = made
    shutil.copytree(made, path, dirs_exist_ok=True)
    return path


#: The ``.cmd`` that ``npm install -g`` writes for a package's command, in the
#: shape current npm writes it. SCRIPT is the script node is to run.
NPM_SHIM = (
    "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\n"
    "SETLOCAL\r\nCALL :find_dp0\r\n\r\nIF EXIST \"%dp0%\\node.exe\" (\r\n"
    "  SET \"_prog=%dp0%\\node.exe\"\r\n) ELSE (\r\n  SET \"_prog=node\"\r\n"
    "  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\n"
    "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & \"%_prog%\"  "
    "\"%dp0%\\SCRIPT\" %*\r\n"
)


def npm_install(root, name, script_text="// the CLI\n", *, shim_text=NPM_SHIM):
    """A CLI as ``npm install -g`` leaves it on Windows: ``<name>.cmd`` in
    ``root``, naming ``node_modules\\<name>\\cli.js``. Returns (shim, script)."""
    script = root / "node_modules" / name / "cli.js"
    script.parent.mkdir(parents=True)
    script.write_text(script_text, encoding="utf-8")
    shim = root / f"{name}.cmd"
    relative = f"node_modules\\{name}\\cli.js"
    shim.write_text(shim_text.replace("SCRIPT", relative), encoding="utf-8")
    return shim, script
