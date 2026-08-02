# Chat support: Claude Code conversations anchored to diff lines

Design doc. Nothing here is implemented yet.

## Goal

Click a line in the seendiff UI, start a back-and-forth conversation about it.
The backend is a real Claude Code instance with filesystem access to the repo —
not a chat wrapper around a model endpoint. It can read the surrounding code,
grep the repo, and (later) propose edits.

## Why the Agent SDK

The [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)
(`claude-agent-sdk` on PyPI) is Claude Code packaged as a library. It ships the
same harness: built-in Read/Edit/Bash/Grep/Glob tools, the agent loop, context
management, permissions, MCP, subagents.

It fits seendiff well because seendiff is already a FastAPI server running with
the repo as its working directory — which is exactly the `cwd` an agent session
wants.

Rejected alternative: calling the Messages API directly with hand-rolled tools.
That means reimplementing the file-reading, searching, and editing loop that
Claude Code already does well, and it bills per token (see Auth below).

## Auth: subscription, not API key

**This is the part that determines whether the feature is free or expensive.**

The SDK spawns the `claude` CLI as a subprocess, so it inherits whatever
credentials that CLI has. With an OAuth login (`~/.claude/.credentials.json`)
and no API key in the environment, sessions run against the Claude subscription
— the same as typing in the terminal. Usage counts against subscription rate
limits, not metered API billing.

### The silent-override trap

`ANTHROPIC_API_KEY` in the environment takes precedence over the OAuth profile,
switching to metered billing with no visible signal. Verified on this machine:

```
$ claude auth status --json
{ "loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty",
  "email": "abhishek@currents.ai", "orgName": "Abhishek",
  "subscriptionType": "max" }

$ ANTHROPIC_API_KEY=sk-ant-fake123 claude auth status --json
{ "loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty",
  "apiKeySource": "ANTHROPIC_API_KEY",
  "email": null, "orgName": null, "subscriptionType": null }
```

Note that `authMethod` reads `"claude.ai"` in **both** cases — it does not
distinguish them. The reliable signals are:

| Signal | Subscription | API key |
|---|---|---|
| `apiKeySource` | absent | `"ANTHROPIC_API_KEY"` |
| `subscriptionType` | `"max"` | `null` |
| `email` / `orgName` | populated | `null` |

`ANTHROPIC_AUTH_TOKEN` shadows the OAuth profile the same way and must be
stripped alongside the key.

### Mitigation

Build the subprocess environment explicitly rather than inheriting an ambient
one:

```python
def _agent_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env
```

Pass this as the session's `env` option, and use it for the status call too.

### Visibility

Query auth once at startup and surface it in two places:

```python
def auth_status() -> dict:
    out = subprocess.run(
        ["claude", "auth", "status", "--json"],
        env=_agent_env(), capture_output=True, text=True, timeout=10,
    )
    return json.loads(out.stdout)
```

- **stderr on boot** — `seendiff: agent auth = max subscription (abhishek@currents.ai)`
- **UI badge** in the chat panel header — `max`, or a loud `⚠ API key` state.

Because the key is stripped before spawning, the warning state should be
unreachable. That makes it a real assertion: if it ever fires, something
upstream changed and is worth knowing about.

The same call catches `loggedIn: false`, so an expired token produces
"run `claude auth login`" rather than an opaque subprocess failure. The current
OAuth token's `expiresAt` is ~2026-08-02.

## Architecture

### Session manager — `seendiff/agent.py` (new)

The SDK's `ClaudeSDKClient` is a persistent bidirectional session: connect once,
then send messages and stream responses repeatedly. That maps 1:1 onto "a
conversation attached to a line."

Hold a `thread_id -> client` map. Construct each session with:

- `cwd` = the repo root seendiff already resolved
- a system-prompt append explaining it is embedded in a diff reviewer
- `allowed_tools` — read-only for v0 (see Permissions)
- `env` = `_agent_env()`
- `model` — Sonnet or Haiku, not Opus (see Rate limits)

