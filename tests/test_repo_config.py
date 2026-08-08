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


def test_a_repository_with_a_file_of_its_own_uses_it_by_default(repo):
    """What the merged design did for anyone who never thought about it."""
    _write_defaults({"branch": {"pattern": "wip/{name}"}})
    _write_repo(repo, {"branch": {"pattern": "dev/rem/{user}/{name}"}})

    settings = repo_config.resolve(repo)

    assert settings.tier is repo_config.Tier.REPO
    assert settings.branch.pattern == "dev/rem/{user}/{name}"
    assert settings.sources == [str(repo_config.repo_config_path(repo))]


def test_the_tier_in_force_is_the_whole_answer(repo):
    """Not a blend. What the file does not say comes from the built-ins, and
    the user tier has no say at all once the repo tier is in force."""
    _write_defaults(
        {
            "branch": {"pattern": "wip/{name}", "user": "sg"},
            "fetch": {"shallow": True, "depth": 20, "tags": False},
        }
    )
    _write_repo(repo, {"fetch": {"depth": 5}})

    settings = repo_config.resolve(repo)

    assert settings.tier is repo_config.Tier.REPO
    assert settings.fetch.depth == 5  # what the file says
    assert settings.fetch.shallow is False  # built-in, not the user tier's True
    assert settings.fetch.tags is True
    assert settings.branch.pattern == "{name}"
    assert settings.branch.user == ""


def test_settings_for_no_repository_at_all_are_the_user_tier(repo):
    _write_defaults({"branch": {"pattern": "wip/{name}"}})
    _write_repo(repo, {"branch": {"pattern": "never/{name}"}})

    assert repo_config.defaults().branch.pattern == "wip/{name}"


# ---- files that are wrong ---------------------------------------------------------
def test_a_broken_repository_file_is_reported_not_obeyed_and_not_raised(repo):
    """A comma out of place must not stop the application, or pass unnoticed."""
    _write_defaults({"branch": {"pattern": "wip/{name}"}})
    _write_repo(repo, '{"branch": {"pattern": "dev/{name}",}}')

    settings = repo_config.resolve(repo)

    assert settings.branch.pattern == "{name}"  # the built-ins still stand
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


def test_a_value_of_the_wrong_type_falls_back_to_the_built_in_one(repo):
    """Hand-edited files say things like `"depth": true`."""
    _write_repo(repo, {"fetch": {"depth": True, "prune": "yes"}, "branch": 5})

    settings = repo_config.resolve(repo)

    assert settings.fetch.depth == repo_config.FetchRules().depth
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


def test_deleting_a_repositorys_file_falls_back_to_the_user_tier(repo):
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

    assert repo_config.create(repo_config.Tier.REPO, repo) == ""

    after = repo_config.resolve(repo)
    assert after.branch.pattern == before.branch.pattern
    assert after.fetch.depth == before.fetch.depth
    # Every key, so the file can be read to find out what there is to set.
    written = json.loads(repo_config.read_text(repo_config.Tier.REPO, repo))
    assert set(written["branch"]) == {"pattern", "user", "push_sets_upstream"}
    assert set(written["fetch"]) == {"shallow", "depth", "prune", "tags"}


def test_creating_one_where_there_is_one_refuses_rather_than_replacing(repo):
    _write_repo(repo, {"branch": {"pattern": "mine/{name}"}})

    problem = repo_config.create(repo_config.Tier.REPO, repo)

    assert "already a" in problem
    assert repo_config.resolve(repo).branch.pattern == "mine/{name}"


def test_the_file_is_edited_as_the_text_it_is(repo):
    """What is edited has to be what was written, down to the key left out."""
    _write_repo(repo, {"fetch": {"depth": 3}})
    assert '"depth": 3' in repo_config.read_text(repo_config.Tier.REPO, repo)
    assert "branch" not in repo_config.read_text(repo_config.Tier.REPO, repo)


def test_saving_takes_effect_without_a_restart(repo):
    repo_config.create(repo_config.Tier.REPO, repo)

    problem = repo_config.write_text(
        repo_config.Tier.REPO, repo, json.dumps({"branch": {"pattern": "dev/rem/{user}/{name}"}})
    )

    assert problem == ""
    assert repo_config.resolve(repo).branch.pattern == "dev/rem/{user}/{name}"


def test_what_cannot_be_read_back_is_not_written(repo):
    """The run that finds it broken is a long way from the keystroke."""
    _write_repo(repo, {"branch": {"pattern": "good/{name}"}})

    problem = repo_config.write_text(repo_config.Tier.REPO, repo, '{"branch": {,}}')

    assert "valid JSON" in problem
    assert repo_config.resolve(repo).branch.pattern == "good/{name}"


