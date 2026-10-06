// Runtime tests against a temporary install: recorded transcripts replayed through the guard, the
// token log (12.17), the heartbeat (12.18), the stop file and jh.py detect, exec env and /jh.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createHmac } from "node:crypto";
import { makeInstall, replay, writeConsent, INSTALL_ID, KEY_HEX } from "./_install.ts";
import { GuardRuntime, expandGlob, parseConfig, manifestHashes, realpathLoose, stopPlatform, strayRunNotice, strayRunTrigger, textWindow } from "../src/runtime.ts";
import { parseKey, sessionHash, verifyArgvProof, verifyEnvProof, verifyGrant } from "../src/grant.ts";
import { DRIVER_TEXT, PY, REPO, TOKEN } from "./_helpers.ts";

function readJsonl(file: string): Record<string, unknown>[] {
  return fs.readFileSync(file, "utf8").split("\n").filter((l) => l.trim()).map((l) => JSON.parse(l));
}

test("transcript: a checkpoint page in a snapshot trips detect and stops the session", () => {
  const inst = makeInstall();
  try {
    replay("transcript-scout-stop.json", inst);
    const stops = fs.readdirSync(path.join(inst.root, "state", "guard")).filter((n) => n.startsWith("stop-"));
    assert.equal(stops.length, 1);
    const payload = JSON.parse(fs.readFileSync(path.join(inst.root, "state", "guard", stops[0]), "utf8"));
    assert.equal(payload.platform, "linkedin");
    assert.equal(payload.url, "https://www.linkedin.com/checkpoint/challenge/AgFexample");
    assert.match(payload.text, /security check/);
    assert.deepEqual(Object.keys(payload).sort(), ["http_status", "platform", "text", "title", "url"]);
    const call = inst.jhCalls.find((a) => a[0] === "detect");
    assert.deepEqual(call, ["detect", "--file", path.join(inst.root, "state", "guard", stops[0]), "--source", "guard"]);
    const log = readJsonl(path.join(inst.root, "logs", "guard-2026-09.jsonl"));
    assert.ok(log.some((e) => e.kind === "stop" && e.code === "li_checkpoint" && e.where === "url"));
    assert.ok(log.some((e) => e.kind === "decision" && e.code === "G_STOPPED"));
    for (const e of log) assert.equal(JSON.stringify(e).includes("security check"), false, "guard log carries no page text");
  } finally {
    inst.cleanup();
  }
});

test("transcript: applier form under a token writes the token log", () => {
  const inst = makeInstall();
  try {
    replay("transcript-applier-form.json", inst);
    const lines = readJsonl(path.join(inst.root, "state", "guard", TOKEN + ".jsonl"));
    assert.deepEqual(lines.map((l) => [l.class, l.action]), [["fill", "type"], ["fill", "upload"], ["commit", "click"], ["commit", "press"]]);
    for (const l of lines) {
      assert.equal(l.token, TOKEN);
      assert.equal(l.agent, "jobhunter-applier");
      assert.equal(l.host, "job-boards.greenhouse.io");
      assert.match(String(l.ts), /^2026-09-27T05:0\d:\d\dZ$/);
      assert.deepEqual(Object.keys(l).sort(), ["action", "agent", "class", "host", "name", "ref", "role", "token", "ts"]);
    }
    assert.equal(lines[2].name, "Submit application");
    assert.equal(lines[2].role, "button");
  } finally {
    inst.cleanup();
  }
});

test("transcript: bypass attempts by the outreach agent and main", () => {
  const inst = makeInstall();
  try {
    replay("transcript-outreach-bypass.json", inst);
    assert.equal(fs.existsSync(path.join(inst.root, "state", "guard", TOKEN + ".jsonl")), false);
  } finally {
    inst.cleanup();
  }
});

test("transcript: claude-cli restricted runs (bridged OpenClaw tools)", () => {
  const inst = makeInstall();
  try {
    replay("transcript-claude-cli-restricted.json", inst);
  } finally {
    inst.cleanup();
  }
});

test("transcript: claude-cli native tool shapes (deny mode, then mode N)", () => {
  const inst = makeInstall();
  try {
    replay("transcript-claude-cli-native.json", inst);
  } finally {
    inst.cleanup();
  }
});

test("commit budget survives a guard restart (counted from the token log)", () => {
  const inst = makeInstall();
  try {
    replay("transcript-applier-form.json", inst);
    const runner = async () => ({ exitCode: 0, stdout: "", stderr: "", envelope: null, error: null });
    const rt2 = new GuardRuntime(inst.runtime.config, { runner, nowMs: () => inst.now.ms });
    rt2.observe(
      { toolName: "browser", params: { action: "snapshot" }, result: { content: [{ type: "text", text: "- button \"Submit application\" [ref=e6]" }], details: { targetId: "t9", url: "https://job-boards.greenhouse.io/kestrel/jobs/4000001" } } },
      { agentId: "jobhunter-applier", sessionKey: "s-new" },
    );
    const r = rt2.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "click", ref: "e6" } }, { agentId: "jobhunter-applier", sessionKey: "s-new" });
    assert.match(String(r && (r as { blockReason?: string }).blockReason), /^G_COMMIT_BUDGET/);
    rt2.close();
  } finally {
    inst.cleanup();
  }
});

test("health: install_id mismatch, missing key or missing detect files block every jobhunter tool", () => {
  const cases: Array<(root: string) => void> = [
    (root) => fs.writeFileSync(path.join(root, "private", "home.json"), JSON.stringify({ install_id: "IZZZZZZZZ", repo: root, db_path: path.join(root, "state", "jobhunter.sqlite3"), ws_root: path.join(root, "ws") })),
    (root) => fs.rmSync(path.join(root, "private", "guard.key")),
    (root) => fs.writeFileSync(path.join(root, "private", "guard.key"), "short"),
    (root) => fs.rmSync(path.join(root, "scripts", "jobhunter", "detect"), { recursive: true }),
    (root) => fs.writeFileSync(path.join(root, "scripts", "jobhunter", "acl.json"), "{"),
    (root) => fs.rmSync(path.join(root, "state", "jobhunter.sqlite3")),
  ];
  for (const breakIt of cases) {
    const inst = makeInstall();
    try {
      breakIt(inst.root);
      const r = inst.runtime.evaluate({ toolName: "exec", params: { command: "x" } }, { agentId: "jobhunter-scout", sessionKey: "s" });
      assert.match(String(r && (r as any).blockReason), /^G_GUARD_UNHEALTHY/, breakIt.toString());
      assert.equal(inst.runtime.heartbeat(), false);
      assert.equal(fs.existsSync(path.join(inst.root, "state", "guard", "heartbeat.json")), false);
      // other agents are not affected
      assert.equal(inst.runtime.evaluate({ toolName: "exec", params: { command: "ls" } }, { agentId: "main" }), undefined);
    } finally {
      inst.cleanup();
    }
  }
});

test("heartbeat pin_hook_seen_at (D10): null until OpenClaw calls before_prompt_build, then the last call", () => {
  const inst = makeInstall();
  try {
    const rt = inst.runtime;
    const read = () => JSON.parse(fs.readFileSync(path.join(inst.root, "state", "guard", "heartbeat.json"), "utf8"));
    inst.now.ms = Date.parse("2026-09-27T05:01:00Z");
    assert.equal(rt.heartbeat(), true);
    assert.equal(read().pin_hook_seen_at, null); // a blocked hook (no allowConversationAccess grant) stays here
    inst.now.ms = Date.parse("2026-09-27T05:02:00Z");
    rt.promptTools({ agentId: "main", sessionKey: "agent:main:main", trigger: "user" }); // any agent counts
    inst.now.ms = Date.parse("2026-09-27T05:03:00Z");
    assert.equal(rt.heartbeat(), true);
    assert.equal(read().pin_hook_seen_at, "2026-09-27T05:02:00Z");
    inst.now.ms = Date.parse("2026-09-27T05:04:00Z");
    rt.promptTools({ agentId: "jobhunter-evaluator", sessionKey: "agent:jobhunter-evaluator:cron:x", trigger: "cron" });
    assert.equal(rt.heartbeat(), true);
    assert.equal(read().pin_hook_seen_at, "2026-09-27T05:04:00Z");
    // the pin switched off still notes the call: the field says whether OpenClaw runs the hook, not the setting
    const off = new GuardRuntime({ ...rt.config, pinToolSurface: false }, { nowMs: () => inst.now.ms });
    inst.now.ms = Date.parse("2026-09-27T05:05:00Z");
    assert.equal(off.promptTools({ agentId: "jobhunter-evaluator", trigger: "user" }), undefined);
    assert.equal(off.heartbeat(), true);
    const hb = read();
    assert.equal(hb.pin_tool_surface, false);
    assert.equal(hb.pin_hook_seen_at, "2026-09-27T05:05:00Z");
  } finally {
    inst.cleanup();
  }
});

