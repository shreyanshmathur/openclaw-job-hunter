// Per-site consent (Chrome-session change): the guard refuses every browser call of a jobhunter agent on a
// site that has no active row in private/consent.json, failing closed when the file cannot be used.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { activeSites, readConsent } from "../src/consent.ts";
import { classifyUrl, consentSiteFor, parseHostsConfig } from "../src/browser.ts";
import { decide, hasConsent } from "../src/policy.ts";
import { BLOCK_CODES } from "../src/types.ts";
import { ALL_CONSENT, REPO, consentOf, loadHosts, reservedToken, snap } from "./_helpers.ts";
import { consentJson, makeInstall, writeConsent } from "./_install.ts";

const hosts = loadHosts();
const G1 = "2026-09-26T08:00:00Z";
const G2 = "2026-09-26T10:00:00Z";
const LI = "https://www.linkedin.com/in/example-person/";
const NK = "https://www.naukri.com/job-listings-data-analyst-kestrel-commerce-1";
const GH = "https://job-boards.greenhouse.io/kestrel/jobs/1";
const GM = "https://mail.google.com/mail/u/0/#inbox";

// One row as install.record_consent writes it (fictional Chrome profile).
function row(site: string, extra: Record<string, unknown> = {}) {
  return {
    site, status: "granted", method: "chrome_import", domains: [site + ".example"], chrome_profile: "Profile 1",
    chrome_profile_name: "Personal", granted_at: G1, revoked_at: null, declined_at: null, by: "owner", ...extra,
  };
}

function doc(...rows: Array<Record<string, unknown>>) {
  const sites: Record<string, unknown> = {};
  for (const r of rows) sites[String(r.site)] = r;
  return { version: 1, updated_at: G2, sites };
}

function sorted(s: Set<string>): string[] {
  return [...s].sort();
}

function outcome(d: ReturnType<typeof decide>): string {
  return d.kind === "block" ? d.code : d.kind;
}

// ------------------------------------------------------------------ the file

test("active consent rows: status granted, as install.consent_active", () => {
  assert.deepEqual(sorted(activeSites(doc(row("linkedin"), row("naukri"), row("gmail", { method: "manual_login", chrome_profile: null })))), ["gmail", "linkedin", "naukri"]);
  assert.deepEqual(sorted(activeSites(JSON.parse(consentJson(["linkedin", "naukri"], ["naukri"])))), ["linkedin"]);
  assert.deepEqual(sorted(activeSites({ version: 1, sites: {} })), []);
  // the row's own "site" may be left out, but may not name another site
  assert.deepEqual(sorted(activeSites({ sites: { linkedin: { status: "granted", granted_at: G1 } } })), ["linkedin"]);
  assert.deepEqual(sorted(activeSites({ sites: { linkedin: row("naukri") } })), []);
});

test("revoked, declined and incomplete rows give no consent (per-site default No)", () => {
  const off = [
    row("linkedin", { status: "revoked", revoked_at: G2 }),
    row("linkedin", { status: "declined", declined_at: G2 }),
    row("linkedin", { status: "none" }),
    row("linkedin", { status: "Granted" }),
    row("linkedin", { status: "active" }),
    row("linkedin", { status: true }),
    row("linkedin", { status: undefined }),
    row("linkedin", { revoked_at: G2 }), // granted but carrying a revoke time: revoked
    row("linkedin", { granted_at: null }),
    row("linkedin", { granted_at: "" }),
    row("linkedin", { granted_at: 1 }),
  ];
  for (const r of off) assert.deepEqual(sorted(activeSites(doc(r))), [], JSON.stringify(r));
  // other shapes are not read (the core does not read them either)
  for (const raw of [[row("linkedin")], { sites: [row("linkedin")] }, { consents: [row("linkedin")] }, { linkedin: row("linkedin") }, null, "linkedin", 3]) {
    assert.deepEqual(sorted(activeSites(raw)), [], JSON.stringify(raw));
  }
  // a site name is compared as written
  assert.deepEqual(sorted(activeSites({ sites: { LinkedIn: row("LinkedIn") } })), ["LinkedIn"]);
  assert.equal(hasConsent(snap({ consent: { ok: true, sites: activeSites({ sites: { LinkedIn: row("LinkedIn") } }), reason: "" } }), "linkedin"), false);
});

