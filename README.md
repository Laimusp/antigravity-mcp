# antigravity-mcp

An MCP server that exposes the **Google Antigravity (Gemini 3 Pro, free tier)** coding **agent** to
Claude Code — a drop-in "second Claude Code" for review pipelines and second opinions.

> **This is a real agent, not a brain in a jar.** `ask-antigravity` / `ask-gemini` launch the actual
> `agy` CLI agent, which has **Read / Write / Bash tools** just like Claude Code — so it can read
> files, run commands and edit code, not merely emit text. (Earlier versions called the raw
> `streamGenerateContent` API with no tools; that path now survives only behind `ping`.)

## Why an agent, and why files

The official Antigravity CLI (`agy`) runs a TUI language-server. In print mode (`agy -p`) it renders
the answer to the terminal but writes **nothing to stdout when stdout is a pipe** (verified on agy
1.0.9) — so you can't just capture its stdout from an MCP server.

The trick (proven in `fb2-translator`'s engine): hand the agent its task **through a file** and make
it deliver the answer **through a file** it writes itself:

```
prompt ─► temp in.txt
agy.exe -p "read in.txt, do the task, write the answer to out.txt" --dangerously-skip-permissions
out.txt ─► stdout
```

The (possibly huge) user prompt goes in `in.txt`, never in `argv` — no Windows command-line length
limit, no argv injection. The wrapper instruction in `argv` only carries the short fixed text plus
the two temp paths.

## Architecture

```
                          ask-antigravity / ask-gemini  (the AGENT, with file tools)
Claude Code ──MCP(stdio)──► src/index.js ──spawn,stdin──► agy_agent.py ──► agy.exe -p … --dangerously-skip-permissions
                                  │                              └─ file-in/out, isolated temp dir, account rotation
                                  └ ping (fast liveness) ──────► agy_backend.py ──► POST /v1internal:streamGenerateContent (raw, no tools)
```

- **`agy_agent.py`** — the agentic backend. Writes the prompt to `in.txt`, spawns the real `agy` CLI
  agent against it with `--dangerously-skip-permissions`, then reads back `out.txt`. The agent runs
  with `cwd` = a throwaway temp dir and `USERPROFILE`/`HOME` pointed at a **private profile**
  (`~/.agy-mcp-profile`) so its `~/.gemini/antigravity` lock never fights your interactive `agy`.
  Without an explicit `workspace`, the agent can only touch its own `in.txt`/`out.txt` — i.e. it is
  **read-only with respect to your repo**. Account rotation is imported from `agy_backend.py`.
- **`agy_backend.py`** — the raw `streamGenerateContent` caller. Used now for **`ping`** (a fast
  liveness round-trip) and as the home of OAuth + **account rotation** (imported by `agy_agent.py`).
  Reads the Antigravity OAuth token from **Windows Credential Manager** (`gemini:antigravity`),
  refreshes it with gemini-cli's public OAuth client when near expiry, and writes it back so the
  real CLI keeps working.
- **`src/index.js`** — the MCP server (a thin caller). Tools: `ask-antigravity`, `ask-gemini`
  (alias), `ping`. Emits keep-alive progress notifications so long agent runs (TB reviews, big
  tasks — minutes) don't time out.
- **RU geo-unlock**: requests flow through the local proxy at `CLOUD_CODE_URL`
  (default `http://127.0.0.1:9999`, the `agy-fix` proxy) which clears eligibility and routes via an
  EU exit. On a non-blocked network you can point `CLOUD_CODE_URL` straight at Google.

## Tools

### `ask-antigravity` / `ask-gemini`
Hand the Gemini 3 agent a task / question / code to analyze, review, or solve. Parameters:

| param | meaning |
|---|---|
| `prompt` | the full task / question / code (inline) |
| `prompt_file` | **absolute** path to a UTF-8 file used as the prompt (the agent reads it itself). Wins over `prompt`. Use this for large artifacts to keep the caller's context clean. |
| `model` | model id; default = let `agy` pick (set `AGY_MODEL` — see the warning below). |
| `system` / `system_file` | optional system instruction (inline or absolute file; file wins). |
| `workspace` | optional **absolute** dir granted to the agent via `--add-dir` so it can **read** your repo. ⚠️ This also makes that dir **writable** to the agent — omit it for read-only review (e.g. a "Triumvirate" review where all context belongs in the prompt). |
| `cleanup` | delete `prompt_file` / `system_file` after the call (default `false`). |

### `ping`
Health check — a fast **raw-API** round-trip (not the agent) that verifies the backend answers.

## Install

```powershell
npm install
# requires: Python 3 + `requests`; the agy-fix proxy on :9999 (for RU); the `agy` CLI installed
# and logged into an Antigravity account at least once.
```

Register in Claude Code (`.claude.json` → `mcpServers`):

```json
{
  "mcpServers": {
    "antigravity": {
      "type": "stdio",
      "command": "node",
      "args": ["C:\\Users\\user\\antigravity-mcp\\src\\index.js"],
      "env": { "AGY_MODEL": "gemini-pro-agent" }
    }
  }
}
```

Then call `mcp__antigravity__ask-antigravity`.

> ⚠️ **Always set `AGY_MODEL=gemini-pro-agent` in `env`.** The MCP server runs `agy` under an
> isolated profile that has no `settings.json`, so with no model `agy` falls back to its built-in
> default — **Flash, not Pro**. Without this you silently get the weaker model.

## OAuth client

Token refresh uses gemini-cli's **public** installed-app OAuth client (published in
[google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli)). It refreshes the
Antigravity-scoped token because the scope rides the `refresh_token`, not the client — so no secret
setup is needed. Override with `AGY_CLIENT_ID` / `AGY_CLIENT_SECRET` if Google ever rotates it.

