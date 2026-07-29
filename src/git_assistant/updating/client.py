"""Talking to the distribution service.

The trust boundary sits at `dist_client`: this module fetches bytes and hands
them over, and every signature, digest, expiry and delegation check happens on
the other side of that call in Rust. Nothing here is in a position to decide
that an update is genuine, which is the point — a bug in this file can fail to
find an update, or fetch the wrong URL, but it cannot install unsigned code.
"""

from __future__ import annotations

import json
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

#: Where this build looks for updates.
#:
#: Committed, so every build carries an address by construction and the release
#: workflow only overrides it. It was previously written at package time from a
#: CI variable and nothing else, which meant an unset variable produced a build
#: whose updater silently did nothing — indistinguishable, from the outside,
#: from the feature being broken.
UPDATE_URL_FILE = "update_url.txt"

#: A user-editable override, in the platform config directory beside
#: `settings.json`. Its own file rather than a key in `settings.json`, because
#: the application *writes* that one — and a file the application rewrites is a
#: poor place to keep the thing that decides where its code comes from. This
#: one is only ever read, so a hand edit cannot be clobbered and no code path
#: can redirect updates.
UPDATE_CONFIG_FILE = "update.json"

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


def update_config_path() -> Path:
    """The user-editable update configuration, beside `settings.json`."""
    return Path(user_config_dir(APP_NAME, appauthor=False)) / UPDATE_CONFIG_FILE


def _clean_url(value: object) -> str:
    """A URL we are willing to fetch from, or `""`.

    Only `http` and `https`. Plain `http` is allowed on purpose: TUF signs the
    metadata and pins the target hashes, so the transport is not what makes an
    update trustworthy, and a loopback deployment is a normal way to run this.
    Anything else — `file:`, `javascript:`, a bare hostname — reads as absent.
    """
    if not isinstance(value, str):
        return ""
    url = value.strip()
    return url if url.startswith(("https://", "http://")) else ""


@dataclass(frozen=True, slots=True)
class UserUpdateConfig:
    """What `update.json` said, and whether it could be read at all."""

    url: str = ""
    channel: str = ""
    problem: str = ""


def user_update_config() -> UserUpdateConfig:
    """Read `update.json`. Never written by this application.

    Exists so an installation can be pointed somewhere else when its usual
    service is unreachable — a build whose only address is compiled in cannot
    recover when that address dies.

    A malformed file is reported, not raised. This is called while building the
    tray menu, and a JSON typo should not stop the application starting; but it
    should not silently look like "no override" either, or someone edits a file
    and watches nothing happen.

    Changing this cannot change what is trusted. The keys a release must be
    signed by are fixed by the root bundled in the build, so pointing this at a
    hostile server produces verification failures rather than bad code.
    """
    path = update_config_path()
    if not path.is_file():
        return UserUpdateConfig()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return UserUpdateConfig(problem=f"{path} could not be read: {exc}")
    if not isinstance(data, dict):
        return UserUpdateConfig(problem=f"{path} does not contain a JSON object")

    url = _clean_url(data.get("url"))
    if data.get("url") and not url:
        return UserUpdateConfig(problem=f"{path} has a 'url' that is not http(s)")

    channel = data.get("channel")
    return UserUpdateConfig(
        url=url,
        channel=channel.strip() if isinstance(channel, str) else "",
    )


#: An override that overrides nothing, so creating it is safe.
#:
#: `url` is empty on purpose rather than pre-filled with the address currently
#: in use: writing that in would pin this installation to whatever the build
#: happened to ship with, and a later build pointing somewhere else would be
#: quietly ignored. Empty means "use the build's address", which is what
#: someone who has not edited the file wants.
_TEMPLATE = {
    "_comment": (
        "Set 'url' to change where this installation looks for updates, for "
        "example if the usual service is down. Leave it empty to use the "
        "address this build was published with. The repository root, not its "
        "metadata directory."
    ),
    "url": "",
    "channel": DEFAULT_CHANNEL,
}


