from git_assistant.config import RepoEntry, Settings


def _settings(*paths, active=""):
    return Settings(repos=[RepoEntry(p) for p in paths], active_repo=active)


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
