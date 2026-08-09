"""Which of the three files a given key belongs in, enforced rather than meant.

There are two homes for a persisted value and one question that decides which:

    Does it change what a run does?

Yes -- the diff mode, a branch pattern, how stale is stale -- and it is a
*setting*. It lives in the shared schema, which is `user_settings.json` and a
repository's `repo_settings.json` holding the same keys, so a project can ship
its answer and a person can override it.

No -- which audits are ticked, which report is on screen, which repository is
active -- and it is a *selection*. It changes what is on screen and nothing
else, so it belongs to the person looking at the screen and lives in
`static_user_settings.json`. Alongside it live the things that were never about
a repository at all: the account, the libraries, the workspace.

Getting this wrong is not a crash, which is why it needs a test. A setting kept
per user cannot be shared with a team. A selection kept per repository forks the
settings to Custom the first time a checkbox is ticked -- which is what used to
happen, and left a `custom_repo_settings.json` behind that nobody asked for.
"""

from __future__ import annotations

import dataclasses

import pytest

from git_assistant import repo_config
from git_assistant.config import Settings

#: Why each field of `Settings` is the user's rather than a repository's.
#:
#:   account    who to call and with what credentials. One person, one account,
#:              and nothing a repository could have an opinion about.
#:   workspace  which repositories this person has, and where to look for more.
#:   library    a collection to pick *from*. What is picked is a setting; the
#:              collection is not, and copying it into every repository's
#:              settings would be copying a library into a preference.
#:   selection  what is on screen. Changes nothing about what a run does.
#:
#: A field added without a line here fails the test below, on purpose: the
#: question has to be answered once by whoever adds it, rather than later by
#: whoever is working out why two files disagree.
CLASSIFIED = {
    "provider": "account",
    "provider_models": "account",
    "provider_temperatures": "account",
    "azure_api_version": "account",
    "selected_model": "account",
    "mcp_allow_writes": "account",
    "mcp_scope": "account",
    "repos": "workspace",
    "active_repo": "workspace",
    "recent_repos": "workspace",
    "scan_roots": "workspace",
    "watched_roots": "workspace",
    "theme": "selection",
    "settings_tiers": "selection",
    "audit_selected": "selection",
    "audit_last": "selection",
    "branch_pattern": "selection",
    "branch_user": "selection",
    "repo_templates": "selection",
    "default_template": "library",
}

KINDS = {"account", "workspace", "library", "selection"}


def test_every_field_the_user_keeps_has_been_classified():
    """A new field is a decision, and this is where it gets made."""
    fields = {f.name for f in dataclasses.fields(Settings)}

    missing = fields - set(CLASSIFIED)
    stale = set(CLASSIFIED) - fields

    assert not missing, f"classify these in tests/test_settings_split.py: {sorted(missing)}"
    assert not stale, f"no longer fields on Settings: {sorted(stale)}"


def test_nothing_here_is_a_setting():
    """The one kind that may not live in this file, because it is shareable."""
    assert set(CLASSIFIED.values()) <= KINDS


def test_no_setting_a_run_reads_is_also_kept_per_user():
    """The duplication the split removed, asserted so it cannot come back.

    Every name here was a field on `Settings` *and* a key in the settings a
    repository carries: two homes, one of them stale, and nothing on screen
    saying which had been read.
    """
    fields = {f.name for f in dataclasses.fields(Settings)}
    assert fields & set(repo_config._BOUND) == set()


