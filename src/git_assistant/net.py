"""How this application trusts the other end of a TLS connection.

Every corporate network that inspects traffic does it the same way: the proxy
terminates TLS, re-signs the response with a root certificate of its own, and
that root is installed on the machine by whoever runs the network. Windows
trusts it. `curl` trusts it. Chrome trusts it.

Python does not, because httpx -- and every library built on it -- verifies
against the CA bundle shipped inside `certifi` rather than against the store the
operating system keeps. A machine whose browser reaches the address perfectly
well therefore gets:

    [SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain

which reads as "this application is broken" and is really "this application is
the only thing here not reading the machine's own trust store".

So it reads it. `ssl.create_default_context()` loads the platform store, and
`certifi` is loaded on top of it rather than instead of it, so a machine with a
sparse store (a container, a stripped image) keeps working exactly as before.
Trusting the union is the point: neither source is asked to be complete alone.

**On Windows that means both `ROOT` and `CA`, and the second one matters.**
`load_default_certs` enumerates the Intermediate Certification Authorities store
as well as Trusted Root, and on a real inspecting network the certificate that
signs the re-issued leaf is often installed only in the intermediate one -- the
anchor this was written against sits there and nowhere else. Anything that
narrows this to Trusted Root, including swapping in a library that enumerates
only that, verifies on the developer's machine and fails on the user's.

None of this weakens anything. Certificates are still verified and hostnames
still checked; the only change is *whose* list of roots is consulted, and the
answer is now "the ones this machine was configured to trust".

**Proxies need no code.** httpx honours `HTTPS_PROXY`, `HTTP_PROXY` and
`NO_PROXY` by default, and nothing here turns that off.
"""

from __future__ import annotations

import os
import ssl
from functools import lru_cache
from pathlib import Path

import httpx

#: Where to look for a CA bundle to use *instead* of the platform store, in
#: order. The first is ours; the other two are what every other Python tool on
#: the machine already reads, so somebody who has configured `pip` or `requests`
#: for their proxy has configured this too, without being told to.
CA_BUNDLE_VARS = ("GIT_ASSISTANT_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


def ca_bundle() -> str:
    """A CA bundle named in the environment, or `""`.

    A path that does not exist is ignored rather than raising: it is almost
    always a stale variable from another machine, and refusing to start over it
    would be worse than falling back to the platform store.
    """
    for name in CA_BUNDLE_VARS:
        value = (os.environ.get(name) or "").strip().strip('"')
        if value and Path(value).is_file():
            return value
    return ""


#: Why a bundle named in the environment was not used. Read through
#: `bundle_problem()`; set where the bundle is loaded, which happens once.
_BUNDLE_PROBLEM = ""


def bundle_problem() -> str:
    """Why the configured CA bundle was ignored, or `""`.

    Asking for the context is what settles this, so it is asked for first --
    otherwise a caller that has not made a connection yet reads `""` and
    reports that all is well.
    """
    ssl_context()
    return _BUNDLE_PROBLEM


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """The trust this application verifies certificates against.

    Cached: building one parses every root on the machine, and it is asked for
    once per request. Nothing mutates it, so one is safe to share -- and it is
    what lets a single context back every client here.
    """
    global _BUNDLE_PROBLEM

    bundle = ca_bundle()
    if bundle:
        # Named explicitly, so it is the whole answer. Adding the platform
        # store underneath would make "I pointed it at our bundle" mean
        # something other than what it says.
        try:
            return ssl.create_default_context(cafile=bundle)
        except (OSError, ssl.SSLError) as exc:
            # A file that is not a PEM bundle -- empty, DER, half-copied, or a
            # path left over from another machine. Falling back keeps the
            # application able to reach the network at all, and on Windows the
            # platform store is the one that was going to work anyway; the
            # reason is kept so nobody is left wondering why their bundle had
            # no effect.
            _BUNDLE_PROBLEM = f"{bundle} is not a readable CA bundle ({exc})"

    context = ssl.create_default_context()  # the platform store
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except Exception:
        # A build without certifi, or a bundle that will not parse. The
        # platform store is already loaded and is the one that matters here.
        pass
    return context


def http_client(**kwargs) -> httpx.Client:
    """An `httpx.Client` that trusts what this machine trusts.

    Every outbound connection in this application goes through here, so the
    answer to "why does it not trust our proxy" has one place to be wrong and
    one place to fix.
    """
    kwargs.setdefault("verify", ssl_context())
    return httpx.Client(**kwargs)


def is_certificate_error(exc: BaseException) -> bool:
    """Whether a failure was the far end's certificate, not the connection.

    Told apart by walking the cause chain to an `ssl.SSLCertVerificationError`,
    and falling back to the text: httpx wraps the original in a `ConnectError`,
    and some transports lose the cause on the way.
    """
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 10:  # a cycle here must not hang a GUI
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)


def certificate_help(url: str) -> str:
    """What to do about a certificate that did not verify.

    The raw message is `[SSL: CERTIFICATE_VERIFY_FAILED] ... (_ssl.c:1077)`,
    which names a line of C and no action. On a corporate network the cause is
    almost always TLS inspection, and the fix is one of two things -- so it says
    both rather than leaving somebody to search for the C file.
    """
    problem = bundle_problem()
    bundle = "" if problem else ca_bundle()
    source = f"the bundle at {bundle}" if bundle else "this machine's certificate store"
    said = (
        f"Could not verify the certificate {url} presented, checked against "
        f"{source}.\n\n"
        "On a corporate network this is usually TLS inspection: the proxy "
        "re-signs traffic with its own root certificate. Either have that root "
        "installed in the Windows certificate store, where this reads it from, "
        f"or point {CA_BUNDLE_VARS[0]} at a .pem file containing it."
    )
    # Said last and in full: somebody who set a bundle and is still being
    # refused needs to know theirs was never loaded before anything else here
    # is worth reading.
    return f"{said}\n\nNote: {problem}." if problem else said
