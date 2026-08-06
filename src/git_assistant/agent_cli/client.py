"""Driving an agent CLI as if it were a chat endpoint.

These programs are agents: they read files, run commands and edit code. Used as
a backend they must do none of that, so every recipe below turns off whatever it
can and each one runs in an **empty temporary directory** rather than in the
repository. ``agy`` in particular offers no way to disable its tools, and a
workspace it cannot see is the only fence left.

Two shapes of overhead worth remembering, both measured (docs/cli-providers.md):

- a process launch per completion, five to six seconds before any inference;
- ``agy`` prepends about 17,000 tokens of its own prompt to every call, and
  offers no flag to replace it. ``claude`` does, through ``--system-prompt``,
  which is why that flag is not optional here.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from dataclasses import dataclass

from git_assistant import usage
from git_assistant.agent_cli import detect, resolved
from git_assistant.llm import LLMError, ModelInfo

#: A completion may legitimately take minutes; the app's own Cancel is what
#: stops it early. Past this the process is killed and reported.
CHAT_TIMEOUT = 900.0

#: What `context_length_for` answers with. These CLIs cannot be asked before a
#: call, and the diff splitter needs a number *first* -- so this is deliberately
#: conservative, and the Context window setting overrides it as it does for
#: every other provider.
ASSUMED_CONTEXT = 200_000
#: `agy` spends this much of it on its own prompt before ours starts. Subtracted
#: rather than ignored: a budget that does not know about it plans a prompt that
#: will not fit.
AGY_OVERHEAD = 18_000


class CliError(LLMError):
    """The CLI could not be run, or answered with something unusable."""


@dataclass(frozen=True)
class Answer:
    """One completion, as the CLI reported it."""

    reply: str
    input_tokens: int = 0
    output_tokens: int = 0
    #: The model that actually served it. `claude` is asked for an alias and
    #: names the real one in its result; `agy` is asked by full id and returns
    #: nothing extra, so this is empty there and nothing is inferred.
    model: str = ""


@dataclass(frozen=True)
class Recipe:
    """How one CLI is asked for a completion, and how its answer is read."""

    name: str
    #: Model ids to offer when the CLI cannot be asked for a list.
    models: tuple[str, ...] = ()
    #: A subcommand that lists models, one per line, without spending anything.
    list_args: tuple[str, ...] = ()
    #: True when the CLI takes a real system prompt. When False the system text
    #: is folded into the user prompt, which is a worse prompt and is said so.
    system_prompt: bool = True
    context: int = ASSUMED_CONTEXT
    overhead: int = 0

    def args(self, exe: str, model: str, system: str, user: str) -> list[str]:
        raise NotImplementedError

    def read(self, stdout: str) -> "Answer":
        """What the CLI printed, as something the client can use."""
        raise NotImplementedError


@dataclass(frozen=True)
class ClaudeRecipe(Recipe):
    name: str = "claude"
    models: tuple[str, ...] = ("sonnet", "opus", "haiku")
    system_prompt: bool = True
    context: int = 200_000

    def args(self, exe: str, model: str, system: str, user: str) -> list[str]:
        return [
            exe,
            "-p",
            user,
            # Replaces Claude Code's own harness prompt rather than adding to
            # it: measured at 3,408 cached tokens with the default and 112 with
            # this, a thirty-fold difference on every call. Never omit it.
            "--system-prompt",
            system,
            "--model",
            model,
            # No tools. This is a backend, not an agent with a repository.
            "--tools",
            "",
            "--output-format",
            "json",
            "--disable-slash-commands",
            # Neither the user's settings nor a project's may change what this
            # sends: the prompt is the app's, and a CLAUDE.md that redirected it
            # would be invisible from here.
            "--setting-sources",
            "",
            "--no-session-persistence",
        ]

    def read(self, stdout: str) -> Answer:
        payload = _json(stdout, self.name)
        if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
            raise CliError(f"claude reported: {payload.get('result') or payload}")
        counted = payload.get("usage") or {}
        # Cache creation and cache reads are input the account paid for; the
        # usage pane counts what was spent, not what was novel.
        went_in = sum(
            int(counted.get(field) or 0)
            for field in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        )
        # `modelUsage` is keyed by the real model id, which is the only place
        # the CLI says what an alias resolved to.
        served = payload.get("modelUsage")
        return Answer(
            reply=str(payload.get("result") or "").strip(),
            input_tokens=went_in,
            output_tokens=int(counted.get("output_tokens") or 0),
            model=next(iter(served), "") if isinstance(served, dict) else "",
        )


@dataclass(frozen=True)
class AgyRecipe(Recipe):
    name: str = "agy"
    list_args: tuple[str, ...] = ("models",)
    #: No `--system-prompt` of any kind, so the two halves are folded together.
    system_prompt: bool = False
    context: int = 200_000
    overhead: int = AGY_OVERHEAD

    def args(self, exe: str, model: str, system: str, user: str) -> list[str]:
        return [
            exe,
            "-p",
            user,
            "--model",
            model,
            "--output-format",
            "json",
            "--disable-slash-commands",
        ]

    def read(self, stdout: str) -> Answer:
        payload = _json(stdout, self.name)
        if str(payload.get("status") or "").upper() not in ("SUCCESS", ""):
            raise CliError(f"agy reported: {payload.get('status')}")
        counted = payload.get("usage") or {}
        return Answer(
            reply=str(payload.get("response") or "").strip(),
            input_tokens=int(counted.get("input_tokens") or 0),
            output_tokens=int(counted.get("output_tokens") or 0),
        )


RECIPES: dict[str, Recipe] = {"claude": ClaudeRecipe(), "agy": AgyRecipe()}


def recipe_for(name: str) -> Recipe:
    found = RECIPES.get(name)
    if found is None:
        raise CliError(f"no recipe for the '{name}' CLI")
    return found


def _json(stdout: str, name: str) -> dict:
    """The JSON object in what the CLI printed.

    Tolerant about leading noise -- a warning line before the payload is common
    and is not a failure -- and refuses anything it cannot read rather than
    returning an empty reply, which would look like a model with nothing to say.
    """
    text = (stdout or "").strip()
    if not text:
        raise CliError(f"{name} printed nothing at all")
    start = text.find("{")
    if start < 0:
        raise CliError(f"{name} printed no JSON: {text[:200]}")
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise CliError(f"{name} printed JSON this build cannot read: {exc}") from exc
    if not isinstance(payload, dict):
        raise CliError(f"{name} printed JSON that is not an object")
    return payload


class CliClient:
    """A chat client backed by a locally installed agent CLI."""

    def __init__(
        self,
        name: str,
        *,
        provider_key: str = "",
        feature: str = "",
        timeout: float = CHAT_TIMEOUT,
    ) -> None:
        self.recipe = recipe_for(name)
        self.name = name
        self.provider_key = provider_key or name
        self.feature = feature
        self.timeout = timeout
        #: Every process this client currently has in flight, so Cancel stops
        #: all of them. A *set*, not a single handle: one client is shared by
        #: every thread of a fan-out, and a single slot meant one thread's
        #: `finally` cleared the handle another was still reading -- an
        #: AttributeError in the middle of a run, reproduced with four threads.
        #: CLI providers are capped to one call at a time (see
        #: providers.Provider.max_parallel), and this holds regardless.
        self._running: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self._cancelled = False
        # Neither CLI takes one, so this exists only for the tracer to record
        # honestly; see git_assistant.tracing.client.
        self.temperature = None

    # ---- the contract --------------------------------------------------------
    def chat(self, model, system, user, max_tokens, temperature=None) -> str:
        exe = self._exe()
        prompt = user if self.recipe.system_prompt else _folded(system, user)
        asked = model or self._default_model()
        args = self.recipe.args(exe, asked, system, prompt)
        answer = self.recipe.read(self._run(args))
        if not answer.reply:
            # An empty reply from an agent CLI usually means it decided to do
            # something instead of answering. Saying so beats handing back "".
            raise CliError(
                f"{self.name} returned no text. It may have been waiting for a "
                "permission it could not ask for."
            )
        # An alias is what should be *sent* -- it tracks whichever model is
        # current -- but the usage table and Langfuse should name the one that
        # answered, or "sonnet" is all anyone ever sees of a month's spending.
        resolved.remember(self.name, asked, answer.model)
        usage.record(
            self.provider_key,
            answer.model or asked,
            answer.input_tokens,
            answer.output_tokens,
            feature=self.feature,
        )
        return answer.reply

    def list_models(self) -> list[ModelInfo]:
        """What this login can reach.

        ``agy models`` answers offline and instantly. ``claude`` has no such
        command -- ``claude models`` is a *prompt*, not a subcommand -- so its
        aliases are listed, which is what its own ``--model`` flag documents,
        each labelled with whatever it last resolved to.
        """
        if not self.recipe.list_args:
            return [ModelInfo(id=name, note=_last_served(self.name, name))
                    for name in self.recipe.models]
        listed = self._run([self._exe(), *self.recipe.list_args], want_json=False)
        names = [line.strip() for line in listed.splitlines() if line.strip()]
        if not names:
            raise CliError(f"{self.name} listed no models")
        return [ModelInfo(id=name) for name in names]

    def context_length_for(self, model_id: str) -> int | None:
        """What to plan against, minus whatever the CLI spends on itself.

        These CLIs cannot be asked before a call, and the splitter needs the
        number first. So this is an assumption, and the Context window setting
        overrides it exactly as it does for every other provider.
        """
        return max(1024, self.recipe.context - self.recipe.overhead)

    def ping(self) -> list[ModelInfo]:
        found = detect.probe(self.name)
        if not found.installed:
            raise CliError(found.describe())
        if found.problem:
            raise CliError(found.problem)
        return self.list_models()

    # ---- running it -----------------------------------------------------------
    def cancel(self) -> None:
        """Stop every completion in flight, killing the CLIs' children with them."""
        self._cancelled = True
        with self._lock:
            running = list(self._running)
        for process in running:
            try:
                process.kill()  # the process group goes with it; see _popen_flags
            except OSError:
                pass

    def _exe(self) -> str:
        path = detect.locate(self.name)
        if not path:
            raise CliError(
                f"The {self.name} CLI is not installed, or not on PATH. Install "
                "it from the Connection & Model tab."
            )
        return path

    def _default_model(self) -> str:
        return self.recipe.models[0] if self.recipe.models else ""

    def _run(self, args: list[str], want_json: bool = True) -> str:
        if self._cancelled:
            raise CliError("Cancelled.")
        # An empty directory, not the repository: these are workspace-aware
        # agents, and `agy` cannot be told to leave a workspace alone.
        #
        # `ignore_cleanup_errors` is not tidiness. These CLIs leave the
        # directory open for a moment after they exit -- and on Windows that is
        # a WinError 32 raised from `__exit__`, *after* a perfectly good answer
        # has been read, which would throw the answer away. A stray empty temp
        # directory is a far cheaper problem than a lost commit message.
        with tempfile.TemporaryDirectory(
            prefix="git-assistant-cli-", ignore_cleanup_errors=True
        ) as empty:
            process = None
            try:
                process = subprocess.Popen(
                    args,
                    cwd=empty,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=detect.child_env(),
                    **_popen_flags(),
                )
                with self._lock:
                    self._running.add(process)
                # Read from the local handle, never from `self._running`: a
                # sibling thread's `finally` must not be able to take it away.
                out, err = process.communicate(timeout=self.timeout)
                code = process.returncode
            except subprocess.TimeoutExpired as exc:
                if process is not None:
                    process.kill()
                raise CliError(
                    f"{self.name} did not answer within {self.timeout:.0f}s."
                ) from exc
            except OSError as exc:
                raise CliError(f"{self.name} could not be run: {exc}") from exc
            finally:
                if process is not None:
                    with self._lock:
                        self._running.discard(process)

        if self._cancelled:
            raise CliError("Cancelled.")
        if code != 0 and not (want_json and (out or "").strip()):
            # Copilot's failure mode exactly: exit 1, nothing on either stream.
            # Say that, rather than "unexpected response shape".
            detail = (err or out or "").strip().splitlines()
            raise CliError(
                f"{self.name} exited with code {code} and "
                + (f"said: {detail[-1][:200]}" if detail else "printed nothing.")
            )
        return out or ""


def _last_served(cli: str, alias: str) -> str:
    """"last: <model>", or nothing when the alias has never been used.

    *Last*, not *is*: the same alias has been observed served by two different
    models, so stating an equivalence would be stating something false.
    """
    served = resolved.get(cli, alias)
    return f"last: {served}" if served else ""


def _folded(system: str, user: str) -> str:
    """One prompt, for a CLI with nowhere to put a system prompt.

    Labelled rather than merely concatenated: the model has to be able to tell
    the instructions from the material, and a bare join leaves it guessing.
    """
    if not system.strip():
        return user
    return f"{system.strip()}\n\n---\n\n{user}"


def _popen_flags() -> dict:
    """Windows: no console flash, and a killable process group."""
    import sys

    if sys.platform != "win32":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    return {"creationflags": flags}
