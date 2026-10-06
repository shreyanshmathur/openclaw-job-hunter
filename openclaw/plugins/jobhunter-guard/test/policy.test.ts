// Decision table for decide() (design 1.4 R0 to R4 and R7; test list in 13.2; Claude subscription route
// changes of the CLI route design 7 and 11.2).

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { blockingScopes, classifyUrl, parseHostsConfig } from "../src/browser.ts";
import { blockReason, decide, effectiveAgentId, globPrefix, isNativeShape, normalizeToolName, pathFormError, resolveOtherPath, shellPathWords } from "../src/policy.ts";
import { argvProof } from "../src/grant.ts";
import { BLOCK_CODES, type Decision, type RefInfo, type Snapshot, type ToolCtx } from "../src/types.ts";
import { CYCLE, HOME, JH, PY, REPO, REPO_PATH, T0, T0S, TEST_KEY, TEST_NONCE, TOKEN, WS, armedToken, config, dwellDone, iso, reservedToken, rewritten, snap } from "./_helpers.ts";

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
  ctx?: Partial<ToolCtx>;
  rawParams?: boolean; // browser calls of jobhunter agents get profile "jobhunter" unless this is set
};

const W = (role: string) => WS + "/" + role + "/work/" + CYCLE;
const EV = WS + "/evaluator";
const OC = HOME + "/.openclaw";
const R7_CONFIG = config({ protectedRoots: { read: [REPO_PATH + "/private", OC], write: [REPO_PATH, WS, OC] } });


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
  // OpenClaw 2026.9.8 exec carries a display title (live run D9): a short one-line string is accepted
  { name: "exec title (9.8)", agent: "jobhunter-evaluator", tool: "exec", params: { title: "Check agent identity", command: JH + " whoami" }, want: "allow" },
  { name: "exec title, bridged name, empty", agent: "jobhunter-scout", tool: "mcp__openclaw__exec", params: { title: "", command: JH + " home show", timeoutSeconds: 90 }, want: "allow" },
  { name: "exec title at the 120 limit", agent: "jobhunter-scout", tool: "exec", params: { title: "t".repeat(120), command: JH + " home show" }, want: "allow" },
  { name: "exec title over 120", agent: "jobhunter-scout", tool: "exec", params: { title: "t".repeat(121), command: JH + " home show" }, want: "G_EXEC_SHAPE" },
  { name: "exec title with a line break", agent: "jobhunter-scout", tool: "exec", params: { title: "Check\nidentity", command: JH + " home show" }, want: "G_EXEC_SHAPE" },
  { name: "exec title not a string", agent: "jobhunter-scout", tool: "exec", params: { title: { text: "x" }, command: JH + " home show" }, want: "G_EXEC_SHAPE" },
  { name: "exec title null", agent: "jobhunter-scout", tool: "exec", params: { title: null, command: JH + " home show" }, want: "G_EXEC_SHAPE" },
  { name: "exec title does not excuse a bad command", agent: "jobhunter-scout", tool: "exec", params: { title: "List files", command: "/bin/ls /" }, want: "G_EXEC_SHAPE" },
  { name: "9.8 exec ask option", agent: "jobhunter-scout", tool: "exec", params: { title: "x", command: JH + " home show", ask: "always" }, want: "G_EXEC_SHAPE" },
  { name: "9.8 exec node option", agent: "jobhunter-scout", tool: "exec", params: { title: "x", command: JH + " home show", node: "mac" }, want: "G_EXEC_SHAPE" },
  { name: "native Bash shape with a title stays denied", agent: "jobhunter-scout", tool: "exec", params: { title: "x", command: JH + " home show", timeout: 1000 }, want: "G_TOOL_DENIED" },
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
  { name: "snapshot allowed with the jobhunter profile", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot" }, snap: { currentUrl: "https://www.naukri.com/x" }, want: "allow" },
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
  ...cliRouteCases(),
];

