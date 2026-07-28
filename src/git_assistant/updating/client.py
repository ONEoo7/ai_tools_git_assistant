"""Talking to the distribution service.

The trust boundary sits at `dist_client`: this module fetches bytes and hands
them over, and every signature, digest, expiry and delegation check happens on
the other side of that call in Rust. Nothing here is in a position to decide
that an update is genuine, which is the point — a bug in this file can fail to
find an update, or fetch the wrong URL, but it cannot install unsigned code.
"""

from __future__ import annotations

import os
import platform
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from platformdirs import user_config_dir

from git_assistant.config import APP_NAME

#: Where the trusted root shipped with this build lives. Embedded rather than
#: fetched: fetching it would open a trust-on-first-use window in which a
#: network attacker could hand us a root of their own.
ROOT_FILENAME = "root.json"

#: Identifies this application to the distribution service. Must match the
#: `app_id` the server publishes under.
APP_ID = "git-assistant"

DEFAULT_CHANNEL = "stable"
NETWORK_TIMEOUT = 20.0

#: Cap on a metadata document. Releases are large; metadata is not.
MAX_METADATA_BYTES = 8 * 1024 * 1024


class UpdateUnavailableError(RuntimeError):
    """Updating is not configured, or the service could not be reached."""


def verifier_available() -> bool:
    """Is the TUF verifier present in this build?

    `dist-client` is an optional dependency: a developer checkout and the test
    suite do not need it, and a build packaged without it has no updater. The
    import is attempted rather than assumed, because the failure it guards
    against — a menu item that always errors — is worse than not offering one.
    """
    from importlib.util import find_spec

    try:
        return find_spec("dist_client") is not None
    except (ImportError, ValueError):
        return False


def is_installed() -> bool:
    """Is this a packaged build rather than a source checkout?

    Self-update replaces the files it is running from. In a packaged build
    those files are the build; in a `git clone` they are a working tree, and
    unpacking a release over one would destroy uncommitted work and leave a
    directory that is neither a checkout nor an install. The check exists
    because that is not a thing to get wrong once.

    `sys.frozen` is the honest signal: PyInstaller sets it, and nothing else
    running out of a source tree does. Deliberately no environment variable to
    override it — a switch that turns self-update on in a working tree is a
    switch someone eventually leaves on.
    """
    return bool(getattr(sys, "frozen", False))


@dataclass(frozen=True, slots=True)
class UpdateConfig:
    """Where to look for updates.

    `base_url` is the repository root, *not* its metadata directory —
    `https://updates.example/` and not `https://updates.example/metadata`.
    Metadata and targets are siblings beneath it. It is the only piece an
    operator has to supply; until it is set, `enabled` is false and the
    application never touches the network for this.
    """

    base_url: str = ""
    channel: str = DEFAULT_CHANNEL
    app_id: str = APP_ID

    @property
    def enabled(self) -> bool:
        """Can this build check for updates at all?

        Three things are required: somewhere to look, the verifier to check
        what comes back, and a packaged build to install into. A build missing
        any of them hides the feature entirely rather than offering a menu item
        that fails — and, more to the point, there is no path here that falls
        back to fetching updates without verifying them.
        """
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        """Why updating is off, or `None` if it is on.

        One place, so the menu and the error message cannot disagree. They did:
        every disabled reason used to surface as "no update URL is configured",
        which sends someone editing environment variables when the actual
        answer is that they are running from a checkout.
        """
        if not self.base_url:
            return "no update URL is configured"
        if not is_installed():
            return (
                "this is a source checkout, not an installed build; "
                "self-update only applies to a packaged install"
            )
        if not verifier_available():
            return "this build was packaged without the update verifier"
        return None

    @classmethod
    def from_env(cls) -> UpdateConfig:
        """Read configuration from the environment.

        Deliberately not from `settings.json`: the update source is an
        operator decision, not a user preference, and a settings file the
        application itself rewrites is a poor place to keep something that
        determines where code comes from.
        """
        return cls(
            base_url=os.environ.get("GIT_ASSISTANT_UPDATE_URL", "").rstrip("/"),
            channel=os.environ.get("GIT_ASSISTANT_UPDATE_CHANNEL", DEFAULT_CHANNEL),
        )


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """What a check found."""

    version: str
    target_path: str
    length: int
    sha256_hex: str
    mandatory: bool


def current_version() -> str:
    """This build's version — the number an update is compared against.

    Read from `git_assistant.__version__`, which is the single place the
    version is written: `pyproject.toml` derives from it, so the packaged
    version and the reported one cannot drift.

    This used to try `importlib.metadata` first and fall back to
    `__version__`, defaulting to `"0.0.0"`. Both halves were wrong. A frozen
    build has no distribution metadata unless PyInstaller is told to copy it,
    so the fallback was in fact the only path that ever ran — and it silently
    reported whatever number `__init__.py` happened to hold, which was a
    release behind. Worse, `"0.0.0"` is the most dangerous possible default:
    it makes every published release look newer, including one the user has
    already declined or rolled back from.
    """
    from git_assistant import __version__

    return __version__


