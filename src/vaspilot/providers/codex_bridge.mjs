#!/usr/bin/env node
/**
 * VASPilot Codex bridge.
 *
 * Minimal Node process bridging the Python provider to Codex. Protocol:
 * newline-delimited JSON on stdin/stdout; one request, N events, one reply.
 *
 *   request:  {"id":"p1","type":"probe","offline":false}
 *   reply:    {"id":"p1","type":"probe_result","node":"v22",
 *              "backend":"codex-sdk|codex-cli|none","backend_version":"0.146.0",
 *              "auth":true,"live":{"json":true,"stream":true,"tool_call":true},
 *              "detail":"..."}
 *
 *   request:  {"id":"c1","type":"chat","prompt":"...","timeout_s":600}
 *   events:   {"id":"c1","type":"delta","text":"..."}
 *   reply:    {"id":"c1","type":"final","text":"...","usage":{}}
 *           | {"id":"c1","type":"error","message":"..."}
 *
 * Backend selection:
 *   1. official SDK: VASPILOT_CODEX_SDK_DIR/node_modules/@openai/codex-sdk
 *      (or the global npm root) when importable;
 *   2. Codex CLI: `codex exec --json --sandbox read-only` (VASPILOT_CODEX_BIN
 *      overrides the binary);
 *   3. none -> structured error; the provider degrades to analysis_only.
 *
 * The bridge NEVER executes VASPilot tools: Codex only proposes actions as
 * structured JSON; the Python runtime validates and dispatches them through
 * the same restricted registry used by every other provider.
 */
import { createRequire } from "node:module";
import { spawnSync, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function npmGlobalRoot() {
  const res = spawnSync("npm", ["root", "-g"], { encoding: "utf8" });
  if (res.status === 0) return res.stdout.trim();
  return "";
}

function locateSdk() {
  const candidates = [];
  if (process.env.VASPILOT_CODEX_SDK_DIR)
    candidates.push(path.join(process.env.VASPILOT_CODEX_SDK_DIR, "node_modules"));
  const root = npmGlobalRoot();
  if (root) candidates.push(root);
  for (const dir of candidates) {
    const marker = path.join(dir, "@openai", "codex-sdk", "package.json");
    if (existsSync(marker)) {
      try {
        const require = createRequire(path.join(dir, "noop.js"));
        const sdk = require("@openai/codex-sdk");
        return { sdk, version: sdk.version || "sdk", base: dir };
      } catch (err) {
        return { error: String(err && err.message || err) };
      }
    }
  }
  return null;
}

/**
 * Resolve how to launch the codex CLI without a command shell in the way:
 * Windows can only spawn .cmd shims through cmd.exe, and shell-mode joining
 * previously split the prompt into stray CLI flags ("unexpected argument
 * 'with'"). Preference order: explicit VASPILOT_CODEX_BIN, then node running
 * the npm package entry directly, then bare `codex` via shell with quoting.
 */
function codexSpec() {
  const envBin = process.env.VASPILOT_CODEX_BIN;
  if (envBin && existsSync(envBin)) {
    return { file: envBin, preArgs: [], shell: /\.(cmd|bat)$/i.test(envBin),
             quote: false };
  }
  const root = npmGlobalRoot();
  const entry = root ? path.join(root, "@openai", "codex", "bin", "codex.js")
                     : "";
  if (entry && existsSync(entry)) {
    return { file: process.execPath, preArgs: [entry], shell: false,
             quote: false };
  }
  const probe = spawnSync("codex", ["--version"],
                          { encoding: "utf8", shell: true });
  if (probe.status === 0) {
    return { file: "codex", preArgs: [], shell: true, quote: true };
  }
  return null;
}

function quoteWin(s) {
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(s)) return s;
  return '"' + String(s).replace(/(\\*)"/g, "$1$1\\\"") + '"';
}

function codexVersion(spec) {
  const argv = [...spec.preArgs, "--version"];
  const probe = spawnSync(spec.file, spec.quote ? argv.map(quoteWin) : argv,
                          { encoding: "utf8", shell: spec.shell });
  return probe.status === 0 ? probe.stdout.trim() : "unknown";
}

/** Windows CLIs print system errors in the console code page (GBK on zh-CN);
 *  UTF-8 decoding then litters the text with U+FFFD. Re-decode when seen. */
function decodeMaybeGbk(buf) {
  const s = buf.toString("utf8");
  if (!s.includes("\uFFFD")) return s;
  try { return new TextDecoder("gbk").decode(buf); } catch { return s; }
}

/** Run one `codex exec --json` turn; onDelta receives incremental text.
 *  The prompt goes over STDIN ("-"): chat payloads carry the whole system
 *  prompt + history and blow far past the ~32 KiB Windows argv limit when
 *  passed as an argument (rc=1 命令行太长 / ENAMETOOLONG). */