function cliRouteCases(): Case[] {
  const ev = "jobhunter-evaluator";
  const fake = "jhp2.jobhunter-evaluator.1790000000.0123456789abcdef.0123456789abcdef." + "a".repeat(64);
  return [
    // 7.1 / 7.2 native tool shapes are off for jobhunter agents (deny mode, the default)
    { name: "native Bash by its raw name", agent: ev, tool: "Bash", params: { command: JH + " home show" }, want: "G_TOOL_DENIED" },
    { name: "projected native Bash (timeout)", agent: ev, tool: "exec", params: { command: JH + " home show", timeout: 60000 }, want: "G_TOOL_DENIED" },
    { name: "projected native Bash (run_in_background)", agent: ev, tool: "exec", params: { command: JH + " home show", run_in_background: false }, want: "G_TOOL_DENIED" },
    { name: "projected native Bash (dangerouslyDisableSandbox)", agent: ev, tool: "exec", params: { command: JH + " home show", dangerouslyDisableSandbox: false }, want: "G_TOOL_DENIED" },
    { name: "native Read by its raw name", agent: ev, tool: "Read", params: { file_path: EV + "/AGENTS.md" }, want: "G_TOOL_DENIED" },
    { name: "projected native Read (file_path)", agent: ev, tool: "read", params: { file_path: EV + "/AGENTS.md", path: EV + "/AGENTS.md" }, want: "G_TOOL_DENIED" },
    { name: "native Edit", agent: ev, tool: "Edit", params: { file_path: EV + "/work/a.json", old_string: "a", new_string: "b" }, want: "G_TOOL_DENIED" },
    { name: "native Glob", agent: ev, tool: "Glob", params: { pattern: "**/*.md" }, want: "G_TOOL_DENIED" },
    { name: "native Grep", agent: ev, tool: "Grep", params: { pattern: "x" }, want: "G_TOOL_DENIED" },
    { name: "native WebFetch", agent: ev, tool: "WebFetch", params: { url: "https://kestrel.example", prompt: "x" }, want: "G_TOOL_DENIED" },
    { name: "native TodoWrite", agent: ev, tool: "TodoWrite", params: { todos: [] }, want: "G_TOOL_DENIED" },
    { name: "native shape even while unhealthy", agent: ev, tool: "Bash", params: { command: "ls" }, snap: { health: { ok: false, reason: "x" } }, want: "G_TOOL_DENIED" },
    // mode N (claudeNativeTools "gate"): native shapes judged like the bridged ones
    { name: "gate: native Bash jh.py", agent: ev, tool: "exec", params: { command: JH + " home show", timeout: 120000, description: "home" }, snap: { config: config({ claudeNativeTools: "gate" }) }, want: "allow" },
    { name: "gate: raw Bash jh.py", agent: ev, tool: "Bash", params: { command: JH + " home show" }, snap: { config: config({ claudeNativeTools: "gate" }) }, want: "allow" },
    { name: "gate: background", agent: ev, tool: "exec", params: { command: JH + " home show", run_in_background: true }, snap: { config: config({ claudeNativeTools: "gate" }) }, want: "G_EXEC_SHAPE" },
    { name: "gate: sandbox off", agent: ev, tool: "exec", params: { command: JH + " home show", dangerouslyDisableSandbox: true }, snap: { config: config({ claudeNativeTools: "gate" }) }, want: "G_EXEC_SHAPE" },
    { name: "gate: timeout too long", agent: ev, tool: "exec", params: { command: JH + " home show", timeout: 600001 }, snap: { config: config({ claudeNativeTools: "gate" }) }, want: "G_EXEC_SHAPE" },
    { name: "gate: extra Bash key", agent: ev, tool: "exec", params: { command: JH + " home show", timeout: 1000, workdir: EV }, snap: { config: config({ claudeNativeTools: "gate" }) }, want: "G_EXEC_SHAPE" },
    { name: "gate: native Bash other program", agent: ev, tool: "Bash", params: { command: "cat /etc/hosts" }, snap: { config: config({ claudeNativeTools: "gate" }) }, want: "G_EXEC_SHAPE" },
    { name: "gate: native Read in the workspace", agent: ev, tool: "read", params: { file_path: EV + "/AGENTS.md", path: EV + "/AGENTS.md", limit: 10 }, snap: { config: config({ claudeNativeTools: "gate" }) }, want: "allow" },
    { name: "gate: file_path and path differ", agent: ev, tool: "read", params: { file_path: EV + "/AGENTS.md", path: EV + "/SOUL.md" }, snap: { config: config({ claudeNativeTools: "gate" }) }, want: "G_PATH_DENIED" },
    { name: "gate: native Read outside", agent: ev, tool: "Read", params: { file_path: REPO_PATH + "/private/home.json" }, snap: { config: config({ claudeNativeTools: "gate" }) }, want: "G_PATH_DENIED" },
    { name: "gate: native Write extra key", agent: ev, tool: "write", params: { file_path: EV + "/work/a.json", path: EV + "/work/a.json", content: "{}", mode: 1 }, snap: { config: config({ claudeNativeTools: "gate" }) }, want: "G_TOOL_DENIED" },
    { name: "gate: native Edit not in the ACL", agent: ev, tool: "Edit", params: { file_path: EV + "/work/a.json", old_string: "a", new_string: "b" }, snap: { config: config({ claudeNativeTools: "gate" }) }, want: "G_TOOL_DENIED" },
    // 7.3 / 7.4 / 7.5 exec: proofs typed by the model, -I, "=" tokens
    { name: "model types --agent-proof", agent: ev, tool: "exec", params: { command: PY + " -I " + REPO_PATH + "/scripts/jh.py --agent-proof " + fake + " home show" }, want: "G_EXEC_PARAM" },
    { name: "model types --agent-proof without -I", agent: ev, tool: "exec", params: { command: JH + " --agent-proof " + fake + " home show" }, want: "G_EXEC_PARAM" },
    { name: "model types --agent-p", agent: ev, tool: "exec", params: { command: JH + " --agent-p " + fake + " home show" }, want: "G_EXEC_PARAM" },
    { name: "model types --agent-proof=x", agent: ev, tool: "exec", params: { command: JH + " --agent-proof=" + fake + " home show" }, want: "G_EXEC_PARAM" },
    { name: "model puts --agent-proof after the command", agent: ev, tool: "exec", params: { command: JH + " home show --agent-proof " + fake }, want: "G_EXEC_PARAM" },
    { name: "another agent's real proof is refused", agent: ev, tool: "exec", params: { command: PY + " -I " + REPO_PATH + "/scripts/jh.py --agent-proof " + argvProof(TEST_KEY, "jobhunter-scout", "", ["home", "show"], T0S, TEST_NONCE) + " home show" }, want: "G_EXEC_PARAM" },
    { name: "another session's real proof is refused", agent: ev, tool: "exec", sessionKey: "agent:jobhunter-evaluator:cron:b", params: { command: rewritten(ev, "home show", "agent:jobhunter-evaluator:cron:a") }, want: "G_EXEC_PARAM" },
    { name: "an own proof for other arguments is refused", agent: ev, tool: "exec", params: { command: PY + " -I " + REPO_PATH + "/scripts/jh.py --agent-proof " + argvProof(TEST_KEY, ev, "", ["status"], T0S, TEST_NONCE) + " home show" }, want: "G_EXEC_PARAM" },
    { name: "an expired own proof is refused", agent: ev, tool: "exec", params: { command: PY + " -I " + REPO_PATH + "/scripts/jh.py --agent-proof " + argvProof(TEST_KEY, ev, "", ["home", "show"], T0S - 121, TEST_NONCE) + " home show" }, want: "G_EXEC_PARAM" },
    { name: "model types -I itself", agent: ev, tool: "exec", params: { command: PY + " -I " + REPO_PATH + "/scripts/jh.py home show" }, want: "allow" },
    { name: "another interpreter flag", agent: ev, tool: "exec", params: { command: PY + " -S " + REPO_PATH + "/scripts/jh.py home show" }, want: "G_EXEC_SHAPE" },
    { name: "-I after jh.py", agent: ev, tool: "exec", params: { command: JH + " -I home show" }, want: "G_EXEC_PARAM" },
    { name: "token starting with =", agent: ev, tool: "exec", params: { command: JH + " home show =python3" }, want: "G_EXEC_SHAPE" },
    // 7.7 R3 path forms (evaluator; reads of the own workspace are allowed in plain form)
    { name: "R3 plain absolute read", agent: ev, tool: "read", params: { path: EV + "/ref/profile_inference.md" }, want: "allow" },
    { name: "R3 ~", agent: ev, tool: "read", params: { path: "~" }, want: "G_PATH_DENIED" },
    { name: "R3 ~/x", agent: ev, tool: "read", params: { path: "~/x" }, want: "G_PATH_DENIED" },
    { name: "R3 ~root/x", agent: ev, tool: "read", params: { path: "~root/x" }, want: "G_PATH_DENIED" },
    { name: "R3 @../x", agent: ev, tool: "read", params: { path: "@../x" }, want: "G_PATH_DENIED" },
    { name: "R3 @x", agent: ev, tool: "read", params: { path: "@x" }, want: "G_PATH_DENIED" },
    { name: "R3 node://", agent: ev, tool: "read", params: { path: "node://n/etc/hosts" }, want: "G_PATH_DENIED" },
    { name: "R3 file:///", agent: ev, tool: "read", params: { path: "file:///etc/hosts" }, want: "G_PATH_DENIED" },
    { name: "R3 file:x", agent: ev, tool: "read", params: { path: "file:x" }, want: "G_PATH_DENIED" },
    { name: "R3 $HOME/x", agent: ev, tool: "read", params: { path: "$HOME/x" }, want: "G_PATH_DENIED" },
    { name: "R3 backslash", agent: ev, tool: "read", params: { path: "a\\b" }, want: "G_PATH_DENIED" },
    { name: "R3 work/../../x", agent: ev, tool: "read", params: { path: "work/../../x" }, want: "G_PATH_DENIED" },
    { name: "R3 NUL", agent: ev, tool: "read", params: { path: EV + "/work/a\u0000b" }, want: "G_PATH_DENIED" },
    { name: "R3 into another workspace via ..", agent: ev, tool: "read", params: { path: EV + "/../scout/AGENTS.md" }, want: "G_PATH_DENIED" },
    { name: "R3 empty path", agent: ev, tool: "read", params: { path: "" }, want: "G_PATH_DENIED" },
    { name: "R3 path not a string", agent: ev, tool: "read", params: { path: 7 }, want: "G_PATH_DENIED" },
    { name: "R3 one bad path among keys", agent: ev, tool: "read", params: { path: EV + "/AGENTS.md", filePath: "~/x" }, want: "G_PATH_DENIED" },
    { name: "R3 symlink in work to private", agent: ev, tool: "read", params: { path: EV + "/work/link/home.json" }, snap: { realpath: (p: string) => p.replace(EV + "/work/link", REPO_PATH + "/private") }, want: "G_PATH_DENIED" },
    { name: "R3 workspace that is a link into private", agent: ev, tool: "read", params: { path: EV + "/AGENTS.md" }, snap: { realpath: (p: string) => p.replace(EV, REPO_PATH + "/private") }, want: "G_PATH_DENIED" },
    { name: "R3 new file under work", agent: ev, tool: "write", params: { path: EV + "/work/" + CYCLE + "/new.json", content: "{}" }, want: "allow" },
    { name: "R3 guard.key absolute", agent: ev, tool: "read", params: { path: REPO_PATH + "/private/guard.key" }, want: "G_PATH_DENIED" },
    { name: "R3 a guard.key copy in the workspace", agent: ev, tool: "read", params: { path: EV + "/work/guard.key" }, want: "G_PATH_DENIED" },
    { name: "R3 ctx workspace of another role", agent: ev, tool: "read", params: { path: "AGENTS.md" }, ctx: { workspaceDir: WS + "/scout" }, want: "G_PATH_DENIED" },
    { name: "R3 ctx workspace of another role, absolute path", agent: ev, tool: "read", params: { path: EV + "/AGENTS.md" }, ctx: { workspaceDir: WS + "/scout" }, want: "G_PATH_DENIED" },
    { name: "R3 ctx workspace of its own role", agent: ev, tool: "read", params: { path: "ref/profile_inference.md" }, ctx: { workspaceDir: EV }, want: "allow" },
    { name: "R3 ctx cwd of its own role", agent: ev, tool: "read", params: { path: "AGENTS.md" }, ctx: { cwd: EV }, want: "allow" },
    { name: "R3 ctx cwd below the workspace", agent: ev, tool: "read", params: { path: "a.json" }, ctx: { cwd: EV + "/work" }, want: "G_PATH_DENIED" },
    { name: "R3 write work/AGENTS.md", agent: ev, tool: "write", params: { path: EV + "/work/AGENTS.md", content: "x" }, want: "G_PATH_DENIED" },
    { name: "R3 write work/agents.md (any case)", agent: ev, tool: "write", params: { path: EV + "/work/agents.md", content: "x" }, want: "G_PATH_DENIED" },
    { name: "R3 write inbox/.mcp.json", agent: "jobhunter-outreach", tool: "write", params: { path: WS + "/outreach/inbox/.mcp.json", content: "{}" }, want: "G_PATH_DENIED" },
    { name: "R3 write work/skills/x", agent: ev, tool: "write", params: { path: EV + "/work/skills/x/SKILL.md", content: "x" }, want: "G_PATH_DENIED" },
    { name: "R3 write work/.claude/settings.json", agent: ev, tool: "write", params: { path: EV + "/work/.claude/settings.json", content: "{}" }, want: "G_PATH_DENIED" },
    { name: "R3 write work/.git/config", agent: ev, tool: "write", params: { path: EV + "/work/.git/config", content: "" }, want: "G_PATH_DENIED" },
    { name: "R3 write work/CLAUDE.md", agent: ev, tool: "write", params: { path: EV + "/work/CLAUDE.md", content: "" }, want: "G_PATH_DENIED" },
    { name: "R3 write the work folder itself", agent: ev, tool: "write", params: { path: EV + "/work", content: "" }, want: "G_PATH_DENIED" },
    // F-QC: the reviewer writes only its verdict file
    { name: "F-QC verdict write", agent: "jobhunter-qc", tool: "write", params: { path: WS + "/qc/work/verdict/0123456789abcdef.json", content: "{}" }, snap: { config: config({ qcVerdictFile: true }) }, want: "allow" },
    { name: "F-QC write outside verdict", agent: "jobhunter-qc", tool: "write", params: { path: WS + "/qc/work/x.json", content: "{}" }, snap: { config: config({ qcVerdictFile: true }) }, want: "G_PATH_DENIED" },
    { name: "F-QC write to inbox", agent: "jobhunter-qc", tool: "write", params: { path: WS + "/qc/inbox/x.json", content: "{}" }, snap: { config: config({ qcVerdictFile: true }) }, want: "G_PATH_DENIED" },
    { name: "F-QC no read", agent: "jobhunter-qc", tool: "read", params: { path: WS + "/qc/AGENTS.md" }, snap: { config: config({ qcVerdictFile: true }) }, want: "G_TOOL_DENIED" },
    { name: "F-QC no exec", agent: "jobhunter-qc", tool: "exec", params: { command: JH + " home show" }, snap: { config: config({ qcVerdictFile: true }) }, want: "G_TOOL_DENIED" },
    { name: "no F-QC: the reviewer cannot write", agent: "jobhunter-qc", tool: "write", params: { path: WS + "/qc/work/verdict/0123456789abcdef.json", content: "{}" }, want: "G_TOOL_DENIED" },
    // 7.8 R4: the profile is required and never filled in
    { name: "R4 missing profile", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot" }, snap: { currentUrl: "https://www.naukri.com/x" }, rawParams: true, want: "G_BROWSER_PROFILE" },
    { name: "R4 null profile", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot", profile: null }, snap: { currentUrl: "https://www.naukri.com/x" }, want: "G_BROWSER_PROFILE" },
    { name: "R4 openclaw profile", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot", profile: "openclaw" }, snap: { currentUrl: "https://www.naukri.com/x" }, want: "G_BROWSER_PROFILE" },
    { name: "R4 padded jobhunter profile", agent: "jobhunter-scout", tool: "browser", params: { action: "snapshot", profile: " jobhunter" }, snap: { currentUrl: "https://www.naukri.com/x" }, want: "G_BROWSER_PROFILE" },
    { name: "R4 upload path form", agent: "jobhunter-applier", tool: "browser", params: { action: "upload", paths: ["~/Alex_Rivera_Resume.pdf"], inputRef: "e4" }, snap: { currentUrl: GH, refs, token: reservedToken(), stagedPath: "~/Alex_Rivera_Resume.pdf" }, want: "G_PATH_DENIED" },
    // 7.9 R7 other agents: protected roots read the way OpenClaw reads paths
    { name: "R7 main ~/ path to the private folder", agent: "main", tool: "read", params: { path: "~/openclaw-job-hunter/private/consent.json" }, snap: { config: R7_CONFIG, homeDir: "/opt/jobhunter-test" }, want: "G_PATH_DENIED" },
    { name: "R7 main ~/ path to guard.key", agent: "main", tool: "read", params: { path: "~/openclaw-job-hunter/private/guard.key" }, snap: { config: R7_CONFIG, homeDir: "/opt/jobhunter-test" }, want: "G_PATH_DENIED" },
    { name: "R7 main @ path", agent: "main", tool: "read", params: { path: "@" + REPO_PATH + "/private/consent.json" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main node:// path", agent: "main", tool: "read", params: { path: "node://n" + REPO_PATH + "/private/consent.json" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main file:// path", agent: "main", tool: "read", params: { path: "file://" + REPO_PATH + "/private/home.json" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main relative path from the repo", agent: "main", tool: "read", params: { path: "private/home.json" }, ctx: { cwd: REPO_PATH }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main relative .. path", agent: "main", tool: "read", params: { path: "../private/home.json" }, ctx: { cwd: REPO_PATH + "/scripts" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main reads through a link", agent: "main", tool: "read", params: { path: HOME + "/notes/k.txt" }, snap: { config: R7_CONFIG, realpath: (p: string) => p.replace(HOME + "/notes", REPO_PATH + "/private") }, want: "G_PATH_DENIED" },
    { name: "R7 main native Read of the private folder", agent: "main", tool: "Read", params: { file_path: REPO_PATH + "/private/home.json" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main Glob of the private folder", agent: "main", tool: "Glob", params: { pattern: REPO_PATH + "/private/*" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main Glob with a path in the private folder", agent: "main", tool: "Glob", params: { pattern: "*.json", path: REPO_PATH + "/private" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main Glob elsewhere", agent: "main", tool: "Glob", params: { pattern: HOME + "/notes/*.md" }, snap: { config: R7_CONFIG }, want: "pass" },
    { name: "R7 main Grep over the repo", agent: "main", tool: "Grep", params: { pattern: "key", path: REPO_PATH }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main Grep over scripts", agent: "main", tool: "Grep", params: { pattern: "key", path: REPO_PATH + "/scripts" }, snap: { config: R7_CONFIG }, want: "pass" },
    { name: "R7 main write under the repo scripts", agent: "main", tool: "write", params: { path: REPO_PATH + "/scripts/jh.py", content: "x" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main edit under WS_ROOT", agent: "main", tool: "edit", params: { path: WS + "/scout/AGENTS.md", oldText: "a", newText: "b" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main write under WS_ROOT", agent: "main", tool: "write", params: { path: WS + "/scout/AGENTS.md", content: "x" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main write under WS_ROOT without config", agent: "main", tool: "write", params: { path: WS + "/scout/skills/x/SKILL.md", content: "x" }, want: "G_PATH_DENIED" },
    { name: "R7 main apply_patch into the repo", agent: "main", tool: "apply_patch", params: { input: "*** Begin Patch\n*** Update File: " + REPO_PATH + "/scripts/jobhunter/acl.json\n*** End Patch" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main reads the OpenClaw state", agent: "main", tool: "read", params: { path: OC + "/openclaw.json" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main reads its own workspace inside the state folder", agent: "main", tool: "read", params: { path: "notes.md" }, ctx: { workspaceDir: OC + "/workspace" }, snap: { config: R7_CONFIG }, want: "pass" },
    { name: "R7 main writes its own workspace inside the state folder", agent: "main", tool: "write", params: { path: OC + "/workspace/notes.md", content: "x" }, ctx: { workspaceDir: OC + "/workspace" }, snap: { config: R7_CONFIG }, want: "pass" },
    { name: "R7 main reads next to its own workspace", agent: "main", tool: "read", params: { path: OC + "/openclaw.json" }, ctx: { workspaceDir: OC + "/workspace" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main reads ~/notes.txt", agent: "main", tool: "read", params: { path: "~/notes.txt" }, snap: { config: R7_CONFIG }, want: "pass" },
    { name: "R7 main reads the repo scripts", agent: "main", tool: "read", params: { path: REPO_PATH + "/scripts/jh.py" }, snap: { config: R7_CONFIG }, want: "pass" },
    { name: "R7 main exec glob into private", agent: "main", tool: "exec", params: { command: "cp ./p?ivat?/consent.json /tmp/k", workdir: REPO_PATH }, snap: { config: R7_CONFIG, glob: (p: string) => (p === REPO_PATH + "/p?ivat?/consent.json" ? [REPO_PATH + "/private/consent.json"] : []) }, want: "G_PATH_DENIED" },
    { name: "R7 main exec cp private/g*", agent: "main", tool: "exec", params: { command: "cp private/g* /tmp/k", workdir: REPO_PATH }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main exec relative private path", agent: "main", tool: "exec", params: { command: "cat private/home.json" }, ctx: { cwd: REPO_PATH }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main exec ~/.openclaw", agent: "main", tool: "exec", params: { command: "cat ~/.openclaw/openclaw.json" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main exec option value path", agent: "main", tool: "exec", params: { command: "tool --in=" + REPO_PATH + "/private/home.json" }, snap: { config: R7_CONFIG }, want: "G_PATH_DENIED" },
    { name: "R7 main exec elsewhere passes", agent: "main", tool: "exec", params: { command: "cat ~/notes.txt ./a/b.md" }, snap: { config: R7_CONFIG }, want: "pass" },
    { name: "R7 main openclaw cron edit of a jobhunter job", agent: "main", tool: "exec", params: { command: "openclaw cron edit jobhunter-scout-cycle --message x" }, want: "G_OTHER_AGENT_DENIED" },
    { name: "R7 main openclaw agent --agent jobhunter", agent: "main", tool: "Bash", params: { command: "openclaw agent --agent jobhunter-evaluator -m hi" }, want: "G_OTHER_AGENT_DENIED" },
    { name: "R7 main openclaw config set on jobhunter", agent: "main", tool: "exec", params: { command: "openclaw config set agents.entries.jobhunter-scout.tools.exec.security full" }, want: "G_OTHER_AGENT_DENIED" },
    { name: "R7 main openclaw plugins disable jobhunter-guard", agent: "main", tool: "exec", params: { command: "openclaw plugins disable jobhunter-guard" }, want: "G_OTHER_AGENT_DENIED" },
    { name: "R7 main openclaw cron list passes", agent: "main", tool: "exec", params: { command: "openclaw cron list" }, want: "pass" },
    { name: "R7 cron tool aimed at jobhunter", agent: "main", tool: "cron", params: { action: "add", agentId: "jobhunter-evaluator", message: "x" }, want: "G_OTHER_AGENT_DENIED" },
    { name: "R7 sessions_send to jobhunter", agent: "main", tool: "sessions_send", params: { to: "agent:jobhunter-scout:x", message: "x" }, want: "G_OTHER_AGENT_DENIED" },
    { name: "R7 sessions_spawn of jobhunter (case)", agent: "main", tool: "sessions_spawn", params: { agentId: "JobHunter-Applier" }, want: "G_OTHER_AGENT_DENIED" },
    { name: "R7 subagents naming jobhunter", agent: "main", tool: "subagents", params: { action: "steer", target: "jobhunter-outreach" }, want: "G_OTHER_AGENT_DENIED" },
    { name: "R7 gateway naming jobhunter", agent: "main", tool: "gateway", params: { action: "config.patch", raw: "{\"plugins\":{\"entries\":{\"jobhunter-guard\":null}}}" }, want: "G_OTHER_AGENT_DENIED" },
    { name: "R7 cron tool without jobhunter", agent: "main", tool: "cron", params: { action: "add", agentId: "main", message: "x" }, want: "pass" },
    { name: "R7 main native Bash jh.py status", agent: "main", tool: "Bash", params: { command: JH + " status", description: "s" }, want: "pass" },
    { name: "R7 main native Bash jh.py approve", agent: "main", tool: "Bash", params: { command: JH + " approve A7K2 --by chat" }, want: "G_EXEC_ACL" },
    { name: "R7 main jh.py status with -I", agent: "main", tool: "exec", params: { command: PY + " -I " + REPO_PATH + "/scripts/jh.py status" }, want: "pass" },
    { name: "R7 main types --agent-proof", agent: "main", tool: "exec", params: { command: JH + " --agent-proof " + fake + " status" }, want: "G_EXEC_PARAM" },
    { name: "R7 main types --agent-p", agent: "main", tool: "exec", params: { command: "python3 x.py --agent-p y" }, want: "G_EXEC_PARAM" },
  ];
}

function outcome(d: Decision): string {
  return d.kind === "block" ? d.code : d.kind;
}

function withProfile(c: Case): Record<string, unknown> {
  if (c.tool !== "browser" || !c.agent.startsWith("jobhunter-") || c.rawParams || Object.prototype.hasOwnProperty.call(c.params, "profile")) return c.params;
  return { profile: "jobhunter", ...c.params };
}

test("decision table", () => {
  for (const c of cases) {
    const d = decide({ toolName: c.tool, params: withProfile(c) }, { agentId: c.agent || undefined, sessionKey: c.sessionKey, ...(c.ctx || {}) }, snap(c.snap || {}));
    assert.equal(outcome(d), c.want, c.name + " -> " + JSON.stringify(d));
    if (d.kind === "block") assert.match(d.code, /^G_[A-Z_]+$/);
  }
});

test("exec rewrites timeout, workdir, -I and the argv proof", () => {
  const d = decide({ toolName: "exec", params: { command: JH + " preflight --lane applier", timeoutSeconds: 1800, workdir: WS + "/applier", yieldMs: 5 } }, { agentId: "jobhunter-applier" }, snap());
  assert.equal(d.kind, "allow");
  if (d.kind === "allow") {
    assert.deepEqual(d.params, { command: rewritten("jobhunter-applier", "preflight --lane applier"), workdir: WS + "/applier/work", timeoutSeconds: 90 });
    assert.equal(d.command, "preflight");
    const tokens = String(d.params!.command).split(" ");
    assert.equal(tokens[0], PY);
    assert.equal(tokens[1], "-I");
    assert.equal(tokens[2], REPO_PATH + "/scripts/jh.py");
    assert.equal(tokens[3], "--agent-proof");
    assert.match(tokens[4], /^jhp2\.jobhunter-applier\.[0-9]{10}\.[0-9a-f]{16}\.[0-9a-f]{16}\.[0-9a-f]{64}$/);
    assert.deepEqual(tokens.slice(5), ["preflight", "--lane", "applier"]);
  }
  const host = decide({ toolName: "mcp__openclaw__exec", params: { command: JH + " --cycle " + CYCLE + " home show", host: "gateway" } }, { agentId: "jobhunter-scout", sessionKey: "agent:jobhunter-scout:cron:x" }, snap());
  assert.equal(host.kind, "allow");
  if (host.kind === "allow") {
    assert.deepEqual(host.params, { command: rewritten("jobhunter-scout", "--cycle " + CYCLE + " home show", "agent:jobhunter-scout:cron:x"), workdir: WS + "/scout/work", timeoutSeconds: 90, host: "gateway" });
  }
});

test("the exec title is display only: the rewrite never carries it into the command", () => {
  const title = "Record --agent-proof x; rm -rf /";
  const d = decide({ toolName: "exec", params: { title, command: JH + " whoami" } }, { agentId: "jobhunter-evaluator", sessionKey: "agent:jobhunter-evaluator:cron:probe" }, snap());
  assert.equal(d.kind, "allow");
  if (d.kind === "allow") {
    assert.deepEqual(d.params, { command: rewritten("jobhunter-evaluator", "whoami", "agent:jobhunter-evaluator:cron:probe"), workdir: WS + "/evaluator/work", timeoutSeconds: 90 });
    assert.equal(String(d.params!.command).includes("rm"), false);
  }
});

test("a second decision of the rewritten call strips the own proof and mints again", () => {
  const ctx = { agentId: "jobhunter-evaluator", sessionKey: "agent:jobhunter-evaluator:cron:onboard" };
  const first = decide({ toolName: "exec", params: { command: JH + " profile infer-record --file " + WS + "/evaluator/work/onboarding/out.json" } }, ctx, snap());
  assert.equal(first.kind, "allow");
  if (first.kind !== "allow") return;
  const again = decide({ toolName: "exec", params: first.params! }, ctx, snap());
  assert.equal(again.kind, "allow", JSON.stringify(again));
  if (again.kind === "allow") assert.equal(again.params!.command, first.params!.command);
  // a fresh nonce on the second mint gives a different token, still exactly one proof pair
  let n = 0;
  const minting = snap({ mintProof: (a: string, s: string, rest: string[]) => argvProof(TEST_KEY, a, s, rest, T0S, (++n).toString(16).padStart(16, "0")) });
  const third = decide({ toolName: "exec", params: first.params! }, ctx, minting);
  assert.equal(third.kind, "allow");
  if (third.kind === "allow") {
    const toks = String(third.params!.command).split(" ");
    assert.equal(toks.filter((t) => t === "--agent-proof").length, 1);
    assert.notEqual(toks[4], String(first.params!.command).split(" ")[4]);
  }
});

test("without the argv carrier the rewrite still adds -I but no proof", () => {
  const d = decide({ toolName: "exec", params: { command: JH + " home show" } }, { agentId: "jobhunter-evaluator" }, snap({ config: config({ proofCarriers: ["env"] }) }));
  assert.equal(d.kind, "allow");
  if (d.kind === "allow") assert.equal(d.params!.command, PY + " -I " + REPO_PATH + "/scripts/jh.py home show");
});

test("mode N rewrites native Bash to the native shape with the proof", () => {
  const gate = snap({ config: config({ claudeNativeTools: "gate" }) });
  const d = decide({ toolName: "exec", params: { command: JH + " home show", timeout: 300000, description: "show home" } }, { agentId: "jobhunter-evaluator" }, gate);
  assert.equal(d.kind, "allow");
  if (d.kind === "allow") {
    assert.deepEqual(d.params, { command: rewritten("jobhunter-evaluator", "home show"), timeout: 90000, description: "show home" });
    assert.equal(d.native, true);
  }
  const short = decide({ toolName: "Bash", params: { command: JH + " home show", timeout: 5000 } }, { agentId: "jobhunter-evaluator" }, gate);
  if (short.kind === "allow") assert.deepEqual(short.params, { command: rewritten("jobhunter-evaluator", "home show"), timeout: 5000 });
  const deny = decide({ toolName: "Bash", params: { command: JH + " home show" } }, { agentId: "jobhunter-evaluator" }, snap());
  assert.equal(deny.kind, "block");
  if (deny.kind === "block") {
    assert.equal(deny.native, true);
    assert.match(deny.reason, /Claude Code tools are off for jobhunter agents; this run is not restricted \(see doctor\)/);
  }
});

test("browser needs the jobhunter profile and is never rewritten", () => {
  const onBoard = snap({ currentUrl: "https://www.naukri.com/x" });
  const d1 = decide({ toolName: "browser", params: { action: "snapshot" } }, { agentId: "jobhunter-scout" }, onBoard);
  assert.equal(d1.kind, "block");
  if (d1.kind === "block") assert.equal(d1.reason, "pass profile: \"jobhunter\" on every browser call");
  const d2 = decide({ toolName: "browser", params: { action: "snapshot", profile: "jobhunter" } }, { agentId: "jobhunter-scout" }, onBoard);
  assert.equal(d2.kind, "allow");
  if (d2.kind === "allow") assert.equal(d2.params, undefined);
});

test("path helpers", () => {
  assert.equal(pathFormError(WS + "/scout/work/a.json"), null);
  assert.equal(pathFormError("work/a.json"), null);
  for (const bad of ["~", "~/a", "@a", "a:b", "C:/x", "http://x", "a/../b", "..", "$x", "a\\b", "a\u0000"]) assert.notEqual(pathFormError(bad), null, bad);
  assert.equal(resolveOtherPath("~/a", "/base", "/h"), "/h/a");
  assert.equal(resolveOtherPath("~", "/base", "/h"), "/h");
  assert.equal(resolveOtherPath("@../x", "/base/w", "/h"), "/base/x");
  assert.equal(resolveOtherPath("node://mac/etc/hosts", "/base", "/h"), "/etc/hosts");
  assert.equal(resolveOtherPath("file:///etc/a%20b", "/base", "/h"), "/etc/a b");
  assert.equal(resolveOtherPath("x/y", "/base", "/h"), "/base/x/y");
  assert.equal(globPrefix("/a/b/*.md"), "/a/b");
  assert.equal(globPrefix("/a/b/c"), "/a/b/c");
  assert.equal(globPrefix("*.md"), "");
  assert.equal(globPrefix("/*"), "/");
  assert.deepEqual(shellPathWords("cat \"a b\" ./x ~/y --in=/z k=v http://h/p; ls"), ["./x", "~/y", "--in=/z", "/z", "http://h/p"]);
  assert.equal(isNativeShape("Bash", "exec", {}), true);
  assert.equal(isNativeShape(null, "exec", { command: "x", timeoutSeconds: 9 }), false);
  assert.equal(isNativeShape(null, "exec", { command: "x", timeout: 9 }), true);
  assert.equal(isNativeShape(null, "read", { path: "a" }), false);
  assert.equal(isNativeShape(null, "read", { file_path: "a" }), true);
});

test("allowed fill and commit produce token log records", () => {
  const d = decide(
    { toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "batch", actions: [{ kind: "type", ref: "e4", text: "A" }, { kind: "click", ref: "e6" }] } },
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
  const read = decide({ toolName: "browser", params: { profile: "jobhunter", action: "snapshot" } }, { agentId: "jobhunter-applier" }, snap({ currentUrl: GH, token: armedToken() }));
  assert.equal(read.kind, "allow");
  if (read.kind === "allow") assert.equal(read.records, undefined);
});

test("block reasons name the code first", () => {
  const d = decide({ toolName: "exec", params: { command: "sqlite3 x" } }, { agentId: "jobhunter-scout" }, snap());
  assert.equal(d.kind, "block");
  if (d.kind === "block") assert.ok(d.reason.length > 10);
});

test("agent id helpers", () => {
  assert.deepEqual(normalizeToolName("mcp__openclaw__browser"), { tool: "browser", nativeName: null });
  assert.deepEqual(normalizeToolName("mcp__other__browser"), { tool: "mcp__other__browser", nativeName: null });
  assert.deepEqual(normalizeToolName("exec"), { tool: "exec", nativeName: null });
  const mapped: Record<string, string> = { Bash: "exec", Read: "read", Write: "write", Edit: "edit", MultiEdit: "edit", NotebookEdit: "notebook_edit", Glob: "glob", Grep: "grep", LS: "ls", WebFetch: "web_fetch", WebSearch: "web_search", Task: "task", TodoWrite: "todo_write" };
  for (const [raw, tool] of Object.entries(mapped)) assert.deepEqual(normalizeToolName(raw), { tool, nativeName: raw });
  // AskUserQuestion never reaches plugin hooks: no mapping is claimed for it
  assert.deepEqual(normalizeToolName("AskUserQuestion"), { tool: "AskUserQuestion", nativeName: null });
  assert.equal(effectiveAgentId({ agentId: "main", sessionKey: "agent:jobhunter-applier:x" }), "jobhunter-applier");
  assert.equal(effectiveAgentId({ sessionKey: "agent:main:main" }), "main");
  assert.equal(effectiveAgentId({}), "");
});

// ------------------------------------------------------------------ FEATURES-OTP-ACCOUNTS-CAPTCHA 2.11
// Secret fields (G_SECRET_FIELD), forbidden names (social sign-in, CAPTCHA widgets), new never hosts, the new
// ATS platforms (Oracle host pattern) and the per-platform ats:<key> breaker.

const WD = "https://kestrel.wd5.myworkdayjobs.com/en-US/careers/job/Senior-Analyst_R1001";
const ORA = "https://ekyx.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/1001";
const NK = "https://www.naukri.com/job-listings-data-analyst-kestrel-commerce-1";
const secretRefs: Record<string, RefInfo> = {
  p1: { role: "textbox", name: "Password" },
  p2: { role: "textbox", name: "Verification code" },
  p3: { role: "spinbutton", name: "PIN" },
  p4: { role: "textbox", name: "Email address" },
  p5: { role: "textbox", name: "Confirm password" },
  p6: { role: "searchbox", name: "One-time passcode" },
  p7: { role: "textbox", name: "Enter the security code" },
  p8: { role: "textbox", name: "OTP" },
  p9: { role: "button", name: "Show password" }, // a button, not a field: clicking it is no secret focus
  q1: { role: "textbox", name: "Spinning classes attended" }, // \bpin\b does not match inside a word
  s1: { role: "button", name: "Sign in with Google" },
  s2: { role: "checkbox", name: "I'm not a robot" },
  s3: { role: "link", name: "Continue with LinkedIn" },
  s4: { role: "button", name: "Use your Google account" },
  s5: { role: "checkbox", name: "Verify you are human" },
  s6: { role: "button", name: "Apply with Indeed" },
  s7: { role: "iframe", name: "reCAPTCHA" },
  s8: { role: "button", name: "Log in with Microsoft" },
  n1: { role: "button", name: "Create account" },
  n2: { role: "button", name: "Next" },
};
const wdTok = { currentUrl: WD, refs: secretRefs, token: reservedToken("workday") };
const wdNoTok = { currentUrl: WD, refs: secretRefs };

const otpCases: Case[] = [
  // G_SECRET_FIELD: type and fill into password, code and PIN fields, with or without a token
  { name: "type into Password with a token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p1", text: "x" }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "type into Password without a token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p1", text: "x" }, snap: wdNoTok, want: "G_SECRET_FIELD" },
  { name: "type into Verification code", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p2", text: "483920" }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "type into a PIN spinbutton", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p3", text: "1234" }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "type into Confirm password", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p5", text: "x" }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "type into a one-time passcode searchbox", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p6", text: "x" }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "type into a security code field", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p7", text: "Q7RZ4M2K" }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "type into OTP", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p8", text: "x" }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "type slowly into Password", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p1", text: "x", slowly: true }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "type into Password through request", agent: "jobhunter-applier", tool: "browser", params: { action: "act", request: { kind: "type", ref: "p1", text: "x" } }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "type by a password selector", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", selector: "input[type=password]", text: "x" }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "type by an otp selector", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", selector: "#otp-input", text: "x" }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "press with a ref on a code field", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "press", ref: "p2", key: "4" }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "fill a Password ref", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "fill", ref: "p1", text: "x" }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "fill fields: the second is a code field", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "fill", fields: [{ ref: "p4", type: "text", value: "alex.rivera@example.com" }, { ref: "p2", type: "text", value: "483920" }] }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "fill fields without a token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "fill", fields: [{ ref: "p3", type: "text", value: "1" }] }, snap: wdNoTok, want: "G_SECRET_FIELD" },
  { name: "fill fields: email only", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "fill", fields: [{ ref: "p4", type: "text", value: "alex.rivera@example.com" }] }, snap: wdTok, want: "allow" },
  { name: "type into Email address", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p4", text: "alex.rivera@example.com" }, snap: wdTok, want: "allow" },
  { name: "a word with pin inside is no PIN field", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "q1", text: "3" }, snap: wdTok, want: "allow" },
  // ref-less type or press right after a click on a secret field (in one batch, or by the tab's flag)
  { name: "batch: click Password then type without a ref", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "batch", actions: [{ kind: "click", ref: "p1" }, { kind: "type", text: "x" }] }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "batch: click code field then press a key", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "batch", actions: [{ kind: "click", ref: "p2" }, { kind: "press", key: "4" }] }, snap: wdTok, want: "G_SECRET_FIELD" },
  { name: "batch: click Password, then Email, then type", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "batch", actions: [{ kind: "click", ref: "p1" }, { kind: "click", ref: "p4" }, { kind: "type", text: "x" }] }, snap: wdTok, want: "allow" },
  { name: "secret focus: type without a ref", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", text: "x" }, snap: { ...wdTok, secretFocus: true }, want: "G_SECRET_FIELD" },
  { name: "secret focus: press without a ref", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "press", key: "a" }, snap: { ...wdTok, secretFocus: true }, want: "G_SECRET_FIELD" },
  { name: "secret focus: press without a token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "press", key: "Tab" }, snap: { ...wdNoTok, secretFocus: true }, want: "G_SECRET_FIELD" },
  { name: "secret focus: type into a named field", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p4", text: "x" }, snap: { ...wdTok, secretFocus: true }, want: "allow" },
  { name: "secret focus: a snapshot is fine", agent: "jobhunter-applier", tool: "browser", params: { action: "snapshot" }, snap: { ...wdTok, secretFocus: true }, want: "allow" },
  { name: "no secret focus: press without a ref", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "press", key: "a" }, snap: wdTok, want: "allow" },
  { name: "secret focus applies to every jobhunter agent", agent: "jobhunter-outreach", tool: "browser", params: { action: "act", kind: "type", text: "x" }, snap: { currentUrl: "https://mail.google.com/mail/u/0/#inbox", secretFocus: true }, want: "G_SECRET_FIELD" },
  // forbidden names: social sign-in and CAPTCHA widgets are never clicked, with or without a token
  { name: "click Sign in with Google (token)", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "s1" }, snap: wdTok, want: "G_TOOL_DENIED" },
  { name: "click Sign in with Google (no token)", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "s1" }, snap: wdNoTok, want: "G_TOOL_DENIED" },
  { name: "click Sign in with Google (armed, dwell done)", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "s1" }, snap: { ...wdTok, token: armedToken("workday"), dwell: dwellDone() }, want: "G_TOOL_DENIED" },
  { name: "click I'm not a robot (token)", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "s2" }, snap: wdTok, want: "G_TOOL_DENIED" },
  { name: "click I'm not a robot (no token)", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "s2" }, snap: wdNoTok, want: "G_TOOL_DENIED" },
  { name: "click Continue with LinkedIn link", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "s3" }, snap: wdNoTok, want: "G_TOOL_DENIED" },
  { name: "click Use your Google account", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "s4" }, snap: wdTok, want: "G_TOOL_DENIED" },
  { name: "click Verify you are human", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "s5" }, snap: wdNoTok, want: "G_TOOL_DENIED" },
  { name: "click Apply with Indeed", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "s6" }, snap: wdTok, want: "G_TOOL_DENIED" },
  { name: "click the reCAPTCHA frame", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "s7" }, snap: wdTok, want: "G_TOOL_DENIED" },
  { name: "double click Log in with Microsoft", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "s8", doubleClick: true }, snap: wdTok, want: "G_TOOL_DENIED" },
  { name: "forbidden click inside a batch", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "batch", actions: [{ kind: "click", ref: "n2" }, { kind: "click", ref: "s2" }] }, snap: wdTok, want: "G_TOOL_DENIED" },
  { name: "type slowly clicks a forbidden ref", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "s1", text: "x", slowly: true }, snap: wdTok, want: "G_TOOL_DENIED" },
  { name: "download through a forbidden ref", agent: "jobhunter-applier", tool: "browser", params: { action: "download", ref: "s1" }, snap: wdTok, want: "G_TOOL_DENIED" },
  { name: "scout clicks a CAPTCHA box on a board", agent: "jobhunter-scout", tool: "browser", params: { action: "act", kind: "click", ref: "s2" }, snap: { currentUrl: NK, refs: secretRefs }, want: "G_TOOL_DENIED" },
  { name: "outreach clicks Sign in with Google", agent: "jobhunter-outreach", tool: "browser", params: { action: "act", kind: "click", ref: "s1" }, snap: { currentUrl: LI, refs: secretRefs }, want: "G_TOOL_DENIED" },
  { name: "hovering a CAPTCHA box is a read", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "hover", ref: "s2" }, snap: wdNoTok, want: "allow" },
  { name: "Create account is not forbidden (needs a token)", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "click", ref: "n1" }, snap: wdNoTok, want: "G_NO_TOKEN" },
  // never hosts: the owner's Microsoft and Apple accounts and LinkedIn OAuth
  ...["https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x", "https://login.live.com/oauth20_authorize.srf", "https://account.live.com/",
    "https://account.microsoft.com/security", "https://appleid.apple.com/auth/authorize", "https://idmsa.apple.com/appleauth/auth", "https://account.apple.com/",
    "https://www.linkedin.com/oauth/v2/authorization?response_type=code&client_id=x", "https://www.linkedin.com/uas/oauth2/authorization?x=1",
    "https://LinkedIn.com/oauth/v2/authorization", "https://www.linkedin.com:443/oauth/v2/authorization"].map((u): Case => (
    { name: "never host " + u, agent: "jobhunter-applier", tool: "browser", params: { action: "navigate", targetUrl: u }, want: "G_HOST_NEVER" })),
  { name: "a page already on login.microsoftonline.com", agent: "jobhunter-applier", tool: "browser", params: { action: "snapshot" }, snap: { currentUrl: "https://login.microsoftonline.com/common/login" }, want: "G_HOST_NEVER" },
  { name: "linkedin jobs stay allowed", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://www.linkedin.com/jobs/view/1001" }, want: "allow" },
  { name: "a path that only starts with oauth text", agent: "jobhunter-scout", tool: "browser", params: { action: "navigate", targetUrl: "https://kestrel.example/linkedin.com/oauth/x" }, want: "allow" },
  // new ATS platforms: forms are public (no consent), fill needs the platform's token
  { name: "oracle form fill with an oracle_hcm token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p4", text: "x" }, snap: { currentUrl: ORA, refs: secretRefs, token: reservedToken("oracle_hcm"), consent: { ok: false, sites: new Set(), reason: "x" } }, want: "allow" },
  { name: "oracle form fill with a workday token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p4", text: "x" }, snap: { currentUrl: ORA, refs: secretRefs, token: reservedToken("workday") }, want: "G_NO_TOKEN" },
  { name: "icims form fill with an icims token", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p4", text: "x" }, snap: { currentUrl: "https://careers-kestrel.icims.com/jobs/1001/job", refs: secretRefs, token: reservedToken("icims") }, want: "allow" },
  // the core's per-platform ATS breaker blocks that platform's hosts only
  { name: "ats:workday open: fill on Workday", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p4", text: "x" }, snap: { ...wdTok, openBreakers: new Set(["ats:workday"]) }, want: "G_BREAKER_OPEN" },
  { name: "ats:workday open: read on Workday", agent: "jobhunter-applier", tool: "browser", params: { action: "snapshot" }, snap: { currentUrl: WD, openBreakers: new Set(["ats:workday"]) }, want: "G_BREAKER_OPEN" },
  { name: "ats:workday open: navigate to Workday", agent: "jobhunter-applier", tool: "browser", params: { action: "navigate", targetUrl: WD }, snap: { openBreakers: new Set(["ats:workday"]) }, want: "G_BREAKER_OPEN" },
  { name: "ats:workday open: close still runs", agent: "jobhunter-applier", tool: "browser", params: { action: "close" }, snap: { currentUrl: WD, openBreakers: new Set(["ats:workday"]) }, want: "allow" },
  { name: "ats:workday open: Greenhouse fill goes on", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "e4", text: "x" }, snap: { currentUrl: GH, refs, token: reservedToken(), openBreakers: new Set(["ats:workday"]) }, want: "allow" },
  { name: "ats:oracle_hcm open: Oracle tenant blocked", agent: "jobhunter-applier", tool: "browser", params: { action: "act", kind: "type", ref: "p4", text: "x" }, snap: { currentUrl: ORA, refs: secretRefs, token: reservedToken("oracle_hcm"), openBreakers: new Set(["ats:oracle_hcm"]) }, want: "G_BREAKER_OPEN" },
];

test("2.11: secret fields, forbidden names, never hosts, new platforms, ats:<key> breakers", () => {
  for (const c of otpCases) {
    const d = decide({ toolName: c.tool, params: withProfile(c) }, { agentId: c.agent, sessionKey: c.sessionKey, ...(c.ctx || {}) }, snap(c.snap || {}));
    assert.equal(outcome(d), c.want, c.name + " -> " + JSON.stringify(d));
  }
});

test("2.11: the G_SECRET_FIELD and forbidden name reasons", () => {
  const d = decide({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "type", ref: "p1", text: "x" } }, { agentId: "jobhunter-applier" }, snap(wdNoTok));
  assert.equal(blockReason(d), "G_SECRET_FIELD: secret fields are filled by code: jh.py account create, account signin or code submit");
  const f = decide({ toolName: "browser", params: { profile: "jobhunter", action: "act", kind: "click", ref: "s2" } }, { agentId: "jobhunter-applier" }, snap(wdNoTok));
  assert.match(blockReason(f), /^G_TOOL_DENIED: social sign-in buttons and CAPTCHA widgets are never clicked/);
  assert.ok(BLOCK_CODES.includes("G_SECRET_FIELD"));
});

test("2.11: the secretFocus flag a decision hands back (set by a click on a secret field, cleared by a snapshot, navigation or another click)", () => {
  const focusOf = (params: Record<string, unknown>, over: Partial<Snapshot> = {}) => {
    const d = decide({ toolName: "browser", params: { profile: "jobhunter", ...params } }, { agentId: "jobhunter-applier" }, snap({ ...wdTok, ...over }));
    assert.equal(d.kind, "allow", JSON.stringify(d));
    return d.kind === "allow" ? d.secretFocus : "blocked";
  };
  assert.equal(focusOf({ action: "act", kind: "click", ref: "p1" }), true);
  assert.equal(focusOf({ action: "act", kind: "click", ref: "p3" }), true);
  assert.equal(focusOf({ action: "act", kind: "click", ref: "p4" }, { secretFocus: true }), false);
  const armed = { token: armedToken("workday"), dwell: dwellDone() };
  assert.equal(focusOf({ action: "act", kind: "click", ref: "p9" }, { ...armed, secretFocus: true }), false); // "Show password" is a button
  assert.equal(focusOf({ action: "act", kind: "click", ref: "zz" }, { ...armed, secretFocus: true }), undefined); // unknown ref: unchanged
  assert.equal(focusOf({ action: "snapshot" }, { secretFocus: true }), false);
  assert.equal(focusOf({ action: "navigate", targetUrl: WD }, { secretFocus: true }), false);
  assert.equal(focusOf({ action: "open", targetUrl: WD }, { secretFocus: true }), undefined); // a new tab: the marked one stays marked
  assert.equal(focusOf({ action: "act", kind: "type", ref: "p4", text: "x" }), undefined);
  assert.equal(focusOf({ action: "act", kind: "batch", actions: [{ kind: "click", ref: "p4" }, { kind: "click", ref: "p1" }] }), true);
  assert.equal(focusOf({ action: "act", kind: "hover", ref: "p1" }), undefined);
});

test("2.11: guard-hosts.json parses the new keys and fails closed without them", () => {
  const raw = JSON.parse(fs.readFileSync(path.join(REPO, "openclaw", "guard-hosts.json"), "utf8"));
  const h = parseHostsConfig(raw);
  assert.ok(h.forbiddenNames.length >= 3);
  assert.ok(h.secretFieldNames.test("Password"));
  const plat = (u: string) => classifyUrl(u, h).platform?.key ?? null;
  assert.equal(plat(ORA), "oracle_hcm");
  assert.equal(plat("https://EKYX.FA.US2.ORACLECLOUD.COM/hcmUI/x"), "oracle_hcm");
  assert.equal(plat("https://fa.us2.oraclecloud.com/x"), null);
  assert.equal(plat("https://ekyx.fa.us2.oraclecloud.com.attacker.example/x"), null);
  assert.equal(plat("https://a.b.fa.us2.oraclecloud.com/x"), null);
  assert.equal(plat("https://www.oracle.com/careers/"), null);
  assert.equal(plat("https://careers-kestrel.icims.com/jobs/1001/job"), "icims");
  assert.equal(plat("https://career5.successfactors.eu/career?company=kestrel"), "successfactors");
  assert.equal(plat("https://kestrel.jobs2web.sapsf.com/job/1001"), "successfactors");
  assert.equal(plat("https://kestrel.taleo.net/careersection/2/jobdetail.ftl?job=1001"), "taleo");
  assert.equal(plat("https://jobs.jobvite.com/kestrel/job/o1001"), "jobvite");
  assert.equal(plat("https://www.jobvite.com/"), null);
  assert.equal(plat(WD), "workday");
  for (const k of ["icims", "successfactors", "taleo", "oracle_hcm", "jobvite"]) {
    const p = h.platforms.find((x) => x.key === k)!;
    assert.equal(p.scope, "ats", k);
    assert.equal(p.consent, null, k);
  }
  const info = classifyUrl(WD, h);
  assert.ok(blockingScopes(info, false).includes("ats:workday"));
  assert.ok(blockingScopes(info, true).includes("ats:workday"));
  assert.equal(blockingScopes(classifyUrl(NK, h), true).some((s) => s.startsWith("ats:")), false);
  // fail closed
  const without = (k: string) => {
    const r = JSON.parse(JSON.stringify(raw));
    delete r[k];
    return r;
  };
  assert.throws(() => parseHostsConfig(without("forbidden_names")));
  assert.throws(() => parseHostsConfig(without("secret_field_names")));
  const bad = JSON.parse(JSON.stringify(raw));
  bad.platforms.oracle_hcm.host_patterns = ["[a-z]+\\.fa\\.oraclecloud\\.com"]; // not anchored
  assert.throws(() => parseHostsConfig(bad));
  bad.platforms.oracle_hcm.host_patterns = "^x$";
  assert.throws(() => parseHostsConfig(bad));
});