def test_a_list_is_refused_as_a_settings_file(repo):
    assert "object" in repo_config.write_text(repo_config.Tier.REPO, repo, "[1, 2]")
    assert not repo_config.has_repo_config(repo)


def test_check_names_what_is_wrong_without_writing_anything():
    assert repo_config.check('{"branch": {}}') == ""
    assert "JSON" in repo_config.check("{oops}")
    assert "object" in repo_config.check("[]")


def test_reading_a_file_that_is_not_there_is_empty_not_an_error(repo):
    assert repo_config.read_text(repo_config.Tier.REPO, repo) == ""
    assert repo_config.has_repo_config(repo) is False


# ---- the defaults as a fallback to go back to ---------------------------------------
def test_a_reset_goes_back_to_the_defaults_and_not_to_what_is_there(repo):
    """The bug this pins: a reset built from `resolve(repo)` reads the file it
    is replacing, and faithfully reproduces whatever went wrong with it."""
    _write_defaults({"branch": {"pattern": "wip/{name}"}, "fetch": {"depth": 9}})
    _write_repo(repo, {"branch": {"pattern": "wrong/{name}"}, "fetch": {"depth": 999}})

    assert repo_config.reset(repo_config.Tier.REPO, repo) == ""

    back = repo_config.resolve(repo)
    assert back.branch.pattern == "wip/{name}"
    assert back.fetch.depth == 9


def test_a_reset_replaces_where_creating_refuses(repo):
    """"You already have one" is not an answer to "this one does not work"."""
    _write_repo(repo, {"branch": {"pattern": "wrong/{name}"}})

    assert "already a" in repo_config.create(repo_config.Tier.REPO, repo)
    assert repo_config.reset(repo_config.Tier.REPO, repo) == ""


def test_a_removed_file_follows_the_defaults_as_they_change(repo):
    """Which is the difference between removing it and resetting it."""
    _write_defaults({"branch": {"pattern": "first/{name}"}})
    repo_config.create(repo_config.Tier.REPO, repo)
    assert repo_config.reset(repo_config.Tier.REPO, repo) == ""

    _write_defaults({"branch": {"pattern": "second/{name}"}})
    assert repo_config.resolve(repo).branch.pattern == "first/{name}"  # pinned

    assert repo_config.remove(repo_config.Tier.REPO, repo) == ""

    assert repo_config.resolve(repo).branch.pattern == "second/{name}"
    assert not repo_config.has_repo_config(repo)


def test_removing_a_file_that_is_not_there_is_not_an_error(repo):
    assert repo_config.remove(repo_config.Tier.REPO, repo) == ""


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


# ---- which of the three is in force --------------------------------------------------
def _write_custom(repo, data) -> None:
    path = repo_config.custom_config_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_the_custom_tier_is_filed_under_a_readable_repository_key(tmp_path):
    """Two repositories called `api` are two directories, and the folder reads."""
    one, two = tmp_path / "a" / "api", tmp_path / "b" / "api"
    for path in (one, two):
        path.mkdir(parents=True)

    first, second = (repo_config.custom_dir(p) for p in (one, two))

    assert first != second
    assert first.name.startswith("api")
    assert second.name.startswith("api")
    assert repo_config.CUSTOM_DIR in first.parts


def test_nobody_having_chosen_a_repository_with_a_file_uses_it(repo):
    assert repo_config.effective_tier(repo) is repo_config.Tier.USER

    _write_repo(repo, {"branch": {"pattern": "x/{name}"}})

    assert repo_config.effective_tier(repo) is repo_config.Tier.REPO


def test_a_choice_beats_what_happens_to_be_there(repo):
    _write_repo(repo, {"branch": {"pattern": "repo/{name}"}})
    _write_defaults({"branch": {"pattern": "user/{name}"}})

    assert repo_config.resolve(repo, "user").branch.pattern == "user/{name}"
    assert repo_config.resolve(repo, "repo").branch.pattern == "repo/{name}"


def test_each_tier_answers_for_itself_and_no_other(repo):
    _write_defaults({"branch": {"pattern": "user/{name}"}})
    _write_repo(repo, {"branch": {"pattern": "repo/{name}"}})
    _write_custom(repo, {"branch": {"pattern": "custom/{name}"}})

    got = {
        tier.value: repo_config.resolve(repo, tier.value).branch.pattern
        for tier in repo_config.Tier
    }

    assert got == {
        "user": "user/{name}",
        "repo": "repo/{name}",
        "custom": "custom/{name}",
    }