test("heartbeat file (12.18)", () => {
  const inst = makeInstall();
  try {
    inst.now.ms = Date.parse("2026-09-27T05:10:00Z");
    assert.equal(inst.runtime.heartbeat(), true);
    const hb = JSON.parse(fs.readFileSync(path.join(inst.root, "state", "guard", "heartbeat.json"), "utf8"));
    assert.equal(hb.install_id, INSTALL_ID);
    assert.equal(hb.version, "2.2.0");
    assert.equal(hb.guard_version, "2.2.0");
    assert.equal(hb.proof_version, 2);
    assert.deepEqual(hb.carriers, ["argv", "env"]);
    assert.equal(hb.native_tools, "deny");
    assert.equal(hb.pin_tool_surface, true);
    assert.equal(hb.pin_hook_seen_at, null);
    assert.equal(hb.loaded_at, "2026-09-27T05:00:00Z");
    assert.equal(hb.beat_at, "2026-09-27T05:10:00Z");
    assert.match(hb.acl_sha256, /^[0-9a-f]{64}$/);
    assert.match(hb.hosts_sha256, /^[0-9a-f]{64}$/);
  } finally {
    inst.cleanup();
  }
});

test("failed jh.py detect is retried on the heartbeat", async () => {
  const inst = makeInstall({ jhExit: 1 });
  try {
    const r = inst.runtime.observe(
      { toolName: "browser", params: { action: "navigate", targetUrl: "https://www.linkedin.com/feed/" }, result: { content: [{ type: "text", text: "x" }], details: { url: "https://www.linkedin.com/authwall?trk=1" } } },
      { agentId: "jobhunter-outreach", sessionKey: "s1" },
    );
    assert.equal(r.stopped, true);
    await new Promise((res) => setTimeout(res, 10));
    assert.equal(inst.jhCalls.length, 1);
    inst.runtime.heartbeat();
    await new Promise((res) => setTimeout(res, 10));
    assert.equal(inst.jhCalls.length, 2);
    assert.equal(inst.jhCalls[1][0], "detect");
  } finally {
    inst.cleanup();
  }
});

test("exec environment (R8)", () => {
  const inst = makeInstall();
  try {
    const env = inst.runtime.execEnv({ agentId: "jobhunter-applier", sessionKey: "agent:jobhunter-applier:cron:a", runId: "r1" });
    assert.equal(env?.JH_AGENT_ID, "jobhunter-applier");
    assert.equal(env?.JH_SESSION_KEY, "agent:jobhunter-applier:cron:a");
    assert.equal(env?.JH_RUN_ID, "r1");
    const proof = String(env?.JH_AGENT_PROOF);
    const nowS = Math.floor(inst.now.ms / 1000);
    const parts = verifyEnvProof(parseKey(KEY_HEX), proof, "agent:jobhunter-applier:cron:a", nowS);
    assert.ok(parts, proof);
    assert.equal(parts!.agent, "jobhunter-applier");
    assert.equal(parts!.sk, sessionHash("agent:jobhunter-applier:cron:a"));
    const fields = proof.split(".");
    assert.equal(fields.length, 6);
    assert.equal(fields[0], "jhe2");
    assert.equal(fields[1], "jobhunter-applier");
    const want = createHmac("sha256", parseKey(KEY_HEX)).update("jh-agent-env\n2\njobhunter-applier\n" + parts!.ts + "\n" + parts!.nonce + "\n" + parts!.sk).digest("hex");
    assert.ok(proof.endsWith("." + want));
    // a fresh nonce on every exec (single use in jh.py)
    const again = inst.runtime.execEnv({ agentId: "jobhunter-applier", sessionKey: "agent:jobhunter-applier:cron:a", runId: "r1" });
    assert.notEqual(again?.JH_AGENT_PROOF, proof);
    assert.equal(inst.runtime.execEnv({ agentId: "main" }), undefined);
    assert.equal(inst.runtime.execEnv({ sessionKey: "agent:jobhunter-scout:x" })?.JH_AGENT_ID, "jobhunter-scout");
    // the argv carrier alone: no env proof
    const argvOnly = new GuardRuntime({ ...inst.runtime.config, proofCarriers: ["argv"] }, { nowMs: () => inst.now.ms });
    const e2 = argvOnly.execEnv({ agentId: "jobhunter-applier", sessionKey: "s" });
    assert.equal(e2?.JH_AGENT_ID, "jobhunter-applier");
    assert.equal(e2?.JH_AGENT_PROOF, undefined);
    argvOnly.close();
  } finally {
    inst.cleanup();
  }
});

test("/jh through the runtime: fallback owner, grant and refusal", async () => {
  const inst = makeInstall({ ownerFallback: [{ channel: "whatsapp", senderId: "+10000000000" }] });
  try {
    const out = await inst.runtime.command({ channel: "whatsapp", senderId: "+10000000000", args: "pause linkedin", isAuthorizedSender: true });
    assert.equal(out.text, "OK from fake jh.py");
    const argv = inst.jhCalls[0];
    assert.deepEqual(argv.slice(2), ["--human", "pause", "--scope", "linkedin"]);
    assert.equal(verifyGrant(parseKey(KEY_HEX), argv[1], "pause", ["--scope", "linkedin"], Math.floor(inst.now.ms / 1000)), true);
    const refused = await inst.runtime.command({ channel: "whatsapp", senderId: "stranger-3", args: "approve A7K9" });
    assert.match(refused.text, /^G_NOT_OWNER/);
    assert.equal(inst.jhCalls.length, 1);
  } finally {
    inst.cleanup();
  }
});

test("argv proof through the runtime: a jh.py exec gets -I and a proof bound to its arguments", () => {
  const inst = makeInstall();
  try {
    const jh = inst.root + "/scripts/jh.py";
    const ctx = { agentId: "jobhunter-evaluator", sessionKey: "agent:jobhunter-evaluator:cron:onboard-profile" };
    const file = path.join(inst.ws, "evaluator", "work", "onboarding", "profile_inference.json");
    const r = inst.runtime.evaluate({ toolName: "mcp__openclaw__exec", params: { command: PY + " " + jh + " profile infer-record --file " + file } }, ctx) as { params: Record<string, unknown> };
    assert.ok(r && r.params, JSON.stringify(r));
    const toks = String(r.params.command).split(" ");
    assert.deepEqual(toks.slice(0, 4), [PY, "-I", jh, "--agent-proof"]);
    const rest = toks.slice(5);
    assert.deepEqual(rest, ["profile", "infer-record", "--file", file]);
    const parts = verifyArgvProof(parseKey(KEY_HEX), toks[4], rest, Math.floor(inst.now.ms / 1000));
    assert.ok(parts);
    assert.equal(parts!.agent, "jobhunter-evaluator");
    assert.equal(parts!.sk, sessionHash(ctx.sessionKey));
    assert.equal(r.params.workdir, path.join(inst.ws, "evaluator", "work"));
    assert.equal(r.params.timeoutSeconds, 90);
    // the host decides the rewritten call again: the own proof is replaced by a fresh one
    const again = inst.runtime.evaluate({ toolName: "mcp__openclaw__exec", params: r.params }, ctx) as { params: Record<string, unknown> };
    const t2 = String(again.params.command).split(" ");
    assert.equal(t2.filter((t) => t === "--agent-proof").length, 1);
    assert.notEqual(t2[4], toks[4]);
    // the scout cannot replay the evaluator's rewritten command
    const scout = inst.runtime.evaluate({ toolName: "mcp__openclaw__exec", params: { command: r.params.command } }, { agentId: "jobhunter-scout", sessionKey: "agent:jobhunter-scout:cron:x" }) as { blockReason?: string };
    assert.match(String(scout.blockReason), /^G_EXEC_PARAM/);
    // a copy older than the TTL is refused too
    inst.now.ms += 121000;
    const late = inst.runtime.evaluate({ toolName: "mcp__openclaw__exec", params: r.params }, ctx) as { blockReason?: string };
    assert.match(String(late.blockReason), /^G_EXEC_PARAM/);
  } finally {
    inst.cleanup();
  }
});

