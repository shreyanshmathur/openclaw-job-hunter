// Plugin entry and manifest: registrations against a fake OpenClaw api, and the jh.py runner.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import entry, { register } from "../index.ts";
import { childEnv, lastJsonObject, makeJhRunner } from "../src/jhcall.ts";
import { ENV_PROOF_RE } from "../src/grant.ts";
import { makeInstall } from "./_install.ts";
import { PLUGIN_DIR, PY } from "./_helpers.ts";

type Reg = { policies: any[]; hooks: Array<{ name: string; handler: any; opts: any }>; commands: any[]; services: any[] };

function fakeApi(pluginConfig: Record<string, unknown> | undefined, opts: { trusted?: boolean; mode?: string } = {}) {
  const reg: Reg = { policies: [], hooks: [], commands: [], services: [] };
  const api: any = {
    id: "jobhunter-guard",
    registrationMode: opts.mode ?? "full",
    pluginConfig,
    logger: { info: () => {}, warn: () => {}, error: () => {} },
    on: (name: string, handler: any, o: any) => reg.hooks.push({ name, handler, opts: o }),
    registerCommand: (c: any) => reg.commands.push(c),
    registerService: (s: any) => reg.services.push(s),
  };
  if (opts.trusted !== false) api.registerTrustedToolPolicy = (p: any) => reg.policies.push(p);
  return { api, reg };
}

test("manifest and package declare the plugin id and the trusted policy contract", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(PLUGIN_DIR, "openclaw.plugin.json"), "utf8"));
  assert.equal(manifest.id, "jobhunter-guard");
  assert.deepEqual(manifest.contracts.trustedToolPolicies, ["jobhunter-guard"]);
  assert.deepEqual(manifest.configSchema.required, ["repo", "python", "homeFile", "publicReadonlyAgents"]);
  assert.equal(manifest.configSchema.additionalProperties, false);
  const pkg = JSON.parse(fs.readFileSync(path.join(PLUGIN_DIR, "package.json"), "utf8"));
  assert.equal(pkg.type, "module");
  assert.deepEqual(pkg.openclaw.extensions, ["./index.ts"]);
  assert.equal(pkg.dependencies, undefined);
  assert.equal(entry.id, manifest.id);
  assert.deepEqual(Object.keys(entry.configSchema.jsonSchema.properties).sort(), Object.keys(manifest.configSchema.properties).sort());
});

test("register wires the policy, hooks, command and heartbeat service", async () => {
  const inst = makeInstall();
  try {
    const cfg = { repo: inst.root, python: PY, homeFile: path.join(inst.root, "private", "home.json"), publicReadonlyAgents: ["main"] };
    const { api, reg } = fakeApi(cfg);
    const rt = register(api);
    assert.ok(rt);
    assert.equal(reg.policies.length, 1);
    assert.equal(reg.policies[0].id, "jobhunter-guard");
    assert.equal(typeof reg.policies[0].evaluate, "function");
    assert.deepEqual(reg.hooks.map((h) => h.name).sort(), ["after_tool_call", "before_prompt_build", "before_tool_call", "resolve_exec_env"]);
    assert.equal(reg.commands.length, 1);
    const cmd = reg.commands[0];
    assert.equal(cmd.name, "jh");
    assert.equal(cmd.requireAuth, true);
    assert.equal(cmd.acceptsArgs, true);
    assert.deepEqual(cmd.requiredScopes, ["operator.admin"]); // no ownerFallback: host owner status required
    assert.equal(reg.services.length, 1);
    const blocked = reg.policies[0].evaluate({ toolName: "exec", params: { command: "sqlite3 x" } }, { agentId: "jobhunter-scout", sessionKey: "s" });
    assert.match(blocked.blockReason, /^G_EXEC_SHAPE: /);
    assert.equal(reg.policies[0].evaluate({ toolName: "exec", params: { command: "ls" } }, { agentId: "main" }), undefined);
    const envHook = reg.hooks.find((h) => h.name === "resolve_exec_env")!.handler;
    const env = envHook({ toolName: "exec", host: "gateway" }, { agentId: "jobhunter-scout" });
    assert.equal(env.JH_AGENT_ID, "jobhunter-scout");
    assert.match(env.JH_AGENT_PROOF, ENV_PROOF_RE);
    // the session key of the event is used when ctx has none
    const env2 = envHook({ toolName: "exec", host: "gateway", sessionKey: "agent:jobhunter-scout:cron:x" }, { agentId: "jobhunter-scout" });
    assert.equal(env2.JH_SESSION_KEY, "agent:jobhunter-scout:cron:x");
    const pin = reg.hooks.find((h) => h.name === "before_prompt_build")!.handler;
    assert.deepEqual(pin({}, { agentId: "jobhunter-evaluator" }), { toolsAllow: ["exec", "read", "write"] });
    assert.deepEqual(pin({}, { agentId: "jobhunter-scout" }), { toolsAllow: ["exec", "read", "write", "browser"] });
    assert.deepEqual(pin({}, { agentId: "jobhunter-qc" }), { toolsAllow: [] });
    assert.deepEqual(pin({}, { sessionKey: "agent:jobhunter-applier:cron:a" }), { toolsAllow: ["exec", "read", "write", "browser"] });
    assert.deepEqual(pin({}, { agentId: "jobhunter-rogue" }), { toolsAllow: [] });
    assert.equal(pin({}, { agentId: "main" }), undefined);
    const stray = pin({}, { agentId: "jobhunter-evaluator", sessionKey: "agent:jobhunter-evaluator:main", trigger: "user" });
    assert.deepEqual(stray.toolsAllow, ["exec", "read", "write"]);
    assert.match(stray.prependSystemContext, /G_STRAY_RUN/);
    const refused = await cmd.handler({ channel: "whatsapp", senderId: "+1", args: "status", isAuthorizedSender: true });
    assert.match(refused.text, /^G_NOT_OWNER/);
    await reg.services[0].start({});
    assert.ok(fs.existsSync(path.join(inst.root, "state", "guard", "heartbeat.json")));
    await reg.services[0].stop({});
  } finally {
    inst.cleanup();
  }
});

