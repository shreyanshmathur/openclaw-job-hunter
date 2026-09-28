// Runtime tests against a temporary install: recorded transcripts replayed through the guard, the
// token log (12.17), the heartbeat (12.18), the stop file and jh.py detect, exec env and /jh.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createHmac } from "node:crypto";
import { makeInstall, replay, writeConsent, INSTALL_ID, KEY_HEX } from "./_install.ts";
import { GuardRuntime, parseConfig, manifestHashes, realpathLoose, stopPlatform, textWindow } from "../src/runtime.ts";
import { parseKey, verifyGrant } from "../src/grant.ts";
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
    const r = rt2.evaluate({ toolName: "browser", params: { action: "act", kind: "click", ref: "e6" } }, { agentId: "jobhunter-applier", sessionKey: "s-new" });
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

test("heartbeat file (12.18)", () => {
  const inst = makeInstall();
  try {
    inst.now.ms = Date.parse("2026-09-27T05:10:00Z");
    assert.equal(inst.runtime.heartbeat(), true);
    const hb = JSON.parse(fs.readFileSync(path.join(inst.root, "state", "guard", "heartbeat.json"), "utf8"));
    assert.equal(hb.install_id, INSTALL_ID);
    assert.equal(hb.version, "2.0.0");
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
    const [ts, mac] = String(env?.JH_AGENT_PROOF).split(".");
    assert.equal(mac, createHmac("sha256", parseKey(KEY_HEX)).update("agent\njobhunter-applier\n" + ts).digest("hex"));
    assert.equal(inst.runtime.execEnv({ agentId: "main" }), undefined);
    assert.equal(inst.runtime.execEnv({ sessionKey: "agent:jobhunter-scout:x" })?.JH_AGENT_ID, "jobhunter-scout");
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

test("config parsing and helpers", () => {
  assert.throws(() => parseConfig({}));
  assert.throws(() => parseConfig({ repo: "relative", python: "/usr/bin/python3", homeFile: "/a/b", publicReadonlyAgents: [] }));
  assert.throws(() => parseConfig({ repo: "/a b", python: "/usr/bin/python3", homeFile: "/a/b", publicReadonlyAgents: [] }));
  const c = parseConfig({ repo: "/r", python: "/usr/bin/python3", homeFile: "/r/private/home.json", publicReadonlyAgents: ["main", "jobhunter-scout"] });
  assert.deepEqual(c.publicReadonlyAgents, ["main"]);
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
    const r = inst.runtime.evaluate({ toolName: "browser", params: { action: "act", kind: "type", ref: "e4", text: "Alex" } }, { agentId: "jobhunter-applier", sessionKey: "s" });
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
    const fill = inst.runtime.evaluate({ toolName: "browser", params: { action: "act", kind: "click", ref: "e5" } }, ctx);
    assert.match(String(fill && (fill as { blockReason?: string }).blockReason), /^G_STOPPED/);
    const ex = inst.runtime.evaluate({ toolName: "exec", params: { command: PY + " " + inst.root + "/scripts/jh.py job set-status JABCDEFG --status needs_human --reason captcha_visible" } }, ctx);
    assert.ok(ex && "params" in ex);
    await new Promise((res) => setTimeout(res, 10));
    const call = inst.jhCalls.find((a) => a[0] === "detect");
    assert.ok(call);
    const payload = JSON.parse(fs.readFileSync(call[2], "utf8"));
    assert.equal(payload.platform, "ats");
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
    const fill = inst.runtime.evaluate({ toolName: "browser", params: { action: "act", kind: "click", ref: "e5" } }, ctx);
    assert.doesNotMatch(String(fill && (fill as { blockReason?: string }).blockReason), /G_STOPPED/);

    const cap = inst.runtime.observe({ toolName: "browser", params: { action: "act", request: call }, result: readFormResult(url, { captcha_visible: true, account_wall: false }) }, ctx);
    assert.deepEqual(cap, { stopped: false, softStopped: true, code: "ats_captcha" });
    const after = inst.runtime.evaluate({ toolName: "browser", params: { action: "act", kind: "click", ref: "e5" } }, ctx);
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
