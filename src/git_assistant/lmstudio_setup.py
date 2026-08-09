"""Getting a working LM Studio from nothing, in one press.

Six steps: install the app, turn on developer mode and the background service,
start it, fetch a model, configure that model, and point this application at
it. Each one is skipped when it is already true, so pressing the button twice
is not a second install -- and so is pressing it on a machine that is half set
up already.

This reads and writes LM Studio's own configuration files. Their shapes are not
a published contract and a future version may move them, at which point the
step that touches them fails with what it expected and everything else still
runs. That is a deliberate trade: the alternative is doing all of this by hand.

The app itself is installed through winget rather than by downloading an
installer from a hard-coded URL: winget resolves "latest" itself, verifies the
publisher's package, and does the silent install -- so the one thing most
likely to rot, the download link, is not ours to maintain.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from git_assistant import repo_config

#: The package as the official publisher lists it.
WINGET_PACKAGE = "ElementLabs.LMStudio"
#: Publisher/repository on Hugging Face, and the quantization to fetch.
MODEL_REPO = "lmstudio-community/Qwen3.5-4B-GGUF"
MODEL_QUANT = "Q8_0"
MODEL_FILE = "Qwen3.5-4B-Q8_0.gguf"
#: What the app should be configured to use once the model is there.
CONTEXT_LENGTH = 32768
#: Where the local server listens, which is what this setup configures it for.
ENDPOINT = "http://127.0.0.1:1234"

#: winget's "there is nothing newer to install", which is a success for us.
_WINGET_NO_UPDATE = 0x8A15002B
#: Windows reports it as a signed 32-bit value.
_WINGET_NO_UPDATE_SIGNED = _WINGET_NO_UPDATE - (1 << 32)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
#: How long to wait for the app to appear after installing or launching it.
_APPEAR_TIMEOUT = 120


class SetupError(RuntimeError):
    """A step could not be completed. The message says what was expected."""


class Cancelled(RuntimeError):
    """The user stopped the run."""


def lmstudio_home() -> Path:
    return Path.home() / ".lmstudio"


def settings_file() -> Path:
    return lmstudio_home() / "settings.json"


def server_config_file() -> Path:
    return lmstudio_home() / ".internal" / "http-server-config.json"


def install_location_file() -> Path:
    return lmstudio_home() / ".internal" / "app-install-location.json"


def cli_path() -> Path:
    """LM Studio's own CLI, which the app installs on first run."""
    name = "lms.exe" if sys.platform == "win32" else "lms"
    return lmstudio_home() / "bin" / name


def model_config_file() -> Path:
    """Where LM Studio keeps the settings a model is loaded with."""
    publisher, repo = MODEL_REPO.split("/", 1)
    return (
        lmstudio_home()
        / ".internal"
        / "user-concrete-model-default-config"
        / publisher
        / repo
        / f"{MODEL_FILE}.json"
    )


def app_executable() -> Path | None:
    """Where the app is, as it last recorded, else where it installs by default."""
    try:
        recorded = json.loads(install_location_file().read_text(encoding="utf-8"))
        path = Path(str(recorded.get("path", "")))
        if path.is_file():
            return path
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError, TypeError):
        pass
    if sys.platform != "win32":
        return None
    default = (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs"
        / "LM Studio"
        / "LM Studio.exe"
    )
    return default if default.is_file() else None


# ---- running things -----------------------------------------------------------
@dataclass
class SetupContext:
    """Where a run reports to, and how it is stopped."""

    progress: Callable[[str], None] = lambda _m: None
    is_cancelled: Callable[[], bool] = lambda: False
    #: Injected by the tests so nothing is installed or downloaded to check the
    #: sequencing. Production passes None and gets the real subprocess.
    runner: Callable[..., Iterator[str]] | None = None

    def say(self, message: str) -> None:
        self.progress(message)

    def check(self) -> None:
        if self.is_cancelled():
            raise Cancelled("Cancelled.")

    def run(self, args: list[str], *, ok_codes: tuple[int, ...] = (0,)) -> list[str]:
        """Run a command, streaming its output through ``progress``."""
        if self.runner is not None:
            return list(self.runner(args, ok_codes=ok_codes))
        return _stream(args, ok_codes, self)


