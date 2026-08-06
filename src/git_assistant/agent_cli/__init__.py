"""Agent CLIs -- Claude Code, Antigravity -- driven as inference backends.

Both are installed and logged in separately, so unlike every other provider
there is no API key here and no address: the app finds the program, runs it once
per completion, and reads its JSON.

**Both are experimental, for reasons measured rather than assumed.** A process
launch costs five to six seconds before any inference, which a per-file code
review pays once per file; and `agy` spends roughly 17,000 tokens of its own
prompt on every call with no flag to remove it. `docs/cli-providers.md` has the
measurements, and the same document says why the GitHub Copilot CLI is not here.

See `detect` for finding one and installing it -- particularly for why the PATH
has to be read out of the registry -- and `client` for what is turned off before
one of these is allowed near a repository.
"""

from __future__ import annotations

from git_assistant.agent_cli.client import (
    RECIPES,
    CliClient,
    CliError,
    recipe_for,
)
from git_assistant.agent_cli.detect import (
    INSTALL_COMMANDS,
    Found,
    child_env,
    install,
    install_command,
    locate,
    probe,
    search_path,
)

__all__ = [
    "INSTALL_COMMANDS",
    "RECIPES",
    "CliClient",
    "CliError",
    "Found",
    "child_env",
    "install",
    "install_command",
    "locate",
    "probe",
    "recipe_for",
    "search_path",
]