def ensure_update_config() -> Path:
    """Create `update.json` with an inert template if it is not there.

    The single place this application writes that file, and it is reached only
    by someone explicitly asking to edit it. The template overrides nothing, so
    calling this cannot change where updates come from — which is what keeps
    "the application only ever reads this" true in the sense that matters.

    Returns the path either way, so a caller can open it.
    """
    path = update_config_path()
    if path.is_file():
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_TEMPLATE, indent=2) + "\n", encoding="utf-8")
    return path


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
    #: Where `base_url` came from, for messages. "It is looking at the wrong
    #: server" is a question the answer to which should not require reading
    #: the source.
    origin: str = ""
    #: Set when `update.json` exists but could not be used. Reported rather
    #: than raised: a JSON typo must not stop the application starting, and
    #: must not look like "no override" either.
    problem: str = ""

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
            # A broken override is a different problem from an absent one, and
            # saying so is the difference between fixing a typo and hunting for
            # a setting that was there all along.
            return self.problem or "no update URL is configured"
        if not is_installed():
            return (
                "this is a source checkout, not an installed build; "
                "self-update only applies to a packaged install"
            )
        if not verifier_available():
            return "this build was packaged without the update verifier"
        return None

    @classmethod
    def load(cls) -> UpdateConfig:
        """Where this build looks for updates: `update.json`, else the build.

        Two sources, each with one owner:

        - **The build** carries the address of the service that published it,
          written at package time. This is what makes a fresh install work with
          no configuration at all — and it has to be the build rather than the
          environment, because an installed desktop application never sees a
          shell's. It is launched from the Start Menu and inherits the *user*
          environment, so a variable exported in a terminal reached a checkout,
          where updating is refused, and reached nothing else.
        - **`update.json`** lets the person running it override that, which is
          the whole point: a build whose only address is compiled in cannot
          recover when that address goes down.

        Neither is a trust decision. The keys a release must be signed by are
        fixed by the root bundled in the build, so the worst a wrong address
        can do is fail to verify.
        """
        user = user_update_config()

        # An unusable `update.json` does *not* fall back to the packaged
        # address. Somebody wrote that file because the packaged one was not
        # working; quietly using it anyway hides their mistake behind the
        # failure they were trying to escape, and they get "could not reach the
        # update service" when the truth is "your override has a typo".
        if user.problem:
            return cls(problem=user.problem)

        if user.url:
            return cls(
                base_url=user.url.rstrip("/"),
                channel=user.channel or DEFAULT_CHANNEL,
                origin=str(update_config_path()),
            )

        packaged = packaged_update_url()
        return cls(
            base_url=packaged.rstrip("/"),
            channel=user.channel or DEFAULT_CHANNEL,
            origin="the address this build was published with" if packaged else "",
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


def _packaged_file(name: str) -> Path | None:
    """A data file shipped beside this module, wherever it ended up.

    PyInstaller puts `--add-data` under `sys._MEIPASS`, so the frozen location
    is checked first and the source tree second. One place, because getting it
    right for the root and wrong for anything else is how a build ends up
    behaving differently from the checkout it was made from.
    """
    candidates = [Path(__file__).resolve().parent / name]
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        candidates.insert(0, Path(frozen) / "git_assistant" / "updating" / name)

    return next((c for c in candidates if c.is_file()), None)


def packaged_update_url() -> str:
    """The update service this build ships pointing at, or `""`.

    Read from `UPDATE_URL_FILE`, which is committed and therefore present in
    every build and every checkout. `""` means somebody deleted it, and the
    release workflow refuses to build in that state.

    Only `http` and `https` are accepted, and anything else reads as absent
    rather than raising — a malformed constant should not stop the application
    starting. Plain `http` is allowed on purpose: TUF signs the metadata and
    pins the target hashes, so the transport is not what makes an update
    trustworthy, and a loopback deployment is a normal way to run this.
    """
    path = _packaged_file(UPDATE_URL_FILE)
    if path is None:
        return ""
    try:
        url = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return _clean_url(url)


def _trusted_root() -> bytes:
    """The root shipped inside this build.

    Raises:
        UpdateUnavailableError: if it is missing, which means the application
            was packaged without it. Failing here is correct — proceeding would
            mean trusting whatever the server sent.
    """
    path = _packaged_file(ROOT_FILENAME)
    if path is None:
        raise UpdateUnavailableError(
            f"{ROOT_FILENAME} is not bundled with this build; updates are disabled"
        )
    return path.read_bytes()


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
