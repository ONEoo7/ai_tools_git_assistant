"""Per-project prompt templates."""

from git_assistant.config import (
    DEFAULT_TEMPLATE_NAME,
    RepoEntry,
    Settings,
    Template,
)
from git_assistant.prompts import DEFAULT_TEMPLATE


def _settings():
    return Settings(
        repos=[RepoEntry("/x/a"), RepoEntry("/x/b")],
        templates=[Template("Work", "WORK BODY"), Template("Personal", "PERS BODY")],
    )


def test_template_names_lists_default_first():
    assert _settings().template_names() == [DEFAULT_TEMPLATE_NAME, "Work", "Personal"]


def test_template_text_resolves_named_and_default():
    s = _settings()
    assert s.template_text("Work") == "WORK BODY"
    assert s.template_text(DEFAULT_TEMPLATE_NAME) == DEFAULT_TEMPLATE
    assert s.template_text("") == DEFAULT_TEMPLATE
    assert s.template_text("missing") == DEFAULT_TEMPLATE  # never blows up


def test_each_repo_can_use_its_own_template():
    s = _settings()
    s.set_repo_template("/x/a", "Work")
    assert s.template_for_repo("/x/a") == "WORK BODY"
    assert s.template_for_repo("/x/b") == DEFAULT_TEMPLATE  # unassigned


def test_assigning_the_default_clears_the_override():
    s = _settings()
    s.set_repo_template("/x/a", "Work")
    s.set_repo_template("/x/a", DEFAULT_TEMPLATE_NAME)
    assert s.repos[0].template == ""
    assert s.template_for_repo("/x/a") == DEFAULT_TEMPLATE


def test_rename_repoints_repositories():
    s = _settings()
    s.set_repo_template("/x/a", "Work")
    s.rename_template("Work", "Day job")
    assert s.repos[0].template == "Day job"
    assert s.template_for_repo("/x/a") == "WORK BODY"


def test_delete_falls_back_to_default():
    s = _settings()
    s.set_repo_template("/x/a", "Work")
    s.remove_template("Work")
    assert s.repos[0].template == ""
    assert s.template_for_repo("/x/a") == DEFAULT_TEMPLATE
    assert "Work" not in s.template_names()


def test_templates_and_assignments_round_trip():
    s = _settings()
    s.set_repo_template("/x/b", "Personal")
    s.prompt_template = "CUSTOM DEFAULT"
    restored = Settings.from_dict(s.to_dict())
    assert [t.name for t in restored.templates] == ["Work", "Personal"]
    assert restored.repos[1].template == "Personal"
    assert restored.template_for_repo("/x/b") == "PERS BODY"
    assert restored.template_for_repo("/x/a") == "CUSTOM DEFAULT"


def test_old_config_without_templates_still_loads():
    """Settings written before templates existed must keep working."""
    legacy = {
        "repos": [{"path": "/x/a"}],
        "prompt_template": "LEGACY BODY",
    }
    s = Settings.from_dict(legacy)
    assert s.templates == []
    assert s.template_for_repo("/x/a") == "LEGACY BODY"
    assert s.template_names() == [DEFAULT_TEMPLATE_NAME]