test("readConsent fails closed on a missing, linked, shared-writable or malformed file", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "jhg-consent-"));
  try {
    const file = path.join(dir, "consent.json");
    let c = readConsent(file);
    assert.equal(c.ok, false);
    assert.match(c.reason, /missing/);
    assert.equal(c.sites.size, 0);

    fs.writeFileSync(file, JSON.stringify(doc(row("linkedin"), row("naukri", { status: "revoked", revoked_at: G2 }))), { mode: 0o600 });
    c = readConsent(file);
    assert.equal(c.ok, true);
    assert.deepEqual(sorted(c.sites), ["linkedin"]);

    fs.chmodSync(file, 0o644); // readable by others is fine, the owner alone writes it
    assert.equal(readConsent(file).ok, true);
    for (const mode of [0o666, 0o620, 0o602]) {
      fs.chmodSync(file, mode);
      c = readConsent(file);
      assert.equal(c.ok, false, mode.toString(8));
      assert.match(c.reason, /writable by other users/);
    }
    fs.chmodSync(file, 0o600);

    const link = path.join(dir, "link.json");
    fs.symlinkSync(file, link);
    assert.match(readConsent(link).reason, /is a link/);
    assert.equal(readConsent(link).ok, false);
    assert.match(readConsent(dir).reason, /not a file/);

    for (const bad of ["{", "null", "[1, 2", "\"linkedin\"", "42", "[]", "{\"version\": 1}", "{\"sites\": []}"]) {
      fs.writeFileSync(file, bad);
      assert.equal(readConsent(file).ok, false, bad);
    }
    fs.writeFileSync(file, " ".repeat(1024 * 1024 + 1));
    assert.match(readConsent(file).reason, /too large/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("the core's own consent writer and the guard agree", { skip: !fs.existsSync(path.join(REPO, "scripts", "jobhunter", "install.py")) }, () => {
  const src = fs.readFileSync(path.join(REPO, "scripts", "jobhunter", "install.py"), "utf8");
  if (!src.includes("def record_consent")) return;
  const m = /^CONSENT_SITES = \{([\s\S]*?)^\}/m.exec(src);
  assert.ok(m, "install.py lists CONSENT_SITES");
  const coreSites = [...m[1].matchAll(/^\s{4}"([a-z0-9_-]+)":/gm)].map((x) => x[1]).sort();
  // every site the guard asks consent for is one the owner can grant, and the reverse
  const guardSites = [...new Set(hosts.platforms.map((p) => p.consent).filter((c): c is string => !!c))].sort();
  assert.deepEqual(guardSites, coreSites);
  assert.match(src, /row\.get\("status"\) == "granted"/);
});

// ------------------------------------------------------------------ guard-hosts.json

test("guard-hosts.json: login sites need consent, ATS forms do not", () => {
  const want: Record<string, string | null> = {
    linkedin: "linkedin", gmail: "gmail", naukri: "naukri", indeed: "indeed", glassdoor: "glassdoor", foundit: "foundit",
    instahyre: "instahyre", wellfound: "wellfound", greenhouse: null, lever: null, workday: null,
  };
  for (const [key, site] of Object.entries(want)) assert.equal(hosts.platforms.find((p) => p.key === key)?.consent, site, key);
  for (const p of hosts.platforms) {
    if (p.scope === "ats") assert.equal(p.consent, null, p.key);
    else assert.equal(p.consent, p.key, p.key);
  }
  assert.equal(classifyUrl("https://in.linkedin.com/feed/", hosts).platform?.consent, "linkedin");
  assert.equal(classifyUrl("https://www.glassdoor.co.in/Job/x", hosts).platform?.consent, "glassdoor");
  assert.equal(classifyUrl("https://kestrel.example/careers", hosts).platform, null);
  assert.ok(BLOCK_CODES.includes("G_NO_CONSENT"));
  assert.ok(ALL_CONSENT.has("gmail") && !ALL_CONSENT.has("greenhouse"));
});

test("a platform entry without a consent key fails closed", () => {
  assert.equal(consentSiteFor("newboard", "site:newboard", undefined), "newboard");
  assert.equal(consentSiteFor("newboard", "site:newboard", null), "newboard");
  assert.equal(consentSiteFor("linkedin", "linkedin", undefined), "linkedin");
  assert.equal(consentSiteFor("gmail", "gmail", undefined), "gmail");
  assert.equal(consentSiteFor("newats", "ats", undefined), null);
  assert.equal(consentSiteFor("newats", "ats", false), null);
  assert.equal(consentSiteFor("x", "site:x", "other"), "other");
  for (const bad of [1, true, "", "Site:Other", "a b"]) assert.throws(() => consentSiteFor("x", "site:x", bad), String(bad));
  const raw = JSON.parse(fs.readFileSync(path.join(REPO, "openclaw", "guard-hosts.json"), "utf8"));
  raw.platforms.newboard = { scope: "site:newboard", hosts: ["newboard.example"], aliases: [] };
  assert.equal(parseHostsConfig(raw).platforms.find((p) => p.key === "newboard")?.consent, "newboard");
  raw.platforms.newboard.consent = true;
  assert.throws(() => parseHostsConfig(raw));
});

// ------------------------------------------------------------------ decide()

const SC = "jobhunter-scout";
const AP = "jobhunter-applier";
const OUT = "jobhunter-outreach";

function nav(url: string) {
  return { toolName: "browser", params: { profile: "jobhunter", action: "navigate", targetUrl: url } };
}

function run(agent: string, ev: { toolName: string; params: Record<string, unknown> }, over: Parameters<typeof snap>[0] = {}) {
  return decide(ev, { agentId: agent, sessionKey: "agent:" + agent + ":cron:x" }, snap(over));
}

test("no consent row: navigation to the site is refused for every lane", () => {
  const none = consentOf([]);
  for (const agent of [SC, AP, OUT]) {
    for (const url of [LI, NK, GM, "https://lnkd.in/abc", "https://www.indeed.com/viewjob?jk=1"]) {
      const d = run(agent, nav(url), { consent: none });
      assert.equal(outcome(d), "G_NO_CONSENT", agent + " " + url);
      if (d.kind === "block") assert.match(d.reason, /not allowed the agent to use (linkedin|naukri|gmail|indeed)/);
    }
    // public pages and ATS forms need no consent
    for (const url of [GH, "https://jobs.lever.co/kestrel/1", "https://kestrel.example/careers", "about:blank"]) {
      assert.equal(outcome(run(agent, nav(url), { consent: none })), "allow", agent + " " + url);
    }
  }
});

test("consent is per site: an allowed site does not open the others", () => {
  const onlyLi = consentOf(["linkedin"]);
  assert.equal(outcome(run(OUT, nav(LI), { consent: onlyLi })), "allow");
  assert.equal(outcome(run(SC, nav(NK), { consent: onlyLi })), "G_NO_CONSENT");
  assert.equal(outcome(run(OUT, nav(GM), { consent: onlyLi })), "G_NO_CONSENT");
  assert.equal(outcome(run(SC, nav(NK), { consent: consentOf(["naukri"]) })), "allow");
});

test("fail closed: no consent state, or a file the runtime could not use", () => {
  const noField = snap();
  delete (noField as { consent?: unknown }).consent;
  assert.equal(hasConsent(noField, "linkedin"), false);
  assert.equal(outcome(decide(nav(LI), { agentId: SC }, noField)), "G_NO_CONSENT");
  assert.equal(outcome(decide(nav(GH), { agentId: SC }, noField)), "allow");
  // ok false with sites listed (should never happen) still refuses
  const broken = { ok: false, sites: new Set(["linkedin"]), reason: "consent.json: is not valid JSON" };
  const d = run(SC, nav(LI), { consent: broken });
  assert.equal(outcome(d), "G_NO_CONSENT");
  if (d.kind === "block") assert.match(d.reason, /is not valid JSON/);
  assert.equal(outcome(run(SC, nav(LI), { consent: { ok: true, sites: ["linkedin"] as unknown as Set<string>, reason: "" } })), "G_NO_CONSENT");
});

test("write actions on a site without consent are refused even with a live token", () => {
  const tok = reservedToken("naukri", "application");
  const refs = { e3: { role: "textbox", name: "Full name" } };
  const typeCall = { toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "type", ref: "e3", text: "Alex Rivera" } };
  assert.equal(outcome(run(AP, typeCall, { currentUrl: NK, token: tok, refs })), "allow");
  const d = run(AP, typeCall, { currentUrl: NK, token: tok, refs, consent: consentOf(["linkedin"]) });
  assert.equal(outcome(d), "G_NO_CONSENT");
  if (d.kind === "block") {
    assert.equal(d.actionClass, "fill");
    assert.equal(d.host, "www.naukri.com");
  }
  // Gmail web route: compose under a cold_email token needs gmail consent
  const gm = reservedToken("gmail", "cold_email");
  const compose = { toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "click", ref: "e1" } };
  assert.equal(outcome(run(OUT, compose, { currentUrl: GM, token: gm, refs: { e1: { role: "button", name: "Compose" } } })), "allow");
  assert.equal(outcome(run(OUT, compose, { currentUrl: GM, token: gm, refs: { e1: { role: "button", name: "Compose" } }, consent: consentOf([]) })), "G_NO_CONSENT");
});