test("the backstop hook skips calls the trusted tier decided and decides the others", () => {
  const inst = makeInstall();
  try {
    inst.db.exec("PRAGMA foreign_keys = OFF");
    inst.db.prepare("INSERT INTO actions (token, kind, route, first_touch, platform, agent_id, status, reserved_at, expires_at, created_at, updated_at) VALUES ('TABCDEFGHJKM', 'application', 'browser', 0, 'greenhouse', 'jobhunter-applier', 'reserved', '2026-09-27T05:00:00Z', '2099-01-01T00:00:00Z', '2026-09-27T05:00:00Z', '2026-09-27T05:00:00Z')").run();
    const cfg = { repo: inst.root, python: PY, homeFile: path.join(inst.root, "private", "home.json"), publicReadonlyAgents: ["main"] };
    const { api, reg } = fakeApi(cfg);
    const rt = register(api)!;
    const ctx = { agentId: "jobhunter-applier", sessionKey: "s-bk" };
    rt.observe({ toolName: "browser", params: { action: "snapshot" }, result: { content: [{ type: "text", text: "- textbox \"Name\" [ref=e4]" }], details: { targetId: "t1", url: "https://job-boards.greenhouse.io/k/jobs/1" } } }, ctx);
    const hook = reg.hooks.find((h) => h.name === "before_tool_call")!.handler;
    const ev = { toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "type", ref: "e4", text: "Alex" }, toolCallId: "call-1" };
    const r1 = reg.policies[0].evaluate(ev, ctx);
    assert.equal(r1, undefined); // allowed, no rewrite
    assert.equal(hook(ev, ctx), undefined); // already decided in the trusted tier
    const log = path.join(inst.root, "state", "guard", "TABCDEFGHJKM.jsonl");
    assert.equal(fs.readFileSync(log, "utf8").trim().split("\n").length, 1);
    // a call that only reaches the hook (trusted tier missing) is decided by the hook
    const blocked = hook({ toolName: "exec", params: { command: "sqlite3 x" } }, ctx);
    assert.match(blocked.blockReason, /^G_EXEC_SHAPE/);
    const ev2 = { toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "type", ref: "e4", text: "Rivera" } };
    reg.policies[0].evaluate(ev2, ctx);
    assert.equal(hook(ev2, ctx), undefined); // matched without a toolCallId too
    assert.equal(fs.readFileSync(log, "utf8").trim().split("\n").length, 2);
    rt.close();
  } finally {
    inst.cleanup();
  }
});

test("with ownerFallback the command does not require host owner scopes", () => {
  const { api, reg } = fakeApi({ repo: "/r", python: PY, homeFile: "/r/private/home.json", publicReadonlyAgents: [], ownerFallback: [{ channel: "whatsapp", senderId: "+10000000000" }] });
  register(api);
  assert.equal(reg.commands[0].requiredScopes, undefined);
});

