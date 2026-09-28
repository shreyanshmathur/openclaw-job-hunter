// Decision table for decide() (design 1.4 R0 to R4 and R7; test list in 13.2).

import test from "node:test";
import assert from "node:assert/strict";
import { decide, effectiveAgentId, normalizeToolName } from "../src/policy.ts";
import type { Decision, RefInfo, Snapshot } from "../src/types.ts";
import { CYCLE, JH, PY, REPO_PATH, T0, TOKEN, WS, armedToken, dwellDone, iso, reservedToken, snap } from "./_helpers.ts";

const GH = "https://job-boards.greenhouse.io/kestrel/jobs/123";
const LI = "https://www.linkedin.com/in/example-person/";
const refs: Record<string, RefInfo> = {
  e1: { role: "link", name: "Jobs" },
  e2: { role: "button", name: "Show more" },
  e3: { role: "button", name: "Send" },
  e4: { role: "textbox", name: "First name" },
  e5: { role: "button", name: "Connect" },
  e6: { role: "button", name: "Submit application" },
  e9: { role: "button", name: "Next" },
};

type Case = {
  name: string;
  agent: string;
  tool: string;
  params: Record<string, unknown>;
  snap?: Partial<Snapshot> & { refs?: Record<string, RefInfo> };
  want: string; // "allow" | "pass" | block code
  sessionKey?: string;
};

const W = (role: string) => WS + "/" + role + "/work/" + CYCLE;

