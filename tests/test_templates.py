"""Commit-message templates, and which file each half of them lives in.

Three things, in three places, because they answer three questions.

**The default** is in `static_user_settings.json`. It is always offered and a
project cannot replace it — that is the point of it: a repository whose prompt
turns out to be wrong should still leave you something to fall back to without
editing a file the whole team shares.

**The named templates** are in the shared schema. A template decides what is
sent, so a project can ship its own — and a repository that ships any
**replaces** yours rather than adding to them, so what is on offer is the
default plus one coherent set.

**Which one a repository uses** is a selection, kept as a mapping in your own
settings.
"""

from git_assistant import repo_config
from git_assistant.config import (
    DEFAULT_TEMPLATE_NAME,
    RepoEntry,
    Settings,
    Template,
)
from git_assistant.prompts import DEFAULT_TEMPLATE, SHORT_TEMPLATE_NAME

MINE = [
    {"name": "Work", "text": "WORK BODY"},
    {"name": "Personal", "text": "PERS BODY"},
]
THEIRS = [{"name": "House", "text": "HOUSE STYLE"}]


def _settings(repos=("/x/a", "/x/b")):
    settings = Settings(repos=[RepoEntry(str(path)) for path in repos])
    settings.save = lambda: None
    return settings


def _mine(templates=None):
    repo_config.set_user_values(
        prompt={"templates": MINE if templates is None else templates}
    )


def _theirs(repo, templates=None):
    import json

    repo_config.write_text(
        repo_config.Tier.REPO,
        str(repo),
        json.dumps({"prompt": {"templates": THEIRS if templates is None else templates}}),
    )


# ---- the default -----------------------------------------------------------------
def test_the_default_is_offered_before_anything_is_configured():
    """With the one the user file ships beside it -- see below."""
    assert repo_config.bind(_settings()).template_names() == [
        DEFAULT_TEMPLATE_NAME,
        SHORT_TEMPLATE_NAME,
    ]


def test_the_default_lives_in_the_users_own_file():
    settings = _settings()
    assert settings.default_template == DEFAULT_TEMPLATE
    assert "default_template" in settings.to_dict()


def test_the_default_is_not_something_a_repository_can_carry():
    """It is the one a project cannot take away, so it is not in their file."""
    assert "template" not in repo_config.RepoSettings().to_dict()["prompt"]


