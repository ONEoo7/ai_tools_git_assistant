"""Self-update: ask the distribution service whether a newer build exists.

Nothing in this package decides whether an update is trustworthy. That happens
in `dist_client`, which calls a Rust verifier through a C ABI, and the answer
comes out of TUF metadata signed by keys this application never holds.

What lives here is the application's side of the conversation: where the
repository is, which channel this build follows, and a stable identifier used
locally to decide whether a staged rollout has reached this install.
"""

from git_assistant.updating.client import (
    UpdateConfig,
    UpdateResult,
    check_for_update,
    clear_staged_updates,
    download_update,
    install_id,
    install_update,
    verifier_available,
)

__all__ = [
    "UpdateConfig",
    "UpdateResult",
    "check_for_update",
    "clear_staged_updates",
    "download_update",
    "install_id",
    "install_update",
    "verifier_available",
]