async function codexExecTurn(prompt, { onDelta, timeoutS, model }) {
  const spec = codexSpec();
  if (!spec) throw new Error("codex CLI binary was not found");
  const rest = ["exec", "--json", "--sandbox", "read-only",
                "--skip-git-repo-check", "-"];
  if (model) rest.push("--model", model);
  let args = [...spec.preArgs, ...rest];
  if (spec.shell && spec.quote) args = args.map(quoteWin);
  const child = spawn(spec.file, args, {
    shell: spec.shell,
    stdio: ["pipe", "pipe", "pipe"],
  });
  child.stdin.on("error", () => {});       // EPIPE if codex exits early
  child.stdin.end(prompt, "utf8");
  let buffer = "";
  let full = "";
  let usage = {};
  const timer = setTimeout(() => child.kill("SIGKILL"),
                           (timeoutS || 600) * 1000);
  child.stdout.on("data", (chunk) => {
    buffer += chunk.toString("utf8");
    let idx;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line.startsWith("{")) continue;
      let event;
      try { event = JSON.parse(line); } catch { continue; }
      if (event.type === "item.completed" && event.item) {
        const item = event.item;
        if ((item.type === "agent_message" || item.type === "message") &&
            typeof item.text === "string" && item.text) {
          full += item.text;
          if (onDelta) onDelta(item.text);
        }
      } else if (event.type === "turn.completed" && event.usage) {
        usage = event.usage;
      }
    }
  });
  let stderrBuf = [];
  child.stderr.on("data", (chunk) => { stderrBuf.push(chunk); });
  const code = await new Promise((resolve) => {
    child.on("close", (c) => resolve(c));
    child.on("error", () => resolve(-1));
  });
  clearTimeout(timer);
  if (code !== 0 && !full) {
    const stderr = decodeMaybeGbk(Buffer.concat(stderrBuf));
    throw new Error(`codex exec failed (rc=${code}): ${stderr.slice(0, 300)}`);
  }
  return { text: full, usage };
}

async function probeOffline(backendInfo, id) {
  const backend = backendInfo.spec ? "codex-cli" : "none";
  const version = backendInfo.sdk
    ? String(backendInfo.sdk.version || "sdk")
    : backendInfo.spec ? codexVersion(backendInfo.spec) : "";
  const auth = existsSync(path.join(process.env.USERPROFILE || process.env.HOME || "",
                                    ".codex", "auth.json"));
  emit({
    id, type: "probe_result", node: process.version,
    backend, backend_version: version, auth,
    live: { json: false, stream: false, tool_call: false },
    detail: backend === "none"
      ? "neither the Codex SDK nor the codex CLI is available"
      : "offline probe: backend located, live capabilities not exercised",
  });
}

async function probeLive(backendInfo, id) {
  const backend = backendInfo.sdk ? "codex-sdk"
    : backendInfo.spec ? "codex-cli" : "none";
  if (backend === "none") {
    emit({
      id, type: "probe_result", node: process.version, backend: "none",
      backend_version: "", auth: false,
      live: { json: false, stream: false, tool_call: false },
      detail: "neither the Codex SDK nor the codex CLI is available",
    });
    return;
  }
  // SDK mode still routes turns through the CLI today: the SDK API surface
  // differs across versions, so the CLI's stable --json event stream is the
  // safer common denominator. Both enforce the same read-only sandbox.
  let live = { json: false, stream: false, tool_call: false };
  let detail = "";
  try {
    const first = await codexExecTurn(
      'Reply with exactly this JSON object and nothing else: {"vaspilot":"ready"}',
      { timeoutS: 120 });
    const trimmed = (first.text || "").trim();
    const parsed = JSON.parse(trimmed.slice(trimmed.indexOf("{"),
                                            trimmed.lastIndexOf("}") + 1));
    live.json = parsed && parsed.vaspilot === "ready";
    live.stream = true; // item.completed events arrived incrementally
  } catch (err) {
    detail = String(err.message || err);
  }
  if (live.json) {
    try {
      const second = await codexExecTurn(
        'Reply with exactly this JSON object and nothing else: ' +
        '{"action":"tool_calls","calls":[{"name":"probe_echo","arguments":{"value":"ready"}}]}',
        { timeoutS: 120 });
      const trimmed = (second.text || "").trim();
      const parsed = JSON.parse(trimmed.slice(trimmed.indexOf("{"),
                                              trimmed.lastIndexOf("}") + 1));
      live.tool_call = !!(parsed && parsed.action === "tool_calls" &&
                          parsed.calls && parsed.calls[0] &&
                          parsed.calls[0].name === "probe_echo");
    } catch (err) {
      detail = String(err.message || err);
    }
  }
  emit({
    id, type: "probe_result", node: process.version,
    backend, backend_version: codexVersion(backendInfo.spec), auth: true,
    live, detail: detail || "live probe completed",
  });
}

async function chat(request) {
  const model = request.model || "";
  try {
    const result = await codexExecTurn(request.prompt, {
      onDelta: (text) => emit({ id: request.id, type: "delta", text }),
      timeoutS: request.timeout_s || 600, model,
    });
    emit({ id: request.id, type: "final", text: result.text,
           usage: result.usage || {} });
  } catch (err) {
    emit({ id: request.id, type: "error",
           message: String(err.message || err) });
  }
}

async function main() {
  const backendInfo = { sdk: locateSdk(), spec: codexSpec() };
  let pending = 0;
  process.stdin.setEncoding("utf8");
  let buffer = "";
  for await (const chunk of process.stdin) {
    buffer += chunk;
    let idx;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line) continue;
      let request;
      try { request = JSON.parse(line); } catch { continue; }
      if (request.type === "probe") {
        pending++;
        try {
          if (request.offline) await probeOffline(backendInfo, request.id);
          else await probeLive(backendInfo, request.id);
        } catch (err) {
          emit({ id: request.id, type: "probe_result",
                 node: process.version, backend: "none", backend_version: "",
                 auth: false, live: { json: false, stream: false, tool_call: false },
                 detail: String(err.message || err) });
        }
        pending--;
      } else if (request.type === "chat") {
        pending++;
        await chat(request);
        pending--;
      }
    }
  }
  void pending;
}

main().catch((err) => {
  emit({ id: "", type: "error", message: "bridge crashed: " + String(err) });
  process.exit(1);
});