test("a tab already on a site without consent: only close and navigating away are allowed", () => {
  const none = consentOf(["gmail"]);
  const on = { currentUrl: LI, consent: none, refs: { e1: { role: "link", name: "Jobs" } } };
  for (const params of [
    { action: "snapshot" },
    { action: "screenshot" },
    { action: "tabs" }, // a tab list carries every tab's title
    { action: "act", kind: "click", ref: "e1" },
    { action: "act", kind: "press", key: "PageDown" },
  ]) {
    assert.equal(outcome(run(SC, { toolName: "browser", params: { profile: "jobhunter", ...params } }, on)), "G_NO_CONSENT", JSON.stringify(params));
  }
  for (const params of [{ action: "close" }, { action: "close", targetId: "T1" }]) {
    assert.equal(outcome(run(SC, { toolName: "browser", params: { profile: "jobhunter", ...params } }, on)), "allow", JSON.stringify(params));
  }
  assert.equal(outcome(run(SC, nav(GM), on)), "allow");
  assert.equal(outcome(run(SC, nav(GH), on)), "allow");
  assert.equal(outcome(run(SC, nav(NK), on)), "G_NO_CONSENT");
});

test("after a consent revoke (row gone, site breaker tripped) the agent can still close its tab, not list tabs", () => {
  // `browser consent revoke gmail` marks the row revoked and trips the gmail breaker (consent_revoked).
  const revoked = { currentUrl: GM, consent: consentOf(["linkedin"]), openBreakers: new Set(["gmail"]) };
  const call = (params: Record<string, unknown>) => ({ toolName: "browser", params: { profile: "jobhunter", ...params } });
  for (const agent of [SC, OUT]) {
    for (const params of [{ action: "close" }, { action: "close", targetId: "T1" }]) {
      const d = run(agent, call(params), revoked);
      assert.equal(outcome(d), "allow", agent + " " + JSON.stringify(params));
      if (d.kind === "allow") assert.equal(d.actionClass, "read");
    }
    // the Gmail tab's title shows the account address: listing tabs is a read of that page
    for (const params of [{ action: "tabs" }, { action: "snapshot" }]) {
      const d = run(agent, call(params), revoked);
      assert.equal(outcome(d), "G_NO_CONSENT", agent + " " + JSON.stringify(params));
      if (d.kind === "block") assert.equal(d.host, "mail.google.com");
    }
  }
  // consent still active but the site or global breaker open: only close passes
  for (const scope of ["gmail", "pause:gmail", "global", "pause:all"]) {
    const on = { currentUrl: GM, openBreakers: new Set([scope]) };
    assert.equal(outcome(run(OUT, call({ action: "close" }), on)), "allow", scope);
    assert.equal(outcome(run(OUT, call({ action: "tabs" }), on)), "G_BREAKER_OPEN", scope);
    assert.equal(outcome(run(OUT, call({ action: "snapshot" }), on)), "G_BREAKER_OPEN", scope);
    assert.equal(outcome(run(OUT, nav(GM), on)), "G_BREAKER_OPEN", scope);
  }
  // a breaker of another site does not stop a tab list on a page of a site with consent
  assert.equal(outcome(run(OUT, call({ action: "tabs" }), { currentUrl: GM, openBreakers: new Set(["linkedin"]) })), "allow");
  assert.equal(outcome(run(OUT, call({ action: "tabs" }), { currentUrl: GM })), "allow");
  // the owner's kill switch still stops everything, close included
  assert.equal(outcome(run(OUT, call({ action: "close" }), { ...revoked, paused: true })), "G_BREAKER_OPEN");
  assert.equal(outcome(run(OUT, call({ action: "tabs" }), { ...revoked, paused: true })), "G_NO_CONSENT");
  for (const params of [{ action: "close" }, { action: "tabs" }]) {
    assert.equal(outcome(run(OUT, call(params), { currentUrl: GM, paused: true })), "G_BREAKER_OPEN", JSON.stringify(params));
    assert.equal(outcome(run(OUT, call(params), { paused: true })), "G_BREAKER_OPEN", "unknown tab " + JSON.stringify(params));
  }
});