test("native tools of a jobhunter agent: refused and written to the guard log as native_tool", () => {
  const inst = makeInstall();
  try {
    const ctx = { agentId: "jobhunter-scout", sessionKey: "agent:jobhunter-scout:main" };
    const r = inst.runtime.evaluate({ toolName: "Bash", params: { command: PY + " " + inst.root + "/scripts/jh.py home show" } }, ctx) as { blockReason?: string };
    assert.equal(r.blockReason, "G_TOOL_DENIED: Claude Code tools are off for jobhunter agents; this run is not restricted (see doctor)");
    const r2 = inst.runtime.evaluate({ toolName: "read", params: { file_path: path.join(inst.ws, "scout", "AGENTS.md"), path: path.join(inst.ws, "scout", "AGENTS.md") } }, ctx) as { blockReason?: string };
    assert.match(String(r2.blockReason), /^G_TOOL_DENIED: Claude Code tools are off/);
    // main's native tools are not logged as native_tool
    inst.runtime.evaluate({ toolName: "Bash", params: { command: "ls" } }, { agentId: "main" });
    const log = readJsonl(path.join(inst.root, "logs", "guard-2026-09.jsonl"));
    const native = log.filter((e) => e.kind === "native_tool");
    assert.equal(native.length, 2);
    assert.deepEqual(native.map((e) => [e.agent, e.tool_raw, e.tool, e.decision, e.code]), [
      ["jobhunter-scout", "Bash", "exec", "block", "G_TOOL_DENIED"],
      ["jobhunter-scout", "read", "read", "block", "G_TOOL_DENIED"],
    ]);
  } finally {
    inst.cleanup();
  }
});

test("stray runs (D10): a jobhunter run not started by cron gets the pin, the notice and a stray_run log line", () => {
  const inst = makeInstall();
  try {
    const rt = inst.runtime;
    // cron runs and runs without a trigger: the pin only, nothing logged
    assert.deepEqual(rt.promptTools({ agentId: "jobhunter-evaluator", sessionKey: "agent:jobhunter-evaluator:cron:onboard-profile", trigger: "cron" }), { toolsAllow: ["exec", "read", "write"] });
    assert.deepEqual(rt.promptTools({ agentId: "jobhunter-evaluator", sessionKey: "agent:jobhunter-evaluator:cron:x", trigger: " CRON " }), { toolsAllow: ["exec", "read", "write"] });
    assert.deepEqual(rt.promptTools({ agentId: "jobhunter-qc", sessionKey: "agent:jobhunter-qc:cron:q" }), { toolsAllow: [] });
    // `openclaw agent --agent jobhunter-evaluator` (trigger user) and a heartbeat run
    const stray = rt.promptTools({ agentId: "jobhunter-evaluator", sessionKey: "agent:jobhunter-evaluator:main", trigger: "user" })!;
    assert.deepEqual(stray.toolsAllow, ["exec", "read", "write"]);
    assert.equal(stray.prependSystemContext, strayRunNotice("jobhunter-evaluator", "user"));
    assert.match(String(stray.prependSystemContext), /^JOBHUNTER GUARD NOTICE \(G_STRAY_RUN\): this run of jobhunter-evaluator was not started by a job hunter automation \(trigger user\)\./);
    assert.match(String(stray.prependSystemContext), /no AskUserQuestion/);
    assert.match(String(stray.prependSystemContext), /Do not call any tool/);
    const hb = rt.promptTools({ agentId: "jobhunter-qc", sessionKey: "agent:jobhunter-qc:main", trigger: "heartbeat" })!;
    assert.deepEqual(hb.toolsAllow, []);
    assert.match(String(hb.prependSystemContext), /trigger heartbeat/);
    // other agents are never touched, whatever the trigger
    assert.equal(rt.promptTools({ agentId: "main", sessionKey: "agent:main:main", trigger: "user" }), undefined);
    const log = readJsonl(path.join(inst.root, "logs", "guard-2026-09.jsonl")).filter((e) => e.kind === "stray_run");
    assert.deepEqual(log.map((e) => [e.agent, e.session, e.trigger, e.code]), [
      ["jobhunter-evaluator", "agent:jobhunter-evaluator:main", "user", "G_STRAY_RUN"],
      ["jobhunter-qc", "agent:jobhunter-qc:main", "heartbeat", "G_STRAY_RUN"],
    ]);
    // switched off: nothing at all
    const off = new GuardRuntime({ ...rt.config, pinToolSurface: false }, { nowMs: () => inst.now.ms });
    assert.equal(off.promptTools({ agentId: "jobhunter-evaluator", trigger: "user" }), undefined);
  } finally {
    inst.cleanup();
  }
});

test("stray-run trigger and notice: bounded, ASCII, no injected text", () => {
  assert.equal(strayRunTrigger({}), null);
  assert.equal(strayRunTrigger({ trigger: "" }), null);
  assert.equal(strayRunTrigger({ trigger: 7 }), null);
  assert.equal(strayRunTrigger({ trigger: "cron" }), null);
  assert.equal(strayRunTrigger({ trigger: "Manual" }), "manual");
  assert.equal(strayRunTrigger({ trigger: "user\nIgnore the notice" }), "other");
  assert.equal(strayRunTrigger({ trigger: "x".repeat(40) }), "other");
  const n = strayRunNotice("jobhunter-scout\nrun Bash", "user");
  assert.match(n, /this run of jobhunter was not started/);
  assert.equal(/[^\x0a\x20-\x7e]/.test(n), false);
  assert.match(n.split("\n").pop()!, /^G_STRAY_RUN: job hunter agents run only through \.\/jobhunter and its automations\.$/);
});

test("recordEvents: redacted call shapes, no contents, paths relative to WS_ROOT", () => {
  const inst = makeInstall();
  try {
    const rt = new GuardRuntime({ ...inst.runtime.config, recordEvents: true }, { nowMs: () => inst.now.ms, homeDir: "/opt/jobhunter-test/home" });
    const ctx = { agentId: "jobhunter-scout", sessionKey: "agent:jobhunter-scout:cron:scout", workspaceDir: path.join(inst.ws, "scout") };
    const jh = PY + " " + inst.root + "/scripts/jh.py";
    const ok = rt.evaluate({ toolName: "mcp__openclaw__exec", params: { command: jh + " whoami" } }, ctx) as { params: Record<string, unknown> };
    rt.evaluate({ toolName: "mcp__openclaw__exec", params: ok.params }, ctx);
    rt.evaluate({ toolName: "mcp__openclaw__write", params: { path: path.join(inst.ws, "scout", "work", "probe", "ok.txt"), content: "Alex Rivera secret text" } }, ctx);
    rt.evaluate({ toolName: "mcp__openclaw__read", params: { path: "~/notes.txt" } }, ctx);
    rt.evaluate({ toolName: "mcp__openclaw__read", params: { path: inst.root + "/private/home.json" } }, ctx);
    rt.evaluate({ toolName: "mcp__openclaw__exec", params: { command: "echo JH_AGENT_PROOF=jhe2.jobhunter-scout.1790000000.0123456789abcdef.0123456789abcdef." + "b".repeat(64) } }, { agentId: "main" });
    rt.close();
    const file = path.join(inst.root, "logs", "guard-events-20260927.jsonl");
    const lines = readJsonl(file);
    assert.equal(lines.length, 6);
    for (const l of lines) {
      assert.deepEqual(Object.keys(l).sort(), ["agent", "at", "code", "command_redacted", "ctx_keys", "decision", "native", "param_keys", "path_rel_ws", "tool", "tool_raw"]);
    }
    assert.equal(lines[0].command_redacted, "@PY@ @REPO@/scripts/jh.py whoami");
    assert.match(String(lines[1].command_redacted), /^@PY@ -I @REPO@\/scripts\/jh\.py --agent-proof jhp2\.jobhunter-scout\.REDACTED whoami$/);
    assert.deepEqual(lines[1].param_keys, ["command", "timeoutSeconds", "workdir"]);
    assert.deepEqual(lines[1].ctx_keys, ["agentId", "sessionKey", "workspaceDir"]);
    assert.equal(lines[2].path_rel_ws, "scout/work/probe/ok.txt");
    assert.equal(lines[2].decision, "allow");
    assert.equal(lines[3].path_rel_ws, "<form>");
    assert.equal(lines[3].code, "G_PATH_DENIED");
    assert.equal(lines[4].path_rel_ws, "<outside>");
    assert.equal(lines[5].command_redacted, "echo JH_AGENT_PROOF=jhe2.jobhunter-scout.REDACTED");
    assert.equal(lines[5].tool_raw, "mcp__openclaw__exec");
    const text = fs.readFileSync(file, "utf8");
    assert.equal(text.includes("Alex Rivera"), false, "contents are never recorded");
    assert.equal(text.includes(inst.root), false, "absolute install paths are replaced");
    assert.equal(/jh[pe]2\.jobhunter-[a-z]+\.[0-9]/.test(text), false, "no proof survives");
    // off by default
    assert.equal(fs.readdirSync(path.join(inst.root, "logs")).filter((n) => n.startsWith("guard-events-")).length, 1);
    inst.runtime.evaluate({ toolName: "mcp__openclaw__exec", params: { command: jh + " whoami" } }, ctx);
    assert.equal(readJsonl(file).length, 6);
  } finally {
    inst.cleanup();
  }
});

