"""API keys, kept in the Windows Credential Manager rather than on disk.

`settings.json` is plain text in a directory the user can open from the
application itself, and this application writes it constantly. An API key there
would be readable by anything running as the user, would land in any backup or
screen-share of the config folder, and would survive in file-history copies
after the key was rotated. The Credential Manager is the operating system's
answer to that: entries are encrypted with the user's login credentials and are
not readable by another account on the machine.

Reached through ctypes rather than a package such as `keyring`. Three reasons:
the application already calls Win32 this way (see the updater's installer
launch), a dependency that resolves its Windows backend dynamically is one more
thing to declare as a PyInstaller hidden import, and the surface needed here is
three functions.

Nothing here logs or returns a secret in an error message.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

#: Prefix for every entry this application owns, so its credentials are
#: identifiable in the Credential Manager UI and cannot collide with another
#: program's target names.
TARGET_PREFIX = "git-assistant"

_CRED_TYPE_GENERIC = 1
#: Stored for this user on this machine, and not synced to a domain profile.
#: An API key is a machine-local secret; roaming it silently widens where it
#: exists to every machine the user signs into.
_CRED_PERSIST_LOCAL_MACHINE = 2

_ERROR_NOT_FOUND = 1168


class CredentialError(RuntimeError):
    """The Credential Manager refused an operation."""


def target_for(provider_key: str) -> str:
    """The Credential Manager entry name for a provider."""
    return f"{TARGET_PREFIX}:{provider_key}"


def available() -> bool:
    """Is a credential store usable on this platform?

    False elsewhere than Windows. The caller decides what to do about it: the
    application is Windows-only in practice, but the test suite is not, and
    neither is a developer checkout on another platform.
    """
    return sys.platform == "win32"


if sys.platform == "win32":

    class _Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    def _advapi32() -> ctypes.WinDLL:
        return ctypes.WinDLL("advapi32", use_last_error=True)


def set_secret(provider_key: str, secret: str, *, comment: str = "") -> None:
    """Store (or replace) the API key for a provider.

    An empty secret deletes the entry rather than storing a blank one: "no key"
    and "a key that is the empty string" are the same intent, and only one of
    them can be checked for later.
    """
    if not available():
        raise CredentialError("the Windows Credential Manager is not available here")
    if not secret:
        delete_secret(provider_key)
        return

    blob = secret.encode("utf-16-le")
    buffer = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)

    credential = _Credential()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = target_for(provider_key)
    credential.Comment = comment or f"Git Assistant API key ({provider_key})"
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(
        buffer, ctypes.POINTER(ctypes.c_byte)
    )
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    # The Credential Manager shows this beside the entry. The provider, not the
    # secret -- it is displayed in plain sight in the control panel.
    credential.UserName = provider_key

    advapi32 = _advapi32()
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    if not advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise CredentialError(
            f"could not store the key (Windows error {ctypes.get_last_error()})"
        )


def get_secret(provider_key: str) -> str | None:
    """Read the API key for a provider, or None when there is not one."""
    if not available():
        return None

    advapi32 = _advapi32()
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]

    pointer = ctypes.POINTER(_Credential)()
    if not advapi32.CredReadW(
        target_for(provider_key), _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
    ):
        code = ctypes.get_last_error()
        if code == _ERROR_NOT_FOUND:
            return None  # never configured, or deleted from the control panel
        raise CredentialError(f"could not read the key (Windows error {code})")

    try:
        credential = pointer.contents
        if not credential.CredentialBlobSize:
            return None
        raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        advapi32.CredFree(pointer)


def delete_secret(provider_key: str) -> None:
    """Remove a provider's key. Absent is success -- the end state is the same."""
    if not available():
        return

    advapi32 = _advapi32()
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    if not advapi32.CredDeleteW(target_for(provider_key), _CRED_TYPE_GENERIC, 0):
        code = ctypes.get_last_error()
        if code != _ERROR_NOT_FOUND:
            raise CredentialError(f"could not remove the key (Windows error {code})")


def has_secret(provider_key: str) -> bool:
    """Is a key stored? Asked by the UI, which must never read the value itself."""
    try:
        return bool(get_secret(provider_key))
    except CredentialError:
        return False