test("tabs on a tab the guard does not know: allowed without consent, stopped by a global breaker", () => {
  const call = (params: Record<string, unknown>) => ({ toolName: "browser", params: { profile: "jobhunter", ...params } });
  // no page, so no site: the consent fence has nothing to judge, and listing tabs is how the agent learns them
  assert.equal(outcome(run(SC, call({ action: "tabs" }), { consent: consentOf([]) })), "allow");
  assert.equal(outcome(run(SC, call({ action: "tabs" }), { openBreakers: new Set(["gmail", "linkedin"]) })), "allow");
  for (const scope of ["global", "pause:all"]) {
    assert.equal(outcome(run(SC, call({ action: "tabs" }), { openBreakers: new Set([scope]) })), "G_BREAKER_OPEN", scope);
    assert.equal(outcome(run(SC, call({ action: "close", targetId: "T1" }), { openBreakers: new Set([scope]) })), "allow", scope);
  }
});

test("a never page: close runs, tabs and reads do not", () => {
  const call = (params: Record<string, unknown>) => ({ toolName: "browser", params: { profile: "jobhunter", ...params } });
  for (const url of ["https://docs.google.com/document/d/abc/edit", "https://myaccount.google.com/", "https://www.linkedin.com/mypreferences/d/categories/account"]) {
    assert.equal(outcome(run(OUT, call({ action: "close" }), { currentUrl: url })), "allow", url);
    assert.equal(outcome(run(OUT, call({ action: "tabs" }), { currentUrl: url })), "G_HOST_NEVER", url);
    assert.equal(outcome(run(OUT, call({ action: "snapshot" }), { currentUrl: url })), "G_HOST_NEVER", url);
    assert.equal(outcome(run(OUT, nav(GM), { currentUrl: url })), "allow", url);
    assert.equal(outcome(run(OUT, call({ action: "close" }), { currentUrl: url, paused: true })), "G_BREAKER_OPEN", url);
  }
});