def _platform_arch() -> tuple[str, str]:
    """This machine, in the vocabulary the server publishes under."""
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine, machine
    )
    return system, arch


def install_id() -> str:
    """A stable identifier for this installation, created on first use.

    Used only to decide locally whether a staged rollout has reached this
    install. It is never sent anywhere: the client evaluates the rollout itself
    from signed metadata, so there is no per-client server response for a
    network attacker to forge in order to push a release at someone early.

    Random rather than derived from anything about the machine, so it cannot be
    correlated back to a user or a device.
    """
    path = Path(user_config_dir(APP_NAME, appauthor=False)) / "install_id"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass

    generated = secrets.token_hex(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(generated, encoding="utf-8")
    tmp.replace(path)  # atomic: two starts must not disagree about who we are
    return generated


def _trusted_root() -> bytes:
    """The root shipped inside this build.

    Raises:
        UpdateUnavailableError: if it is missing, which means the application
            was packaged without it. Failing here is correct — proceeding would
            mean trusting whatever the server sent.
    """
    candidates = [Path(__file__).resolve().parent / ROOT_FILENAME]
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        candidates.insert(0, Path(frozen) / "git_assistant" / "updating" / ROOT_FILENAME)

    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_bytes()

    raise UpdateUnavailableError(
        f"{ROOT_FILENAME} is not bundled with this build; updates are disabled"
    )


def _fetcher(config: UpdateConfig, client: httpx.Client):  # type: ignore[no-untyped-def]
    """Route a repository-relative path to a URL.

    `base_url` is the repository *root*. Metadata and targets are siblings
    beneath it — `/metadata/timestamp.json`, `/targets/<app>/...` — matching
    what the edge serves. `dist_client` asks for target paths already prefixed
    with `targets/` and metadata paths bare, so only metadata needs the prefix
    added.
    """

    def fetch(path: str) -> bytes:
        relative = path if path.startswith("targets/") else f"metadata/{path}"
        response = client.get(f"{config.base_url}/{relative}")
        response.raise_for_status()
        if len(response.content) > MAX_METADATA_BYTES and not path.startswith("targets/"):
            raise UpdateUnavailableError(f"{path} is implausibly large for metadata")
        return response.content

    return fetch


def check_for_update(config: UpdateConfig) -> UpdateResult | None:
    """Ask whether a newer version exists for this install.

    Returns `None` when there is nothing to offer: no pointer published yet,
    this install is outside the rollout slice, or the current release is not
    newer than what is running. None of those is an error.

    Raises:
        UpdateUnavailableError: updates are not configured or not bundled.
        Exception: anything `dist_client` raises. A verification failure is
            deliberately not swallowed here — it means something served
            metadata that did not verify, and the caller should say so rather
            than report "no updates".
    """
    reason = config.unavailable_reason()
    if reason is not None:
        raise UpdateUnavailableError(reason)

    from dist_client.update import Channel, UpdateCheck

    system, arch = _platform_arch()
    with httpx.Client(timeout=NETWORK_TIMEOUT, follow_redirects=False) as client:
        available = UpdateCheck(
            root=_trusted_root(),
            channel=Channel(config.app_id, config.channel, system, arch),
            fetch=_fetcher(config, client),
            install_id=install_id(),
        ).run()

    if available is None:
        return None

    from dist_client.update import is_newer

    if not is_newer(available.version, current_version()):
        return None

    return UpdateResult(
        version=available.version,
        target_path=available.target_path,
        length=available.info.length,
        sha256_hex=available.info.sha256_hex,
        mandatory=available.mandatory,
    )


def download_update(config: UpdateConfig, result: UpdateResult, destination: Path) -> Path:
    """Fetch the release and verify it against its signed description.

    The bytes are checked before they are written anywhere the installer would
    look, and they will be checked again immediately before installation —
    verifying only once leaves a window in which a staged file can be swapped.

    Raises:
        Exception: from `dist_client` if the bytes are not what was signed.
    """
    from dist_client import TargetInfo, verify_payload
    from dist_client.update import stored_target_path

    stored = stored_target_path(result.target_path, result.sha256_hex)
    with httpx.Client(timeout=NETWORK_TIMEOUT, follow_redirects=False) as client:
        response = client.get(f"{config.base_url}/targets/{stored}")
        response.raise_for_status()
        body = response.content

    info = TargetInfo(
        version=result.version,
        length=result.length,
        sha256=bytes.fromhex(result.sha256_hex),
        rollout_pct=100,
        mandatory=result.mandatory,
    )
    verify_payload(info, body)

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    tmp.write_bytes(body)
    tmp.replace(destination)
    return destination