def test_the_default_is_still_offered_when_a_repository_ships_its_own(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    _mine()
    _theirs(repo)

    names = repo_config.bind(_settings([repo]), repo).template_names()

    assert names[0] == DEFAULT_TEMPLATE_NAME
    assert repo_config.bind(_settings([repo]), repo).template_text(
        DEFAULT_TEMPLATE_NAME
    ) == DEFAULT_TEMPLATE


def test_editing_the_default_changes_it_for_every_repository():
    settings = _settings()
    settings.default_template = "MINE ALONE"

    assert repo_config.bind(settings).template_text(DEFAULT_TEMPLATE_NAME) == (
        "MINE ALONE"
    )


def test_a_default_edited_to_nothing_falls_back_to_the_built_in_one():
    settings = _settings()
    settings.default_template = ""

    assert repo_config.bind(settings).template_text("") == DEFAULT_TEMPLATE


# ---- yours, and theirs -------------------------------------------------------------
def test_the_user_file_ships_a_template_of_its_own():
    """So the file arrives saying what an entry looks like."""
    shipped = repo_config.RepoSettings().prompt.templates

    assert [one["name"] for one in shipped] == [SHORT_TEMPLATE_NAME]
    assert shipped[0]["text"] != DEFAULT_TEMPLATE, "a second copy teaches nothing"


def test_your_templates_are_offered_when_no_repository_ships_any():
    _mine()
    assert repo_config.bind(_settings()).template_names() == [
        DEFAULT_TEMPLATE_NAME,
        "Work",
        "Personal",
    ]


def test_a_repository_that_ships_templates_replaces_yours(tmp_path):
    """The rule: the default and the repository's, and nothing of yours."""
    repo = tmp_path / "demo"
    repo.mkdir()
    _mine()
    _theirs(repo)

    bound = repo_config.bind(_settings([repo]), repo)

    assert bound.template_names() == [DEFAULT_TEMPLATE_NAME, "House"]
    assert bound.template_text("House") == "HOUSE STYLE"


def test_a_repository_that_ships_none_leaves_yours_alone(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    _mine()
    repo_config.write_text(
        repo_config.Tier.REPO, str(repo), '{"fetch": {"depth": 9}}'
    )

    assert repo_config.bind(_settings([repo]), repo).template_names() == [
        DEFAULT_TEMPLATE_NAME,
        "Work",
        "Personal",
    ]


def test_an_empty_list_in_a_repository_is_not_a_set_of_templates(tmp_path):
    """`"templates": []` is a project that has not decided, not one that says none."""
    repo = tmp_path / "demo"
    repo.mkdir()
    _mine()
    _theirs(repo, templates=[])

    assert "Work" in repo_config.bind(_settings([repo]), repo).template_names()


def test_a_repository_replaces_yours_whichever_settings_are_in_force(tmp_path):
    """Not the tier in force.

    A project that checked a prompt in did so to be used, and choosing "User
    settings" to change a fetch depth is not a decision about that.
    """
    repo = tmp_path / "demo"
    repo.mkdir()
    _mine()
    _theirs(repo)
    settings = _settings([repo])
    settings.set_settings_tier(str(repo), "user")

    assert repo_config.bind(settings, repo).template_names() == [
        DEFAULT_TEMPLATE_NAME,
        "House",
    ]


def test_a_template_missing_its_text_is_left_out_rather_than_half_loaded():
    _mine([{"name": "Broken"}, MINE[0]])
    assert [one.name for one in repo_config.bind(_settings()).templates] == ["Work"]


# ---- which one a repository uses ----------------------------------------------------
def test_the_mapping_is_kept_by_repository_in_your_own_settings():
    settings = _settings()

    settings.set_repo_template("/x/a", "Work")

    assert settings.repo_templates == {
        __import__("git_assistant.config", fromlist=["repo_key"]).repo_key("/x/a"): "Work"
    }
    assert settings.repo_template("/x/b") == ""


def test_each_repository_gets_the_template_it_was_mapped_to():
    _mine()
    settings = _settings()
    settings.set_repo_template("/x/a", "Work")

    bound = repo_config.bind(settings)

    assert bound.template_for_repo("/x/a") == "WORK BODY"
    assert bound.template_for_repo("/x/b") == DEFAULT_TEMPLATE  # unmapped


def test_choosing_the_default_forgets_the_mapping_rather_than_storing_it():
    settings = _settings()
    settings.set_repo_template("/x/a", "Work")

    settings.set_repo_template("/x/a", DEFAULT_TEMPLATE_NAME)

    assert settings.repo_templates == {}
    assert repo_config.bind(settings).template_for_repo("/x/a") == DEFAULT_TEMPLATE


def test_the_mapping_round_trips_through_the_file():
    _mine()
    settings = _settings()
    settings.set_repo_template("/x/b", "Personal")

    restored = Settings.from_dict(settings.to_dict())

    assert repo_config.bind(restored).template_for_repo("/x/b") == "PERS BODY"


def test_a_mapping_to_a_template_that_is_gone_gets_the_default():
    """A repository that stopped shipping the one it was pointed at."""
    _mine()
    settings = _settings()
    settings.set_repo_template("/x/a", "Work")

    _mine([MINE[1]])

    assert repo_config.bind(settings).template_for_repo("/x/a") == DEFAULT_TEMPLATE


def test_a_mapping_written_by_an_older_build_is_carried_over():
    """It used to be a field on the repository entry."""
    from git_assistant.config import repo_key

    settings = Settings.from_dict(
        {"repos": [{"path": "/x/a", "template": "Work"}]}
    )

    assert settings.repo_templates == {repo_key("/x/a"): "Work"}
    assert settings.repo_template("/x/a") == "Work"


def test_a_default_written_by_an_older_build_is_carried_over():
    """It used to be `prompt_template` in the one settings file there was."""
    settings = Settings.from_dict({"prompt_template": "LEGACY BODY"})

    assert settings.default_template == "LEGACY BODY"
    assert repo_config.bind(settings).template_text("") == "LEGACY BODY"


# ---- keeping the pointers honest -----------------------------------------------------
def test_a_rename_repoints_the_repositories_that_named_it():
    """The library and the pointers are in different files, so they follow by hand."""
    _mine()
    settings = _settings()
    settings.set_repo_template("/x/a", "Work")

    repo_config.save_user_templates([{"name": "Day job", "text": "WORK BODY"}, MINE[1]])
    settings.repoint_template("Work", "Day job")

    assert settings.repo_template("/x/a") == "Day job"
    assert repo_config.bind(settings).template_for_repo("/x/a") == "WORK BODY"


def test_a_removal_forgets_the_mapping_rather_than_leaving_it_dangling():
    _mine()
    settings = _settings()
    settings.set_repo_template("/x/a", "Work")

    repo_config.save_user_templates([MINE[1]])
    settings.repoint_template("Work", "")

    assert settings.repo_templates == {}
    assert repo_config.bind(settings).template_for_repo("/x/a") == DEFAULT_TEMPLATE


def test_the_library_survives_being_written_as_objects():
    """The window holds `Template`s; the file holds dicts."""
    repo_config.save_user_templates([Template("Work", "WORK BODY")])
    assert repo_config.user_templates() == [{"name": "Work", "text": "WORK BODY"}]