def test_the_settings_say_which_tier_they_are(repo):
    """A value nobody can trace to a file is a value nobody trusts."""
    _write_custom(repo, {"fetch": {"depth": 3}})

    settings = repo_config.resolve(repo, "custom")

    assert settings.tier is repo_config.Tier.CUSTOM
    assert settings.sources == [str(repo_config.custom_config_path(repo))]


def test_a_tier_chosen_but_missing_says_so_rather_than_using_another(repo):
    """"The settings you picked are missing" and "they say nothing" differ."""
    _write_defaults({"branch": {"pattern": "user/{name}"}})

    settings = repo_config.resolve(repo, "custom")

    assert settings.branch.pattern == "{name}"  # built-in, not the user tier's
    assert repo_config.CUSTOM_FILE in settings.problem
    assert "not there" in settings.problem


def test_a_name_that_is_not_a_tier_is_read_as_no_choice(repo):
    """Hand-edited settings files say anything at all."""
    _write_repo(repo, {"branch": {"pattern": "repo/{name}"}})

    assert repo_config.tier_of("sideways") is None
    assert repo_config.effective_tier(repo, "sideways") is repo_config.Tier.REPO


def test_each_tier_is_created_read_and_removed_where_it_lives(repo):
    for tier in (repo_config.Tier.REPO, repo_config.Tier.CUSTOM):
        assert not repo_config.exists(tier, repo)
        assert repo_config.create(tier, repo) == ""
        assert repo_config.exists(tier, repo)
        assert '"pattern"' in repo_config.read_text(tier, repo)
        assert repo_config.remove(tier, repo) == ""
        assert not repo_config.exists(tier, repo)


def test_the_user_tier_cannot_be_removed(repo):
    """It is what everything falls back to; there is a restore for putting it right."""
    repo_config.ensure_defaults()

    problem = repo_config.remove(repo_config.Tier.USER, repo)

    assert "cannot be removed" in problem
    assert repo_config.defaults_path().is_file()


# ---- what the audits run on ----------------------------------------------------------
def test_the_stale_defaults_are_the_ones_the_audit_ships_with():
    """A mirrored dataclass that drifts is worse than no mirror at all."""
    from git_assistant.agents import branches

    mine = repo_config.StaleRules()

    assert mine.months == branches.DEFAULT_MONTHS
    assert mine.protect == list(branches.DEFAULT_PROTECTED)
    assert mine.merged_only is branches.StaleRules().merged_only
    assert mine.keep_unpushed is branches.StaleRules().keep_unpushed


def test_the_stale_rules_convert_to_what_the_audit_takes():
    from git_assistant.agents.branches import StaleRules as Branches

    converted = repo_config.StaleRules(months=9, protect=["only"]).as_branch_rules()

    assert isinstance(converted, Branches)
    assert (converted.months, converted.protect) == (9, ["only"])


def test_the_audit_settings_round_trip_through_a_file(repo):
    _write_repo(
        repo,
        {
            "audit": {
                "narrate": False,
                "fast": True,
                "large_file_mb": 42,
                "history_limit": 3,
                "selected": ["metrics", "size-audit"],
                "last": "metrics",
                "stale": {"months": 12, "protect": ["main"], "merged_only": False},
            }
        },
    )

    audit = repo_config.resolve(repo).audit

    assert audit.narrate is False and audit.fast is True
    assert (audit.large_file_mb, audit.history_limit) == (42, 3)
    assert audit.selected == ["metrics", "size-audit"]
    assert audit.last == "metrics"
    assert (audit.stale.months, audit.stale.merged_only) == (12, False)
    assert audit.stale.keep_unpushed is True  # not set, so the built-in


def test_a_selected_list_holding_nonsense_keeps_only_the_names(repo):
    _write_repo(repo, {"audit": {"selected": ["size-audit", 7, None]}})
    assert repo_config.resolve(repo).audit.selected == ["size-audit"]


# ---- carrying the old settings over --------------------------------------------------
def _legacy():
    from git_assistant.config import Settings

    settings = Settings(
        agents_narrate=False,
        agent_fast_mode=True,
        agent_large_file_mb=77,
        agent_history_limit=3,
        agent_selected_ids=["metrics"],
        agent_last_id="metrics",
        stale_branch_rules={"months": 18, "protect": ["main"]},
    )
    settings.save = lambda: None
    return settings