test("R7 with the real file system: protected roots, glob expansion and links", () => {
  const inst = makeInstall();
  try {
    const home = path.join(inst.root, "home");
    const oc = path.join(home, ".openclaw");
    fs.mkdirSync(path.join(oc, "workspace"), { recursive: true });
    fs.writeFileSync(path.join(oc, "openclaw.json"), "{}");
    fs.writeFileSync(path.join(oc, "workspace", "notes.md"), "x");
    fs.mkdirSync(path.join(home, "notes"), { recursive: true });
    fs.symlinkSync(path.join(inst.root, "private"), path.join(home, "notes", "p"));
    const cfg = { ...inst.runtime.config, protectedRoots: { read: [path.join(inst.root, "private"), oc], write: [inst.root, inst.ws, oc] } };
    const rt = new GuardRuntime(cfg, { nowMs: () => inst.now.ms, homeDir: home });
    const main = { agentId: "main", sessionKey: "agent:main:main", workspaceDir: path.join(oc, "workspace") };
    const code = (tool: string, params: Record<string, unknown>, ctx: Record<string, unknown> = main) => {
      const r = rt.evaluate({ toolName: tool, params }, ctx) as { blockReason?: string } | undefined;
      return r && r.blockReason ? r.blockReason.split(":")[0] : "pass";
    };
    assert.equal(code("exec", { command: "cp " + inst.root + "/pr?vate/h*.json /tmp/k" }), "G_PATH_DENIED");
    assert.equal(code("exec", { command: "cat p?iv*/home.json", workdir: inst.root }), "G_PATH_DENIED");
    assert.equal(code("exec", { command: "cat ~/notes/p/home.json" }), "G_PATH_DENIED");
    assert.equal(code("read", { path: "~/notes/p/consent.json" }), "G_PATH_DENIED");
    assert.equal(code("read", { path: "~/.openclaw/openclaw.json" }), "G_PATH_DENIED");
    assert.equal(code("read", { path: "notes.md" }), "pass");
    assert.equal(code("write", { path: "notes.md", content: "y" }), "pass");
    assert.equal(code("write", { path: path.join(inst.ws, "scout", "work", "x.json"), content: "{}" }), "G_PATH_DENIED");
    assert.equal(code("exec", { command: "ls " + inst.root + "/scripts" }), "pass");
    assert.equal(code("Glob", { pattern: "*", path: path.join(home, "notes", "p") }), "G_PATH_DENIED");
    rt.close();
  } finally {
    inst.cleanup();
  }
});

