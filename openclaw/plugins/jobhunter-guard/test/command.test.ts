import test from "node:test";
import assert from "node:assert/strict";
import { buildArgv, handleJh, HELP_TEXT, isOwner, parseJh, replyFromResult } from "../src/command.ts";
import { parseKey, verifyGrant } from "../src/grant.ts";
import type { JhResult } from "../src/jhcall.ts";

const key = parseKey("ab".repeat(32));
const fallback = [{ channel: "whatsapp", senderId: "+10000000000" }];

test("parse /jh subcommands (12.19 mapping)", () => {
  assert.deepEqual(parseJh("approve a7k9"), { command: "approve", args: ["A7K9", "--by", "chat"] });
  assert.deepEqual(parseJh("skip A7K9 not now, maybe later"), { command: "skip", args: ["A7K9", "--reason", "not now, maybe later"] });
  assert.deepEqual(parseJh("skip A7K9"), { command: "skip", args: ["A7K9"] });
  assert.deepEqual(parseJh("edit A7K9 Hi Alex,\nThanks for the note."), { command: "edit", args: ["A7K9", "--text", "Hi Alex,\nThanks for the note."] });
  assert.deepEqual(parseJh("answer Q3 30 days"), { command: "profile answer", args: ["--field", "Q3", "--value", "30 days"] });
  assert.deepEqual(parseJh("pause"), { command: "pause", args: ["--scope", "all"] });
  assert.deepEqual(parseJh("pause linkedin"), { command: "pause", args: ["--scope", "linkedin"] });
  assert.deepEqual(parseJh("pause site:naukri"), { command: "pause", args: ["--scope", "site:naukri"] });
  assert.deepEqual(parseJh("status"), { command: "status", args: [] });
  assert.deepEqual(parseJh("inbox"), { command: "inbox", args: [] });
  assert.deepEqual(parseJh("lower gmail.ceilings.conservative.cold_day 10"), { command: "config lower", args: ["gmail.ceilings.conservative.cold_day", "10"] });
  assert.deepEqual(parseJh(""), { help: true });
  assert.deepEqual(parseJh("help"), { help: true });
  for (const bad of ["approve", "approve A7K", "approve A7K9 extra", "approve B0O1", "edit A7K9", "answer", "pause everything", "lower x 1", "lower a.b --1", "unpause", "breaker reset linkedin", "raise a.b 5", "status now"]) {
    assert.ok("error" in (parseJh(bad) as object), bad);
  }
});

test("owner check", () => {
  assert.equal(isOwner({ senderIsOwner: true }, undefined), true);
  assert.equal(isOwner({ senderIsOwner: false, senderId: "+10000000000", channel: "whatsapp" }, fallback), true);
  assert.equal(isOwner({ senderId: "10000000000", channel: "whatsapp" }, fallback), true);
  assert.equal(isOwner({ senderId: "stranger-1", channel: "whatsapp" }, fallback), false);
  assert.equal(isOwner({ senderId: "+10000000000", channel: "telegram" }, fallback), false);
  assert.equal(isOwner({ senderId: "+10000000000", channel: "whatsapp" }, undefined), false);
  assert.equal(isOwner({}, fallback), false);
});

test("argv puts the grant and --human before the command words", () => {
  assert.deepEqual(buildArgv({ command: "profile answer", args: ["--field", "Q3", "--value", "x"] }, "G"), ["--grant", "G", "--human", "profile", "answer", "--field", "Q3", "--value", "x"]);
});

function fakeRunner(result: Partial<JhResult> = {}) {
  const calls: string[][] = [];
  const run = async (argv: string[]) => {
    calls.push(argv);
    return { exitCode: 0, stdout: "Approved A7K9. It goes out after 10:40.", stderr: "", envelope: null, error: null, ...result };
  };
  return { calls, run };
}

test("a non-owner is refused and nothing runs", async () => {
  const r = fakeRunner();
  const logs: Record<string, unknown>[] = [];
  const out = await handleJh({ senderId: "stranger-2", channel: "whatsapp", args: "approve A7K9" }, { key: () => key, run: r.run, ownerFallback: fallback, log: (e) => logs.push(e) });
  assert.match(out.text, /^G_NOT_OWNER/);
  assert.equal(r.calls.length, 0);
  assert.equal(logs[0].code, "G_NOT_OWNER");
});

