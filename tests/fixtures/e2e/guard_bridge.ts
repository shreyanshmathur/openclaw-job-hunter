// INT end-to-end helper (not a test file): the real jobhunter-guard runtime (openclaw/plugins/jobhunter-guard,
// the same GuardRuntime the plugin registers) driven one JSON line at a time by the e2e tests
// (tests/test_e2e_guard_replay.py, test_e2e_claude_cli_route.py, test_e2e_api_route_mock.py and others).
// The runtime reads the temp install the Python side built (home.json, guard.key, the real acl.json,
// guard-hosts.json, detect files and driver manifest of this repo, and the ledger the real core writes). Its
// jh.py runner only records the calls it would make; the Python side runs them against the real core.
//
// stdin, one JSON object per line; stdout, one JSON answer per line. `ctx` is optional everywhere: the extra
// hook context OpenClaw passes (workspaceDir, cwd, sessionId, runId); agentId and sessionKey come from
// `agent` and `session`.
//   {"op": "init", "config": {...}, "now": "<ts>"}          -> {"ok": true}
//   {"op": "clock", "now": "<ts>"}                           -> {"ok": true}
//   {"op": "call", "agent", "session", "tool", "params", "ctx"}
//        before_tool_call                                    -> {"outcome": "allow" | "pass" | "G_*", "reason", "params"}
//        (a decision that would ask a person, OpenClaw's requireApproval, is the outcome "REQUIRE_APPROVAL")
//   {"op": "exec", "agent", "session", "tool", "params", "ctx"}
//        OpenClaw's exec tool: before_tool_call, then (when not blocked) resolve_exec_env right before the spawn
//                                                            -> {"outcome", "reason", "params", "env": {...} | null}
//   {"op": "result", "agent", "session", "tool", "params", "result", "error", "ctx"} -> {"stopped", "softStopped", "code"}
//   {"op": "env", "agent", "session", "runId"}               -> {"env": {...} | null}
//   {"op": "prompt_tools", "agent", "session"}               -> {"tools": [...] | null}   (before_prompt_build pin)
//   {"op": "heartbeat"}                                      -> {"ok": bool}
//   {"op": "jh_calls"}                                       -> {"calls": [[...argv]]}
//   {"op": "command", "ctx": {...}}                          -> {"text": "..."}   (the /jh chat command, R6)

import readline from "node:readline";
import { GuardRuntime } from "../../../openclaw/plugins/jobhunter-guard/src/runtime.ts";

let runtime: GuardRuntime | null = null;
const clock = { ms: 0 };
const jhCalls: string[][] = [];

function outcome(r: unknown, agent: string): { outcome: string; reason: string | null; params: unknown } {
  const o = (r && typeof r === "object" ? r : {}) as Record<string, unknown>;
  if (o.block === true) {
    const reason = String(o.blockReason || "");
    return { outcome: reason.split(":")[0], reason, params: null };
  }
  // A decision that asks a person (OpenClaw's requireApproval) is never "allow": nobody is there to answer.
  if (o.requireApproval !== undefined && o.requireApproval !== null && o.requireApproval !== false) {
    return { outcome: "REQUIRE_APPROVAL", reason: JSON.stringify(o.requireApproval), params: null };
  }
  return { outcome: agent.startsWith("jobhunter-") ? "allow" : "pass", reason: null, params: o.params ?? null };
}

// The hook ctx: the optional extra fields of the message, then the agent and session it names.
function ctxOf(msg: Record<string, any>): Record<string, any> {
  const extra = msg.ctx && typeof msg.ctx === "object" ? msg.ctx : {};
  return { ...extra, agentId: msg.agent, sessionKey: msg.session };
}

function handle(msg: Record<string, any>): unknown {
  if (msg.op === "init") {
    clock.ms = Date.parse(msg.now);
    const runner = async (argv: string[]) => {
      jhCalls.push(argv);
      return { exitCode: 0, stdout: "", stderr: "", envelope: { ok: true }, error: null };
    };
    runtime = new GuardRuntime(msg.config, { runner, nowMs: () => clock.ms });
    return { ok: true };
  }
  if (!runtime) throw new Error("init first");
  switch (msg.op) {
    case "clock":
      clock.ms = Date.parse(msg.now);
      return { ok: true };
    case "call": {
      return outcome(runtime.evaluate({ toolName: msg.tool, params: msg.params }, ctxOf(msg)), msg.agent);
    }
    case "exec": {
      const ctx = ctxOf(msg);
      const dec = outcome(runtime.evaluate({ toolName: msg.tool, params: msg.params }, ctx), msg.agent);
      if (dec.outcome !== "allow" && dec.outcome !== "pass") return { ...dec, env: null };
      const env = runtime.execEnv({ agentId: ctx.agentId, sessionKey: ctx.sessionKey, runId: ctx.runId });
      return { ...dec, env: env ?? null };
    }
    case "result": {
      const r = runtime.observe({ toolName: msg.tool, params: msg.params, result: msg.result, error: msg.error }, ctxOf(msg));
      return { stopped: r.stopped === true, softStopped: r.softStopped === true, code: r.code ?? null };
    }
    case "env":
      return { env: runtime.execEnv({ agentId: msg.agent, sessionKey: msg.session, runId: msg.runId }) ?? null };
    case "prompt_tools": {
      const r = runtime.promptTools({ agentId: msg.agent, sessionKey: msg.session });
      return { tools: r ? r.toolsAllow : null };
    }
    case "heartbeat":
      return { ok: runtime.heartbeat() };
    case "jh_calls":
      return { calls: jhCalls };
    case "command":
      return runtime.command(msg.ctx || {});
    default:
      throw new Error("unknown op " + String(msg.op));
  }
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
let queue: Promise<void> = Promise.resolve();
rl.on("line", (line: string) => {
  if (!line.trim()) return;
  // answers keep the order of the requests, also for the one asynchronous op (command)
  queue = queue.then(async () => {
    let answer: unknown;
    try {
      answer = await handle(JSON.parse(line));
    } catch (e) {
      answer = { error: (e as Error).message || String(e) };
    }
    process.stdout.write(JSON.stringify(answer) + "\n");
  });
});
rl.on("close", () => {
  void queue.then(() => {
    if (runtime) runtime.close();
    process.exit(0);
  });
});