test("expandGlob is bounded and matches hidden names", () => {
  const dir = fs.mkdtempSync(path.join(fs.realpathSync(process.env.TMPDIR || "/tmp"), "jhg-glob-"));
  try {
    fs.mkdirSync(path.join(dir, "private"));
    fs.writeFileSync(path.join(dir, "private", "guard.key"), "x");
    fs.writeFileSync(path.join(dir, "private", ".hidden"), "x");
    assert.deepEqual(expandGlob(path.join(dir, "pr*", "g*")), [path.join(dir, "private", "guard.key")]);
    assert.deepEqual(expandGlob(path.join(dir, "private", "[.]h*")), [path.join(dir, "private", ".hidden")]);
    assert.deepEqual(expandGlob(path.join(dir, "nothing*")), []);
    assert.deepEqual(expandGlob("relative/*"), []);
    assert.equal(expandGlob(path.join(dir, "private", "*"), 1).length, 1);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("config parsing and helpers", () => {
  assert.throws(() => parseConfig({}));
  assert.throws(() => parseConfig({ repo: "relative", python: "/usr/bin/python3", homeFile: "/a/b", publicReadonlyAgents: [] }));
  assert.throws(() => parseConfig({ repo: "/a b", python: "/usr/bin/python3", homeFile: "/a/b", publicReadonlyAgents: [] }));
  const c = parseConfig({ repo: "/r", python: "/usr/bin/python3", homeFile: "/r/private/home.json", publicReadonlyAgents: ["main", "jobhunter-scout"] });
  assert.deepEqual(c.publicReadonlyAgents, ["main"]);
  // defaults of the optional CLI route keys
  assert.equal(c.claudeNativeTools, "deny");
  assert.equal(c.pinToolSurface, true);
  assert.deepEqual(c.proofCarriers, ["argv", "env"]);
  assert.equal(c.recordEvents, false);
  assert.deepEqual(c.protectedRoots, { read: ["/r/private"], write: ["/r"] });
  assert.equal(c.qcVerdictFile, false);
  const c2 = parseConfig({ repo: "/r", python: "/usr/bin/python3", homeFile: "/r/private/home.json", publicReadonlyAgents: [], claudeNativeTools: "gate", pinToolSurface: false,
    proofCarriers: ["env"], recordEvents: true, protectedRoots: { read: ["/h/.openclaw"], write: ["/w"] }, qcVerdictFile: true });
  assert.equal(c2.claudeNativeTools, "gate");
  assert.equal(c2.pinToolSurface, false);
  assert.deepEqual(c2.proofCarriers, ["env"]);
  assert.deepEqual(c2.protectedRoots, { read: ["/r/private", "/h/.openclaw"], write: ["/r", "/w"] });
  assert.equal(c2.qcVerdictFile, true);
  assert.equal(manifestHashes({ a: "A".repeat(64), b: { sha256: "b".repeat(64) }, c: "nothex" }).size, 2);
  const dir = fs.mkdtempSync(path.join(fs.realpathSync(process.env.TMPDIR || "/tmp"), "jhg-rp-"));
  try {
    assert.equal(realpathLoose(path.join(dir, "missing", "file.json")), path.join(fs.realpathSync(dir), "missing", "file.json"));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("symlinked work folder cannot escape the workspace", () => {
  const inst = makeInstall();
  try {
    const link = path.join(inst.ws, "applier", "work", "escape");
    fs.symlinkSync(inst.root, link);
    const r = inst.runtime.evaluate({ toolName: "write", params: { path: path.join(link, "state", "PAUSED"), content: "" } }, { agentId: "jobhunter-applier", sessionKey: "s" });
    assert.match(String(r && (r as any).blockReason), /^G_PATH_DENIED/);
    const ok = inst.runtime.evaluate({ toolName: "write", params: { path: path.join(inst.ws, "applier", "work", "C20260927T050000ZABCD", "a.json"), content: "{}" } }, { agentId: "jobhunter-applier", sessionKey: "s" });
    assert.equal(ok, undefined);
  } finally {
    inst.cleanup();
  }
});

test("a fill is blocked when its token log line cannot be written", () => {
  const inst = makeInstall();
  try {
    inst.db.exec("PRAGMA foreign_keys = OFF");
    inst.db.prepare("INSERT INTO actions (token, kind, route, first_touch, platform, agent_id, status, reserved_at, expires_at, created_at, updated_at) VALUES (?, 'application', 'browser', 0, 'greenhouse', 'jobhunter-applier', 'reserved', '2026-09-27T05:00:00Z', '2026-09-27T05:30:00Z', '2026-09-27T05:00:00Z', '2026-09-27T05:00:00Z')").run(TOKEN);
    inst.runtime.observe(
      { toolName: "browser", params: { action: "snapshot" }, result: { content: [{ type: "text", text: "- textbox \"First Name\" [ref=e4]" }], details: { targetId: "t1", url: "https://job-boards.greenhouse.io/kestrel/jobs/1" } } },
      { agentId: "jobhunter-applier", sessionKey: "s" },
    );
    const guardDir = path.join(inst.root, "state", "guard");
    fs.rmSync(guardDir, { recursive: true, force: true });
    fs.writeFileSync(guardDir, "not a folder");
    const r = inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "type", ref: "e4", text: "Alex" } }, { agentId: "jobhunter-applier", sessionKey: "s" });
    assert.match(String(r && (r as { blockReason?: string }).blockReason), /^G_GUARD_UNHEALTHY: the guard cannot write the token log/);
  } finally {
    inst.cleanup();
  }
});

test("/jh pause still works while the guard is unhealthy for agents", async () => {
  const inst = makeInstall({ ownerFallback: [{ channel: "whatsapp", senderId: "+10000000000" }] });
  try {
    fs.rmSync(path.join(inst.root, "scripts", "jobhunter", "detect"), { recursive: true });
    const r = inst.runtime.evaluate({ toolName: "read", params: { path: "AGENTS.md" } }, { agentId: "jobhunter-scout", sessionKey: "s" });
    assert.match(String(r && (r as { blockReason?: string }).blockReason), /^G_GUARD_UNHEALTHY/);
    const out = await inst.runtime.command({ channel: "whatsapp", senderId: "+10000000000", args: "pause" });
    assert.equal(out.text, "OK from fake jh.py");
    assert.deepEqual(inst.jhCalls[0].slice(2), ["--human", "pause", "--scope", "all"]);
  } finally {
    inst.cleanup();
  }
});

test("a CAPTCHA on an ATS form stops writes on that site only", async () => {
  const inst = makeInstall();
  try {
    inst.db.exec("PRAGMA foreign_keys = OFF");
    inst.db.prepare("INSERT INTO actions (token, kind, route, first_touch, platform, agent_id, status, reserved_at, expires_at, created_at, updated_at) VALUES (?, 'application', 'browser', 0, 'greenhouse', 'jobhunter-applier', 'reserved', '2026-09-27T05:00:00Z', '2026-09-27T05:30:00Z', '2026-09-27T05:00:00Z', '2026-09-27T05:00:00Z')").run(TOKEN);
    const ctx = { agentId: "jobhunter-applier", sessionKey: "s-cap" };
    const o = inst.runtime.observe(
      { toolName: "browser", params: { action: "snapshot" }, result: { content: [{ type: "text", text: "- textbox \"First Name\" [ref=e4]\n- checkbox \"I'm not a robot\" [ref=e5]" }], details: { targetId: "t1", url: "https://job-boards.greenhouse.io/kestrel/jobs/1" } } },
      ctx,
    );
    assert.deepEqual(o, { stopped: false, softStopped: true, code: "ats_captcha" });
    const fill = inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "click", ref: "e4" } }, ctx);
    assert.match(String(fill && (fill as { blockReason?: string }).blockReason), /^G_STOPPED/);
    // the CAPTCHA widget itself is never clicked by an agent (forbidden_names), stop or no stop
    const widget = inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "click", ref: "e5" } }, ctx);
    assert.match(String(widget && (widget as { blockReason?: string }).blockReason), /^G_TOOL_DENIED/);
    const ex = inst.runtime.evaluate({ toolName: "exec", params: { command: PY + " " + inst.root + "/scripts/jh.py job set-status JABCDEFG --status needs_human --reason captcha_visible" } }, ctx);
    assert.ok(ex && "params" in ex);
    await new Promise((res) => setTimeout(res, 10));
    const call = inst.jhCalls.find((a) => a[0] === "detect");
    assert.ok(call);
    const payload = JSON.parse(fs.readFileSync(call[2], "utf8"));
    assert.equal(payload.platform, "ats");
    // the CAPTCHA hand-off (handoff "captcha"): the agent, its open token and the tab ride along
    assert.deepEqual(Object.keys(payload).sort(), ["agent", "http_status", "platform", "tab_id", "text", "title", "token", "url"]);
    assert.equal(payload.agent, "jobhunter-applier");
    assert.equal(payload.token, TOKEN);
    assert.equal(payload.tab_id, "t1");
  } finally {
    inst.cleanup();
  }
});

test("detect payload helpers", () => {
  assert.equal(stopPlatform("linkedin", "linkedin", "www.linkedin.com"), "linkedin");
  assert.equal(stopPlatform("site:*", "site:naukri", "www.naukri.com"), "site:naukri");
  assert.equal(stopPlatform("boards", null, "www.kestrel.example"), "site:kestrel");
  assert.equal(stopPlatform("gmail", null, "accounts.google.com"), "gmail");
  const long = "a".repeat(30000) + "captcha" + "b".repeat(30000);
  const w = textWindow(long, 30000, 20000);
  assert.equal(w.length, 20000);
  assert.ok(w.includes("captcha"));
  assert.equal(textWindow("short", 2, 20000), "short");
  assert.equal(textWindow(long, -1, 20000), long.slice(0, 20000));
});

// drivers/read_form.js output (fictional values) as an evaluate result on `url`.
function readFormResult(url: string, flags: Record<string, boolean>) {
  const out = {
    observed: { fields: [{ label: "Email", value: "alex.rivera@example.com" }], resume_filename_visible: "Alex_Rivera_Resume.pdf" },
    required_empty: [],
    ...flags,
    validation_errors: [],
    page_url: url,
  };
  return { content: [{ type: "text", text: JSON.stringify(out) }], details: { ok: true, targetId: "t1", url } };
}

function stopFiles(root: string): string[] {
  return fs.readdirSync(path.join(root, "state", "guard")).filter((n) => n.startsWith("stop-"));
}

test("allowlisted driver output: flag key names do not stop the session, true flags do", async () => {
  const inst = makeInstall();
  try {
    const url = "https://job-boards.greenhouse.io/kestrel/jobs/1";
    const ctx = { agentId: "jobhunter-applier", sessionKey: "s-form" };
    const call = { action: "act", kind: "evaluate", fn: DRIVER_TEXT };
    assert.ok(inst.runtime.isDriverCall(call));
    assert.ok(inst.runtime.isDriverCall({ action: "act", request: { kind: "evaluate", fn: "\n" + DRIVER_TEXT + "\n" } }));
    assert.equal(inst.runtime.isDriverCall({ action: "act", kind: "evaluate", fn: "() => document.body.innerText" }), false);
    assert.equal(inst.runtime.isDriverCall({ action: "act", kind: "wait", fn: DRIVER_TEXT }), false);
    assert.equal(inst.runtime.isDriverCall({ action: "snapshot" }), false);

    const clear = inst.runtime.observe({ toolName: "browser", params: call, result: readFormResult(url, { captcha_visible: false, account_wall: false }) }, ctx);
    assert.deepEqual(clear, { stopped: false });
    assert.deepEqual(stopFiles(inst.root), []);
    const fill = inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "click", ref: "e5" } }, ctx);
    assert.doesNotMatch(String(fill && (fill as { blockReason?: string }).blockReason), /G_STOPPED/);

    const cap = inst.runtime.observe({ toolName: "browser", params: { action: "act", request: call }, result: readFormResult(url, { captcha_visible: true, account_wall: false }) }, ctx);
    assert.deepEqual(cap, { stopped: false, softStopped: true, code: "ats_captcha" });
    const after = inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "click", ref: "e5" } }, ctx);
    assert.match(String(after && (after as { blockReason?: string }).blockReason), /^G_STOPPED/);
    await new Promise((res) => setTimeout(res, 10));
    const det = inst.jhCalls.filter((a) => a[0] === "detect");
    assert.equal(det.length, 1);
    const payload = JSON.parse(fs.readFileSync(det[0][2], "utf8"));
    assert.equal(payload.platform, "ats");
    assert.match(payload.text, /^captcha_visible: true /);
    assert.equal(payload.text.includes("\"captcha_visible\""), false);
  } finally {
    inst.cleanup();
  }
});