def _stream(args: list[str], ok_codes: tuple[int, ...], ctx: SetupContext) -> list[str]:
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        raise SetupError(f"could not run {args[0]}: {exc}") from exc
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                lines.append(line)
                ctx.say(line)
            if ctx.is_cancelled():
                proc.kill()
                raise Cancelled("Cancelled.")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    if proc.returncode not in ok_codes:
        tail = " ".join(lines[-3:]) or "no output"
        raise SetupError(f"{args[0]} exited with {proc.returncode}: {tail}")
    return lines


def _patch_json(path: Path, changes: dict) -> list[str]:
    """Set keys in a JSON file, keeping everything else exactly as it was.

    Read-modify-write rather than replace: this is LM Studio's file and it holds
    far more than the two keys wanted here.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SetupError(f"{path.name} is not readable JSON: {exc}") from exc
    except OSError as exc:
        raise SetupError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SetupError(f"{path.name} does not hold an object")

    changed = [k for k, v in changes.items() if data.get(k) != v]
    if not changed:
        return []
    data.update(changes)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".git-assistant.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise SetupError(f"could not write {path}: {exc}") from exc
    return changed


def _wait_for(path: Path, ctx: SetupContext, what: str, timeout: int = _APPEAR_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        ctx.check()
        if time.monotonic() > deadline:
            raise SetupError(f"{what} did not appear at {path} within {timeout}s")
        time.sleep(1)


# ---- the steps -----------------------------------------------------------------
def install_app(ctx: SetupContext) -> str:
    """Install or update LM Studio, silently, through winget."""
    if sys.platform != "win32":
        raise SetupError("this setup is written for Windows")
    ctx.say(f"Installing {WINGET_PACKAGE} with winget...")
    lines = ctx.run(
        [
            "winget",
            "install",
            "--id",
            WINGET_PACKAGE,
            "--exact",
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--disable-interactivity",
        ],
        # "already installed, nothing newer" is the answer we want on a second
        # press, and winget says it with a non-zero code.
        ok_codes=(0, _WINGET_NO_UPDATE, _WINGET_NO_UPDATE_SIGNED),
    )
    if any("no applicable" in line.lower() for line in lines):
        return "already at the latest version"
    return "installed"


def enable_service_and_developer_mode(ctx: SetupContext) -> str:
    """Turn on developer mode and the background service, and the API server."""
    ctx.say("Enabling developer mode and the local service...")
    changed = _patch_json(
        settings_file(), {"developerMode": True, "enableLocalService": True}
    )
    changed += _patch_json(
        server_config_file(), {"autoStartOnLaunch": True, "port": 1234}
    )
    return ", ".join(changed) if changed else "already configured"


def start_service(ctx: SetupContext) -> str:
    """Run the app in the background, then make sure the API server is up."""
    exe = app_executable()
    if exe is None:
        raise SetupError(
            "LM Studio is installed but its executable could not be found; "
            "start it once by hand and press this again"
        )
    if not cli_path().exists():
        # The CLI is written on first launch, and every later step needs it.
        ctx.say("Starting LM Studio for the first time...")
        _launch_detached(exe)
        _wait_for(cli_path(), ctx, "LM Studio's CLI")
    ctx.say("Starting LM Studio as a background service...")
    _launch_detached(exe, "--run-as-service")
    ctx.run([str(cli_path()), "server", "start"])
    return "running"


def _launch_detached(exe: Path, *args: str) -> None:
    """Start the app and leave it running after this process exits."""
    flags = _NO_WINDOW
    if sys.platform == "win32":
        flags |= subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(
            [str(exe), *args],
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise SetupError(f"could not start {exe.name}: {exc}") from exc


def download_model(ctx: SetupContext) -> str:
    """Fetch the model, unless it is already on disk."""
    cli = cli_path()
    if not cli.exists():
        raise SetupError(f"LM Studio's CLI is not at {cli}")
    if _model_present(ctx, cli):
        return "already downloaded"
    ctx.say(f"Downloading {MODEL_REPO}@{MODEL_QUANT} (about 5 GB)...")
    ctx.run([str(cli), "get", f"{MODEL_REPO}@{MODEL_QUANT}", "--yes", "--gguf"])
    return "downloaded"


def _model_present(ctx: SetupContext, cli: Path) -> bool:
    try:
        listed = ctx.run([str(cli), "ls"])
    except SetupError:
        return False
    return any("qwen3.5-4b" in line.lower() for line in listed)


def configure_model(ctx: SetupContext) -> str:
    """Set the context length, and turn thinking off.

    Written as the app writes it: a list of keyed fields, ``load`` for what is
    fixed when the model is loaded and ``operation`` for what applies per
    request.
    """
    ctx.say(f"Setting the model to {CONTEXT_LENGTH:,} tokens with thinking off...")
    path = model_config_file()
    wanted = {
        "preset": "",
        "operation": {
            "fields": [
                {"key": "llm.prediction.reasoning.enableThinking", "value": False}
            ]
        },
        "load": {
            "fields": [{"key": "llm.load.contextLength", "value": CONTEXT_LENGTH}]
        },
    }
    try:
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) == wanted:
            return "already configured"
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        pass  # unreadable: overwrite it with something that works
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(wanted, indent=2), encoding="utf-8")
    except OSError as exc:
        raise SetupError(f"could not write {path}: {exc}") from exc
    return f"{CONTEXT_LENGTH:,} tokens, thinking off"


def point_app_at_it(ctx: SetupContext, settings) -> str:
    """Select the provider, model and context window in this application."""
    settings.provider = "lmstudio"
    settings.set_provider_model("lmstudio", "qwen3.5-4b")
    settings.save()
    # The window belongs to the model, not to a repository, so it goes in the
    # User tier -- the answer every repository without one of its own gets. A
    # repository that has said otherwise keeps what it said: silently
    # overwriting it would undo a deliberate choice.
    endpoints = dict(repo_config.defaults().model.endpoints)
    endpoints["lmstudio"] = ENDPOINT
    repo_config.set_user_values(
        model={"context_window": CONTEXT_LENGTH, "endpoints": endpoints}
    )
    return "provider, model and context window set"


@dataclass
class Step:
    key: str
    title: str
    run: Callable[[SetupContext], str]


@dataclass
class StepResult:
    key: str
    title: str
    note: str = ""
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem


def steps(settings) -> list[Step]:
    """Everything the button does, in the order it has to happen."""
    return [
        Step("install", "Install LM Studio", install_app),
        Step("configure", "Enable developer mode and the service", enable_service_and_developer_mode),
        Step("service", "Start it in the background", start_service),
        Step("model", f"Download {MODEL_REPO}@{MODEL_QUANT}", download_model),
        Step("model-config", "Configure the model", configure_model),
        Step("app", "Point this app at it", lambda c: point_app_at_it(c, settings)),
    ]


@dataclass
class SetupOutcome:
    results: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def summary(self) -> str:
        done = [r for r in self.results if r.ok]
        failed = [r for r in self.results if not r.ok]
        if not failed:
            return f"LM Studio is set up: {len(done)} step(s) completed."
        return (
            f"{len(done)} step(s) completed, {len(failed)} failed: "
            + "; ".join(f"{r.title} — {r.problem}" for r in failed)
        )


def run(settings, ctx: SetupContext) -> SetupOutcome:
    """Do the whole thing, stopping at the first step that cannot continue.

    Stopping is the point: downloading a model into an app that did not install
    would just fail again, further along, with a worse message.
    """
    outcome = SetupOutcome()
    for step in steps(settings):
        ctx.check()
        try:
            note = step.run(ctx)
        except Cancelled:
            raise
        except SetupError as exc:
            outcome.results.append(StepResult(step.key, step.title, problem=str(exc)))
            break
        except Exception as exc:  # a step must not take the window with it
            outcome.results.append(
                StepResult(step.key, step.title, problem=f"{type(exc).__name__}: {exc}")
            )
            break
        outcome.results.append(StepResult(step.key, step.title, note=note))
        ctx.say(f"{step.title}: {note}")
    return outcome


# ---- what is true already --------------------------------------------------------
def describe_state() -> list[str]:
    """What is already in place, for the confirmation dialog to show."""
    lines = []
    exe = app_executable()
    lines.append(f"LM Studio: {exe}" if exe else "LM Studio: not installed")
    lines.append(
        f"CLI: {cli_path()}" if cli_path().exists() else "CLI: not installed yet"
    )
    try:
        data = json.loads(settings_file().read_text(encoding="utf-8"))
        lines.append(
            f"developer mode: {bool(data.get('developerMode'))}, "
            f"background service: {bool(data.get('enableLocalService'))}"
        )
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        lines.append("developer mode: unknown (LM Studio has no settings file yet)")
    lines.append(
        f"model config: {'present' if model_config_file().exists() else 'not written yet'}"
    )
    return lines
