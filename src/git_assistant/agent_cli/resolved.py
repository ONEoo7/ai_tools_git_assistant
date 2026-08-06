"""Which model last answered for an alias.

``claude`` takes ``--model sonnet`` and that is the right thing to send: the
alias tracks whichever model is current. But an alias is not an answer to "what
am I actually running", and the CLI has no command that says -- ``claude
models`` is not a subcommand, it is a prompt, and asking it costs a call.

What it does say is in the *result* of every completion: ``modelUsage`` is keyed
by the real model id. So the answer is free; it just arrives afterwards.

**And it is not a fixed mapping.** Measured on this machine, the same
``--model sonnet`` was served once by ``claude-sonnet-4-6`` and once by
``claude-haiku-4-5-20251001`` -- Claude Code routes per call. So what is kept
here is *the last model that answered*, and it is labelled that way. Showing
``sonnet (claude-haiku-4-5-20251001)`` unqualified would read as "sonnet means
haiku", which is not true and would be believed.

The usage table is where the truth lives: every call is recorded under the model
that actually served it, so a month's spending is not all filed under "sonnet".

A cache, not a setting: losing it costs a label and nothing else.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant.config import APP_NAME

CACHE_FILE = "agent_cli_models.json"
SCHEMA_VERSION = 1

_LOCK = threading.Lock()


def cache_path() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False)) / CACHE_FILE


def load() -> dict[str, dict[str, str]]:
    """``{cli: {alias: model_id}}``. Never raises; a broken file reads as empty."""
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for cli, aliases in (data.get("aliases") or {}).items():
        if isinstance(aliases, dict):
            out[str(cli)] = {
                str(k): str(v) for k, v in aliases.items() if str(v).strip()
            }
    return out


def get(cli: str, alias: str) -> str:
    return load().get(cli, {}).get(alias, "")


def remember(cli: str, alias: str, model_id: str) -> None:
    """Note that ``alias`` resolved to ``model_id``. Never raises.

    Called on the way back from a completion, so it must not be able to fail
    one: a cache that cannot be written is a label that does not appear.
    """
    alias, model_id = (alias or "").strip(), (model_id or "").strip()
    if not alias or not model_id or alias == model_id:
        return
    try:
        with _LOCK:
            known = load()
            if known.get(cli, {}).get(alias) == model_id:
                return  # nothing changed; do not rewrite the file every call
            known.setdefault(cli, {})[alias] = model_id
            _write(known)
    except OSError:
        return


def _write(known: dict[str, dict[str, str]]) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": SCHEMA_VERSION, "aliases": known}
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