test("evaluate output of a script that is not an allowlisted driver keeps the plain text scan", () => {
  const inst = makeInstall();
  try {
    const url = "https://job-boards.greenhouse.io/kestrel/jobs/1";
    const o = inst.runtime.observe(
      { toolName: "browser", params: { action: "act", kind: "evaluate", fn: "() => 1" }, result: readFormResult(url, { captcha_visible: false }) },
      { agentId: "jobhunter-applier", sessionKey: "s-raw" },
    );
    assert.deepEqual(o, { stopped: false, softStopped: true, code: "ats_captcha" });
  } finally {
    inst.cleanup();
  }
});

test("read_form flags on a job board with the repo's detect files", { skip: !fs.existsSync(path.join(REPO, "scripts", "jobhunter", "detect", "boards.json")) }, async () => {
  const inst = makeInstall();
  try {
    const dir = path.join(inst.root, "scripts", "jobhunter", "detect");
    for (const n of fs.readdirSync(dir)) fs.rmSync(path.join(dir, n));
    for (const n of fs.readdirSync(path.join(REPO, "scripts", "jobhunter", "detect")).filter((x) => x.endsWith(".json"))) {
      fs.copyFileSync(path.join(REPO, "scripts", "jobhunter", "detect", n), path.join(dir, n));
    }
    const url = "https://www.naukri.com/job-listings-data-analyst-kestrel-commerce-1";
    const call = { action: "act", kind: "evaluate", fn: DRIVER_TEXT };
    const ctx = { agentId: "jobhunter-applier", sessionKey: "s-board" };
    assert.deepEqual(inst.runtime.observe({ toolName: "browser", params: call, result: readFormResult(url, { captcha_visible: false, account_wall: false }) }, ctx), { stopped: false });
    await new Promise((res) => setTimeout(res, 10));
    assert.deepEqual(inst.jhCalls.filter((a) => a[0] === "detect"), []);
    assert.equal(inst.runtime.isStopped("s-board"), false);
    const ats = inst.runtime.observe({ toolName: "browser", params: call, result: readFormResult("https://job-boards.greenhouse.io/kestrel/jobs/1", { captcha_visible: false, account_wall: false }) }, { agentId: "jobhunter-applier", sessionKey: "s-ats" });
    assert.deepEqual(ats, { stopped: false });
    const wall = inst.runtime.observe({ toolName: "browser", params: call, result: readFormResult("https://job-boards.greenhouse.io/kestrel/jobs/1", { account_wall: true }) }, { agentId: "jobhunter-applier", sessionKey: "s-ats" });
    assert.deepEqual(wall, { stopped: false, softStopped: true, code: "ats_account_wall" });
    const cap = inst.runtime.observe({ toolName: "browser", params: call, result: readFormResult(url, { captcha_visible: true }) }, ctx);
    assert.deepEqual(cap, { stopped: true, softStopped: false, code: "site_captcha" });
    await new Promise((res) => setTimeout(res, 10));
    const det = inst.jhCalls.filter((a) => a[0] === "detect");
    assert.equal(det.length, 2);
    const payload = JSON.parse(fs.readFileSync(det[1][2], "utf8"));
    assert.equal(payload.platform, "site:naukri");
    assert.match(payload.text, /^captcha_visible: true /);
  } finally {
    inst.cleanup();
  }
});

test("act calls are judged on the tab and request OpenClaw runs", () => {
  const inst = makeInstall();
  try {
    const ctx = { agentId: "jobhunter-scout", sessionKey: "s-tabs" };
    const snapshot = (tab: string, url: string, text: string) =>
      inst.runtime.observe({ toolName: "browser", params: { action: "snapshot", targetId: tab }, result: { content: [{ type: "text", text }], details: { ok: true, targetId: tab, url } } }, ctx);
    snapshot("A", "https://www.naukri.com/data-analyst-jobs", "- link \"Data Analyst\" [ref=e5]");
    snapshot("B", "https://www.linkedin.com/in/example-person/", "- button \"Connect\" [ref=e5]");
    const code = (params: Record<string, unknown>) => {
      const r = inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", ...params } }, ctx);
      return r && "block" in r ? r.blockReason.split(":")[0] : "allow";
    };
    assert.equal(code({ action: "act", kind: "click", ref: "e5", targetId: "A" }), "allow");
    assert.equal(code({ action: "act", kind: "click", ref: "e5", targetId: "B" }), "G_NO_TOKEN");
    assert.equal(code({ action: "act", request: { kind: "click", ref: "e5", targetId: "B" } }), "G_NO_TOKEN");
    assert.equal(code({ action: "act", targetId: "A", request: { kind: "click", ref: "e5" } }), "allow");
    assert.equal(code({ action: "act", targetId: "A", request: { kind: "click", ref: "e5", targetId: "B" } }), "G_TOOL_DENIED");
    assert.equal(code({ action: "act", targetId: "A", request: { kind: "wait" }, fn: "() => { document.forms[0].submit(); return true; }" }), "G_SCRIPT_NOT_ALLOWED");
    assert.equal(code({ action: "act", targetId: "A", kind: "wait", fn: "() => { document.forms[0].submit(); return true; }" }), "G_SCRIPT_NOT_ALLOWED");
    // the driver output scan uses the same merged request
    assert.ok(inst.runtime.isDriverCall({ action: "act", request: { kind: "evaluate" }, fn: DRIVER_TEXT }));
    assert.equal(inst.runtime.isDriverCall({ action: "act", request: { kind: "evaluate", fn: "() => document.body.innerText" }, fn: DRIVER_TEXT }), false);
    assert.equal(inst.runtime.isDriverCall({ action: "act", kind: "evaluate", fn: DRIVER_TEXT, request: { kind: "wait", text: "x" } }), false);
  } finally {
    inst.cleanup();
  }
});