def test_settings_configured_before_this_build_are_carried_over(repo):
    """An upgrade must not silently reset what somebody configured."""
    settings = _legacy()

    assert repo_config.migrate_user_settings(settings) is True

    audit = repo_config.resolve(repo, "user").audit
    assert audit.narrate is False and audit.fast is True
    assert (audit.large_file_mb, audit.history_limit) == (77, 3)
    assert audit.selected == ["metrics"] and audit.last == "metrics"
    assert audit.stale.months == 18 and audit.stale.protect == ["main"]


def test_the_carry_over_happens_once_and_not_again(repo):
    settings = _legacy()
    repo_config.migrate_user_settings(settings)
    repo_config.write_text(repo_config.Tier.USER, "", '{"audit": {"narrate": true}}')

    assert repo_config.migrate_user_settings(settings) is False
    assert repo_config.resolve(repo, "user").audit.narrate is True


def test_the_old_fields_are_left_where_a_downgrade_can_read_them(repo):
    """Removing them would leave the previous build with nothing to fall back on."""
    settings = _legacy()

    repo_config.migrate_user_settings(settings)

    assert settings.agent_large_file_mb == 77
    assert settings.settings_migrated is True


# ---- one change, wherever it comes from ----------------------------------------------
def _live_settings():
    from git_assistant.config import Settings

    settings = Settings()
    settings.save = lambda: None
    return settings


def test_a_change_from_anywhere_lands_in_custom_and_switches_to_it(repo):
    """A tick box must not edit the file a team shares."""
    settings = _live_settings()
    _write_repo(repo, {"audit": {"narrate": True}})

    problem = repo_config.change(
        settings, str(repo), lambda data: data["audit"].update({"narrate": False})
    )

    assert problem == ""
    assert repo_config.resolve(repo, "repo").audit.narrate is True  # untouched
    assert repo_config.resolve(repo, "custom").audit.narrate is False
    assert settings.settings_tier(str(repo)) == "custom"


def test_a_change_while_custom_is_in_force_just_saves_it(repo):
    settings = _live_settings()
    repo_config.write_text(repo_config.Tier.CUSTOM, repo, '{"audit": {"fast": false}}')
    settings.set_settings_tier(str(repo), "custom")

    repo_config.change(
        settings, str(repo), lambda data: data["audit"].update({"fast": True})
    )

    assert repo_config.resolve(repo, "custom").audit.fast is True


def test_a_change_that_would_replace_custom_settings_is_refused_unasked(repo):
    """No answer means no: this is the only thing between a tick box and them."""
    settings = _live_settings()
    _write_repo(repo, {"audit": {"narrate": True}})
    repo_config.write_text(repo_config.Tier.CUSTOM, repo, '{"audit": {"fast": true}}')

    problem = repo_config.change(
        settings, str(repo), lambda data: data["audit"].update({"narrate": False})
    )

    assert "not replaced" in problem
    assert repo_config.resolve(repo, "custom").audit.fast is True


def test_a_change_replaces_custom_settings_when_that_is_agreed(repo):
    settings = _live_settings()
    _write_repo(repo, {"audit": {"narrate": True}})
    repo_config.write_text(repo_config.Tier.CUSTOM, repo, '{"audit": {"fast": true}}')
    asked = []

    problem = repo_config.change(
        settings,
        str(repo),
        lambda data: data["audit"].update({"narrate": False}),
        may_replace_custom=lambda before, after: asked.append((before, after)) or True,
    )

    assert problem == "" and len(asked) == 1
    assert repo_config.resolve(repo, "custom").audit.narrate is False


def test_a_change_keeps_a_file_saying_only_what_it_said(repo):
    """A file trimmed to the one key somebody cared about stays trimmed."""
    settings = _live_settings()
    _write_repo(repo, {"audit": {"narrate": False}})

    repo_config.change(
        settings, str(repo), lambda data: data["audit"].update({"fast": True})
    )

    written = json.loads(repo_config.read_text(repo_config.Tier.CUSTOM, repo))
    assert set(written["audit"]) == {"narrate", "fast"}


# ---- one repository's answers in front of the user's ---------------------------------
def _user_settings(**kw):
    from git_assistant.config import Settings

    settings = Settings(**kw)
    settings.save = lambda: None
    return settings