test("before_tool_call decides alone when trusted policies are unavailable", () => {
  const { api, reg } = fakeApi({ repo: "/r", python: PY, homeFile: "/r/private/home.json", publicReadonlyAgents: [] }, { trusted: false });
  register(api);
  assert.equal(reg.policies.length, 0);
  const h = reg.hooks.find((x) => x.name === "before_tool_call");
  assert.ok(h);
  assert.ok(h.opts.priority >= 1000);
  assert.match(h.handler({ toolName: "exec", params: { command: "ls" } }, { agentId: "jobhunter-scout" }).blockReason, /^G_GUARD_UNHEALTHY/);
});

test("an invalid config blocks jobhunter agents and nothing else", () => {
  const { api, reg } = fakeApi({ repo: "relative" });
  assert.equal(register(api), null);
  const ev = reg.policies[0].evaluate;
  assert.match(ev({ toolName: "read", params: {} }, { agentId: "jobhunter-scout" }).blockReason, /^G_GUARD_UNHEALTHY/);
  assert.match(ev({ toolName: "read", params: {} }, { sessionKey: "agent:jobhunter-qc:x" }).blockReason, /^G_GUARD_UNHEALTHY/);
  assert.equal(ev({ toolName: "read", params: {} }, { agentId: "main" }), undefined);
  assert.equal(reg.services.length, 0);
  assert.equal(entry.configSchema.safeParse({ repo: "relative" }).success, false);
  assert.equal(entry.configSchema.safeParse({ repo: "/r", python: PY, homeFile: "/r/h.json", publicReadonlyAgents: [] }).success, true);
});

test("the tool-surface pin: F-QC, switched off, and an invalid config", () => {
  const inst = makeInstall();
  try {
    const base = { repo: inst.root, python: PY, homeFile: path.join(inst.root, "private", "home.json"), publicReadonlyAgents: ["main"] };
    const pinOf = (cfg: Record<string, unknown>) => {
      const { api, reg } = fakeApi(cfg);
      const rt = register(api);
      const h = reg.hooks.find((x) => x.name === "before_prompt_build")!.handler;
      return { h, close: () => rt && rt.close() };
    };
    const fqc = pinOf({ ...base, qcVerdictFile: true });
    assert.deepEqual(fqc.h({}, { agentId: "jobhunter-qc" }), { toolsAllow: ["write"] });
    assert.deepEqual(fqc.h({}, { agentId: "jobhunter-evaluator" }), { toolsAllow: ["exec", "read", "write"] });
    fqc.close();
    const off = pinOf({ ...base, pinToolSurface: false });
    assert.equal(off.h({}, { agentId: "jobhunter-scout" }), undefined);
    off.close();
  } finally {
    inst.cleanup();
  }
  const { api, reg } = fakeApi({ repo: "relative" });
  register(api);
  const h = reg.hooks.find((x) => x.name === "before_prompt_build")!.handler;
  assert.deepEqual(h({}, { agentId: "jobhunter-scout" }), { toolsAllow: [] });
  assert.deepEqual(h({}, { sessionKey: "agent:jobhunter-qc:x" }), { toolsAllow: [] });
  assert.equal(h({}, { agentId: "main" }), undefined);
  // invalid config, stray run: still no tools, plus the notice
  const stray = h({}, { sessionKey: "agent:jobhunter-qc:main", trigger: "user" });
  assert.deepEqual(stray.toolsAllow, []);
  assert.match(stray.prependSystemContext, /^JOBHUNTER GUARD NOTICE \(G_STRAY_RUN\): this run of jobhunter-qc /);
  assert.deepEqual(h({}, { agentId: "jobhunter-scout", trigger: "cron" }), { toolsAllow: [] });
  assert.equal(h({}, { agentId: "main", trigger: "user" }), undefined);
});

