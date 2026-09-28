// Plugin entry and manifest: registrations against a fake OpenClaw api, and the jh.py runner.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import entry, { register } from "../index.ts";
import { childEnv, lastJsonObject, makeJhRunner } from "../src/jhcall.ts";
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
    assert.deepEqual(reg.hooks.map((h) => h.name).sort(), ["after_tool_call", "before_tool_call", "resolve_exec_env"]);
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
    const env = reg.hooks.find((h) => h.name === "resolve_exec_env")!.handler({ toolName: "exec", host: "gateway" }, { agentId: "jobhunter-scout" });
    assert.equal(env.JH_AGENT_ID, "jobhunter-scout");
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
    const ev = { toolName: "browser", params: { action: "act", kind: "type", ref: "e4", text: "Alex" }, toolCallId: "call-1" };
    const r1 = reg.policies[0].evaluate(ev, ctx);
    assert.deepEqual(r1.params.profile, "jobhunter");
    assert.equal(hook(ev, ctx), undefined); // already decided in the trusted tier
    const log = path.join(inst.root, "state", "guard", "TABCDEFGHJKM.jsonl");
    assert.equal(fs.readFileSync(log, "utf8").trim().split("\n").length, 1);
    // a call that only reaches the hook (trusted tier missing) is decided by the hook
    const blocked = hook({ toolName: "exec", params: { command: "sqlite3 x" } }, ctx);
    assert.match(blocked.blockReason, /^G_EXEC_SHAPE/);
    const ev2 = { toolName: "browser", params: { action: "act", kind: "type", ref: "e4", text: "Rivera" } };
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

test("cli-metadata mode registers nothing", () => {
  const { api, reg } = fakeApi({ repo: "/r", python: PY, homeFile: "/r/private/home.json", publicReadonlyAgents: [] }, { mode: "cli-metadata" });
  assert.equal(register(api), null);
  assert.equal(reg.policies.length + reg.hooks.length + reg.commands.length + reg.services.length, 0);
});

test("jh.py runner: no shell, clean env, last JSON envelope", async () => {
  assert.deepEqual(lastJsonObject("noise\n{\"ok\": true, \"code\": \"OK\"}\n"), { ok: true, code: "OK" });
  assert.equal(lastJsonObject("NO_REPLY"), null);
  const env = childEnv({ PATH: "/usr/bin", OPENCLAW_SHELL: "exec", JH_AGENT_ID: "jobhunter-scout", JOBHUNTER_HOME: "/x", HOME: "/h" });
  assert.equal(env.OPENCLAW_SHELL, undefined);
  assert.equal(env.JH_AGENT_ID, undefined);
  assert.equal(env.JOBHUNTER_HOME, undefined);
  assert.equal(env.HOME, "/h");
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
