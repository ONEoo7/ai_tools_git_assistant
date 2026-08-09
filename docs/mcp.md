# MCP server

Offers the repositories, audits and commit-message generation to an MCP client
over stdio. The **MCP Server** tab starts it, tests it and registers it.

## Tools

Read-only by default. The five write tools are only present when the server was
registered with writes allowed.

### Read

| Tool | What it answers |
|---|---|
| `list_repos` | The configured repositories, with branch and whether they are dirty. Start here: every other tool names one of these. |
| `repo_status` | Branch, upstream, unpushed commits, and what is staged and unstaged. |
| `get_diff` | The staged diff or everything uncommitted, truncated to `max_bytes` with the truncation marked. |
| `list_branches` | Local branches, most recently committed to first, the checked-out one marked. |
| `list_tags` | Tags with their dates, newest first. |
| `describe_push_auth` | Which remote, credential helper and identity a push would use. Worth checking before enabling writes. |
| `generate_commit_message` | A Conventional Commits message for what is staged, using the configured provider. Takes as long as that provider does. |
| `run_agent` | Runs an audit and returns its report. |
| `list_agent_runs` | Audits already recorded for a repository, newest first. |
| `get_agent_run` | The full report of one recorded audit. |
| `compare_agent_runs` | What changed between two recorded audits. |

### Write

| Tool | What it does |
|---|---|
| `commit` | Commits what is staged with the given message. Stages nothing itself. |
| `push` | Pushes the checked-out branch to its remote. |
| `create_tag` | Creates a local tag; annotated when a message is given. |
| `push_tag` | Publishes a local tag. |
| `switch_branch` | Checks out an existing local branch. Refused when the work tree is dirty. |

Every tool takes an optional `repo` — a path or a label — and uses the active
repository when it is left out.

## The write gate

**Allow write operations** is off by default, and ticking it is not enough: the
server has to be **registered again**. The flag lives in the registered command
line, not in a file the server re-reads, so a client that was given a read-only
server keeps a read-only server until you deliberately replace it.

`generate_commit_message` is marked read-only but reaches the network, so it is
the one read tool that is not free.

## Registering

| Client | Where |
|---|---|
| Claude Desktop | `claude_desktop_config.json` |
| Claude Code | `claude mcp add`, user scope by default |
| VS Code (GitHub Copilot) | `mcp.json` beside its user settings, in the `servers` shape VS Code reads |
| Antigravity | `~/.gemini/config/mcp_config.json`, or the older `~/.gemini/antigravity/` file when that is the one there |

Each file client is a **merge** that leaves every other setting alone, and a
file that will not parse is refused rather than replaced.

For a client that is not on the list, **Copy command** and **Copy JSON** give
you what to paste.

**Test server** starts it and reports what it answered, which separates "the
server is broken" from "the client is not calling it".
