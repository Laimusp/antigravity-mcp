#!/usr/bin/env node
// antigravity-mcp — expose Google Antigravity (Gemini 3 Pro) to Claude Code as an MCP tool.
//
// THIN CALLER ONLY. This file has ZERO rotation / quota / auth logic. It just spawns
// agy_backend.py, pipes the prompt in over STDIN, and returns its stdout. All the smarts
// (OAuth from Credential Manager, refresh, proactive + reactive account rotation) live in
// agy_backend.py — exactly like jamubc's gemini-mcp called `gemini -p` while gchange handled
// rotation outside it. Same pattern, just agy_backend.py in place of the gemini CLI.
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BACKEND = path.resolve(__dirname, "..", "agy_backend.py");
const PYTHON = process.env.AGY_PYTHON || "python";
const DEFAULT_MODEL = process.env.AGY_MODEL || "gemini-pro-agent";

const log = (...a) => console.error("[antigravity-mcp]", ...a);

/** Run agy_backend.py, feeding `prompt` over stdin, returning trimmed stdout. */
function runBackend({ prompt, model, system, thinking }, onProgress) {
  return new Promise((resolve, reject) => {
    const args = [BACKEND, "--model", model || DEFAULT_MODEL];
    if (system) args.push("--system", system);
    if (thinking) args.push("--thinking");
    const child = spawn(PYTHON, args, { stdio: ["pipe", "pipe", "pipe"] });
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

const TOOLS = [
  {
    name: "ask-antigravity",
    description:
      "Ask Google Antigravity (Gemini 3 Pro, free) a question or hand it code to analyze/review. " +
      "Headless second-opinion brain for Claude Code. Returns Antigravity's text answer.",
    inputSchema: {
      type: "object",
      properties: {
        prompt: { type: "string", description: "The full prompt / question / code to send to Antigravity (Gemini 3)." },
        model: { type: "string", description: "Model id (default gemini-pro-agent = Gemini 3.1 Pro High)." },
        system: { type: "string", description: "Optional system instruction." },
        thinking: { type: "boolean", description: "Enable model thinking budget (default false)." },
      },
      required: ["prompt"],
    },
  },
  // alias kept identical so it can act as a drop-in for a gemini-mcp `ask-gemini` slot
  {
    name: "ask-gemini",
    description: "Alias of ask-antigravity (Gemini 3 Pro via Antigravity). Drop-in second-opinion tool.",
    inputSchema: {
      type: "object",
      properties: {
        prompt: { type: "string", description: "The full prompt / question / code." },
        model: { type: "string", description: "Model id (default gemini-pro-agent)." },
        system: { type: "string", description: "Optional system instruction." },
        thinking: { type: "boolean", description: "Enable thinking (default false)." },
      },
      required: ["prompt"],
    },
  },
  {
    name: "ping",
    description: "Health check — verifies the Antigravity backend answers (round-trips one token).",
    inputSchema: { type: "object", properties: { prompt: { type: "string" } } },
  },
];

const server = new Server(
  { name: "antigravity-mcp", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const name = request.params.name;
  const args = request.params.arguments || {};
  const progressToken = request.params._meta?.progressToken;

  // keep the client alive during long Antigravity calls (TB reviews can take minutes)
  let progress = 0;
  const ticker = progressToken
    ? setInterval(() => {
        server.notification({
          method: "notifications/progress",
          params: { progressToken, progress: ++progress, message: "🛰️ Antigravity is thinking..." },
        }).catch(() => {});
      }, 25000)
    : null;

  try {
    if (name === "ping") {
      const r = await runBackend({ prompt: args.prompt || "Reply with exactly one word: PONG" });
      return { content: [{ type: "text", text: r || "(empty)" }] };
    }
    if (name === "ask-antigravity" || name === "ask-gemini") {
      if (!args.prompt || !String(args.prompt).trim()) {
        throw new Error("prompt is required");
      }
      const text = await runBackend({
        prompt: String(args.prompt),
        model: args.model,
        system: args.system,
        thinking: !!args.thinking,
      });
      return { content: [{ type: "text", text: `Antigravity (Gemini 3) response:\n${text}` }] };
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
  log("antigravity-mcp listening on stdio; backend:", BACKEND);
}
main().catch((e) => { log("fatal:", e); process.exit(1); });
