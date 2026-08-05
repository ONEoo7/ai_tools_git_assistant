from git_assistant.config import RepoEntry, Settings, build_repo_tree


def _settings(*paths, active=""):
    return Settings(repos=[RepoEntry(p) for p in paths], active_repo=active)


def _p(*parts):
    """A path under a common root, in a form both platforms normalize."""
    return "/".join(("/x", *parts))


def test_scan_roots_and_owner_roundtrip():
    s = Settings(
        repos=[RepoEntry("/a/repo", owner="ONEoo7")],
        scan_roots=["/a", "/b"],
        watched_roots=["/a"],
    )
    s2 = Settings.from_dict(s.to_dict())
    assert s2.scan_roots == ["/a", "/b"]
    assert s2.watched_roots == ["/a"]
    assert s2.repos[0].owner == "ONEoo7"


def test_display_uses_owner_prefix():
    assert RepoEntry("/x/ai_tools", owner="ONEoo7").display() == "ONEoo7\\ai_tools"


def test_display_without_owner_is_basename():
    assert RepoEntry("/x/ai_tools").display() == "ai_tools"


def test_display_label_overrides_owner():
    assert RepoEntry("/x/ai_tools", label="Work", owner="ONEoo7").display() == "Work"


def test_ordered_repos_active_first():
    s = _settings("/a", "/b", "/c", active="/c")
    assert [r.path for r in s.ordered_repos()] == ["/c", "/a", "/b"]


def test_ordered_repos_recent_after_active():
    s = _settings("/a", "/b", "/c", active="/a")
    s.recent_repos = ["/c"]
    assert [r.path for r in s.ordered_repos()] == ["/a", "/c", "/b"]


def test_mark_recent_moves_to_front():
    s = _settings("/a", "/b", "/c")
    s.mark_recent("/b")
    s.mark_recent("/c")
    assert s.recent_repos == ["/c", "/b"]


def test_mark_recent_ignores_unknown_and_removed():
    s = _settings("/a", "/b")
    s.mark_recent("/x")  # not a known repo
    assert s.recent_repos == []
    s.mark_recent("/a")
    s.repos = [RepoEntry("/b")]  # /a removed
    s.mark_recent("/b")  # stale /a should be dropped from recents
    assert s.recent_repos == ["/b"]


def test_ordered_repos_dedups_and_keeps_only_known():
    s = _settings("/a", "/b")
    s.recent_repos = ["/gone", "/b", "/b"]
    assert [r.path for r in s.ordered_repos()] == ["/b", "/a"]


# ---- nesting submodules under the repository that contains them -------------
def _paths(nodes):
    """(path, depth) for every node in the tree, in display order."""
    return [(e.path, d) for n in nodes for e, d in n.walk()]


def test_build_repo_tree_nests_submodules():
    nodes = build_repo_tree(
        [RepoEntry(p) for p in (_p("a"), _p("a", "libs", "sub"), _p("b"))]
    )
    assert _paths(nodes) == [
        (_p("a"), 0),
        (_p("a", "libs", "sub"), 1),
        (_p("b"), 0),
    ]


def test_build_repo_tree_nests_submodules_of_submodules():
    nodes = build_repo_tree(
        [RepoEntry(p) for p in (_p("a"), _p("a", "sub"), _p("a", "sub", "deep"))]
    )
    assert _paths(nodes) == [
        (_p("a"), 0),
        (_p("a", "sub"), 1),
        (_p("a", "sub", "deep"), 2),
    ]


def test_build_repo_tree_nests_regardless_of_input_order():
    """ordered_repos() puts the active repo first - a submodule can lead."""
    nodes = build_repo_tree([RepoEntry(_p("a", "sub")), RepoEntry(_p("a"))])
    assert _paths(nodes) == [(_p("a"), 0), (_p("a", "sub"), 1)]


def test_build_repo_tree_keeps_sibling_order_and_dedups():
    nodes = build_repo_tree([RepoEntry(_p("b")), RepoEntry(_p("a")), RepoEntry(_p("b"))])
    assert _paths(nodes) == [(_p("b"), 0), (_p("a"), 0)]


def test_build_repo_tree_does_not_nest_on_a_partial_name_match():
    """`repo-tools` is not inside `repo`, however similar the paths look."""
    nodes = build_repo_tree([RepoEntry(_p("repo")), RepoEntry(_p("repo-tools"))])
    assert _paths(nodes) == [(_p("repo"), 0), (_p("repo-tools"), 0)]


# ---- code-review rule tables ---------------------------------------------------
def test_a_repository_s_rule_table_survives_a_round_trip_through_the_settings_file():
    """RepoEntry is rebuilt field by field, so a new one is dropped unless named."""
    s = Settings(repos=[RepoEntry("/a/repo", review_rules="House rules")])
    assert Settings.from_dict(s.to_dict()).repos[0].review_rules == "House rules"


def test_the_table_a_repository_uses_is_asked_of_the_settings():
    s = _settings("/a/one", "/a/two")
    s.set_repo_review_table("/a/one", "House rules")
    assert s.review_table_for_repo("/a/one") == "House rules"
    assert s.review_table_for_repo("/a/two") == ""


def test_renaming_a_rule_table_repoints_the_repositories_that_used_it():
    """Otherwise their next review runs against nothing, and looks clean."""
    s = _settings("/a/one", "/a/two")
    s.set_repo_review_table("/a/one", "House rules")
    s.rename_review_table("House rules", "Team rules")
    assert s.review_table_for_repo("/a/one") == "Team rules"


def test_deleting_a_rule_table_leaves_its_repositories_without_one():
    s = _settings("/a/one")
    s.set_repo_review_table("/a/one", "House rules")
    s.remove_review_table("House rules")
    assert s.review_table_for_repo("/a/one") == ""


def test_two_spellings_of_one_repository_path_share_one_key():
    """Both stores file a repository under this; they must not disagree."""
    from git_assistant.config import repo_key

    assert repo_key(r"D:\Repo\Demo") == repo_key("d:/repo/demo/")
