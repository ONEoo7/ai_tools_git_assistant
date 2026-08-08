"""Per-repository settings: where they come from, and which one wins.

No git and no Qt. This module answers "what is configured" and nothing else,
which is most of why it can be tested like this.
"""

import json

import pytest

from git_assistant import repo_config


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Redirect the user config; patched where it is imported, as elsewhere."""
    monkeypatch.setattr(
        repo_config, "user_config_dir", lambda *a, **k: str(tmp_path / "config")
    )
    return tmp_path


def _write_defaults(data: dict) -> None:
    path = repo_config.defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_repo(repo, data) -> None:
    path = repo_config.repo_config_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        data if isinstance(data, str) else json.dumps(data), encoding="utf-8"
    )


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "demo").mkdir()
    return tmp_path / "demo"


# ---- the three tiers ------------------------------------------------------------
def test_a_repository_with_nothing_configured_still_has_settings(repo):
    settings = repo_config.resolve(repo)

    assert settings.branch.pattern == "{name}"
    assert settings.fetch.shallow is False
    assert settings.sources == []
    assert settings.problem == ""


def test_the_users_defaults_answer_for_a_repository_with_no_file(repo):
    _write_defaults({"branch": {"pattern": "wip/{name}"}, "fetch": {"prune": False}})

    settings = repo_config.resolve(repo)

    assert settings.branch.pattern == "wip/{name}"
    assert settings.fetch.prune is False
    assert settings.sources == [str(repo_config.defaults_path())]


def test_a_repository_outranks_the_defaults(repo):
    _write_defaults({"branch": {"pattern": "wip/{name}"}})
    _write_repo(repo, {"branch": {"pattern": "dev/rem/{user}/{name}"}})

    settings = repo_config.resolve(repo)

    assert settings.branch.pattern == "dev/rem/{user}/{name}"
    assert settings.sources[-1] == str(repo_config.repo_config_path(repo))


def test_a_repository_setting_one_key_keeps_the_rest(repo):
    """Otherwise changing one thing means copying every other thing beside it."""
    _write_defaults(
        {
            "branch": {"pattern": "wip/{name}", "user": "sg"},
            "fetch": {"shallow": True, "depth": 20, "tags": False},
        }
    )
    _write_repo(repo, {"fetch": {"depth": 5}})

    settings = repo_config.resolve(repo)

    assert settings.fetch.depth == 5  # the one that was set
    assert settings.fetch.shallow is True  # and everything that was not
    assert settings.fetch.tags is False
    assert settings.branch.pattern == "wip/{name}"
    assert settings.branch.user == "sg"


def test_settings_for_no_repository_at_all_are_the_defaults(repo):
    _write_defaults({"branch": {"pattern": "wip/{name}"}})
    _write_repo(repo, {"branch": {"pattern": "never/{name}"}})

    assert repo_config.defaults().branch.pattern == "wip/{name}"


# ---- files that are wrong ---------------------------------------------------------
def test_a_broken_repository_file_is_reported_not_obeyed_and_not_raised(repo):
    """A comma out of place must not stop the application, or pass unnoticed."""
    _write_defaults({"branch": {"pattern": "wip/{name}"}})
    _write_repo(repo, '{"branch": {"pattern": "dev/{name}",}}')

    settings = repo_config.resolve(repo)

    assert settings.branch.pattern == "wip/{name}"  # the defaults still stand
    assert "not valid JSON" in settings.problem
    assert repo_config.REPO_FILE in settings.problem


def test_a_broken_defaults_file_leaves_the_built_in_ones(repo):
    _write_defaults({"branch": {"pattern": "wip/{name}"}})
    repo_config.defaults_path().write_text("not json at all", encoding="utf-8")

    settings = repo_config.resolve(repo)

    assert settings.branch.pattern == "{name}"
    assert "not valid JSON" in settings.problem


def test_a_file_holding_a_list_is_not_a_settings_file(repo):
    _write_repo(repo, [1, 2, 3])
    assert "settings object" in repo_config.resolve(repo).problem


def test_a_value_of_the_wrong_type_falls_back_to_the_one_it_replaces(repo):
    """Hand-edited files say things like `"depth": true`."""
    _write_defaults({"fetch": {"depth": 7}})
    _write_repo(repo, {"fetch": {"depth": True, "prune": "yes"}, "branch": 5})

    settings = repo_config.resolve(repo)

    assert settings.fetch.depth == 7
    assert settings.fetch.prune is True
    assert settings.branch.pattern == "{name}"
    assert settings.problem == ""  # readable, just partly ignored


def test_keys_this_build_has_never_heard_of_are_left_alone(repo):
    """A file written by a newer build must not stop an older one."""
    _write_repo(repo, {"branch": {"pattern": "x/{name}", "colour": "pink"}, "moon": {}})

    assert repo_config.resolve(repo).branch.pattern == "x/{name}"


# ---- noticing a change ------------------------------------------------------------
def test_editing_a_file_is_noticed_without_a_restart(repo):
    _write_repo(repo, {"branch": {"pattern": "one/{name}"}})
    assert repo_config.resolve(repo).branch.pattern == "one/{name}"

    _write_repo(repo, {"branch": {"pattern": "two/{name}"}})

    assert repo_config.resolve(repo).branch.pattern == "two/{name}"


def test_deleting_a_repositorys_file_falls_back_to_the_defaults(repo):
    _write_defaults({"branch": {"pattern": "wip/{name}"}})
    _write_repo(repo, {"branch": {"pattern": "dev/{name}"}})
    assert repo_config.resolve(repo).branch.pattern == "dev/{name}"

    repo_config.repo_config_path(repo).unlink()

    assert repo_config.resolve(repo).branch.pattern == "wip/{name}"


def test_an_edit_of_the_same_length_is_still_noticed(repo):
    """Two writes can share a timestamp *and* a size.

    An (mtime, size) cache calls those two files the same file, and hands back
    a setting the user has already changed. Reading twice costs less than that.
    """
    _write_repo(repo, {"branch": {"pattern": "one/{name}"}})
    assert repo_config.resolve(repo).branch.pattern == "one/{name}"

    _write_repo(repo, {"branch": {"pattern": "two/{name}"}})

    assert repo_config.resolve(repo).branch.pattern == "two/{name}"


def test_two_repositories_do_not_share_an_answer(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    for path in (one, two):
        path.mkdir()
    _write_repo(one, {"branch": {"pattern": "one/{name}"}})
    _write_repo(two, {"branch": {"pattern": "two/{name}"}})

    assert repo_config.resolve(one).branch.pattern == "one/{name}"
    assert repo_config.resolve(two).branch.pattern == "two/{name}"
    assert repo_config.resolve(one).branch.pattern == "one/{name}"


# ---- the defaults file ------------------------------------------------------------
def test_the_defaults_file_is_written_so_it_can_be_found(repo):
    """A settings file that appears only once edited cannot be discovered."""
    assert repo_config.ensure_defaults() is True
    assert repo_config.defaults_path().is_file()

    written = json.loads(repo_config.defaults_path().read_text(encoding="utf-8"))
    assert written["version"] == repo_config.SCHEMA_VERSION
    assert written["branch"]["pattern"] == "{name}"
    assert written["fetch"]["shallow"] is False


def test_existing_defaults_are_never_overwritten(repo):
    _write_defaults({"branch": {"pattern": "mine/{name}"}})

    assert repo_config.ensure_defaults() is False
    assert repo_config.resolve(repo).branch.pattern == "mine/{name}"


def test_saved_defaults_come_back(repo):
    settings = repo_config.RepoSettings()
    settings.branch.pattern = "dev/rem/{user}/{name}"
    settings.fetch.shallow = True

    assert repo_config.save_defaults(settings) == ""

    back = repo_config.resolve(repo)
    assert back.branch.pattern == "dev/rem/{user}/{name}"
    assert back.fetch.shallow is True


def test_a_disk_that_refuses_reports_rather_than_raises(monkeypatch):
    monkeypatch.setattr(
        repo_config.Path,
        "mkdir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
    )
    assert "read-only" in repo_config.save_defaults(repo_config.RepoSettings())


# ---- building a branch name --------------------------------------------------------
def test_the_pattern_is_filled_in_with_the_user_and_the_name():
    rules = repo_config.BranchRules(pattern="dev/rem/{user}/{name}", user="stefanghitescu")
    assert rules.render("new-thing") == "dev/rem/stefanghitescu/new-thing"


def test_the_configured_user_beats_the_one_offered():
    rules = repo_config.BranchRules(pattern="{user}/{name}", user="configured")
    assert rules.render("x", user="from-git") == "configured/x"


def test_the_offered_user_is_used_when_none_is_configured():
    """Blank means "ask git", and the caller is the one that can."""
    rules = repo_config.BranchRules(pattern="{user}/{name}")
    assert rules.render("x", user="from-git") == "from-git/x"


def test_a_user_nobody_could_supply_does_not_leave_an_empty_piece():
    """`dev//x` is a name git refuses; `dev/x` is what was meant."""
    rules = repo_config.BranchRules(pattern="dev/{user}/{name}")
    assert rules.render("x") == "dev/x"


def test_a_name_is_made_safe_to_be_a_branch():
    rules = repo_config.BranchRules(pattern="{name}")
    assert rules.render("a name with spaces") == "a-name-with-spaces"
    assert rules.render("feat: colons?and*stars") == "feat-colons-and-stars"
    assert rules.render("  padded  ") == "padded"


def test_a_name_cannot_climb_out_of_the_pattern_it_is_put_in():
    """`..` is a ref git refuses, and `dev/rem/sg/../../main` is why."""
    rules = repo_config.BranchRules(pattern="dev/{name}")
    assert rules.render("../escape") == "dev/escape"
    assert ".." not in rules.render("a..b")


def test_case_is_kept_because_a_ticket_is_not_a_word():
    assert repo_config.BranchRules(pattern="{name}").render("JIRA-412") == "JIRA-412"


def test_a_name_with_nothing_usable_in_it_renders_as_nothing():
    """The window can then refuse it, rather than creating `dev/rem/sg`."""
    assert repo_config.BranchRules(pattern="{name}").render("///") == ""


def test_a_placeholder_nobody_recognises_is_left_where_it_is():
    """So a typo shows up in the name on offer instead of vanishing from it."""
    rules = repo_config.BranchRules(pattern="{tikcet}/{name}")
    assert rules.render("x") == "{tikcet}/x"


# ---- how deep a fetch goes ----------------------------------------------------------
def test_a_full_fetch_asks_for_no_depth_at_all():
    assert repo_config.FetchRules().effective_depth() is None


def test_a_shallow_fetch_asks_for_the_configured_depth():
    assert repo_config.FetchRules(shallow=True, depth=25).effective_depth() == 25


def test_the_depth_survives_being_turned_off_and_on_again():
    rules = repo_config.FetchRules(shallow=False, depth=25)
    assert rules.effective_depth() is None
    rules.shallow = True
    assert rules.effective_depth() == 25


def test_a_nonsense_depth_is_still_a_fetch_of_something():
    assert repo_config.FetchRules(shallow=True, depth=0).effective_depth() == 1
    assert repo_config.FetchRules(shallow=True, depth=-5).effective_depth() == 1


# ---- writing a repository's own file -------------------------------------------------
def test_a_created_file_holds_everything_this_repository_already_uses(repo):
    """Creating one must not change how the repository behaves, only pin it."""
    _write_defaults({"branch": {"pattern": "wip/{user}/{name}"}, "fetch": {"depth": 9}})
    before = repo_config.resolve(repo)

    assert repo_config.create_repo_config(repo) == ""

    after = repo_config.resolve(repo)
    assert after.branch.pattern == before.branch.pattern
    assert after.fetch.depth == before.fetch.depth
    # Every key, so the file can be read to find out what there is to set.
    written = json.loads(repo_config.read_repo_text(repo))
    assert set(written["branch"]) == {"pattern", "user", "push_sets_upstream"}
    assert set(written["fetch"]) == {"shallow", "depth", "prune", "tags"}


def test_creating_one_where_there_is_one_refuses_rather_than_replacing(repo):
    _write_repo(repo, {"branch": {"pattern": "mine/{name}"}})

    problem = repo_config.create_repo_config(repo)

    assert "already has" in problem
    assert repo_config.resolve(repo).branch.pattern == "mine/{name}"


def test_the_file_is_edited_as_the_text_it_is(repo):
    """What is edited has to be what was written, down to the key left out."""
    _write_repo(repo, {"fetch": {"depth": 3}})
    assert '"depth": 3' in repo_config.read_repo_text(repo)
    assert "branch" not in repo_config.read_repo_text(repo)


def test_saving_takes_effect_without_a_restart(repo):
    repo_config.create_repo_config(repo)

    problem = repo_config.write_repo_text(
        repo, json.dumps({"branch": {"pattern": "dev/rem/{user}/{name}"}})
    )

    assert problem == ""
    assert repo_config.resolve(repo).branch.pattern == "dev/rem/{user}/{name}"


def test_what_cannot_be_read_back_is_not_written(repo):
    """The run that finds it broken is a long way from the keystroke."""
    _write_repo(repo, {"branch": {"pattern": "good/{name}"}})

    problem = repo_config.write_repo_text(repo, '{"branch": {,}}')

    assert "valid JSON" in problem
    assert repo_config.resolve(repo).branch.pattern == "good/{name}"


def test_a_list_is_refused_as_a_settings_file(repo):
    assert "object" in repo_config.write_repo_text(repo, "[1, 2]")
    assert not repo_config.has_repo_config(repo)


def test_check_names_what_is_wrong_without_writing_anything():
    assert repo_config.check('{"branch": {}}') == ""
    assert "JSON" in repo_config.check("{oops}")
    assert "object" in repo_config.check("[]")


def test_reading_a_file_that_is_not_there_is_empty_not_an_error(repo):
    assert repo_config.read_repo_text(repo) == ""
    assert repo_config.has_repo_config(repo) is False


# ---- the defaults as a fallback to go back to ---------------------------------------
def test_a_reset_goes_back_to_the_defaults_and_not_to_what_is_there(repo):
    """The bug this pins: a reset built from `resolve(repo)` reads the file it
    is replacing, and faithfully reproduces whatever went wrong with it."""
    _write_defaults({"branch": {"pattern": "wip/{name}"}, "fetch": {"depth": 9}})
    _write_repo(repo, {"branch": {"pattern": "wrong/{name}"}, "fetch": {"depth": 999}})

    assert repo_config.reset_repo_config(repo) == ""

    back = repo_config.resolve(repo)
    assert back.branch.pattern == "wip/{name}"
    assert back.fetch.depth == 9


def test_a_reset_replaces_where_creating_refuses(repo):
    """"You already have one" is not an answer to "this one does not work"."""
    _write_repo(repo, {"branch": {"pattern": "wrong/{name}"}})

    assert "already has" in repo_config.create_repo_config(repo)
    assert repo_config.reset_repo_config(repo) == ""


def test_a_removed_file_follows_the_defaults_as_they_change(repo):
    """Which is the difference between removing it and resetting it."""
    _write_defaults({"branch": {"pattern": "first/{name}"}})
    repo_config.create_repo_config(repo)
    assert repo_config.reset_repo_config(repo) == ""

    _write_defaults({"branch": {"pattern": "second/{name}"}})
    assert repo_config.resolve(repo).branch.pattern == "first/{name}"  # pinned

    assert repo_config.remove_repo_config(repo) == ""

    assert repo_config.resolve(repo).branch.pattern == "second/{name}"
    assert not repo_config.has_repo_config(repo)


def test_removing_a_file_that_is_not_there_is_not_an_error(repo):
    assert repo_config.remove_repo_config(repo) == ""


def test_the_defaults_can_be_put_back_to_what_this_build_ships_with(repo):
    """The last thing standing when the file has been edited into nonsense."""
    repo_config.defaults_path().parent.mkdir(parents=True, exist_ok=True)
    repo_config.defaults_path().write_text("}{ not json", encoding="utf-8")
    assert "valid JSON" in repo_config.resolve(repo).problem

    assert repo_config.restore_defaults() == ""

    settings = repo_config.resolve(repo)
    assert settings.problem == ""
    assert settings.branch.pattern == repo_config.BranchRules().pattern


def test_restoring_cannot_fail_for_want_of_something_to_restore_from(tmp_path):
    """Every value below the defaults is a constant, not another file."""
    assert repo_config.restore_defaults() == ""
    assert repo_config.defaults_path().is_file()


def test_a_starter_file_holds_the_defaults_not_the_repositorys_own_values(repo):
    _write_defaults({"fetch": {"depth": 4}})
    _write_repo(repo, {"fetch": {"depth": 77}})

    assert '"depth": 4' in repo_config.starter_text()
    assert "77" not in repo_config.starter_text()
