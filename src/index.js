#!/usr/bin/env node
// antigravity-mcp — expose Google Antigravity (Gemini 3) to Claude Code as an MCP tool.
//
// ask-antigravity / ask-gemini run the REAL `agy` CLI AGENT (via agy_agent.py): the agent has
// Read/Write/Bash tools (like Claude Code), so it can actually touch files/code — not a brain in a
// jar. The agent writes its answer to a temp file which the backend reads back (because `agy -p` is
// silent on a pipe). The old raw-API path (agy_backend.py, streamGenerateContent) is kept ONLY for
// `ping` (fast liveness) and is imported by agy_agent.py for account rotation.
//
// THIN CALLER: this file just spawns the python backend, pipes the prompt over stdin, returns its
// stdout. All smarts (agent run, file I/O, OAuth, proxy, rotation) live in the python layer.
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { spawn } from "node:child_process";
import { readFileSync, unlinkSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const AGENT = path.resolve(__dirname, "..", "agy_agent.py");     // agentic path (ask-*)
const BACKEND = path.resolve(__dirname, "..", "agy_backend.py"); // raw API, used by ping only
const PYTHON = process.env.AGY_PYTHON || "python";
const DEFAULT_MODEL = process.env.AGY_MODEL || "";              // set AGY_MODEL=gemini-pro-agent. "" => agy uses its built-in default = FLASH, NOT Pro (clean MCP profile has no settings.json)!

const log = (...a) => console.error("[antigravity-mcp]", ...a);

/** Spawn a python script, feed `prompt` over stdin, resolve to trimmed stdout. */
function spawnPython(scriptArgs, prompt, onProgress) {
  return new Promise((resolve, reject) => {
    // PYTHONIOENCODING=utf-8 so the backend reads the prompt (and writes its answer) as UTF-8
    // regardless of the Windows console locale — same trick fb2-translator uses for its subprocesses.
    const env = { ...process.env, PYTHONIOENCODING: "utf-8" };
    const child = spawn(PYTHON, scriptArgs, { stdio: ["pipe", "pipe", "pipe"], env });
    let out = "", err = "";
    child.stdout.on("data", (d) => {
      out += d.toString();
      if (onProgress) onProgress(out);
    });
    child.stderr.on("data", (d) => { err += d.toString(); });
    child.on("error", (e) => reject(new Error(`spawn ${PYTHON} failed: ${e.message}`)));
    child.on("close", (code) => {
      if (code === 0) resolve(out.trim());
      else reject(new Error(`backend exited ${code}: ${(err.trim() || "no stderr").slice(-600)}`));
    });
    child.stdin.on("error", () => {});
    child.stdin.write(prompt);
    child.stdin.end();
  });
}

/** Run the agy CLI AGENT (file-in / file-out). */
function runAgent({ prompt, model, system, workspace }, onProgress) {
  const args = [AGENT];
  const m = model || DEFAULT_MODEL;
  if (m) args.push("--model", m);
  if (system) args.push("--system", system);
  if (workspace) args.push("--workspace", workspace);
  return spawnPython(args, prompt, onProgress);
}

/** Run the raw streamGenerateContent backend (used for ping). */
function runRaw({ prompt, model }) {
  return spawnPython([BACKEND, "--model", model || "gemini-pro-agent"], prompt);
}

const stripBom = (s) => (typeof s === "string" ? s.replace(/^﻿/, "") : s);

/**
 * Resolve prompt/system from inline args OR files (a *_file wins over its inline twin). Lets the
 * caller hand a path instead of a wall of text — keeps the caller's context clean. Returns
 * { prompt, system, cleanup:[paths] }; cleanup files are unlinked after the call when args.cleanup.
 */
function resolveInput(args) {
  const cleanup = [];
  let prompt = args.prompt;
  if (args.prompt_file) {
    prompt = stripBom(readFileSync(args.prompt_file, "utf8"));
    if (args.cleanup) cleanup.push(args.prompt_file);
  }
  let system = args.system;
  if (args.system_file) {
    system = stripBom(readFileSync(args.system_file, "utf8"));
    if (args.cleanup) cleanup.push(args.system_file);
  }
  return { prompt, system, cleanup };
}

// Shared schema for ask-antigravity / ask-gemini. `prompt` OR `prompt_file` required (enforced in
// the handler — JSON Schema can't cleanly express "one of two").
const ASK_PROPS = {
  prompt: { type: "string", description: "The full task / question / code for the Antigravity AGENT (Gemini 3, with file tools). For large inputs prefer prompt_file." },
  prompt_file: { type: "string", description: "ABSOLUTE path to a UTF-8 file whose contents become the prompt (the agent receives it via a temp file it reads itself). Use instead of `prompt` for large artifacts. Wins over `prompt`. Must be absolute." },
  model: { type: "string", description: "Model id (default: let agy pick = Gemini 3.1 Pro High). e.g. gemini-pro-agent, gemini-3.1-pro-low, gemini-3-flash." },
  system: { type: "string", description: "Optional system instruction (prepended to the task)." },
  system_file: { type: "string", description: "Optional ABSOLUTE path to a UTF-8 file used as the system instruction. Wins over `system`." },
  workspace: { type: "string", description: "Optional ABSOLUTE dir to grant the agent (--add-dir) so it can READ the repo/code. NOTE: also makes that dir writable to the agent — omit it for read-only review (TB), where all context should be in the prompt instead." },
  cleanup: { type: "boolean", description: "Delete prompt_file / system_file after the call (default false). Leave false in TB — the artifact is shared by all three reviewers." },
};

const TOOLS = [
  {
    name: "ask-antigravity",
    description:
      "Ask the Google Antigravity AGENT (Gemini 3 Pro, free) — a headless coding agent WITH file " +
      "tools (Read/Write/Bash), like a second Claude Code. Hand it a task/question/code to " +
      "analyze, review, or solve. The agent works in an isolated temp dir and returns its answer. " +
      "For large inputs pass an absolute `prompt_file`; to let it read the repo pass `workspace`.",
    inputSchema: { type: "object", properties: ASK_PROPS },
  },
  {
    name: "ask-gemini",
    description: "Alias of ask-antigravity (Gemini 3 agent via Antigravity). Drop-in second-opinion agent.",
    inputSchema: { type: "object", properties: ASK_PROPS },
  },
  {
    name: "ping",
    description: "Health check — verifies the Antigravity backend answers (fast raw-API round-trip, not the agent).",
    inputSchema: { type: "object", properties: { prompt: { type: "string" } } },
  },
];

const server = new Server(
  { name: "antigravity-mcp", version: "1.2.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const name = request.params.name;
  const args = request.params.arguments || {};
  const progressToken = request.params._meta?.progressToken;

  // keep the client alive during long agent runs (TB reviews / big tasks take minutes)
  let progress = 0;
  const ticker = progressToken
    ? setInterval(() => {
        server.notification({
          method: "notifications/progress",
          params: { progressToken, progress: ++progress, message: "🛰️ Antigravity agent is working..." },
        }).catch(() => {});
      }, 25000)
    : null;

  try {
    if (name === "ping") {
      const r = await runRaw({ prompt: args.prompt || "Reply with exactly one word: PONG" });
      return { content: [{ type: "text", text: r || "(empty)" }] };
    }
    if (name === "ask-antigravity" || name === "ask-gemini") {
      let resolved;
      try {
        resolved = resolveInput(args);
      } catch (e) {
        throw new Error(`failed to read prompt_file/system_file: ${e.message}`);
      }
      if (!resolved.prompt || !String(resolved.prompt).trim()) {
        throw new Error("prompt is required — pass `prompt` or a non-empty `prompt_file`");
      }
      try {
        const text = await runAgent({
          prompt: String(resolved.prompt),
          model: args.model,
          system: resolved.system,
          workspace: args.workspace,
        });
        return { content: [{ type: "text", text: `Antigravity (Gemini 3 agent) response:\n${text}` }] };
      } finally {
        for (const f of resolved.cleanup) {
          try { unlinkSync(f); } catch (e) { log("cleanup failed for", f, "-", e.message); }
        }
      }
    }
    throw new Error(`Unknown tool: ${name}`);
  } catch (e) {
    return { content: [{ type: "text", text: `Error: ${e.message}` }], isError: true };
  } finally {
    if (ticker) clearInterval(ticker);
  }
});

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  log("antigravity-mcp listening on stdio; agent:", AGENT, "| raw(ping):", BACKEND);
}
main().catch((e) => { log("fatal:", e); process.exit(1); });