test("the owner's approve runs jh.py with a valid grant", async () => {
  const r = fakeRunner();
  const now = 1790000000;
  const out = await handleJh({ senderIsOwner: true, channel: "whatsapp", args: "approve A7K9" }, { key: () => key, run: r.run, ownerFallback: undefined, nowS: () => now });
  assert.equal(out.text, "Approved A7K9. It goes out after 10:40.");
  assert.equal(r.calls.length, 1);
  const argv = r.calls[0];
  assert.equal(argv[0], "--grant");
  assert.deepEqual(argv.slice(2), ["--human", "approve", "A7K9", "--by", "chat"]);
  assert.equal(verifyGrant(key, argv[1], "approve", ["A7K9", "--by", "chat"], now), true);
});

test("help, usage errors and a missing key", async () => {
  const r = fakeRunner();
  assert.equal((await handleJh({ senderIsOwner: true, args: "" }, { key: () => key, run: r.run, ownerFallback: undefined })).text, HELP_TEXT);
  assert.match((await handleJh({ senderIsOwner: true, args: "approve" }, { key: () => key, run: r.run, ownerFallback: undefined })).text, /usage/);
  assert.match((await handleJh({ senderIsOwner: true, args: "status" }, { key: () => null, run: r.run, ownerFallback: undefined })).text, /^G_GUARD_UNHEALTHY/);
  assert.equal(r.calls.length, 0);
});

test("reply text from jh.py results", () => {
  assert.equal(replyFromResult({ exitCode: 0, stdout: "", stderr: "", error: null }), "Done.");
  assert.match(replyFromResult({ exitCode: 11, stdout: "", stderr: "x", error: null }), /exit 11/);
  assert.match(replyFromResult({ exitCode: -1, stdout: "", stderr: "", error: "timeout" }), /could not run/);
  assert.match(replyFromResult({ exitCode: 0, stdout: "x".repeat(5000), stderr: "", error: null }), /truncated/);
});

test("help text is ASCII and has no spaced hyphen dashes", () => {
  assert.ok(/^[\x0a\x20-\x7e]*$/.test(HELP_TEXT));
  assert.ok(!/ - /.test(HELP_TEXT));
});

// ------------------------------------------------------------------ /jh continue (CAPTCHA hand-off, 3.3)

test("/jh continue <code> parses; bad shapes are refused with the usage error", () => {
  assert.deepEqual(parseJh("continue K7QA"), { command: "continue", args: ["K7QA"] });
  assert.deepEqual(parseJh("continue k7qa"), { command: "continue", args: ["K7QA"] });
  assert.deepEqual(parseJh("Continue  K7QA  "), { command: "continue", args: ["K7QA"] });
  for (const bad of ["continue", "continue K7Q", "continue K7QAX", "continue K7QA now", "continue B0O1", "continue K7-A", "continue --grant x"]) {
    const p = parseJh(bad) as { error?: string };
    assert.ok("error" in p, bad);
    assert.equal(p.error, "usage: /jh continue <4-character code>", bad);
  }
  assert.ok(HELP_TEXT.split("\n").includes("/jh continue <code>  after you solved a CAPTCHA in the agent's browser window"));
});

test("/jh continue: the owner runs jh.py continue with a valid grant; a non-owner gets G_NOT_OWNER and nothing runs", async () => {
  const r = fakeRunner({ stdout: "Resolved. The job goes back to the front of the apply queue." });
  const now = 1790000000;
  const out = await handleJh({ senderIsOwner: true, channel: "whatsapp", args: "continue K7QA" }, { key: () => key, run: r.run, ownerFallback: undefined, nowS: () => now });
  assert.match(out.text, /^Resolved/);
  assert.equal(r.calls.length, 1);
  assert.deepEqual(r.calls[0].slice(2), ["--human", "continue", "K7QA"]);
  assert.equal(verifyGrant(key, r.calls[0][1], "continue", ["K7QA"], now), true);

  const r2 = fakeRunner();
  const logs: Record<string, unknown>[] = [];
  for (const ctx of [{ senderId: "stranger-4", channel: "whatsapp" }, { senderIsOwner: false }, { senderId: "+10000000000", channel: "telegram" }]) {
    const refused = await handleJh({ ...ctx, args: "continue K7QA" }, { key: () => key, run: r2.run, ownerFallback: fallback, log: (e) => logs.push(e) });
    assert.match(refused.text, /^G_NOT_OWNER/);
  }
  assert.equal(r2.calls.length, 0);
  assert.ok(logs.every((e) => e.code === "G_NOT_OWNER"));
  const usage = await handleJh({ senderIsOwner: true, args: "continue K7" }, { key: () => key, run: r2.run, ownerFallback: undefined });
  assert.match(usage.text, /^usage: \/jh continue/);
  assert.equal(r2.calls.length, 0);
});