test("never hosts and a stopped session keep their own codes", () => {
  const none = consentOf([]);
  assert.equal(outcome(run(SC, nav("https://www.linkedin.com/mypreferences/d/categories/account"), { consent: none })), "G_HOST_NEVER");
  assert.equal(outcome(run(SC, nav(LI), { consent: none, sessionStopped: true })), "G_STOPPED");
});

// ------------------------------------------------------------------ runtime (reads private/consent.json)

function call(inst: ReturnType<typeof makeInstall>, agent: string, params: Record<string, unknown>): string {
  const r = inst.runtime.evaluate({ toolName: "browser", params }, { agentId: agent, sessionKey: "agent:" + agent + ":cron:c1" });
  if (r && typeof r === "object" && "block" in r && r.block === true) return r.blockReason.split(":")[0];
  return "allow";
}

test("runtime: no consent file, grant, revoke and a shared-writable file", () => {
  const inst = makeInstall({ consent: null });
  try {
    const navLi = { profile: "jobhunter", action: "navigate", targetUrl: LI };
    const navGh = { profile: "jobhunter", action: "navigate", targetUrl: GH };
    assert.equal(call(inst, OUT, navLi), "G_NO_CONSENT");
    assert.equal(call(inst, AP, navGh), "allow");
    // the owner allows LinkedIn: the next call sees it (no cache)
    writeConsent(inst.root, ["linkedin"]);
    assert.equal(call(inst, OUT, navLi), "allow");
    assert.equal(call(inst, SC, { profile: "jobhunter", action: "navigate", targetUrl: NK }), "G_NO_CONSENT");
    // `./jobhunter browser forget linkedin` marks the row revoked
    writeConsent(inst.root, ["linkedin"], ["linkedin"]);
    assert.equal(call(inst, OUT, navLi), "G_NO_CONSENT");
    // a file other users can write is not trusted
    writeConsent(inst.root, ["linkedin"], [], 0o666);
    assert.equal(call(inst, OUT, navLi), "G_NO_CONSENT");
    writeConsent(inst.root, ["linkedin"], [], 0o600);
    assert.equal(call(inst, OUT, navLi), "allow");
    // not valid JSON
    fs.writeFileSync(path.join(inst.root, "private", "consent.json"), "{", { mode: 0o600 });
    assert.equal(call(inst, OUT, navLi), "G_NO_CONSENT");
    // the refusals are in the guard log, without the consent file's contents
    const log = fs.readFileSync(path.join(inst.root, "logs", "guard-2026-09.jsonl"), "utf8").split("\n").filter((l) => l.trim()).map((l) => JSON.parse(l));
    const refused = log.filter((e) => e.code === "G_NO_CONSENT");
    assert.equal(refused.length, 5);
    for (const e of refused) assert.equal(JSON.stringify(e).includes("Profile 1"), false);
    // the guard never writes the consent file
    assert.equal(fs.readFileSync(path.join(inst.root, "private", "consent.json"), "utf8"), "{");
    // other agents are not judged on consent (they cannot use the jobhunter profile at all)
    assert.equal(call(inst, "main", { action: "navigate", targetUrl: LI }), "allow");
  } finally {
    inst.cleanup();
  }
});