Sessions are subprocesses. Do **not** create one per HTTP request. Pool them and
reap idle ones; `server.py` already has cache-pruning machinery worth imitating.

### Anchoring a thread to a line

The frontend knows `path`, `hunk_id`, and the line number. The first message
into a fresh session is a synthesized preamble the user never types:

```
File: seendiff/server.py, line 130 (post-change).

Diff context:
<~15 lines of hunk text around the anchor>

User's question: {message}
```

That is the whole trick. The agent has real filesystem access via `cwd`, so it
reads the rest of the file itself. The preamble only has to point at the spot.

### Routes

| Route | Body / params | Returns |
|---|---|---|
| `POST /api/chat` | `{thread_id?, path, hunk_id, line, message}` | `{thread_id}` |
| `GET /api/chat/stream` | `?thread_id=` | SSE event stream |
| `GET /api/chat/threads` | `?path=` | thread list for gutter badges |

The SDK yields typed message objects (assistant messages containing text and
tool-use blocks, plus a terminal result message). Map each to an SSE event so
the UI can render streaming text *and* a live tool-activity line
(`🔧 Read server.py`), the way the Claude Code terminal does.

FastAPI's `StreamingResponse` over an async generator handles this. The existing
routes are sync `def`; mixing sync and async routes is fine.

### Persistence

`store.py` (SQLite) already exists. Key threads by `(path, hunk_id, line)`.
Once stored, a per-line 💬 badge showing thread count is nearly free.

Open question: hunk IDs are content-derived, so they change when the underlying
diff changes. Decide whether threads survive a `refresh()` or are explicitly
point-in-time like the rest of seendiff's state.

## Permissions

The agent can edit files. In a diff reviewer that is a feature — fix the thing
you are reviewing — but it needs a gate.

The SDK supports a pre-tool-use callback: invoked before any tool runs, returns
allow or deny. Wire it to a queue, push a "permission requested" SSE event,
render approve/deny in the UI, resolve the future. Roughly 40 lines, and it is
what makes this feel like Claude Code rather than a chatbot.

**v0 skips this** by passing a read-only `allowed_tools` set (Read, Grep, Glob;
no Bash, no Edit/Write). Add the approval flow in v2 alongside edits.

## Gotchas

- **Stale diffs.** seendiff reviews a point-in-time snapshot. If the agent edits
  files, the displayed diff goes stale. Either refresh after an edit
  (`AppState.refresh()` exists) or keep the agent read-only. Read-only for v0
  sidesteps this entirely.
- **Rate limits, not bills.** The failure mode on a subscription is exhausting
  the usage window early, not a surprise invoice. Reuse sessions rather than
  spawning per message, and pick a cheap model — per-line Q&A does not need
  Opus.
- **Localhost only.** The existing `host_guard` middleware keeps this local,
  which matters when handing an agent access to a repo. Do not relax it.
- **Subprocess lifecycle.** Sessions must be torn down on server shutdown or
  they leak `claude` processes.

## v0 scope

1. Read-only agent (Read/Grep/Glob), one thread per line
2. SSE streaming with tool-activity display
3. Threads persisted in SQLite, gutter badges
4. Auth badge + startup log, API key stripped from the subprocess env

Deferred to v2: edit permissions and the approval UI, diff refresh after edits,
multi-line / whole-hunk threads.

## Before implementing

The SDK is not currently installed in this repo, so the exact option names and
class signatures used above are from documentation memory and have **not** been
verified against the installed package. Confirm against
`code.claude.com/docs/en/agent-sdk` — specifically the session class name, the
`env` / `cwd` / `allowed_tools` / `model` option spellings, the streamed message
types, and the permission-callback signature — before writing code against them.

The architecture and the auth findings above are independent of those details;
the auth behavior was verified directly on this machine and is reproducible with
the commands shown.