def test_a_run_reads_the_repositorys_answer_and_the_users_account(repo):
    """A run reads a mixture, and neither half should have to be threaded."""
    _write_repo(
        repo,
        {"commit": {"diff_mode": "working"}, "model": {"parallel_calls": 9}},
    )
    settings = _user_settings(
        provider="claude", provider_models={"claude": "opus"}
    )

    bound = repo_config.bind(settings, repo)

    assert bound.diff_mode == "working"  # the repository's
    assert bound.parallel_calls == 9
    assert bound.provider == "claude"  # the user's
    assert bound.active_model() == "opus"
    assert bound.tier is repo_config.Tier.REPO


def test_every_name_a_run_reads_is_answered(repo):
    """The deep consumers were left alone, so every name they use must work."""
    bound = repo_config.bind(_user_settings(), repo)

    for name in (
        "diff_mode",
        "ignore_globs",
        "commit_subject_target",
        "commit_subject_limit",
        "commit_body_limit",
        "commit_history_limit",
        "review_history_limit",
        "context_window",
        "safety_margin",
        "parallel_calls",
        "agents_narrate",
        "agent_fast_mode",
        "agent_large_file_mb",
        "agent_history_limit",
    ):
        assert getattr(bound, name) is not None, name


def test_the_bound_settings_cannot_be_written_through(repo):
    """Setting one would change a copy and persist nothing."""
    bound = repo_config.bind(_user_settings(), repo)

    with pytest.raises(AttributeError) as caught:
        bound.diff_mode = "working"

    assert "repo_config.change" in str(caught.value)


def test_two_repositories_bind_to_their_own_answers(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    for path in (one, two):
        path.mkdir()
    _write_repo(one, {"commit": {"diff_mode": "working"}})
    _write_repo(two, {"commit": {"diff_mode": "cached"}})
    settings = _user_settings()

    assert repo_config.bind(settings, one).diff_mode == "working"
    assert repo_config.bind(settings, two).diff_mode == "cached"


def test_the_commit_and_model_settings_round_trip(repo):
    _write_repo(
        repo,
        {
            "commit": {
                "diff_mode": "working",
                "subject_limit": 100,
                "body_limit": 5,
                "history_limit": 2,
                "ignore_globs": ["*.big"],
            },
            "review": {"history_limit": 7},
            "model": {"context_window": 8000, "safety_margin": 0.25},
        },
    )

    settings = repo_config.resolve(repo)

    assert settings.commit.diff_mode == "working"
    assert (settings.commit.subject_limit, settings.commit.body_limit) == (100, 5)
    assert settings.commit.ignore_globs == ["*.big"]
    assert settings.review.history_limit == 7
    assert (settings.model.context_window, settings.model.safety_margin) == (8000, 0.25)


def test_a_margin_written_as_a_whole_number_is_still_a_margin(repo):
    """JSON has one number type and a hand-edited file says `0`."""
    _write_repo(repo, {"model": {"safety_margin": 0}})
    assert repo_config.resolve(repo).model.safety_margin == 0.0


def test_a_margin_that_is_not_a_number_falls_back(repo):
    _write_repo(repo, {"model": {"safety_margin": "a tenth"}})
    assert repo_config.resolve(repo).model.safety_margin == (
        repo_config.ModelRules().safety_margin
    )


# ---- changing part of the user tier ------------------------------------------------
def test_setting_user_values_keeps_the_keys_it_was_not_given():
    repo_config.write_text(
        repo_config.Tier.USER,
        "",
        '{"commit": {"diff_mode": "working", "body_limit": 9}, "model": {}}',
    )

    repo_config.set_user_values(model={"context_window": 4096})

    written = repo_config.defaults()
    assert written.model.context_window == 4096
    assert written.commit.diff_mode == "working"
    assert written.commit.body_limit == 9


def test_setting_user_values_on_a_file_that_does_not_exist_yet():
    repo_config.set_user_values(model={"parallel_calls": 3})
    assert repo_config.defaults().model.parallel_calls == 3


def test_setting_user_values_over_a_mangled_section():
    """A hand-edited file with a string where a section belongs still takes it."""
    repo_config.write_text(repo_config.Tier.USER, "", '{"model": "fast"}')

    repo_config.set_user_values(model={"parallel_calls": 3})

    assert repo_config.defaults().model.parallel_calls == 3


def test_an_override_answers_for_this_run_and_is_written_nowhere(repo):
    _write_repo(repo, {"commit": {"diff_mode": "cached"}})

    bound = repo_config.bind(_user_settings(), repo, diff_mode="working")

    assert bound.diff_mode == "working"
    assert bound.ignore_globs == repo_config.CommitRules().ignore_globs  # untouched
    assert repo_config.resolve(repo).commit.diff_mode == "cached"  # not written