const cases: Case[] = [
  // R0 health
  { name: "unhealthy blocks everything", agent: "jobhunter-scout", tool: "read", params: { path: W("scout") + "/a.json" }, snap: { health: { ok: false, reason: "install_id differs" } }, want: "G_GUARD_UNHEALTHY" },
  { name: "missing acl is unhealthy", agent: "jobhunter-scout", tool: "read", params: { path: "x" }, snap: { acl: null }, want: "G_GUARD_UNHEALTHY" },
  // R1 tools
  { name: "qc reviewer has no tools", agent: "jobhunter-qc", tool: "read", params: { path: "AGENTS.md" }, want: "G_TOOL_DENIED" },
  { name: "evaluator has no browser", agent: "jobhunter-evaluator", tool: "browser", params: { action: "snapshot" }, want: "G_TOOL_DENIED" },
  { name: "web_fetch denied", agent: "jobhunter-outreach", tool: "web_fetch", params: { url: "https://kestrel.example" }, want: "G_TOOL_DENIED" },
  { name: "message tool denied", agent: "jobhunter-outreach", tool: "message", params: { action: "send" }, want: "G_TOOL_DENIED" },
  { name: "unknown jobhunter agent", agent: "jobhunter-rogue", tool: "read", params: { path: "a" }, want: "G_TOOL_DENIED" },
  { name: "mcp prefix is normalized", agent: "jobhunter-scout", tool: "mcp__openclaw__exec", params: { command: JH + " home show" }, want: "allow" },
  // R2 exec
  { name: "allowed jh.py command", agent: "jobhunter-scout", tool: "exec", params: { command: JH + " preflight --lane scout", timeoutSeconds: 90 }, want: "allow" },
  { name: "sqlite3 exec", agent: "jobhunter-applier", tool: "exec", params: { command: "sqlite3 " + REPO_PATH + "/state/jobhunter.sqlite3 .dump" }, want: "G_EXEC_SHAPE" },
  { name: "--home", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " --home /tmp/x preflight --lane applier" }, want: "G_EXEC_PARAM" },
  { name: "pty", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " preflight --lane applier", pty: true }, want: "G_EXEC_SHAPE" },
  { name: "env", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " preflight --lane applier", env: { JH_AGENT_ID: "jobhunter-outreach" } }, want: "G_EXEC_SHAPE" },
  { name: "empty env is fine", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " preflight --lane applier", env: {} }, want: "allow" },
  { name: "background", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " preflight --lane applier", background: true }, want: "G_EXEC_SHAPE" },
  { name: "elevated", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " preflight --lane applier", elevated: true }, want: "G_EXEC_SHAPE" },
  { name: "host node", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " preflight --lane applier", host: "node" }, want: "G_EXEC_SHAPE" },
  { name: "security override", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " preflight --lane applier", security: "full" }, want: "G_EXEC_SHAPE" },
  { name: "ask override", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " preflight --lane applier", ask: "off" }, want: "G_EXEC_SHAPE" },
  { name: "workdir outside workspace", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " preflight --lane applier", workdir: REPO_PATH }, want: "G_EXEC_SHAPE" },
  { name: "workdir inside workspace", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " preflight --lane applier", workdir: WS + "/applier" }, want: "allow" },
  { name: "not in the ACL", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " approve A7K2 --by chat" }, want: "G_EXEC_ACL" },
  { name: "grant flag", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " --grant 1.aaaaaaaaaaaaaaaa.bb approve A7K2" }, want: "G_EXEC_PARAM" },
  // R3 files
  { name: "read own workspace", agent: "jobhunter-applier", tool: "read", params: { path: WS + "/applier/AGENTS.md" }, want: "allow" },
  { name: "read relative path", agent: "jobhunter-applier", tool: "read", params: { path: "skills/jobhunter-gate/SKILL.md" }, want: "allow" },
  { name: "read another workspace", agent: "jobhunter-applier", tool: "read", params: { path: WS + "/outreach/AGENTS.md" }, want: "G_PATH_DENIED" },
  { name: "read the repo", agent: "jobhunter-applier", tool: "read", params: { path: REPO_PATH + "/private/guard.key" }, want: "G_PATH_DENIED" },
  { name: "read dotdot escape", agent: "jobhunter-applier", tool: "read", params: { path: "../outreach/AGENTS.md" }, want: "G_PATH_DENIED" },
  { name: "write in work", agent: "jobhunter-applier", tool: "write", params: { path: W("applier") + "/observed.json", content: "{}" }, want: "allow" },
  { name: "write in inbox", agent: "jobhunter-outreach", tool: "write", params: { path: WS + "/outreach/inbox/x.json", content: "{}" }, want: "allow" },
  { name: "write outside work", agent: "jobhunter-applier", tool: "write", params: { path: WS + "/applier/AGENTS.md", content: "x" }, want: "G_PATH_DENIED" },
  { name: "write to state", agent: "jobhunter-applier", tool: "write", params: { path: REPO_PATH + "/state/PAUSED", content: "" }, want: "G_PATH_DENIED" },
  { name: "write without path", agent: "jobhunter-applier", tool: "write", params: { content: "x" }, want: "G_PATH_DENIED" },
  { name: "write via symlink out of work", agent: "jobhunter-applier", tool: "write", params: { path: W("applier") + "/link/x" }, snap: { realpath: (p: string) => p.replace(W("applier") + "/link", REPO_PATH + "/scripts") }, want: "G_PATH_DENIED" },
  // R4 browser: profiles and hosts
  { name: "snapshot allowed and profile rewritten", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot" }, snap: { currentUrl: "https://www.naukri.com/x" }, want: "allow" },
  { name: "other profile", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot", profile: "user" }, want: "G_BROWSER_PROFILE" },
  { name: "node target", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot", target: "node" }, want: "G_BROWSER_PROFILE" },
  { name: "google sheets navigation", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://docs.google.com/spreadsheets/d/abc/edit" }, want: "G_HOST_NEVER" },
  { name: "open file url", agent: "jobhunter-scout", tool: "browser", params: { action: "open", targetUrl: "file:///etc/hosts" }, want: "G_HOST_NEVER" },
  { name: "navigate to loopback", agent: "jobhunter-outreach", tool: "browser", params: { action: "navigate", url: "http://127.0.0.1:18789/" }, want: "G_HOST_NEVER" },
  { name: "act on a never page", agent: "jobhunter-scout", tool: "browser", params: { action: "act", kind: "click", ref: "e1" }, snap: { currentUrl: "https://myaccount.google.com/", refs }, want: "G_HOST_NEVER" },
  { name: "cookie import", agent: "jobhunter-scout", tool: "browser", params: { action: "importprofile" }, want: "G_TOOL_DENIED" },
  { name: "navigate board", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://www.naukri.com/data-analyst-jobs" }, want: "allow" },
  // Google: allowing Gmail copies the whole Google session, so every google.com host but Gmail is never
  // allowed, however the host is spelled; Hacker News carries the YC session
  { name: "google contacts", agent: "jobhunter-outreach", tool: "browser", params: { action: "navigate", targetUrl: "https://contacts.google.com/" }, want: "G_HOST_NEVER" },
  { name: "google calendar", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://calendar.google.com/calendar/u/0/r" }, want: "G_HOST_NEVER" },
  { name: "google photos", agent: "jobhunter-scout", tool: "browser", params: { action: "open", targetUrl: "https://photos.google.com/" }, want: "G_HOST_NEVER" },
  { name: "google keep", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://keep.google.com/" }, want: "G_HOST_NEVER" },
  { name: "google search", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://www.google.com/search?q=kestrel+commerce" }, want: "G_HOST_NEVER" },
  { name: "bare google.com", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://google.com" }, want: "G_HOST_NEVER" },
  { name: "google with case, port and trailing dot", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "HTTPS://WWW.Google.COM.:443/search?q=x" }, want: "G_HOST_NEVER" },
  { name: "google with a percent-encoded dot", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://www%2Egoogle.com/search?q=x" }, want: "G_HOST_NEVER" },
  { name: "google with backslashes", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https:\\\\calendar.google.com\\r" }, want: "G_HOST_NEVER" },
  { name: "reading a google page the tab is on", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot" }, snap: { currentUrl: "https://www.google.com/url?q=https://kestrel.example/" }, want: "G_HOST_NEVER" },
  { name: "gmail inbox stays allowed", agent: "jobhunter-outreach", tool: "browser", params: { action: "navigate", targetUrl: "https://mail.google.com/mail/u/0/#inbox" }, want: "allow" },
  { name: "gmail with case and port", agent: "jobhunter-outreach", tool: "browser", params: { action: "navigate", targetUrl: "https://Mail.Google.com:443/mail/u/0/#inbox" }, want: "allow" },
  { name: "a host that only starts like gmail", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://mail.google.com.kestrel.example/" }, want: "allow" },
  { name: "a host that only ends like google", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://notgoogle.com.example/" }, want: "allow" },
  { name: "hacker news", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://news.ycombinator.com/item?id=45100001" }, want: "G_HOST_NEVER" },
  { name: "work at a startup stays allowed", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://www.workatastartup.com/companies" }, want: "allow" },
  // a tab whose page the guard does not know: only calls that do not touch the page
  { name: "unknown tab: snapshot", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot" }, want: "G_PAGE_UNKNOWN" },
  { name: "unknown tab: snapshot with a targetId", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot", targetId: "t1" }, want: "G_PAGE_UNKNOWN" },
  { name: "unknown tab: screenshot", agent: "jobhunter-scout", tool: "browser", params: { action: "screenshot" }, want: "G_PAGE_UNKNOWN" },
  { name: "unknown tab: text", agent: "jobhunter-outreach", tool: "browser", params: { action: "text", targetId: "t1" }, want: "G_PAGE_UNKNOWN" },
  { name: "unknown tab: console", agent: "jobhunter-scout", tool: "browser", params: { action: "console" }, want: "G_PAGE_UNKNOWN" },
  { name: "unknown tab: pdf", agent: "jobhunter-scout", tool: "browser", params: { action: "pdf" }, want: "G_PAGE_UNKNOWN" },
  { name: "unknown tab: focus", agent: "jobhunter-scout", tool: "browser", params: { action: "focus", targetId: "t1" }, want: "G_PAGE_UNKNOWN" },
  { name: "unknown tab: stop", agent: "jobhunter-scout", tool: "browser", params: { action: "stop" }, want: "G_PAGE_UNKNOWN" },
  { name: "unknown tab: driver", agent: "jobhunter-scout", tool: "browser", params: { action: "act", kind: "evaluate", fn: "() => { return { ok: true, title: document.title }; }" }, want: "G_PAGE_UNKNOWN" },
  { name: "unknown tab: hover", agent: "jobhunter-scout", tool: "browser", params: { action: "act", kind: "hover", ref: "e1" }, want: "G_PAGE_UNKNOWN" },
  { name: "unknown tab: link click", agent: "jobhunter-scout", tool: "browser", params: { action: "act", kind: "click", ref: "e1" }, snap: { refs }, want: "G_PAGE_UNKNOWN" },
  { name: "unknown tab: dialog dismiss", agent: "jobhunter-scout", tool: "browser", params: { action: "dialog", accept: false }, want: "G_PAGE_UNKNOWN" },
  { name: "unknown tab: tabs", agent: "jobhunter-scout", tool: "browser", params: { action: "tabs" }, want: "allow" },
  { name: "unknown tab: close", agent: "jobhunter-scout", tool: "browser", params: { action: "close", targetId: "t1" }, want: "allow" },
  { name: "unknown tab: open", agent: "jobhunter-scout", tool: "browser", params: { action: "open", targetUrl: "https://www.naukri.com/" }, want: "allow" },
  { name: "unknown tab: navigate", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetId: "t1", targetUrl: "https://www.naukri.com/" }, want: "allow" },
  { name: "unknown tab: start, status, profiles, doctor", agent: "jobhunter-scout", tool: "browser", params: { action: "status" }, want: "allow" },
  { name: "unknown tab: start", agent: "jobhunter-scout", tool: "browser", params: { action: "start" }, want: "allow" },
  { name: "unknown tab: profiles", agent: "jobhunter-scout", tool: "browser", params: { action: "profiles" }, want: "allow" },
  { name: "unknown tab: doctor", agent: "jobhunter-scout", tool: "browser", params: { action: "doctor" }, want: "allow" },
  { name: "unknown tab: paused still wins", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot" }, snap: { paused: true }, want: "G_BREAKER_OPEN" },
  { name: "blank tab: snapshot", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot" }, snap: { currentUrl: "about:blank" }, want: "allow" },
  // breakers and pause
  { name: "paused", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot" }, snap: { paused: true }, want: "G_BREAKER_OPEN" },
  { name: "global breaker", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot" }, snap: { openBreakers: new Set(["global"]) }, want: "G_BREAKER_OPEN" },
  { name: "linkedin breaker blocks linkedin", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: LI }, snap: { openBreakers: new Set(["linkedin"]) }, want: "G_BREAKER_OPEN" },
  { name: "linkedin breaker does not block boards", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://www.naukri.com/" }, snap: { openBreakers: new Set(["linkedin"]) }, want: "allow" },
  { name: "site breaker", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot" }, snap: { currentUrl: "https://www.naukri.com/x", openBreakers: new Set(["site:naukri"]) }, want: "G_BREAKER_OPEN" },
  { name: "pause:linkedin", agent: "jobhunter-outreach", tool: "browser", params: { action: "snapshot" }, snap: { currentUrl: LI, openBreakers: new Set(["pause:linkedin"]) }, want: "G_BREAKER_OPEN" },
  { name: "ats breaker blocks fill", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "e4", text: "A" }, snap: { currentUrl: GH, refs, token: reservedToken(), openBreakers: new Set(["ats"]) }, want: "G_BREAKER_OPEN" },
  // reader agents
  { name: "scout link click", agent: "jobhunter-scout", tool: "browser", params: { action: "act", kind: "click", ref: "e1" }, snap: { currentUrl: "https://www.naukri.com/x", refs }, want: "allow" },
  { name: "scout show more", agent: "jobhunter-scout", tool: "browser", params: { action: "act", kind: "click", ref: "e2" }, snap: { currentUrl: "https://www.naukri.com/x", refs }, want: "allow" },
  { name: "scout driver", agent: "jobhunter-scout", tool: "browser", params: { action: "act", kind: "evaluate", fn: "() => { return { ok: true, title: document.title }; }" }, snap: { currentUrl: "https://www.naukri.com/x" }, want: "allow" },
  { name: "evaluate unknown script", agent: "jobhunter-scout", tool: "browser", params: { action: "act", kind: "evaluate", fn: "() => document.querySelector('form').submit()" }, snap: { currentUrl: "https://www.naukri.com/x" }, want: "G_SCRIPT_NOT_ALLOWED" },
  { name: "scout typing", agent: "jobhunter-scout", tool: "browser", params: { action: "act", kind: "type", ref: "e4", text: "x" }, snap: { currentUrl: "https://www.naukri.com/x", refs }, want: "G_NO_TOKEN" },
  // writer agents: tokens
  { name: "click Send without a token", agent: "jobhunter-outreach", tool: "browser", params: { action: "act", kind: "click", ref: "e3" }, snap: { currentUrl: LI, refs }, want: "G_NO_TOKEN" },
  { name: "type with a reserved token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "e4", text: "Alex", slowly: true }, snap: { currentUrl: GH, refs, token: reservedToken() }, want: "allow" },
  { name: "submit with a reserved token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "e6" }, snap: { currentUrl: GH, refs, token: reservedToken() }, want: "G_NOT_ARMED" },
  { name: "Enter with a reserved token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "press", key: "Enter" }, snap: { currentUrl: GH, token: reservedToken() }, want: "G_NOT_ARMED" },
  { name: "type submit with a reserved token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "e4", text: "x", submit: true }, snap: { currentUrl: GH, refs, token: reservedToken() }, want: "G_NOT_ARMED" },
  { name: "prepare button with a reserved token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "e9" }, snap: { currentUrl: GH, refs, token: reservedToken() }, want: "allow" },
  { name: "connect with an li_invite token", agent: "jobhunter-outreach", tool: "browser", params: { action: "act", kind: "click", ref: "e5" }, snap: { currentUrl: LI, refs, token: reservedToken("linkedin", "li_invite") }, want: "allow" },
  { name: "token for another platform", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "e4", text: "x" }, snap: { currentUrl: "https://jobs.lever.co/kestrel/1", refs, token: reservedToken("greenhouse") }, want: "G_NO_TOKEN" },
  { name: "token on an unknown page", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "e4", text: "x" }, snap: { currentUrl: null, refs, token: reservedToken() }, want: "G_PAGE_UNKNOWN" },
  { name: "expired token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "e4", text: "x" }, snap: { currentUrl: GH, refs, token: { ...reservedToken(), expiresAt: iso(T0 - 1000) } }, want: "G_NO_TOKEN" },
  { name: "armed commit after dwell", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "e6" }, snap: { currentUrl: GH, refs, token: armedToken(), dwell: dwellDone() }, want: "allow" },
  { name: "armed commit without dwell", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "e6" }, snap: { currentUrl: GH, refs, token: armedToken(), dwell: null }, want: "G_NOT_ARMED" },
  { name: "dwell not elapsed", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "e6" }, snap: { currentUrl: GH, refs, token: armedToken(), dwell: { acquiredAt: iso(T0 - 30000), expiresAt: iso(T0 + 5000) } }, want: "G_NOT_ARMED" },
  { name: "dwell drawn before arm", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "e6" }, snap: { currentUrl: GH, refs, token: armedToken("greenhouse", "application", 60), dwell: { acquiredAt: iso(T0 - 120000), expiresAt: iso(T0 - 100000) } }, want: "G_NOT_ARMED" },
  { name: "commit under the floor", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "e6" }, snap: { currentUrl: GH, refs, token: armedToken("greenhouse", "application", 2), dwell: { acquiredAt: iso(T0 - 1000), expiresAt: iso(T0) } }, want: "G_NOT_ARMED" },
  { name: "third commit", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "e6" }, snap: { currentUrl: GH, refs, token: armedToken(), dwell: dwellDone(), commitsUsed: 2 }, want: "G_COMMIT_BUDGET" },
  { name: "batch with two commits after one", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "batch", actions: [{ kind: "click", ref: "e6" }, { kind: "press", key: "Enter" }] }, snap: { currentUrl: GH, refs, token: armedToken(), dwell: dwellDone(), commitsUsed: 1 }, want: "G_COMMIT_BUDGET" },
  { name: "dialog accept needs armed", agent: "jobhunter-applier", tool: "browser", params: { action: "dialog", accept: true }, snap: { currentUrl: GH, token: reservedToken() }, want: "G_NOT_ARMED" },
  // uploads
  { name: "upload the staged file", agent: "jobhunter-applier", tool: "browser", params: { action: "upload", paths: ["/tmp/openclaw/uploads/TABCDEFGHJKM/Alex_Rivera_Resume.pdf"], ref: "e9" }, snap: { currentUrl: GH, refs, token: reservedToken(), stagedPath: "/tmp/openclaw/uploads/TABCDEFGHJKM/Alex_Rivera_Resume.pdf" }, want: "allow" },
  { name: "upload another file", agent: "jobhunter-applier", tool: "browser", params: { action: "upload", paths: ["/tmp/openclaw/uploads/other.pdf"] }, snap: { currentUrl: GH, token: reservedToken(), stagedPath: "/tmp/openclaw/uploads/TABCDEFGHJKM/Alex_Rivera_Resume.pdf" }, want: "G_UPLOAD_PATH" },
  { name: "upload with nothing staged", agent: "jobhunter-applier", tool: "browser", params: { action: "upload", paths: ["/tmp/openclaw/uploads/a.pdf"] }, snap: { currentUrl: GH, token: reservedToken() }, want: "G_UPLOAD_PATH" },
  { name: "upload two files", agent: "jobhunter-applier", tool: "browser", params: { action: "upload", paths: ["/tmp/a.pdf", "/tmp/a.pdf"] }, snap: { currentUrl: GH, token: reservedToken(), stagedPath: "/tmp/a.pdf" }, want: "G_UPLOAD_PATH" },
  // act keys at the top level next to request (OpenClaw fills the request from them)
  { name: "wait fn beside a request", agent: "jobhunter-scout", tool: "browser", params: { action: "act", request: { kind: "wait" }, fn: "() => { document.forms[0].submit(); return true; }" }, snap: { currentUrl: "https://www.naukri.com/x" }, want: "G_SCRIPT_NOT_ALLOWED" },
  { name: "submit beside a request", agent: "jobhunter-applier", tool: "browser", params: { action: "act", request: { kind: "type", ref: "e4", text: "x" }, submit: true }, snap: { currentUrl: GH, refs, token: reservedToken() }, want: "G_NOT_ARMED" },
  { name: "Enter key beside a request", agent: "jobhunter-applier", tool: "browser", params: { action: "act", request: { kind: "press" }, key: "Enter" }, snap: { currentUrl: GH, token: reservedToken() }, want: "G_NOT_ARMED" },
  { name: "batch actions beside a request", agent: "jobhunter-applier", tool: "browser", params: { action: "act", request: { kind: "batch" }, actions: [{ kind: "click", ref: "e6" }, { kind: "click", ref: "e6" }] }, snap: { currentUrl: GH, refs, token: armedToken(), dwell: dwellDone(), commitsUsed: 1 }, want: "G_COMMIT_BUDGET" },
  { name: "two tabs in one act call", agent: "jobhunter-scout", tool: "browser", params: { action: "act", targetId: "A", request: { kind: "click", ref: "e1", targetId: "B" } }, snap: { currentUrl: "https://www.naukri.com/x", refs }, want: "G_TOOL_DENIED" },
  { name: "snake case target url", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", url: "https://www.naukri.com/", target_url: "https://docs.google.com/document/d/x" }, want: "G_TOOL_DENIED" },
  // type slowly, upload with a ref, Space and NumpadEnter
  { name: "type slowly on the submit button", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "e6", text: "x", slowly: true }, snap: { currentUrl: GH, refs, token: reservedToken() }, want: "G_NOT_ARMED" },
  { name: "type slowly with a newline", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "e4", text: "a@b.example\n", slowly: true }, snap: { currentUrl: GH, refs, token: reservedToken() }, want: "G_NOT_ARMED" },
  { name: "upload through the submit button", agent: "jobhunter-applier", tool: "browser", params: { action: "upload", paths: ["/tmp/openclaw/uploads/TABCDEFGHJKM/Alex_Rivera_Resume.pdf"], ref: "e6" }, snap: { currentUrl: GH, refs, token: reservedToken(), stagedPath: "/tmp/openclaw/uploads/TABCDEFGHJKM/Alex_Rivera_Resume.pdf" }, want: "G_NOT_ARMED" },
  { name: "press NumpadEnter", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "press", key: "NumpadEnter" }, snap: { currentUrl: GH, token: reservedToken() }, want: "G_NOT_ARMED" },
  { name: "press Space", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "press", key: "Space" }, snap: { currentUrl: GH, token: reservedToken() }, want: "G_NOT_ARMED" },
  { name: "press Tab", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "press", key: "Tab" }, snap: { currentUrl: GH, token: reservedToken() }, want: "allow" },
  { name: "dialog accept as a string", agent: "jobhunter-applier", tool: "browser", params: { action: "dialog", accept: "false" }, snap: { currentUrl: GH, token: reservedToken() }, want: "G_NOT_ARMED" },
  // job-level stop on one site
  { name: "job-level stop blocks fill on that site", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "e4", text: "A" }, snap: { currentUrl: GH, refs, token: reservedToken(), writeBlocked: new Set(["greenhouse"]) }, want: "G_STOPPED" },
  { name: "job-level stop still allows reads", agent: "jobhunter-applier", tool: "browser", params: { action: "snapshot" }, snap: { currentUrl: GH, writeBlocked: new Set(["greenhouse"]) }, want: "allow" },
  { name: "job-level stop still allows jh.py", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " job set-status JABCDEFG --status needs_human --reason captcha_visible" }, snap: { writeBlocked: new Set(["greenhouse"]) }, want: "allow" },
  { name: "job-level stop on another site", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "e4", text: "A" }, snap: { currentUrl: GH, refs, token: reservedToken(), writeBlocked: new Set(["lever"]) }, want: "allow" },
  // stopped session
  { name: "stopped: browser blocked", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot" }, snap: { sessionStopped: true }, want: "G_STOPPED" },
  { name: "stopped: other jh.py blocked", agent: "jobhunter-scout", tool: "exec", params: { command: JH + " usage add --platform linkedin --metric page_view" }, snap: { sessionStopped: true }, want: "G_STOPPED" },
  { name: "stopped: detect allowed", agent: "jobhunter-scout", tool: "exec", params: { command: JH + " detect --file " + W("scout") + "/stop.json" }, snap: { sessionStopped: true }, want: "allow" },
  { name: "stopped: gate reserve blocked", agent: "jobhunter-applier", tool: "exec", params: { command: JH + " gate reserve --kind application --draft DABCDEFG --precheck 1 --platform greenhouse" }, snap: { sessionStopped: true }, want: "G_STOPPED" },
  { name: "stopped: cycle end allowed", agent: "jobhunter-scout", tool: "exec", params: { command: JH + " cycle end --cycle " + CYCLE }, snap: { sessionStopped: true }, want: "allow" },
  { name: "stopped: note in work allowed", agent: "jobhunter-scout", tool: "write", params: { path: W("scout") + "/blocked.txt", content: "G_STOPPED" }, snap: { sessionStopped: true }, want: "allow" },
  { name: "stopped: write outside work still denied", agent: "jobhunter-scout", tool: "write", params: { path: WS + "/scout/AGENTS.md", content: "x" }, snap: { sessionStopped: true }, want: "G_PATH_DENIED" },
  { name: "stopped: qc still has no tools", agent: "jobhunter-qc", tool: "write", params: { path: WS + "/qc/work/a" }, snap: { sessionStopped: true }, want: "G_STOPPED" },
  // R7 other agents
  { name: "main runs jh.py approve", agent: "main", tool: "exec", params: { command: JH + " approve A7K2 --by chat" }, want: "G_EXEC_ACL" },
  { name: "main runs jh.py status", agent: "main", tool: "exec", params: { command: JH + " status" }, want: "pass" },
  { name: "main runs jh.py in a pipeline", agent: "main", tool: "exec", params: { command: "cd " + REPO_PATH + " && " + PY + " scripts/jh.py unpause" }, want: "G_EXEC_SHAPE" },
  { name: "unlisted agent runs jh.py status", agent: "work", tool: "exec", params: { command: JH + " status" }, want: "G_EXEC_ACL" },
  { name: "main spoofs JH_AGENT_ID", agent: "main", tool: "exec", params: { command: "python3 x.py", env: { JH_AGENT_ID: "jobhunter-applier" } }, want: "G_EXEC_PARAM" },
  { name: "main exports JH_AGENT_ID", agent: "main", tool: "exec", params: { command: "export JH_AGENT_ID=jobhunter-applier; python3 x.py" }, want: "G_EXEC_PARAM" },
  { name: "main reads guard.key", agent: "main", tool: "read", params: { path: REPO_PATH + "/private/guard.key" }, want: "G_PATH_DENIED" },
  { name: "main globs guard.key", agent: "main", tool: "exec", params: { command: "cat " + REPO_PATH + "/private/guard.k*" }, want: "G_PATH_DENIED" },
  { name: "main globs the private folder", agent: "main", tool: "exec", params: { command: "cat " + REPO_PATH + "/private/*" }, want: "G_PATH_DENIED" },
  { name: "main quotes guard.key apart", agent: "main", tool: "exec", params: { command: "cat " + REPO_PATH + "/private/gua''rd.key" }, want: "G_PATH_DENIED" },
  { name: "main globs jh.py", agent: "main", tool: "exec", params: { command: "env -u OPENCLAW_SHELL " + PY + " " + REPO_PATH + "/scripts/j?.py approve A7K2 --by chat" }, want: "G_EXEC_SHAPE" },
  { name: "unlisted agent globs jh.py", agent: "work", tool: "exec", params: { command: PY + " " + REPO_PATH + "/scripts/[j]h.py status" }, want: "G_EXEC_ACL" },
  { name: "main generic globs pass", agent: "main", tool: "exec", params: { command: "ls *.py scripts/* g* && cat notes/*.md" }, want: "pass" },
  { name: "main other exec passes", agent: "main", tool: "exec", params: { command: "ls -la" }, want: "pass" },
  { name: "main browser passes", agent: "main", tool: "browser", params: { action: "navigate", targetUrl: "https://docs.google.com/" }, want: "pass" },
  { name: "main may not drive the jobhunter profile", agent: "main", tool: "browser", params: { action: "snapshot", profile: "jobhunter" }, want: "G_BROWSER_PROFILE" },
  { name: "main names the profile with spaces and capitals", agent: "main", tool: "browser", params: { action: "snapshot", profile: " JobHunter " }, want: "G_BROWSER_PROFILE" },
  { name: "main imports Chrome cookies into the jobhunter profile", agent: "main", tool: "browser", params: { action: "importprofile", into: "jobhunter", systemProfile: "Default" }, want: "G_BROWSER_PROFILE" },
  { name: "main imports into a padded jobhunter name", agent: "main", tool: "browser", params: { action: "importprofile", into: "  JOBHUNTER\n", systemProfile: "Default", domains: ["google.com"] }, want: "G_BROWSER_PROFILE" },
  { name: "main imports into its own profile", agent: "main", tool: "browser", params: { action: "importprofile", into: "imported", systemProfile: "Default" }, want: "pass" },
  { name: "main uses another profile", agent: "main", tool: "browser", params: { action: "snapshot", profile: "user" }, want: "pass" },
  { name: "main points a dashboard widget at the jobhunter profile", agent: "main", tool: "dashboard", params: { action: "widget_put", pluginKind: "browser:dashboard", name: "w", props: { url: "https://mail.google.com/", profile: "jobhunter" } }, want: "G_BROWSER_PROFILE" },
  { name: "main dashboard widget on its own profile", agent: "main", tool: "dashboard", params: { action: "widget_put", pluginKind: "browser:dashboard", name: "w", props: { url: "https://kestrel.example/" } }, want: "pass" },
  { name: "a profile field of another name is not a profile", agent: "main", tool: "exec", params: { command: "echo jobhunter", description: "profile jobhunter" }, want: "pass" },
  { name: "session key names a jobhunter agent", agent: "", sessionKey: "agent:jobhunter-scout:cron:scout", tool: "exec", params: { command: "rm -rf /" }, want: "G_EXEC_SHAPE" },
];

function outcome(d: Decision): string {
  return d.kind === "block" ? d.code : d.kind;
}

test("decision table", () => {
  for (const c of cases) {
    const d = decide({ toolName: c.tool, params: c.params }, { agentId: c.agent || undefined, sessionKey: c.sessionKey }, snap(c.snap || {}));
    assert.equal(outcome(d), c.want, c.name + " -> " + JSON.stringify(d));
    if (d.kind === "block") assert.match(d.code, /^G_[A-Z_]+$/);
  }
});

test("exec rewrites timeout and workdir", () => {
  const d = decide({ toolName: "exec", params: { command: JH + " preflight --lane applier", timeoutSeconds: 1800, workdir: WS + "/applier", yieldMs: 5 } }, { agentId: "jobhunter-applier" }, snap());
  assert.equal(d.kind, "allow");
  if (d.kind === "allow") {
    assert.deepEqual(d.params, { command: JH + " preflight --lane applier", workdir: WS + "/applier/work", timeoutSeconds: 90 });
    assert.equal(d.command, "preflight");
  }
});

test("browser rewrites a missing profile only", () => {
  const onBoard = snap({ currentUrl: "https://www.naukri.com/x" });
  const d1 = decide({ toolName: "browser", params: { action: "snapshot" } }, { agentId: "jobhunter-scout" }, onBoard);
  assert.equal(d1.kind, "allow");
  if (d1.kind === "allow") assert.deepEqual(d1.params, { action: "snapshot", profile: "jobhunter" });
  const d2 = decide({ toolName: "browser", params: { action: "snapshot", profile: "jobhunter" } }, { agentId: "jobhunter-scout" }, onBoard);
  if (d2.kind === "allow") assert.equal(d2.params, undefined);
});

test("allowed fill and commit produce token log records", () => {
  const d = decide(
    { toolName: "browser", params: { action: "act", kind: "batch", actions: [{ kind: "type", ref: "e4", text: "A" }, { kind: "click", ref: "e6" }] } },
    { agentId: "jobhunter-applier" },
    snap({ currentUrl: GH, refs, token: armedToken(), dwell: dwellDone() }),
  );
  assert.equal(d.kind, "allow");
  if (d.kind === "allow") {
    assert.equal(d.token, TOKEN);
    assert.equal(d.host, "job-boards.greenhouse.io");
    assert.deepEqual(d.records, [
      { class: "fill", action: "type", ref: "e4", role: "textbox", name: "First name" },
      { class: "commit", action: "click", ref: "e6", role: "button", name: "Submit application" },
    ]);
  }
  const read = decide({ toolName: "browser", params: { action: "snapshot" } }, { agentId: "jobhunter-applier" }, snap({ currentUrl: GH, token: armedToken() }));
  if (read.kind === "allow") assert.equal(read.records, undefined);
});

test("block reasons name the code first", () => {
  const d = decide({ toolName: "exec", params: { command: "sqlite3 x" } }, { agentId: "jobhunter-scout" }, snap());
  assert.equal(d.kind, "block");
  if (d.kind === "block") assert.ok(d.reason.length > 10);
});

test("agent id helpers", () => {
  assert.equal(normalizeToolName("mcp__openclaw__browser"), "browser");
  assert.equal(normalizeToolName("mcp__other__browser"), "mcp__other__browser");
  assert.equal(effectiveAgentId({ agentId: "main", sessionKey: "agent:jobhunter-applier:x" }), "jobhunter-applier");
  assert.equal(effectiveAgentId({ sessionKey: "agent:main:main" }), "main");
  assert.equal(effectiveAgentId({}), "");
});