## Config (env)

| var | default | meaning |
|---|---|---|
| `AGY_MODEL` | _(unset → Flash!)_ | model id; **set to `gemini-pro-agent`** (= Gemini 3.1 Pro High). |
| `CLOUD_CODE_URL` | `http://127.0.0.1:9999` | proxy/base for the Antigravity backend (RU geo-unlock). |
| `AGY_EXE` | `%LOCALAPPDATA%\agy\bin\agy.exe` | path to the `agy` CLI binary. |
| `AGY_AGENT_PROFILE` | `~/.agy-mcp-profile` | isolated `USERPROFILE`/`HOME` for the agent (own lock). |
| `AGY_AGENT_TIMEOUT` | `900` | seconds before an agent run is killed. |
| `AGY_PYTHON` | `python` | python interpreter for the backends. |
| `AGY_UPSTREAM_PROXY` | _(empty → direct)_ | optional `http://user:pass@host:port` to tunnel the straight-to-Google calls (OAuth refresh + API) when the VPS IP is DPI-throttled. **Keep credentials here in `env`, never in code.** |
| `AGY_MAX_SWAPS` | `3` | account rotations to try on quota errors. |
| `AGY_QUOTA_THRESHOLD` | `10` | proactively switch account when its quota % drops below this (`0` = off). |
| `AGY_PROACTIVE` | `1` | set `0` to disable the proactive pre-check. |

Proactive rotation is also configurable persistently via
[`antigravity-auth-manager`](../antigravity-auth-manager): `agychange config threshold <pct>` →
`~/.gemini/antigravity_config.json`.

## Quota rotation

Lives in `agy_backend.py` (imported by the agent), not the MCP layer:

- **proactive** — before a run, if the active account's quota for the model is below the threshold,
  switch to one that still has quota (cached ~3 min);
- **reactive** — on HTTP 429 / `RESOURCE_EXHAUSTED`, switch and retry.

Both rotate via the sibling [`antigravity-auth-manager`](../antigravity-auth-manager) (`agychange`).

## Caveats

- The agent runs with `--dangerously-skip-permissions` — it **will** execute Bash and write files
  without prompting. It's sandboxed to a throwaway temp `cwd` by default; passing `workspace` opens
  the named dir for **read *and* write**. Don't pass `workspace` for untrusted prompts or read-only
  reviews.
- This relies on the private Antigravity backend (reverse-engineered request shape) and an OAuth
  token meant for the first-party CLI — it can break on Antigravity updates, and heavy/abusive use
  of free accounts carries Google ToS/ban risk. Use disposable accounts and a pool.

Forked conceptually from the `gemini-mcp-tool` pattern.