test("runtime: after a Gmail revoke the Gmail tab can be closed, not listed or read", () => {
  const inst = makeInstall({ consent: ["gmail", "linkedin"] });
  try {
    const ctx = { agentId: OUT, sessionKey: "agent:" + OUT + ":cron:c1" };
    const navGm = { profile: "jobhunter", action: "navigate", targetUrl: GM };
    assert.equal(call(inst, OUT, navGm), "allow");
    inst.runtime.observe({ toolName: "browser", params: navGm, result: { content: [{ type: "text", text: "Inbox" }], details: { ok: true, targetId: "CDP0A1", url: GM } } }, ctx);
    assert.equal(call(inst, OUT, { profile: "jobhunter", action: "tabs" }), "allow");
    // `./jobhunter browser forget gmail`: the row is revoked and the gmail breaker trips
    writeConsent(inst.root, ["gmail", "linkedin"], ["gmail"]);
    inst.db.prepare("INSERT INTO breakers (scope, state, reason_code, tripped_at, updated_at) VALUES ('gmail', 'open', 'consent_revoked', ?, ?)").run(G2, G2);
    assert.equal(call(inst, OUT, { profile: "jobhunter", action: "tabs" }), "G_NO_CONSENT");
    assert.equal(call(inst, OUT, { profile: "jobhunter", action: "snapshot" }), "G_NO_CONSENT");
    assert.equal(call(inst, OUT, { profile: "jobhunter", action: "close" }), "allow");
    assert.equal(call(inst, OUT, { profile: "jobhunter", action: "close", targetId: "CDP0A1" }), "allow");
    // consent granted again, breaker still open: close passes, a tab list does not
    writeConsent(inst.root, ["gmail", "linkedin"]);
    assert.equal(call(inst, OUT, { profile: "jobhunter", action: "tabs" }), "G_BREAKER_OPEN");
    assert.equal(call(inst, OUT, { profile: "jobhunter", action: "close" }), "allow");
  } finally {
    inst.cleanup();
  }
});

test("the test helper writes the core's consent shape", () => {
  const raw = JSON.parse(consentJson(["linkedin", "naukri"], ["naukri"]));
  assert.deepEqual(Object.keys(raw).sort(), ["sites", "updated_at", "version"]);
  assert.deepEqual(Object.keys(raw.sites.linkedin).sort(), Object.keys(row("linkedin")).sort());
  assert.equal(raw.sites.naukri.status, "revoked");
  assert.deepEqual(sorted(activeSites(raw)), ["linkedin"]);
});
