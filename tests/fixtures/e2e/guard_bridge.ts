// INT end-to-end helper (not a test file): the real jobhunter-guard runtime (openclaw/plugins/jobhunter-guard,
// the same GuardRuntime the plugin registers) driven one JSON line at a time by tests/test_e2e_guard_replay.py.
// The runtime reads the temp install the Python side built (home.json, guard.key, the real acl.json,
// guard-hosts.json, detect files and driver manifest of this repo, and the ledger the real core writes). Its
// jh.py runner only records the calls it would make; the Python side runs them against the real core.
//
// stdin, one JSON object per line; stdout, one JSON answer per line:
//   {"op": "init", "config": {...}, "now": "<ts>"}          -> {"ok": true}
//   {"op": "clock", "now": "<ts>"}                           -> {"ok": true}
//   {"op": "call", "agent", "session", "tool", "params"}     -> {"outcome": "allow" | "pass" | "G_*", "reason", "params"}
//   {"op": "result", "agent", "session", "tool", "params", "result", "error"} -> {"stopped", "softStopped", "code"}
//   {"op": "env", "agent", "session", "runId"}               -> {"env": {...} | null}
//   {"op": "heartbeat"}                                      -> {"ok": bool}
//   {"op": "jh_calls"}                                       -> {"calls": [[...argv]]}

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
  return { outcome: agent.startsWith("jobhunter-") ? "allow" : "pass", reason: null, params: o.params ?? null };
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
      const ctx = { agentId: msg.agent, sessionKey: msg.session };
      return outcome(runtime.evaluate({ toolName: msg.tool, params: msg.params }, ctx), msg.agent);
    }
    case "result": {
      const ctx = { agentId: msg.agent, sessionKey: msg.session };
      const r = runtime.observe({ toolName: msg.tool, params: msg.params, result: msg.result, error: msg.error }, ctx);
      return { stopped: r.stopped === true, softStopped: r.softStopped === true, code: r.code ?? null };
    }
    case "env":
      return { env: runtime.execEnv({ agentId: msg.agent, sessionKey: msg.session, runId: msg.runId }) ?? null };
    case "heartbeat":
      return { ok: runtime.heartbeat() };
    case "jh_calls":
      return { calls: jhCalls };
    default:
      throw new Error("unknown op " + String(msg.op));
  }
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on("line", (line: string) => {
  if (!line.trim()) return;
  let answer: unknown;
  try {
    answer = handle(JSON.parse(line));
  } catch (e) {
    answer = { error: (e as Error).message || String(e) };
  }
  process.stdout.write(JSON.stringify(answer) + "\n");
});
rl.on("close", () => {
  if (runtime) runtime.close();
  process.exit(0);
});
