"""Per-project prompt templates.

The library is a *setting*: a template decides what is sent to the model, so a
project whose commits follow a house style can ship the prompt that produces
one. Which of them a repository uses is a *selection* and stays with the user.
So the two halves live in different files, and every test here says which.
"""

from git_assistant import repo_config
from git_assistant.config import (
    DEFAULT_TEMPLATE_NAME,
    RepoEntry,
    Settings,
    Template,
)
from git_assistant.prompts import DEFAULT_TEMPLATE

LIBRARY = [
    {"name": "Work", "text": "WORK BODY"},
    {"name": "Personal", "text": "PERS BODY"},
]


def _bound(templates=None, template="", repos=("/x/a", "/x/b")):
    """Settings with a template library in the user tier, as a run reads them."""
    repo_config.set_user_values(
        prompt={
            "templates": LIBRARY if templates is None else templates,
            "template": template,
        }
    )
    settings = Settings(repos=[RepoEntry(path) for path in repos])
    settings.save = lambda: None
    return repo_config.bind(settings)


def test_template_names_lists_default_first():
    assert _bound().template_names() == [DEFAULT_TEMPLATE_NAME, "Work", "Personal"]


def test_template_text_resolves_named_and_default():
    bound = _bound()
    assert bound.template_text("Work") == "WORK BODY"
    assert bound.template_text(DEFAULT_TEMPLATE_NAME) == DEFAULT_TEMPLATE
    assert bound.template_text("") == DEFAULT_TEMPLATE
    assert bound.template_text("missing") == DEFAULT_TEMPLATE  # never blows up


def test_each_repo_can_use_its_own_template():
    bound = _bound()
    bound._settings.set_repo_template("/x/a", "Work")

    assert bound.template_for_repo("/x/a") == "WORK BODY"
    assert bound.template_for_repo("/x/b") == DEFAULT_TEMPLATE  # unassigned


def test_assigning_the_default_clears_the_override():
    bound = _bound()
    settings = bound._settings

    settings.set_repo_template("/x/a", "Work")
    settings.set_repo_template("/x/a", DEFAULT_TEMPLATE_NAME)

    assert settings.repos[0].template == ""
    assert bound.template_for_repo("/x/a") == DEFAULT_TEMPLATE


def test_a_rename_repoints_the_repositories_that_named_it():
    """The library moved; the pointers into it did not, so they follow by hand."""
    bound = _bound()
    settings = bound._settings
    settings.set_repo_template("/x/a", "Work")

    repo_config.save_user_templates(
        [{"name": "Day job", "text": "WORK BODY"}, LIBRARY[1]]
    )
    settings.repoint_template("Work", "Day job")

    assert settings.repos[0].template == "Day job"
    assert repo_config.bind(settings).template_for_repo("/x/a") == "WORK BODY"


def test_a_removal_falls_back_to_the_default():
    bound = _bound()
    settings = bound._settings
    settings.set_repo_template("/x/a", "Work")

    repo_config.save_user_templates([LIBRARY[1]])
    settings.repoint_template("Work", "")

    after = repo_config.bind(settings)
    assert settings.repos[0].template == ""
    assert after.template_for_repo("/x/a") == DEFAULT_TEMPLATE
    assert "Work" not in after.template_names()


def test_a_repository_pointing_at_a_template_that_is_gone_gets_the_default():
    """Nothing repointed it -- a file edited by hand, or another machine."""
    bound = _bound()
    bound._settings.set_repo_template("/x/a", "Work")

    repo_config.save_user_templates([LIBRARY[1]])

    assert repo_config.bind(bound._settings).template_for_repo("/x/a") == (
        DEFAULT_TEMPLATE
    )


def test_the_library_round_trips_through_the_settings_file():
    _bound(template="CUSTOM DEFAULT")

    written = repo_config.defaults().prompt

    assert [one["name"] for one in written.templates] == ["Work", "Personal"]
    assert written.template == "CUSTOM DEFAULT"


def test_the_assignment_round_trips_through_the_users_own_file():
    bound = _bound()
    bound._settings.set_repo_template("/x/b", "Personal")

    restored = Settings.from_dict(bound._settings.to_dict())

    assert restored.repos[1].template == "Personal"
    assert repo_config.bind(restored).template_for_repo("/x/b") == "PERS BODY"


def test_a_repository_can_ship_its_own_template(tmp_path):
    """The point of the library being a setting rather than a preference."""
    repo = tmp_path / "demo"
    repo.mkdir()
    repo_config.write_text(
        repo_config.Tier.REPO,
        str(repo),
        '{"prompt": {"template": "HOUSE STYLE"}}',
    )
    settings = Settings(repos=[RepoEntry(str(repo))])
    settings.save = lambda: None

    assert repo_config.bind(settings, repo).template_for_repo(str(repo)) == (
        "HOUSE STYLE"
    )


def test_settings_with_nothing_configured_still_have_a_template():
    settings = Settings(repos=[RepoEntry("/x/a")])
    settings.save = lambda: None

    bound = repo_config.bind(settings)

    assert bound.template_for_repo("/x/a") == DEFAULT_TEMPLATE
    assert bound.template_names() == [DEFAULT_TEMPLATE_NAME]


def test_a_template_missing_its_text_is_left_out_rather_than_half_loaded():
    bound = _bound(templates=[{"name": "Broken"}, LIBRARY[0]])
    assert [one.name for one in bound.templates] == ["Work"]


def test_the_library_survives_being_written_as_objects():
    """The window holds `Template`s; the file holds dicts."""
    repo_config.save_user_templates([Template("Work", "WORK BODY")])
    assert repo_config.user_templates() == [{"name": "Work", "text": "WORK BODY"}]