test("a tab the session has not seen: reads refused until its page is known, then judged by its host", () => {
  const inst = makeInstall({ consent: ["gmail", "naukri"] });
  try {
    const INBOX = "https://mail.google.com/mail/u/0/#inbox";
    const NAUKRI = "https://www.naukri.com/data-analyst-jobs";
    const s1 = { agentId: "jobhunter-outreach", sessionKey: "agent:jobhunter-outreach:cron:replies" };
    const s2 = { agentId: "jobhunter-scout", sessionKey: "agent:jobhunter-scout:cron:scout" };
    const code = (ctx: typeof s1, params: Record<string, unknown>) => {
      const r = inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", ...params } }, ctx);
      return r && "block" in r ? r.blockReason.split(":")[0] : "allow";
    };
    // session 1 opens Gmail; OpenClaw names the new tab by its raw targetId, its tab id and suggested id
    assert.equal(code(s1, { action: "open", targetUrl: INBOX }), "allow");
    inst.runtime.observe({ toolName: "browser", params: { profile: "jobhunter", action: "open", targetUrl: INBOX },
      result: { content: [{ type: "text", text: "{}" }], details: { targetId: "CDP0A1", tabId: "t1", suggestedTargetId: "t1", url: INBOX, title: "Inbox" } } }, s1);
    assert.equal(code(s1, { action: "snapshot" }), "allow");
    assert.equal(code(s1, { action: "snapshot", targetId: "t1" }), "allow");
    // refs cached under the raw id are found through the tab id
    inst.runtime.observe({ toolName: "browser", params: { profile: "jobhunter", action: "snapshot", targetId: "t1" },
      result: { content: [{ type: "text", text: "- link \"Primary\" [ref=e3]" }], details: { ok: true, targetId: "CDP0A1", url: INBOX } } }, s1);
    assert.equal(code(s1, { action: "act", kind: "click", ref: "e3", targetId: "t1" }), "allow");
    assert.equal(code(s1, { action: "act", kind: "click", ref: "e3", targetId: "CDP0A1" }), "allow");

    // session 2 (another agent, or the same agent in a new cycle) has never seen that tab
    for (const p of [{ action: "snapshot" }, { action: "snapshot", targetId: "t1" }, { action: "text", targetId: "t1" },
                     { action: "screenshot", targetId: "CDP0A1" }, { action: "act", kind: "evaluate", targetId: "t1", fn: DRIVER_TEXT }]) {
      assert.equal(code(s2, p), "G_PAGE_UNKNOWN", JSON.stringify(p));
    }
    for (const p of [{ action: "tabs" }, { action: "close", targetId: "t1" }, { action: "status" }, { action: "navigate", targetUrl: NAUKRI }]) {
      assert.equal(code(s2, p), "allow", JSON.stringify(p));
    }
    // a tab list tells the guard every listed tab's page: the Gmail tab is now judged as Gmail
    inst.runtime.observe({ toolName: "browser", params: { profile: "jobhunter", action: "tabs" },
      result: { content: [{ type: "text", text: "tabs" }], details: { running: true, tabCount: 2, tabs: [
        { suggestedTargetId: "t1", tabId: "t1", title: "Inbox", url: INBOX, type: "page", targetId: "CDP0A1" },
        { suggestedTargetId: "jobs", tabId: "t2", label: "jobs", title: "Jobs", url: NAUKRI, type: "page", targetId: "CDP0B2" },
      ] } } }, s2);
    assert.equal(code(s2, { action: "snapshot", targetId: "t1" }), "allow");
    assert.equal(code(s2, { action: "text", targetId: "jobs" }), "allow");
    assert.equal(code(s2, { action: "snapshot", targetId: "CDP0B2" }), "allow");
    // listing tabs does not choose a tab: a call without a targetId still names no known page
    assert.equal(code(s2, { action: "snapshot" }), "G_PAGE_UNKNOWN");

    // the owner revokes Gmail: both sessions are refused on the Gmail tab at once, the board tab reads on
    writeConsent(inst.root, ["naukri"], ["gmail"]);
    assert.equal(code(s1, { action: "snapshot" }), "G_NO_CONSENT");
    assert.equal(code(s2, { action: "snapshot", targetId: "t1" }), "G_NO_CONSENT");
    assert.equal(code(s2, { action: "text", targetId: "t1" }), "G_NO_CONSENT");
    assert.equal(code(s2, { action: "text", targetId: "jobs" }), "allow");
    assert.equal(code(s2, { action: "close", targetId: "t1" }), "allow");
    // a site breaker applies the same way
    inst.db.prepare("INSERT INTO breakers (scope, state, reason_code, tripped_at, updated_at) VALUES ('site:naukri', 'open', 'site_challenge', ?, ?)").run("2026-09-27T05:00:00Z", "2026-09-27T05:00:00Z");
    assert.equal(code(s2, { action: "text", targetId: "jobs" }), "G_BREAKER_OPEN");
  } finally {
    inst.cleanup();
  }
});

// ------------------------------------------------------------------ FEATURES-OTP-ACCOUNTS-CAPTCHA (U8)

const WD_URL = "https://kestrel.wd5.myworkdayjobs.com/en-US/careers/job/Senior-Analyst_R1001";

// private/consent.json with every site consent of the default install plus capability rows (fictional).
function writeCapabilities(root: string, caps: Array<[string, string, Record<string, unknown>?]>): void {
  const file = path.join(root, "private", "consent.json");
  const doc = JSON.parse(fs.readFileSync(file, "utf8"));
  const out: Record<string, Record<string, unknown>> = {};
  for (const [cap, site, extra] of caps) {
    (out[cap] = out[cap] || {})[site] = { site, capability: cap, status: "granted", granted_at: "2026-09-26T08:00:00Z", revoked_at: null, declined_at: null, by: "owner", method: "pin", ...(extra || {}) };
  }
  doc.capabilities = out;
  fs.writeFileSync(file, JSON.stringify(doc), { mode: 0o600 });
}

function addWorkdayToken(inst: { db: { exec: (s: string) => void; prepare: (s: string) => { run: (...a: unknown[]) => unknown } } }, status = "reserved", armedAt: string | null = null): void {
  inst.db.exec("PRAGMA foreign_keys = OFF");
  inst.db.prepare("INSERT INTO actions (token, kind, route, first_touch, platform, agent_id, status, reserved_at, armed_at, expires_at, created_at, updated_at) VALUES (?, 'application', 'browser', 0, 'workday', 'jobhunter-applier', ?, '2026-09-27T05:00:00Z', ?, '2026-09-27T05:30:00Z', '2026-09-27T05:00:00Z', '2026-09-27T05:00:00Z')").run(TOKEN, status, armedAt);
}

function blockCode(r: unknown): string {
  return r && typeof r === "object" && "block" in (r as object) ? String((r as { blockReason: string }).blockReason).split(":")[0] : "allow";
}

test("capability-aware job-level stop: granted for the site, no soft stop, the stop file is still written", async () => {
  const inst = makeInstall();
  try {
    writeCapabilities(inst.root, [["ats_accounts", "workday"], ["email_codes", "icims"]]);
    addWorkdayToken(inst);
    const ctx = { agentId: "jobhunter-applier", sessionKey: "s-acct" };
    const o = inst.runtime.observe({ toolName: "browser", params: { action: "snapshot" }, result: { content: [{ type: "text", text: "Create an account to apply\n- textbox \"Email address\" [ref=e4]" }], details: { targetId: "t1", url: WD_URL } } }, ctx);
    assert.deepEqual(o, { stopped: false, softStopped: false, code: "ats_account_wall" });
    assert.equal(blockCode(inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "type", ref: "e4", text: "alex.rivera@example.com" } }, ctx)), "allow");
    await new Promise((res) => setTimeout(res, 10));
    const det = inst.jhCalls.filter((a) => a[0] === "detect");
    assert.equal(det.length, 1);
    const payload = JSON.parse(fs.readFileSync(det[0][2], "utf8"));
    assert.deepEqual(Object.keys(payload).sort(), ["http_status", "platform", "text", "title", "url"]); // no hand-off keys
    const log = readJsonl(path.join(inst.root, "logs", "guard-2026-09.jsonl"));
    assert.ok(log.some((e) => e.kind === "capability_page" && e.code === "ats_account_wall" && e.capability === "ats_accounts" && e.host === "kestrel.wd5.myworkdayjobs.com"));
    assert.equal(log.some((e) => e.kind === "stop"), false);
    // email_codes is granted for icims only: the code page on Workday soft-stops
    const code = inst.runtime.observe({ toolName: "browser", params: { action: "snapshot" }, result: { content: [{ type: "text", text: "We sent a verification code to your email" }], details: { targetId: "t1", url: WD_URL } } }, ctx);
    assert.deepEqual(code, { stopped: false, softStopped: true, code: "ats_email_code" });
    assert.equal(blockCode(inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "type", ref: "e4", text: "x" } }, ctx)), "G_STOPPED");
  } finally {
    inst.cleanup();
  }
});

test("capability-aware job-level stop: not granted, revoked or an unusable consent file gives today's soft stop", () => {
  const variants: Array<(root: string) => void> = [
    () => {}, // no capabilities object
    (root) => writeCapabilities(root, [["ats_accounts", "greenhouse"]]), // another site
    (root) => writeCapabilities(root, [["email_codes", "workday"]]), // another capability
    (root) => writeCapabilities(root, [["ats_accounts", "workday", { status: "revoked", revoked_at: "2026-09-26T09:00:00Z" }]]),
    (root) => writeCapabilities(root, [["ats_accounts", "workday", { status: "declined", granted_at: null }]]),
    (root) => writeCapabilities(root, [["ats_accounts", "host:kestrel.wd5.myworkdayjobs.com"]]), // a platform host needs the platform key
    (root) => {
      writeCapabilities(root, [["ats_accounts", "workday"]]);
      fs.chmodSync(path.join(root, "private", "consent.json"), 0o666); // shared-writable: no consent at all
    },
  ];
  for (const v of variants) {
    const inst = makeInstall();
    try {
      v(inst.root);
      addWorkdayToken(inst);
      const ctx = { agentId: "jobhunter-applier", sessionKey: "s-wall" };
      const o = inst.runtime.observe({ toolName: "browser", params: { action: "snapshot" }, result: { content: [{ type: "text", text: "Sign up to apply\n- textbox \"Email address\" [ref=e4]" }], details: { targetId: "t1", url: WD_URL } } }, ctx);
      assert.deepEqual(o, { stopped: false, softStopped: true, code: "ats_account_wall" }, v.toString());
      assert.equal(blockCode(inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "type", ref: "e4", text: "x" } }, ctx)), "G_STOPPED", v.toString());
    } finally {
      inst.cleanup();
    }
  }
});

