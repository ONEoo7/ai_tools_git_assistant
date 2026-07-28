import pytest

from git_assistant import versioning
from git_assistant.versioning import bump, latest_version, parse_version, proposals


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v0.2.0", ("v", 0, 2, 0)),
        ("0.2.0", ("", 0, 2, 0)),
        ("release-1.20.3", ("release-", 1, 20, 3)),
        ("v10.0.1", ("v", 10, 0, 1)),
    ],
)
def test_parse_version(tag, expected):
    v = parse_version(tag)
    assert (v.prefix, v.major, v.minor, v.patch) == expected


@pytest.mark.parametrize("tag", ["", "nightly", "v1.2", "abc"])
def test_parse_version_rejects_non_semver(tag):
    assert parse_version(tag) is None


def test_bump_preserves_prefix_and_resets_lower_parts():
    v = parse_version("v0.2.0")
    assert str(bump(v, "patch")) == "v0.2.1"
    assert str(bump(v, "minor")) == "v0.3.0"
    assert str(bump(v, "major")) == "v1.0.0"


def test_bump_resets_from_deep_version():
    v = parse_version("v1.4.7")
    assert str(bump(v, "minor")) == "v1.5.0"
    assert str(bump(v, "major")) == "v2.0.0"


def test_bump_without_prefix():
    assert str(bump(parse_version("0.2.0"), "patch")) == "0.2.1"


def test_bump_rejects_unknown_part():
    with pytest.raises(ValueError):
        bump(parse_version("v1.0.0"), "build")


def test_latest_version_picks_highest_not_lexicographic():
    # "v0.9.0" sorts after "v0.10.0" as a string; numeric order must win.
    tags = ["v0.9.0", "v0.10.0", "v0.2.0"]
    assert str(latest_version(tags)) == "v0.10.0"


def test_latest_version_ignores_non_semver_tags():
    assert str(latest_version(["nightly", "v1.2.3", "latest"])) == "v1.2.3"


def test_latest_version_none_when_no_tags():
    assert latest_version([]) is None
    assert latest_version(["nightly"]) is None


def test_proposals_from_current():
    # The user's example: 0.2.0 -> patch proposes 0.2.1
    p = proposals(parse_version("v0.2.0"))
    assert p == {"major": "v1.0.0", "minor": "v0.3.0", "patch": "v0.2.1"}


def test_proposals_with_no_existing_tags():
    p = proposals(None)
    assert set(p) == set(versioning.PARTS)
    assert all(v == versioning.DEFAULT_FIRST_VERSION for v in p.values())
