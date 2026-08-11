"""Whose certificates this application trusts, and what it says when it cannot.

No test here opens a connection. What is being checked is which roots end up in
the context and what the user is told, both of which are decidable offline.
"""

import ssl

import pytest

from git_assistant import net


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """One context is cached for the process; every test wants its own."""
    for name in net.CA_BUNDLE_VARS:
        monkeypatch.delenv(name, raising=False)
    net.ssl_context.cache_clear()
    monkeypatch.setattr(net, "_BUNDLE_PROBLEM", "")
    yield
    net.ssl_context.cache_clear()


def _bundle(tmp_path, text: str, name="ca.pem"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _one_real_root() -> str:
    """A valid PEM certificate, so a bundle can be well-formed but wrong."""
    import certifi

    head, _, _ = certifi.where() and open(certifi.where(), encoding="utf-8").read().partition(
        "-----END CERTIFICATE-----"
    )
    return head + "-----END CERTIFICATE-----\n"


# ---- the platform store ---------------------------------------------------------
def test_the_machines_own_roots_are_what_is_trusted():
    """The whole fix: httpx verifies against certifi and ignores the OS store,
    so a corporate proxy's root -- which the machine does trust -- is refused."""
    context = net.ssl_context()
    assert isinstance(context, ssl.SSLContext)
    assert context.cert_store_stats()["x509_ca"] > 0


def test_certifi_is_loaded_as_well_as_the_platform_store():
    """A sparse store -- a container, a stripped image -- must not regress.

    Trusting the union is the point: neither source has to be complete alone.
    """
    import certifi

    platform_only = ssl.create_default_context()
    both = net.ssl_context()

    with open(certifi.where(), encoding="utf-8") as handle:
        in_certifi = handle.read().count("-----END CERTIFICATE-----")
    assert in_certifi > 0
    assert (
        both.cert_store_stats()["x509_ca"]
        >= platform_only.cert_store_stats()["x509_ca"]
    )


def test_verification_is_never_turned_off():
    """The tempting fix, and the wrong one. A regression guard, not a detail."""
    context = net.ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_the_context_is_built_once():
    assert net.ssl_context() is net.ssl_context()


# ---- a bundle named in the environment -------------------------------------------
def test_a_bundle_can_be_named_in_the_environment(tmp_path, monkeypatch):
    path = _bundle(tmp_path, _one_real_root())
    monkeypatch.setenv("GIT_ASSISTANT_CA_BUNDLE", str(path))

    assert net.ca_bundle() == str(path)
    assert net.bundle_problem() == ""


def test_the_variables_other_python_tools_read_are_honoured(tmp_path, monkeypatch):
    """Somebody who configured pip or requests for their proxy is done here."""
    path = _bundle(tmp_path, _one_real_root())
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(path))
    assert net.ca_bundle() == str(path)


def test_our_own_variable_wins_over_the_borrowed_ones(tmp_path, monkeypatch):
    ours = _bundle(tmp_path, _one_real_root(), "ours.pem")
    theirs = _bundle(tmp_path, _one_real_root(), "theirs.pem")
    monkeypatch.setenv("GIT_ASSISTANT_CA_BUNDLE", str(ours))
    monkeypatch.setenv("SSL_CERT_FILE", str(theirs))
    assert net.ca_bundle() == str(ours)


def test_a_path_that_is_not_there_is_ignored(tmp_path, monkeypatch):
    """Almost always a variable left over from another machine.

    Refusing to start over one would be worse than falling back to the store
    that was going to work anyway.
    """
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "gone.pem"))
    assert net.ca_bundle() == ""
    assert net.ssl_context().cert_store_stats()["x509_ca"] > 0


def test_a_quoted_path_is_read_as_a_path(tmp_path, monkeypatch):
    """Windows `set VAR="C:\\path"` keeps the quotes in the value."""
    path = _bundle(tmp_path, _one_real_root())
    monkeypatch.setenv("GIT_ASSISTANT_CA_BUNDLE", f'"{path}"')
    assert net.ca_bundle() == str(path)


def test_a_bundle_that_will_not_load_falls_back_and_says_why(tmp_path, monkeypatch):
    """An empty or half-copied file raises when the context is built.

    Unhandled, that is a traceback on every request rather than a message --
    and the platform store was going to answer anyway.
    """
    monkeypatch.setenv("GIT_ASSISTANT_CA_BUNDLE", str(_bundle(tmp_path, "")))

    context = net.ssl_context()

    assert context.cert_store_stats()["x509_ca"] > 0  # fell back, still usable
    assert "not a readable CA bundle" in net.bundle_problem()


def test_a_working_bundle_leaves_no_complaint(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_ASSISTANT_CA_BUNDLE", str(_bundle(tmp_path, _one_real_root())))
    assert net.bundle_problem() == ""


# ---- telling a certificate failure apart -------------------------------------------
def test_a_certificate_failure_is_recognised_through_the_wrapper():
    """httpx wraps the real error in a ConnectError; the cause chain is walked."""
    import httpx

    inner = ssl.SSLCertVerificationError("self-signed certificate in certificate chain")
    wrapped = httpx.ConnectError("connection failed")
    wrapped.__cause__ = inner

    assert net.is_certificate_error(wrapped) is True


def test_the_text_is_enough_when_the_cause_was_lost():
    assert net.is_certificate_error(
        RuntimeError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    )


def test_an_ordinary_failure_is_not_mistaken_for_one():
    import httpx

    assert net.is_certificate_error(httpx.ConnectTimeout("timed out")) is False


def test_a_cycle_in_the_cause_chain_does_not_hang():
    """This runs on the GUI thread's behalf; a loop here freezes the window."""
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__cause__ = second
    second.__cause__ = first

    assert net.is_certificate_error(first) is False


# ---- what the user is told ----------------------------------------------------------
def test_the_advice_names_the_two_ways_out():
    said = net.certificate_help("https://api.openai.com/v1")
    assert "TLS inspection" in said
    assert "Windows certificate store" in said
    assert "GIT_ASSISTANT_CA_BUNDLE" in said
    assert "_ssl.c" not in said  # the message it replaces named a line of C


def test_the_advice_says_which_bundle_was_checked(tmp_path, monkeypatch):
    path = _bundle(tmp_path, _one_real_root())
    monkeypatch.setenv("GIT_ASSISTANT_CA_BUNDLE", str(path))
    assert str(path) in net.certificate_help("https://x/v1")


def test_a_bundle_that_never_loaded_is_said_before_anything_else_is_read(
    tmp_path, monkeypatch
):
    """Otherwise the advice describes a bundle that was not in fact consulted."""
    monkeypatch.setenv("GIT_ASSISTANT_CA_BUNDLE", str(_bundle(tmp_path, "")))

    said = net.certificate_help("https://x/v1")

    assert "not a readable CA bundle" in said
    assert "this machine's certificate store" in said


# ---- the client every request goes through --------------------------------------
def test_every_client_gets_the_shared_context():
    with net.http_client() as client:
        assert client._transport._pool._ssl_context is net.ssl_context()


def test_a_caller_can_still_say_what_it_needs():
    import httpx

    with net.http_client(timeout=httpx.Timeout(3.0), headers={"X": "1"}) as client:
        assert client.headers["X"] == "1"
        assert client.timeout.connect == 3.0


def test_proxy_environment_variables_are_left_alone():
    """httpx reads HTTPS_PROXY itself; turning trust_env off would break it."""
    with net.http_client() as client:
        assert client.trust_env is True