test("a capability key on a tripping signature makes the guard unhealthy (fail closed)", () => {
  const inst = makeInstall();
  try {
    fs.writeFileSync(path.join(inst.root, "scripts", "jobhunter", "detect", "zz_bad.json"), JSON.stringify({ platform: "ats", signatures: [{ id: "ats_phone_code", verdict: "stop", trip: true, reason_code: "ats_security", capability: "email_codes", text: "sms" }] }));
    const r = inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "snapshot" } }, { agentId: "jobhunter-applier", sessionKey: "s" });
    assert.match(String(r && (r as { blockReason?: string }).blockReason), /^G_GUARD_UNHEALTHY/);
    assert.equal(inst.runtime.heartbeat(), false);
  } finally {
    inst.cleanup();
  }
});

test("CAPTCHA hand-off payload without an open token: agent and tab only; the raw targetId behind an alias", async () => {
  const inst = makeInstall();
  try {
    const ctx = { agentId: "jobhunter-applier", sessionKey: "s-cap2" };
    inst.runtime.observe({ toolName: "browser", params: { action: "open", targetUrl: WD_URL }, result: { content: [{ type: "text", text: "{}" }], details: { targetId: "6F1E0A9C3B2D4E5F", tabId: "t3", url: WD_URL } } }, ctx);
    const o = inst.runtime.observe({ toolName: "browser", params: { action: "snapshot", targetId: "t3" }, result: { content: [{ type: "text", text: "Please complete the CAPTCHA below" }], details: { url: WD_URL } } }, ctx);
    assert.deepEqual(o, { stopped: false, softStopped: true, code: "ats_captcha" });
    await new Promise((res) => setTimeout(res, 10));
    const det = inst.jhCalls.filter((a) => a[0] === "detect");
    const payload = JSON.parse(fs.readFileSync(det[0][2], "utf8"));
    assert.deepEqual(Object.keys(payload).sort(), ["agent", "http_status", "platform", "tab_id", "text", "title", "url"]);
    assert.equal(payload.agent, "jobhunter-applier");
    assert.equal(payload.tab_id, "6F1E0A9C3B2D4E5F");
    // the soft stop is exactly as before
    assert.equal(blockCode(inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "press", key: "a", targetId: "t3" } }, ctx)), "G_STOPPED");
    assert.equal(inst.runtime.isStopped("s-cap2"), false);
  } finally {
    inst.cleanup();
  }
});

test("code-written commit lines (by: code) in the token log use the token's commit budget", () => {
  const inst = makeInstall();
  try {
    addWorkdayToken(inst, "armed", "2026-09-27T04:59:00Z");
    inst.db.prepare("INSERT INTO locks (name, holder, acquired_at, expires_at) VALUES ('pace:jobhunter-applier:dwell', 'jobhunter-applier', '2026-09-27T04:59:05Z', '2026-09-27T04:59:50Z')").run();
    const ctx = { agentId: "jobhunter-applier", sessionKey: "s-code" };
    inst.runtime.observe({ toolName: "browser", params: { action: "snapshot" }, result: { content: [{ type: "text", text: "- button \"Submit\" [ref=e6]" }], details: { targetId: "t1", url: WD_URL } } }, ctx);
    const logFile = path.join(inst.root, "state", "guard", TOKEN + ".jsonl");
    const codeLine = (action: string) => JSON.stringify({ ts: "2026-09-27T04:59:30Z", token: TOKEN, agent: "jobhunter-applier", by: "code", class: "commit", action, host: "kestrel.wd5.myworkdayjobs.com", ref: null, role: null, name: null });
    fs.writeFileSync(logFile, codeLine("code_resubmit") + "\n", { mode: 0o600 });
    // one code commit: one guard commit is left
    const first = inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "click", ref: "e6" } }, ctx);
    assert.equal(blockCode(first), "allow");
    const second = inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "click", ref: "e6" } }, ctx);
    assert.equal(blockCode(second), "G_COMMIT_BUDGET");
    const lines = readJsonl(logFile);
    assert.deepEqual(lines.map((l) => [l.by ?? "guard", l.class]), [["code", "commit"], ["guard", "commit"]]);
    // a fresh guard (restart) recounts both kinds from the file: two code lines alone use the whole budget
    fs.writeFileSync(logFile, codeLine("account_create") + "\n" + codeLine("code_resubmit") + "\n", { mode: 0o600 });
    const rt2 = new GuardRuntime(inst.runtime.config, { runner: inst.runner, nowMs: () => inst.now.ms });
    rt2.observe({ toolName: "browser", params: { action: "snapshot" }, result: { content: [{ type: "text", text: "- button \"Submit\" [ref=e6]" }], details: { targetId: "t1", url: WD_URL } } }, ctx);
    assert.equal(blockCode(rt2.evaluate({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "click", ref: "e6" } }, ctx)), "G_COMMIT_BUDGET");
    rt2.close();
  } finally {
    inst.cleanup();
  }
});

test("secret focus across calls: a click on a password field, then a ref-less press or type is G_SECRET_FIELD until a snapshot", () => {
  const inst = makeInstall();
  try {
    addWorkdayToken(inst);
    const ctx = { agentId: "jobhunter-applier", sessionKey: "s-secret" };
    const snapText = "- textbox \"Email address\" [ref=e4]\n- textbox \"Password\" [ref=e5]\n- button \"Sign in with Google\" [ref=e7]";
    const snapshot = () => inst.runtime.observe({ toolName: "browser", params: { action: "snapshot" }, result: { content: [{ type: "text", text: snapText }], details: { targetId: "t1", url: WD_URL } } }, ctx);
    const call = (params: Record<string, unknown>) => blockCode(inst.runtime.evaluate({ toolName: "browser", params: { profile: "jobhunter", ...params } }, ctx));
    snapshot();
    assert.equal(call({ action: "act", kind: "type", ref: "e5", text: "x" }), "G_SECRET_FIELD");
    assert.equal(call({ action: "act", kind: "click", ref: "e7" }), "G_TOOL_DENIED");
    assert.equal(call({ action: "act", kind: "press", key: "a" }), "allow");
    assert.equal(call({ action: "act", kind: "click", ref: "e5" }), "allow");
    assert.equal(call({ action: "act", kind: "press", key: "a" }), "G_SECRET_FIELD");
    assert.equal(call({ action: "act", kind: "type", text: "x" }), "G_SECRET_FIELD");
    assert.equal(call({ action: "act", kind: "type", text: "x", targetId: "t1" }), "G_SECRET_FIELD");
    assert.equal(call({ action: "act", kind: "type", ref: "e4", text: "alex.rivera@example.com" }), "allow"); // a named field is fine
    // the next snapshot clears it
    assert.equal(call({ action: "snapshot" }), "allow");
    snapshot();
    assert.equal(call({ action: "act", kind: "press", key: "a" }), "allow");
    // a click on another ref clears it too, a navigation as well
    assert.equal(call({ action: "act", kind: "click", ref: "e5" }), "allow");
    assert.equal(call({ action: "act", kind: "click", ref: "e4" }), "allow");
    assert.equal(call({ action: "act", kind: "press", key: "a" }), "allow");
    assert.equal(call({ action: "act", kind: "click", ref: "e5" }), "allow");
    assert.equal(call({ action: "navigate", targetUrl: WD_URL }), "allow");
    assert.equal(call({ action: "act", kind: "press", key: "a" }), "allow");
    // the flag is per tab: a click in tab t1 does not mark another tab
    assert.equal(call({ action: "act", kind: "click", ref: "e5" }), "allow");
    inst.runtime.observe({ toolName: "browser", params: { action: "open", targetUrl: WD_URL }, result: { content: [{ type: "text", text: "{}" }], details: { targetId: "t2", url: WD_URL } } }, ctx);
    assert.equal(call({ action: "act", kind: "press", key: "a", targetId: "t2" }), "allow");
    assert.equal(call({ action: "act", kind: "press", key: "a", targetId: "t1" }), "G_SECRET_FIELD");
  } finally {
    inst.cleanup();
  }
});