# ---- and the other direction ---------------------------------------------------------
#: Every leaf of the shared schema, and what makes it a setting rather than a
#: selection: the thing it changes about a run.
CHANGES = {
    "branch.pattern": "the name a new branch is given",
    "branch.patterns": "the names offered for a new branch",
    "branch.user": "who {user} is in that name",
    "branch.push_sets_upstream": "whether a push sets the upstream",
    "fetch.shallow": "how much history a fetch asks for",
    "fetch.depth": "how much history a fetch asks for",
    "fetch.prune": "whether a fetch removes gone branches",
    "fetch.tags": "whether a fetch brings tags",
    "audit.narrate": "whether the provider is called to write the prose",
    "audit.fast": "whether the per-file history is scanned",
    "audit.large_file_mb": "which files an audit flags",
    "audit.history_limit": "how many runs are kept",
    "audit.stale.months": "when a branch counts as stale",
    "audit.stale.protect": "which branches are never proposed for deletion",
    "audit.stale.merged_only": "which branches may be proposed",
    "audit.stale.keep_unpushed": "which branches may be proposed",
    "commit.diff_mode": "which diff is described",
    "commit.subject_target": "what the model is asked for",
    "commit.subject_limit": "what the model is asked for",
    "commit.body_limit": "what the model is asked for",
    "commit.history_limit": "how many messages are kept",
    "commit.ignore_globs": "which files reach the model",
    "review.history_limit": "how many reviews are kept",
    "model.context_window": "how much is sent per call",
    "model.safety_margin": "how much is reserved for the answer",
    "model.parallel_calls": "how many calls run at once",
    "model.endpoints": "which server the request is sent to",
    "prompt.templates": "what the model is asked, for the ones that name one",
    "review.profiles": "which rules a review runs against",
    "tracing.enabled": "whether a trace is sent at all",
    "tracing.host": "where the trace is sent",
    "tracing.environment": "what the trace is filed under",
    "tracing.release": "what the trace is filed under",
    "tracing.send_prompts": "whether the prompt travels with the trace",
    "version": "which schema this file is",
}


def _leaves(data, prefix=""):
    """Every leaf key, dotted. An empty map is a leaf: it is a key with no
    entries yet, not a section with no keys."""
    out = set()
    for key, value in data.items():
        name = f"{prefix}{key}"
        nested = isinstance(value, dict) and value
        out |= _leaves(value, f"{name}.") if nested else {name}
    return out


def test_every_shared_key_changes_what_a_run_does():
    """Nothing in the shared schema is merely a selection.

    The reverse of the test above, and the reason `audit.selected` and
    `audit.last` are no longer there: they said what was on screen, so a
    repository could carry an opinion about what its reader was looking at.
    """
    shared = _leaves(repo_config.RepoSettings().to_dict())

    missing = shared - set(CHANGES)
    stale = set(CHANGES) - shared

    assert not missing, (
        "say what these change about a run, or move them to the user's own "
        f"settings: {sorted(missing)}"
    )
    assert not stale, f"no longer in the shared schema: {sorted(stale)}"


@pytest.mark.parametrize("name", ["selected", "last", "active", "shown", "chosen"])
def test_no_shared_key_is_named_like_a_selection(name):
    """A blunt instrument, and it would have caught the two that were there."""
    shared = _leaves(repo_config.RepoSettings().to_dict())
    assert not [key for key in shared if key.rsplit(".", 1)[-1] == name]


# ---- and every one of them explains itself in the file --------------------------------
# These files are meant to be opened and edited by hand. A key nobody can
# explain in the file is a key somebody has to go and read the source for, which
# is the thing the comments exist to avoid.
def test_every_shared_key_has_a_comment_in_the_file():
    shared = _leaves(repo_config.RepoSettings().to_dict())
    # A section header explains the section; its leaves explain themselves.
    sections = {key.rsplit(".", 1)[0] for key in shared if "." in key}

    missing = (shared | sections) - set(repo_config.FIELD_COMMENTS)

    assert not missing, (
        "add a line to repo_config.FIELD_COMMENTS for: " + str(sorted(missing))
    )


def test_no_comment_is_left_behind_for_a_key_that_is_gone():
    """A comment explaining a key nobody can set is worse than none."""
    shared = _leaves(repo_config.RepoSettings().to_dict())
    sections = {key.rsplit(".", 1)[0] for key in shared if "." in key}

    stale = set(repo_config.FIELD_COMMENTS) - (shared | sections)

    assert not stale, "no longer in the shared schema: " + str(sorted(stale))


def test_every_field_the_user_keeps_has_a_comment_in_the_file():
    from git_assistant import config as user_config

    fields = {f.name for f in dataclasses.fields(Settings)}

    missing = fields - set(user_config.FIELD_COMMENTS)
    stale = set(user_config.FIELD_COMMENTS) - fields

    assert not missing, "add a line to config.FIELD_COMMENTS for: " + str(sorted(missing))
    assert not stale, "no longer a field: " + str(sorted(stale))


def test_the_two_files_say_which_one_overrides_the_other():
    """The question somebody has when they find a file they did not expect."""
    from git_assistant import config as user_config

    assert user_config.HEADER == (
        "These settings are not overridden by repo_settings.json"
    )
    assert repo_config.HEADERS[repo_config.Tier.USER] == (
        "These settings are overridden by repo_settings.json"
    )
