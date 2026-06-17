# antigravity-mcp

MCP server that exposes **Google Antigravity (Gemini 3 Pro, free tier)** to Claude Code as a
headless tool — a drop-in "second brain" for review pipelines, replacing a dying gemini-cli MCP.

## Why this is not just `agy -p`

The official Antigravity CLI (`agy`) runs a TUI language-server. In print mode (`agy -p`) it
sends the prompt and **renders the answer to the terminal**, but writes **nothing to stdout when
stdout is a pipe** — so it can't be wrapped by an MCP server. Verified on agy 1.0.9.

Instead, this server talks to the same backend the CLI talks to:

```
Claude Code ──MCP(stdio)──> src/index.js ──spawn,stdin──> agy_backend.py
   └─> POST /v1internal:streamGenerateContent (SSE)  via local proxy  ──> Gemini 3
```

## How it works

- **agy_backend.py** — headless caller. Reads the Antigravity OAuth token from **Windows
  Credential Manager** (`gemini:antigravity`), refreshes it with Antigravity's own OAuth client
  when near expiry (and writes it back so the real CLI keeps working), then POSTs
  `streamGenerateContent` and parses the SSE stream to clean text. Big prompts arrive over stdin
  (no command-line length limit).
- **src/index.js** — MCP server. Tools: `ask-antigravity`, `ask-gemini` (alias), `ping`. Streams
  keep-alive progress so long reviews don't time out.
- **RU geo-unlock**: requests go through the local proxy at `CLOUD_CODE_URL`
  (default `http://127.0.0.1:9999`, the `agy-fix` proxy) which clears eligibility + routes via an
  EU exit. On a non-blocked network you can point `CLOUD_CODE_URL` straight at
  `https://daily-cloudcode-pa.googleapis.com`.
- **Quota rotation**: on HTTP 429 the backend calls the sibling
  [`antigravity-auth-manager`](../antigravity-auth-manager) `agy_switch.py next` and retries.

## Install

```powershell
npm install
# requires: Python 3 + `requests`, the agy-fix proxy running on :9999,
# and an Antigravity account logged into the official CLI at least once.
```

## OAuth client

Token refresh uses gemini-cli's **public** installed-app OAuth client (the same one published in
[google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli)). It refreshes the
Antigravity-scoped token because the scope rides the refresh_token, not the client — so no secret
setup is needed. Override with env `AGY_CLIENT_ID` / `AGY_CLIENT_SECRET` if Google ever rotates it.

Register in Claude Code (`.claude.json` / `mcp` config) as a NEW server (does not touch your
existing gemini server):

```json
{
  "mcpServers": {
    "antigravity": {
      "command": "node",
      "args": ["C:\\Users\\user\\antigravity-mcp\\src\\index.js"]
    }
  }
}
```

Then call `mcp__antigravity__ask-antigravity`.

## Config (env)

| var | default | meaning |
|---|---|---|
| `CLOUD_CODE_URL` | `http://127.0.0.1:9999` | proxy/base for the Antigravity backend |
| `AGY_MODEL` | `gemini-pro-agent` | model id (= Gemini 3.1 Pro High) |
| `AGY_PYTHON` | `python` | python interpreter for the backend |
| `AGY_MAX_SWAPS` | `3` | account rotations to try on quota |

Model ids seen on the account: `gemini-pro-agent`, `gemini-3.1-pro-high`, `gemini-3.1-pro-low`,
`gemini-3-flash`, `claude-opus-4-6-thinking`, `claude-sonnet-4-6`, `gpt-oss-120b-medium`.

## Caveats

This relies on the private Antigravity backend (reverse-engineered request shape) and an OAuth
token meant for the first-party CLI — it can break on Antigravity updates, and heavy/abusive use
of free accounts carries Google ToS/ban risk. Use disposable accounts and a pool. Forked from the
`gemini-mcp-tool` pattern.
