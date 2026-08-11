# Providers

Pick one in the **Providers** list on the left of *Connection & Model*. The same
choice appears as **AI Provider** on the Generate, Audit and Code Review tabs, so
you can switch without leaving what you are doing. The form reshapes around the
selection — only the settings a provider actually has are shown.

| Provider | Needs | Notes |
|---|---|---|
| **LM Studio** | endpoint | Local, no key. The default. `http://127.0.0.1:1234` |
| **Claude** | API key | Anthropic Messages API, via the official SDK |
| **OpenAI** | API key | Fixed endpoint |
| **Azure AI Foundry** | API key + endpoint | Endpoint is per-resource; `api-version` is configurable |
| **Litellm Proxy** | endpoint (+ optional key) | `localhost:4000`. Connects unauthenticated if your proxy has no auth |
| **Ollama** | endpoint | `localhost:11434/v1`. No key — Ollama has no auth of its own |
| **Lemonade Server** | endpoint | `localhost:13305/api/v1`. No key, same reason |
| **Claude Code CLI** | the CLI, logged in | Experimental. See below |
| **Antigravity CLI** | the CLI, logged in | Experimental. See below |

The self-hosted providers all default to localhost, so the usual setup needs no
typing. Put a remote one behind auth via the **Litellm Proxy** entry, which has a
key field.

Every address lives in one place — `model.endpoints` in the settings a
repository carries, keyed by provider. A project can therefore pin the server it
is meant to be generated against. **The model** is kept per provider in your own
settings, so switching backend does not carry one vendor's model name into
another's request.

## API keys

Keys go in the **Windows Credential Manager** and never into a settings file.
Those files are plain text, the application rewrites them constantly, and a key
in one would survive in backups and file-history copies long after it was
rotated. Entries are named `git-assistant:<provider>` and are visible in
*Credential Manager → Windows Credentials*.

The key field is an input, not a display: it says whether a key is stored, never
what it is, and clears itself after saving. **Remove** deletes the entry.

Claude is its own client because Anthropic differs on the wire: the system prompt
is a top-level parameter rather than a message, `max_tokens` is required,
`content` comes back as a list of typed blocks, and current models **reject
`temperature`**. Every OpenAI-compatible provider shares one client that differs
only by base URL, auth header and query parameters.

## Behind a corporate proxy

Networks that inspect TLS terminate the connection, re-sign it with a root
certificate of their own, and install that root on the machine. Windows trusts
it, `curl` trusts it, the browser trusts it — and Python, by default, does not,
because httpx verifies against the CA bundle inside `certifi` rather than the
store the machine keeps. The symptom is a connection that works everywhere else
failing with:

```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
self-signed certificate in certificate chain (_ssl.c:1077)
```

**This reads the machine's own store**, so on a managed Windows machine there is
usually nothing to configure: the root your IT department installed is the root
it verifies against. `certifi` is loaded as well, not instead, so a machine with
a sparse store keeps working exactly as before. Verification itself is never
weakened — hostnames are still checked and certificates still verified; the only
change is whose list of roots is consulted.

If the root is *not* in the machine's store — an unmanaged laptop, a bundle IT
hands out as a file — point one of these at a `.pem`:

| Variable | Read by |
|---|---|
| `GIT_ASSISTANT_CA_BUNDLE` | this application |
| `SSL_CERT_FILE` | most Python tooling |
| `REQUESTS_CA_BUNDLE` | `requests`, `pip` and friends |

The first that names a file that exists wins. A path that does not exist is
ignored rather than fatal — it is nearly always a variable left from another
machine — and a file that is not a readable bundle falls back to the machine's
store and says so, rather than failing every request.

**Proxies themselves need no configuration here.** `HTTPS_PROXY`, `HTTP_PROXY`
and `NO_PROXY` are honoured as every other Python tool honours them. A proxy that
needs credentials takes them in the URL:
`HTTPS_PROXY=http://user:pass@proxy.corp:8080`. Windows' system proxy settings
and PAC files are *not* read — set the variables if you rely on those.

## Agent CLIs (experimental)

Claude Code and Antigravity can be driven as backends: each call is a whole process,
run non-interactively, using the login that CLI already has. There is no API key
here and no address.

Marked experimental for reasons that were measured rather than assumed:

- **One call at a time.** Four processes starting at once is four runtimes
  starting at once for no throughput gain — the seconds are start-up, not
  queueing. `max_parallel` is 1 for both.
- **Start-up dominates.** For a small diff the process costs more than the
  completion.
- **No token accounting.** The CLI does not report what it spent, so the usage
  pane cannot count these calls the way it counts the others.

They are useful when the login you already have is the only access you have.

## Temperature

Kept per provider *and* per model, because it is a property of the weights: what
is careful for one model is mute for another. Set it on *Connection & Model*; a
model you have never set one for uses the default, and the note beside the field
says which of the two you are looking at.

Providers that reject temperature never receive it.

## What has been asked, and of whom

*Connection & Model* carries a usage pane on the right: lifetime totals per
provider, per model and per **feature** — a commit message, a code review, a
repository audit — and the last few hundred calls behind them, with input tokens,
output tokens and when.

Per feature because a single figure does not answer the question anyone is
asking. The count is taken where the answer comes back, inside the provider
clients, so it counts what was actually spent rather than what was estimated.