test("the config schema accepts the CLI route keys and refuses bad values", () => {
  const ok = {
    repo: "/r", python: PY, homeFile: "/r/private/home.json", publicReadonlyAgents: ["main"],
    claudeNativeTools: "deny", pinToolSurface: true, proofCarriers: ["argv", "env"], recordEvents: false,
    protectedRoots: { read: ["/r/private", "/h/.openclaw"], write: ["/r", "/w", "/h/.openclaw"] }, qcVerdictFile: false,
  };
  assert.equal(entry.configSchema.safeParse(ok).success, true);
  for (const props of ["claudeNativeTools", "pinToolSurface", "proofCarriers", "recordEvents", "protectedRoots", "qcVerdictFile"]) {
    assert.ok(Object.prototype.hasOwnProperty.call(entry.configSchema.jsonSchema.properties, props), props);
  }
  const bad: Array<Record<string, unknown>> = [
    { claudeNativeTools: "full" },
    { pinToolSurface: "yes" },
    { proofCarriers: [] },
    { proofCarriers: ["argv", "argv"] },
    { proofCarriers: ["cookie"] },
    { recordEvents: 1 },
    { protectedRoots: { read: ["relative"] } },
    { protectedRoots: ["/r"] },
    { qcVerdictFile: "true" },
  ];
  for (const b of bad) assert.equal(entry.configSchema.safeParse({ ...ok, ...b }).success, false, JSON.stringify(b));
  const manifest = JSON.parse(fs.readFileSync(path.join(PLUGIN_DIR, "openclaw.plugin.json"), "utf8"));
  assert.deepEqual(manifest.configSchema.properties.proofCarriers.items.enum, ["argv", "env"]);
  assert.deepEqual(manifest.configSchema.properties.claudeNativeTools.enum, ["deny", "gate"]);
  assert.equal(manifest.configSchema.properties.protectedRoots.additionalProperties, false);
});

test("cli-metadata mode registers nothing", () => {
  const { api, reg } = fakeApi({ repo: "/r", python: PY, homeFile: "/r/private/home.json", publicReadonlyAgents: [] }, { mode: "cli-metadata" });
  assert.equal(register(api), null);
  assert.equal(reg.policies.length + reg.hooks.length + reg.commands.length + reg.services.length, 0);
});

test("jh.py runner: no shell, clean env, last JSON envelope", async () => {
  assert.deepEqual(lastJsonObject("noise\n{\"ok\": true, \"code\": \"OK\"}\n"), { ok: true, code: "OK" });
  assert.equal(lastJsonObject("NO_REPLY"), null);
  const env = childEnv({
    PATH: "/usr/bin", OPENCLAW_SHELL: "exec", JH_AGENT_ID: "jobhunter-scout", JH_AGENT_PROOF: "jhe2.x", JOBHUNTER_HOME: "/x", JOBHUNTER_DB: "/x/db", HOME: "/h",
    OPENCLAW_CHANNEL_CONTEXT: "c", OPENCLAW_MCP_TOKEN: "t", OPENCLAW_MCP_URL: "u", CLAUDECODE: "1", CLAUDE_CODE_ENTRYPOINT: "cli", CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS: "1",
    OPENCLAW_STATE_DIR: "/s", CLAUDE_CONFIG_DIR: "/c",
  });
  for (const k of ["OPENCLAW_SHELL", "JH_AGENT_ID", "JH_AGENT_PROOF", "JOBHUNTER_HOME", "JOBHUNTER_DB", "OPENCLAW_CHANNEL_CONTEXT", "OPENCLAW_MCP_TOKEN", "OPENCLAW_MCP_URL", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"]) {
    assert.equal(env[k], undefined, k);
  }
  assert.equal(env.HOME, "/h");
  assert.equal(env.OPENCLAW_STATE_DIR, "/s"); // only the 5.5 list is dropped
  assert.equal(env.CLAUDE_CONFIG_DIR, "/c");
  if (!fs.existsSync(PY)) return;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "jhg-run-"));
  try {
    const script = path.join(dir, "jh.py");
    fs.writeFileSync(script, [
      "import json, os, sys",
      "print(json.dumps({'ok': True, 'argv': sys.argv[1:], 'shell': os.environ.get('OPENCLAW_SHELL'), 'agent': os.environ.get('JH_AGENT_ID')}))",
      "sys.exit(5 if 'stop' in sys.argv else 0)",
    ].join("\n"));
    process.env.OPENCLAW_SHELL = "exec";
    const run = makeJhRunner(PY, script, dir);
    const r = await run(["status", "a b; rm -rf /"]);
    delete process.env.OPENCLAW_SHELL;
    assert.equal(r.exitCode, 0);
    assert.deepEqual(r.envelope?.argv, ["status", "a b; rm -rf /"]);
    assert.equal(r.envelope?.shell, null);
    const r5 = await run(["stop"]);
    assert.equal(r5.exitCode, 5);
    const missing = await makeJhRunner("/nonexistent/python3", script, dir)(["status"]);
    assert.equal(missing.exitCode, -1);
  } finally {
    delete process.env.OPENCLAW_SHELL;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
