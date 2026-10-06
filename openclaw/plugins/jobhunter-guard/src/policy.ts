// decide(event, ctx, snapshot): the guard's pure decision function (design 1.4, rules R0 to R4 and R7, plus
// the per-site consent fence of the Chrome-session change and the Claude subscription route changes: native
// tool shapes, the argv identity proof, OpenClaw path forms, protected roots for other agents).
// No I/O and no clock: the runtime builds the snapshot (files, ledger, session state, the proof minter, a
// realpath and a glob expander) and applies the side effects the decision asks for (token log lines, guard log).

import path from "node:path";
import { blockingScopes, checkSecretFields, classifyBrowserCall, classifyUrl, tokenMatchesHost, type ActionItem, type HostInfo } from "./browser.ts";
import { AGENT_PROOF_FLAG, AGENT_PROOF_PREFIX, ISOLATED_FLAG, jhPath, parseAgentExec, parsePublicExec, trimSlash } from "./exec_parse.ts";
import { tsMs } from "./ledger.ts";
import type { Decision, ProofCarrier, Snapshot, TokenRecord, ToolCtx, ToolEvent } from "./types.ts";

export const JOBHUNTER_PREFIX = "jobhunter-";
export const EXEC_TIMEOUT_S = 90;
export const MAX_COMMITS_PER_TOKEN = 2;
export const DWELL_FLOOR_MS = 5000; // hard minimum of browser.dwell_seconds[0]
export const BROWSER_PROFILE = "jobhunter";
export const NATIVE_TIMEOUT_MS = 90000; // mode N: the native Bash timeout the rewrite sets (90 seconds)
const NATIVE_TIMEOUT_MAX_MS = 600000;
export const DEFAULT_CARRIERS: ProofCarrier[] = ["argv", "env"];

const MCP_PREFIX = "mcp__openclaw__";
// Claude Code's own tool names, in case a host hands them to the hooks without projecting them.
// (AskUserQuestion never reaches plugin hooks, so no mapping is claimed for it.)
export const NATIVE_TOOL_NAMES: Record<string, string> = {
  Bash: "exec",
  Read: "read",
  Write: "write",
  Edit: "edit",
  MultiEdit: "edit",
  NotebookEdit: "notebook_edit",
  Glob: "glob",
  Grep: "grep",
  LS: "ls",
  WebFetch: "web_fetch",
  WebSearch: "web_search",
  Task: "task",
  TodoWrite: "todo_write",
};
// Parameters only Claude Code's native tools carry (OpenClaw projects native Bash to `exec` and native
// Read/Write/Edit to `read/write/edit` with `path = file_path`, keeping these keys).
const NATIVE_EXEC_MARKERS = ["timeout", "run_in_background", "dangerouslyDisableSandbox"];
const NATIVE_EXEC_KEYS = new Set(["command", "description", "timeout", "run_in_background", "dangerouslyDisableSandbox"]);
const NATIVE_READ_KEYS = new Set(["file_path", "path", "offset", "limit", "pages"]);
const NATIVE_WRITE_KEYS = new Set(["file_path", "path", "content"]);
const NATIVE_DENY_REASON = "Claude Code tools are off for jobhunter agents; this run is not restricted (see doctor)";
// After a stop the agent may only report it and end the cycle; detect and breaker trip can only stop more.
const STOPPED_COMMANDS = new Set(["cycle end", "detect", "breaker trip"]);
const FILE_TOOLS = new Set(["read", "write", "edit", "apply_patch"]);
// `title` is the display label OpenClaw 2026.9.8 adds to its exec tool (`executionTitleSchema`, at most 120
// characters). It is display only: never part of the command, never used by the guard or jh.py. Any other key
// of that schema (`ask`, `node`) stays refused.
const EXEC_KEYS = new Set(["command", "workdir", "timeoutSeconds", "yieldMs", "host", "pty", "background", "elevated", "env", "description", "title"]);
export const EXEC_TITLE_MAX = 120;
const CLASS_RANK: Record<string, number> = { read: 0, nav_click: 1, driver: 2, fill: 3, commit: 4 };
// Browser actions that do not read or act on the addressed tab's page: they open a page (judged by its
// target), manage tabs, or manage the browser. Only these run on a tab whose page the guard does not know.
// `tabs` is here so the agent can learn which pages its tabs show; on a known page it is judged by that
// page like any read (a tab list carries titles), and only `close` skips the consent and breaker checks.
export const PAGELESS_ACTIONS = new Set(["open", "navigate", "tabs", "close", "start", "status", "profiles", "doctor"]);

// The OpenClaw tool name of a call: the `mcp__openclaw__` bridge prefix removed, a raw Claude Code name mapped
// (`nativeName` keeps the raw name then).
export function normalizeToolName(name: unknown): { tool: string; nativeName: string | null } {
  const n = typeof name === "string" ? name : "";
  if (n.startsWith(MCP_PREFIX)) return { tool: n.slice(MCP_PREFIX.length), nativeName: null };
  if (Object.prototype.hasOwnProperty.call(NATIVE_TOOL_NAMES, n)) return { tool: NATIVE_TOOL_NAMES[n], nativeName: n };
  return { tool: n, nativeName: null };
}

function has(o: Record<string, unknown>, k: string): boolean {
  return Object.prototype.hasOwnProperty.call(o, k);
}

// Whether a call has the shape of a Claude Code native tool: a raw native name, an exec with a native Bash
// option, or a file call with `file_path`.
export function isNativeShape(nativeName: string | null, tool: string, params: Record<string, unknown>): boolean {
  if (nativeName) return true;
  if (tool === "exec" && NATIVE_EXEC_MARKERS.some((k) => has(params, k))) return true;
  return has(params, "file_path");
}

// ctx.agentId, or the agent named in a session key "agent:<id>:...". A jobhunter id in either wins.
export function effectiveAgentId(ctx: ToolCtx): string {
  const fromCtx = typeof ctx.agentId === "string" ? ctx.agentId : "";
  const m = typeof ctx.sessionKey === "string" ? /^agent:([^:]+):/.exec(ctx.sessionKey) : null;
  const fromKey = m ? m[1] : "";
  if (fromCtx.startsWith(JOBHUNTER_PREFIX)) return fromCtx;
  if (fromKey.startsWith(JOBHUNTER_PREFIX)) return fromKey;
  return fromCtx || fromKey;
}

export function roleOf(agentId: string): string {
  return agentId.startsWith(JOBHUNTER_PREFIX) ? agentId.slice(JOBHUNTER_PREFIX.length) : agentId;
}

function block(code: string, reason: string, extra?: { command?: string; actionClass?: string; host?: string | null }): Decision {
  return { kind: "block", code, reason, ...(extra || {}) };
}

export function blockReason(d: Decision): string {
  return d.kind === "block" ? d.code + ": " + d.reason : "";
}

function under(p: string, base: string): boolean {
  const b = trimSlash(base);
  return p.startsWith(b + "/") && p.length > b.length + 1;
}

// ------------------------------------------------------------------ entry

export function decide(event: ToolEvent, ctx: ToolCtx, snap: Snapshot): Decision {
  const agentId = effectiveAgentId(ctx);
  const { tool, nativeName } = normalizeToolName(event.toolName);
  const params = event.params && typeof event.params === "object" ? event.params : {};
  const native = isNativeShape(nativeName, tool, params);
  if (!agentId.startsWith(JOBHUNTER_PREFIX)) {
    const d = decideOther(tool, params, event, agentId, ctx, snap);
    return native ? { ...d, native: true } : d;
  }
  const d = decideAgent(tool, native, params, event, agentId, ctx, snap);
  return native ? { ...d, native: true } : d;
}

// The tools of a jobhunter agent: its ACL list; in the F-QC fallback the reviewer also writes its verdict file.
export function agentTools(agentId: string, snap: Snapshot): string[] {
  const agent = snap.acl ? snap.acl.agents[agentId] : undefined;
  const tools = agent && Array.isArray(agent.tools) ? [...agent.tools] : [];
  if (agentId === JOBHUNTER_PREFIX + "qc" && snap.config.qcVerdictFile === true && !tools.includes("write")) tools.push("write");
  return tools;
}

function decideAgent(tool: string, native: boolean, params: Record<string, unknown>, event: ToolEvent, agentId: string, ctx: ToolCtx, snap: Snapshot): Decision {
  // Claude Code's native tools bypass OpenClaw's exec policy and workspace confinement: off for jobhunter
  // agents (restricted runs never offer them). Only the reduced-protection mode N gates them instead.
  if (native && snap.config.claudeNativeTools !== "gate") return block("G_TOOL_DENIED", NATIVE_DENY_REASON);

  // R0 health
  if (!snap.health.ok || !snap.acl || !snap.hosts) {
    return block("G_GUARD_UNHEALTHY", "the guard cannot verify this install (" + (snap.health.reason || "not loaded") + "); end the cycle");
  }
  const agent = snap.acl.agents[agentId];
  if (!agent) return block("G_TOOL_DENIED", "unknown jobhunter agent " + agentId);
  const tools = agentTools(agentId, snap);
  if (native) {
    const g = gateNative(tool, params);
    if (g) return g;
  }

  // R5 consequence: a stopped session may only report the stop and end its cycle (and keep notes in its
  // own work/ folder, which the agent programs do before cycle end)
  if (snap.sessionStopped) {
    if (tool === "exec" && tools.includes("exec")) {
      const d = decideExec(params, agentId, agent.commands, ctx, snap, native);
      if (d.kind === "allow" && d.command && STOPPED_COMMANDS.has(d.command)) return d;
    }
    if ((tool === "write" || tool === "read") && tools.includes(tool)) {
      return decideFile(tool, params, event, agentId, ctx, snap);
    }
    return block("G_STOPPED", "a stop signature was seen in this session; run jh.py cycle end, then finish with CYCLE_DONE");
  }

  // R1 tool allowlist
  if (!tools.includes(tool)) {
    return block("G_TOOL_DENIED", "tool " + (tool || "(none)") + " is not allowed for " + agentId);
  }

  if (tool === "exec") return decideExec(params, agentId, agent.commands, ctx, snap, native);
  if (FILE_TOOLS.has(tool)) return decideFile(tool, params, event, agentId, ctx, snap);
  if (tool === "browser") return decideBrowser(params, agentId, snap);
  return { kind: "allow" };
}

// Mode N only: the native call must use the native keys, no background run, no sandbox override, a bounded
// timeout, and one file path.
function gateNative(tool: string, params: Record<string, unknown>): Decision | null {
  const keys = Object.keys(params);
  if (tool === "exec") {
    for (const k of keys) if (!NATIVE_EXEC_KEYS.has(k)) return block("G_EXEC_SHAPE", "native Bash option " + k + " is not allowed");
    if (params.run_in_background === true) return block("G_EXEC_SHAPE", "background runs are not allowed");
    if (params.dangerouslyDisableSandbox === true) return block("G_EXEC_SHAPE", "the sandbox cannot be turned off");
    if (params.timeout !== undefined && params.timeout !== null) {
      if (typeof params.timeout !== "number" || !(params.timeout >= 0) || params.timeout > NATIVE_TIMEOUT_MAX_MS) return block("G_EXEC_SHAPE", "timeout must be at most " + NATIVE_TIMEOUT_MAX_MS + " ms");
    }
    return null;
  }
  if (tool === "read" || tool === "write") {
    const allowed = tool === "read" ? NATIVE_READ_KEYS : NATIVE_WRITE_KEYS;
    for (const k of keys) if (!allowed.has(k)) return block("G_TOOL_DENIED", "native " + tool + " option " + k + " is not allowed");
    if (typeof params.file_path !== "string") return block("G_PATH_DENIED", "no file path given");
    if (params.path !== undefined && params.path !== params.file_path) return block("G_PATH_DENIED", "file_path and path differ");
  }
  return null;
}

// ------------------------------------------------------------------ R2 exec

// The proof carriers of this install (plugin config proofCarriers; default argv and env).
export function proofCarriers(snap: Snapshot): ProofCarrier[] {
  const c = snap.config.proofCarriers;
  return Array.isArray(c) && c.length > 0 ? c : DEFAULT_CARRIERS;
}

function decideExec(params: Record<string, unknown>, agentId: string, commands: Record<string, Record<string, string>>, ctx: ToolCtx, snap: Snapshot, native: boolean): Decision {
  const role = roleOf(agentId);
  const wsRole = path.join(snap.wsRoot, role);
  const workRoot = path.join(wsRole, "work");
  if (!native) {
    for (const k of Object.keys(params)) {
      if (!EXEC_KEYS.has(k)) return block("G_EXEC_SHAPE", "exec option " + k + " is not allowed");
    }
    if (params.title !== undefined) {
      const t = params.title;
      if (typeof t !== "string" || t.length > EXEC_TITLE_MAX || /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/.test(t)) {
        return block("G_EXEC_SHAPE", "exec title must be one line of at most " + EXEC_TITLE_MAX + " characters");
      }
    }
    if (params.pty === true) return block("G_EXEC_SHAPE", "pty is not allowed");
    if (params.background === true) return block("G_EXEC_SHAPE", "background exec is not allowed");
    if (params.elevated === true) return block("G_EXEC_SHAPE", "elevated exec is not allowed");
    if (params.env !== undefined && params.env !== null) {
      if (typeof params.env !== "object" || Object.keys(params.env as object).length > 0) return block("G_EXEC_SHAPE", "agents cannot set environment variables");
    }
    if (params.host !== undefined && params.host !== null && params.host !== "gateway") return block("G_EXEC_SHAPE", "exec host must be gateway");
    if (params.workdir !== undefined && params.workdir !== null) {
      if (typeof params.workdir !== "string") return block("G_EXEC_SHAPE", "workdir must be a path");
      const wd = path.resolve(wsRole, params.workdir);
      if (wd !== wsRole && !under(wd, wsRole)) return block("G_EXEC_SHAPE", "workdir must be inside the agent's own workspace");
    }
  }
  const sessionKey = typeof ctx.sessionKey === "string" ? ctx.sessionKey : "";
  const parsed = parseAgentExec(params.command, {
    python: snap.config.python,
    repo: snap.config.repo,
    commands: commands || {},
    classes: (snap.acl && snap.acl.value_classes) || {},
    workRoots: [workRoot, path.join(wsRole, "inbox")],
    ownProof: (token: string, restAfter: string[]) => snap.verifyOwnProof(token, agentId, sessionKey, restAfter),
  });
  if (!parsed.ok) return block(parsed.code, parsed.reason);
  // R2 rewrite: always "<PY> -I <jh.py>", plus the guard's argv proof when argv is a carrier.
  const tokens = [snap.config.python, ISOLATED_FLAG, jhPath(snap.config.repo)];
  if (proofCarriers(snap).includes("argv")) tokens.push(AGENT_PROOF_FLAG, snap.mintProof(agentId, sessionKey, parsed.rest));
  tokens.push(...parsed.rest);
  const command = tokens.join(" ");
  if (native) {
    // mode N: Claude Code owns the working folder; only the command, its description and the timeout.
    const asked = typeof params.timeout === "number" && params.timeout > 0 ? params.timeout : NATIVE_TIMEOUT_MS;
    const rewritten: Record<string, unknown> = { command, timeout: Math.min(asked, NATIVE_TIMEOUT_MS) };
    if (typeof params.description === "string") rewritten.description = params.description;
    return { kind: "allow", params: rewritten, command: parsed.command };
  }
  const rewritten: Record<string, unknown> = { command, workdir: workRoot, timeoutSeconds: EXEC_TIMEOUT_S };
  if (params.host === "gateway") rewritten.host = "gateway";
  return { kind: "allow", params: rewritten, command: parsed.command };
}

// ------------------------------------------------------------------ R3 files

function patchPaths(params: Record<string, unknown>, event: ToolEvent): string[] {
  const out = new Set<string>();
  for (const p of event.derivedPaths || []) if (typeof p === "string" && p) out.add(p);
  for (const key of ["input", "patch", "diff"]) {
    const text = params[key];
    if (typeof text !== "string") continue;
    const re = /^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$/gm;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) out.add(m[1].trim());
  }
  return [...out];
}

const FILE_PATH_KEYS = ["path", "file_path", "filePath"];
export const PATH_FORM_REASON = "path form not allowed: use an absolute path inside your workspace";
// Names a jobhunter agent may never write (as any path segment, any case): files a model or harness loads.
const WRITE_DENY_NAMES = new Set(["agents.md", "claude.md", ".mcp.json", ".claude", "skills", ".git"]);

function sameOrUnder(p: string, base: string): boolean {
  return p === trimSlash(base) || under(p, base);
}

// OpenClaw reads a tool path in ways the guard cannot follow safely (`~` and `~/x` expand to the home folder,
// a leading `@` is stripped, `node://` goes to a paired node, URIs). R3 refuses every such form outright:
// a jobhunter agent passes plain paths only, best absolute ones inside its workspace.
export function pathFormError(raw: unknown): string | null {
  if (typeof raw !== "string" || raw.length === 0) return "no file path given";
  if (raw.includes("\u0000") || raw.includes("\\") || raw.includes("$") || raw.includes("://")) return PATH_FORM_REASON;
  if (raw.startsWith("~") || raw.startsWith("@")) return PATH_FORM_REASON;
  if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(raw)) return PATH_FORM_REASON;
  if (raw.split("/").includes("..")) return PATH_FORM_REASON;
  return null;
}

// R3 for one path of a jobhunter agent: form check, the call's base folder must be the agent's own workspace,
// realpath of the longest existing ancestor, never private/ or a guard.key, reads inside the workspace,
// writes inside work/ or inbox/ (the F-QC reviewer: work/verdict/ only) and never to a reserved name.
export function resolveAgentPath(raw: unknown, mode: "read" | "write", agentId: string, ctx: ToolCtx, snap: Snapshot): { ok: true; real: string } | { ok: false; reason: string } {
  const form = pathFormError(raw);
  if (form) return { ok: false, reason: form };
  const p = raw as string;
  const role = roleOf(agentId);
  const wsRole = path.join(snap.wsRoot, role);
  const base = typeof ctx.workspaceDir === "string" && ctx.workspaceDir ? ctx.workspaceDir : typeof ctx.cwd === "string" && ctx.cwd ? ctx.cwd : wsRole;
  let realWs: string;
  let real: string;
  let privateDir: string;
  try {
    realWs = snap.realpath(wsRole);
    if (!path.isAbsolute(base) || snap.realpath(base) !== realWs) return { ok: false, reason: "workspace mismatch: this call does not run in the agent's own workspace" };
    real = snap.realpath(path.isAbsolute(p) ? path.normalize(p) : path.join(base, p));
    privateDir = snap.realpath(path.join(snap.config.repo, "private"));
  } catch {
    return { ok: false, reason: "path cannot be resolved" };
  }
  if (sameOrUnder(real, privateDir) || path.basename(real).toLowerCase() === "guard.key") {
    return { ok: false, reason: "private/ is only for the owner and the jobhunter-guard plugin" };
  }
  if (mode === "read") {
    return sameOrUnder(real, realWs) ? { ok: true, real } : { ok: false, reason: "read is limited to the agent's own workspace" };
  }
  const verdictOnly = role === "qc" && snap.config.qcVerdictFile === true;
  const roots = verdictOnly ? [path.join(wsRole, "work", "verdict")] : [path.join(wsRole, "work"), path.join(wsRole, "inbox")];
  let inside = false;
  try {
    inside = roots.some((r) => under(real, snap.realpath(r)));
  } catch {
    inside = false;
  }
  if (!inside) {
    return { ok: false, reason: verdictOnly ? "the reviewer writes only its verdict file under work/verdict/" : "writes are limited to the agent's own work/ and inbox/ folders" };
  }
  if (path.relative(realWs, real).split(path.sep).some((seg) => WRITE_DENY_NAMES.has(seg.toLowerCase()))) {
    return { ok: false, reason: "AGENTS.md, CLAUDE.md, .mcp.json, .claude, skills and .git are never written by an agent" };
  }
  return { ok: true, real };
}

function decideFile(tool: string, params: Record<string, unknown>, event: ToolEvent, agentId: string, ctx: ToolCtx, snap: Snapshot): Decision {
  let targets: unknown[];
  if (tool === "apply_patch") targets = patchPaths(params, event);
  else targets = FILE_PATH_KEYS.filter((k) => params[k] !== undefined && params[k] !== null).map((k) => params[k]);
  if (targets.length === 0) return block("G_PATH_DENIED", "no file path given");
  for (const t of targets) {
    const r = resolveAgentPath(t, tool === "read" ? "read" : "write", agentId, ctx, snap);
    if (!r.ok) return block("G_PATH_DENIED", r.reason);
  }
  return { kind: "allow" };
}

// ------------------------------------------------------------------ R4 browser

function maxClass(items: ActionItem[]): string {
  let best = "read";
  for (const it of items) if ((CLASS_RANK[it.cls] ?? 99) > (CLASS_RANK[best] ?? 0)) best = it.cls;
  return best;
}

// Fails closed: no consent state, or a consent file the runtime could not use, means no site has consent.
export function hasConsent(snap: Snapshot, site: string): boolean {
  const c = snap.consent;
  return !!c && c.ok === true && c.sites instanceof Set && c.sites.has(site);
}

function samePath(a: string, b: string): boolean {
  return path.normalize(a) === path.normalize(b);
}

function decideBrowser(params: Record<string, unknown>, agentId: string, snap: Snapshot): Decision {
  const hosts = snap.hosts!;
  // The profile is required, never filled in: a call without it would run on the owner's default profile
  // wherever a host ignored a rewrite.
  if (params.profile !== BROWSER_PROFILE) {
    return block("G_BROWSER_PROFILE", "pass profile: \"" + BROWSER_PROFILE + "\" on every browser call");
  }
  if (params.target !== undefined && params.target !== null && params.target !== "host") return block("G_BROWSER_PROFILE", "browser target must be the host browser");
  if (params.node !== undefined && params.node !== null) return block("G_BROWSER_PROFILE", "browser nodes are not allowed");
  if (params.dashboard !== undefined && params.dashboard !== null) return block("G_BROWSER_PROFILE", "browser dashboards are not allowed");

  const items = classifyBrowserCall(params, {
    hosts,
    driverHashes: snap.driverHashes,
    tokenKind: snap.token ? snap.token.kind : null,
    lookupRef: snap.lookupRef,
  });
  const action = typeof params.action === "string" ? params.action : "";
  for (const it of items) {
    if (it.cls === "forbidden") return block("G_TOOL_DENIED", "browser action " + it.action + " is not allowed (" + it.why + ")");
    if (it.cls === "script_denied") return block("G_SCRIPT_NOT_ALLOWED", "evaluate only runs the allowlisted read-only drivers from ref/drivers/");
  }
  // Secret fields (G_SECRET_FIELD) and forbidden names (social sign-in, CAPTCHA widgets: G_TOOL_DENIED), with
  // or without a token, before any host, consent, breaker or token check.
  const secret = checkSecretFields(params, hosts, snap.lookupRef, snap.secretFocus === true);
  if (secret.block) return block(secret.block.code, secret.block.reason);
  const focus = secret.focus === undefined ? {} : { secretFocus: secret.focus };

  // Host class: a navigation is judged by its target, everything else by the tab's current page.
  const current = classifyUrl(snap.currentUrl, hosts);
  let info: HostInfo = current;
  for (const it of items) {
    if (it.url !== null || action === "open" || action === "navigate") {
      const target = classifyUrl(it.url, hosts);
      if (it.url === null) return block("G_HOST_NEVER", "navigation without a URL");
      if (target.never) return block("G_HOST_NEVER", target.neverReason || "this host is never allowed", { host: target.host });
      info = target;
    }
  }
  // Cleanup: closing a tab is the only call that returns no page data, so it alone runs on a never page, on a
  // site without consent and past an open site or global breaker, so the agent can still close the tab.
  // `browser consent revoke` both removes the consent row and trips the site's breaker, so both checks must
  // let it through. Listing tabs is not cleanup: a tab list carries every tab's title (a Gmail title shows
  // the account address), so `tabs` goes through the never list, the consent fence and the breakers like a
  // read of the addressed tab. Only the owner's kill switch state/PAUSED stops a close too.
  const cleanup = action === "close";
  if (current.never && action !== "open" && action !== "navigate" && !cleanup) {
    return block("G_HOST_NEVER", current.neverReason || "the current page is on a never host", { host: current.host });
  }

  const cls = maxClass(items);
  // Consent: a site the owner has not allowed in private/consent.json is not used at all, not even read. A
  // navigation is judged by its target (`info`), every other call by the tab's page.
  const consentSite = info.platform ? info.platform.consent : null;
  if (consentSite && !cleanup && !hasConsent(snap, consentSite)) {
    const why = snap.consent && !snap.consent.ok ? " (" + snap.consent.reason + ")" : "";
    return block("G_NO_CONSENT", "the owner has not allowed the agent to use " + consentSite + ": no active consent row in private/consent.json" + why + "; end the cycle", { actionClass: cls, host: info.host });
  }
  const write = cls === "fill" || cls === "commit";
  if (snap.paused) return block("G_BREAKER_OPEN", "state/PAUSED is present: every browser action is stopped", { actionClass: cls, host: info.host });
  for (const scope of cleanup ? [] : blockingScopes(info, write)) {
    if (snap.openBreakers.has(scope)) return block("G_BREAKER_OPEN", "breaker " + scope + " is open", { actionClass: cls, host: info.host });
  }
  // A tab whose page the guard has not seen (a new session, or a tab this session did not open, navigate or
  // list) has no host class, so the never list, the consent fence and the site breakers could not judge a
  // read or an action on it. Only calls that do not touch that page run.
  if (current.url === null && !PAGELESS_ACTIONS.has(action)) {
    return block("G_PAGE_UNKNOWN", "the guard does not know which page this tab shows; open or navigate to a page first, or list tabs and pass the targetId of a listed tab", { actionClass: cls, host: null });
  }
  if (!write) return { kind: "allow", actionClass: cls, host: info.host, ...focus };

  // a job-level stop page (CAPTCHA, account wall) was seen on this site in this session
  const site = info.platform ? info.platform.key : info.host || "";
  if (site && snap.writeBlocked && snap.writeBlocked.has(site)) {
    return block("G_STOPPED", "a stop page (for example a CAPTCHA or an account wall) was seen on this site; mark the job needs_human and end the cycle", { actionClass: cls, host: info.host });
  }

  // fill and commit need this agent's live token for this host's platform
  const tok = snap.token;
  const live = tok !== null && (tok.status === "reserved" || tok.status === "armed") && tsMs(tok.expiresAt) > snap.nowMs;
  if (!tok || !live) {
    return block("G_NO_TOKEN", "fill and submit actions need a live token from jh.py gate reserve; read with snapshot and links instead", { actionClass: cls, host: info.host });
  }
  if (!tokenMatchesHost(tok.platform, info)) {
    return block("G_NO_TOKEN", "the open token is for " + tok.platform + ", not for " + (info.host || "this page"), { actionClass: cls, host: info.host });
  }
  for (const it of items) {
    if (it.uploadPaths !== undefined) {
      for (const up of it.uploadPaths) {
        if (pathFormError(up)) return block("G_PATH_DENIED", PATH_FORM_REASON, { actionClass: cls, host: info.host });
      }
      if (!snap.stagedPath || it.uploadPaths.length !== 1 || !samePath(it.uploadPaths[0], snap.stagedPath)) {
        return block("G_UPLOAD_PATH", "upload only the file staged by jh.py resume stage for this token", { actionClass: cls, host: info.host });
      }
    }
  }
  const commits = items.filter((it) => it.cls === "commit").length;
  if (commits > 0) {
    if (tok.status !== "armed") return block("G_NOT_ARMED", "run jh.py gate arm (read-back) before any submit action", { actionClass: cls, host: info.host });
    const armedMs = tsMs(tok.armedAt);
    const d = snap.dwell;
    const dwellOk =
      !Number.isNaN(armedMs) &&
      d !== null &&
      tsMs(d.acquiredAt) >= armedMs &&
      snap.nowMs >= tsMs(d.expiresAt) &&
      snap.nowMs - armedMs >= DWELL_FLOOR_MS;
    if (!dwellOk) return block("G_NOT_ARMED", "run jh.py pace wait --kind dwell after gate arm and wait until remaining_s is 0", { actionClass: cls, host: info.host });
    if (snap.commitsUsed + commits > MAX_COMMITS_PER_TOKEN) {
      return block("G_COMMIT_BUDGET", "this token already used its " + MAX_COMMITS_PER_TOKEN + " submit actions; never retry a submit", { actionClass: cls, host: info.host });
    }
  }
  const records: TokenRecord[] = items
    .filter((it) => it.cls === "fill" || it.cls === "commit")
    .map((it) => ({ class: it.cls as "fill" | "commit", action: it.action, ref: it.ref, role: it.role, name: it.name }));
  return { kind: "allow", records, token: tok.token, actionClass: cls, host: info.host, ...focus };
}

// ------------------------------------------------------------------ R7 other agents
// R7 is a best-effort filter over the text of a call. An agent that may run shell commands as the same user
// can always name a file in a way no text filter sees (variables, command substitution, encodings), so R7
// only closes the plain and the lazy spellings; docs/ENFORCEMENT.md says so.

const GLOB_CHARS = /[*?[]/;
// A glob that also matches these names is a generic pattern (for example "*" or "*.py"), not a spelling
// of guard.key or jh.py.
const GLOB_DECOYS = ["zq", "zq.x", "zq.py", "zq.key", "zq.k"];

function globRegExp(glob: string): RegExp | null {
  let out = "";
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === "*") out += "[^/]*";
    else if (c === "?") out += "[^/]";
    else if (c === "[") {
      const end = glob.indexOf("]", i + 2);
      if (end < 0) {
        out += "\\[";
        continue;
      }
      let body = glob.slice(i + 1, end);
      if (body.startsWith("!")) body = "^" + body.slice(1);
      out += "[" + body.replace(/\\/g, "\\\\") + "]";
      i = end;
    } else out += c.replace(/[.+^${}()|\\\]\/]/g, "\\$&");
  }
  try {
    return new RegExp("^" + out + "$", "i");
  } catch {
    return null;
  }
}

// Whether a command names `target` (a file name such as "guard.key"): as text once quotes and backslashes are
// removed, or through a shell glob whose last path segment matches it and is not generic, or (when `dir` is
// given) through a glob inside a folder named `dir` (for example "private/*").
export function commandNames(command: string, target: string, dir: string | null): boolean {
  const flat = command.replace(/["'\\]/g, "");
  if (flat.toLowerCase().includes(target)) return true;
  for (const word of flat.split(/[\s;&|()<>`=]+/)) {
    if (!GLOB_CHARS.test(word)) continue;
    const cut = word.lastIndexOf("/");
    const seg = word.slice(cut + 1);
    const parent = cut >= 0 ? word.slice(0, cut) : "";
    const parentSeg = parent.slice(parent.lastIndexOf("/") + 1);
    const parentRe = parentSeg ? (GLOB_CHARS.test(parentSeg) ? globRegExp(parentSeg) : null) : null;
    if (dir && parentSeg && (parentSeg.toLowerCase() === dir || (parentRe !== null && parentRe.test(dir)))) {
      if (GLOB_CHARS.test(seg) || seg === "") return true;
    }
    if (!GLOB_CHARS.test(seg)) continue;
    const re = globRegExp(seg);
    if (!re || !re.test(target)) continue;
    const literal = seg.replace(/\[[^\]]*\]|[*?]/g, "");
    if (literal.length >= 2 && !GLOB_DECOYS.some((d) => re.test(d))) return true;
  }
  return false;
}

// Whether a call names the jobhunter browser profile as the profile it drives or writes: a `profile` (the
// browser route, or a browser dashboard widget's props) or an `into` (the target of a cookie import) whose
// value is "jobhunter" once trimmed and lower-cased, as OpenClaw trims these names. Looks inside nested
// objects and arrays (a dashboard widget carries its profile in `props`).
export function namesJobhunterProfile(v: unknown, depth = 0): boolean {
  if (depth > 8 || v === null || typeof v !== "object") return false;
  if (Array.isArray(v)) return v.some((x) => namesJobhunterProfile(x, depth + 1));
  for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
    if ((k === "profile" || k === "into") && typeof x === "string" && x.trim().toLowerCase() === BROWSER_PROFILE) return true;
    if (namesJobhunterProfile(x, depth + 1)) return true;
  }
  return false;
}

// Tools another agent can use to drive or reconfigure an OpenClaw agent: refused when they name jobhunter.
const CONTROL_TOOL_RE = /^(cron|gateway|subagents?|sessions?_[a-z_]+|agents?_[a-z_]+)$/;
// OpenClaw CLI words that reach agents, jobs, sessions, config, plugins or approvals.
const OPENCLAW_CONTROL_RE = /(^|[^A-Za-z0-9_-])(cron|agents?|sessions?|config|plugins?|approvals?|exec-policy)([^A-Za-z0-9_-]|$)/i;
const OTHER_READ_TOOLS = new Set(["read", "edit", "apply_patch", "notebook_edit", "glob", "grep", "ls"]);
const OTHER_WRITE_TOOLS = new Set(["write", "edit", "apply_patch", "notebook_edit"]);
const OTHER_PATH_KEYS = ["path", "file_path", "filePath", "notebook_path"];
const MAX_SHELL_WORDS = 400;

type Roots = { read: string[]; write: string[]; privateDir: string; ownWs: string | null };

function safeReal(snap: Snapshot, p: string): string {
  try {
    return snap.realpath(p);
  } catch {
    return path.normalize(p);
  }
}

// The protected roots for agents outside jobhunter-* (plugin config protectedRoots; private/ and the repo are
// always protected, and so is WS_ROOT for writes), resolved with realpath.
function protectedRoots(ctx: ToolCtx, snap: Snapshot): Roots {
  const cfg = snap.config.protectedRoots;
  const privateDir = safeReal(snap, path.join(snap.config.repo, "private"));
  const read = new Set<string>([privateDir]);
  const write = new Set<string>([safeReal(snap, snap.config.repo)]);
  if (snap.wsRoot && path.isAbsolute(snap.wsRoot) && !snap.wsRoot.startsWith("/nonexistent")) write.add(safeReal(snap, snap.wsRoot));
  for (const r of (cfg && Array.isArray(cfg.read) ? cfg.read : [])) if (typeof r === "string" && path.isAbsolute(r)) read.add(safeReal(snap, r));
  for (const r of (cfg && Array.isArray(cfg.write) ? cfg.write : [])) if (typeof r === "string" && path.isAbsolute(r)) write.add(safeReal(snap, r));
  const ws = typeof ctx.workspaceDir === "string" && path.isAbsolute(ctx.workspaceDir) ? safeReal(snap, ctx.workspaceDir) : null;
  return { read: [...read], write: [...write], privateDir, ownWs: ws };
}

// Whether `real` lies in (or is) one of `roots`. An agent's own workspace inside a protected root (main's
// workspace inside the OpenClaw state folder) stays usable, except private/ and anything named guard.key.
function hitsRoot(real: string, roots: string[], r: Roots): boolean {
  if (sameOrUnder(real, r.privateDir) || path.basename(real).toLowerCase() === "guard.key") return true;
  for (const root of roots) {
    if (!sameOrUnder(real, root)) continue;
    if (r.ownWs && under(r.ownWs, root) && sameOrUnder(real, r.ownWs)) continue;
    return true;
  }
  return false;
}

// A tool path read the way OpenClaw reads it: a leading "@" stripped, `node://<node>/<p>` and `file://<p>` as
// the local <p>, "~" and "~/x" in the home folder, relative paths against the call's base folder.
export function resolveOtherPath(raw: string, base: string, home: string): string {
  let p = raw;
  if (p.startsWith("@")) p = p.slice(1);
  const node = /^node:\/\/[^/]*(\/.*)?$/i.exec(p);
  if (node) p = node[1] || "/";
  const file = /^file:\/\/(\/.*)$/i.exec(p);
  if (file) {
    try {
      p = decodeURIComponent(file[1]);
    } catch {
      p = file[1];
    }
  }
  if (p === "~") p = home;
  else if (p.startsWith("~/")) p = path.join(home, p.slice(2));
  return path.isAbsolute(p) ? path.normalize(p) : path.resolve(base, p);
}

// The folder part of a glob before its first pattern character ("/a/b/*.md" -> "/a/b").
export function globPrefix(pattern: string): string {
  const i = pattern.search(/[*?[{]/);
  if (i < 0) return pattern;
  const cut = pattern.lastIndexOf("/", i);
  if (cut < 0) return "";
  return cut === 0 ? "/" : pattern.slice(0, cut);
}

function otherBase(ctx: ToolCtx, snap: Snapshot): string {
  if (typeof ctx.workspaceDir === "string" && path.isAbsolute(ctx.workspaceDir)) return ctx.workspaceDir;
  if (typeof ctx.cwd === "string" && path.isAbsolute(ctx.cwd)) return ctx.cwd;
  return snap.homeDir || "/";
}

// R7 for file tools of other agents (OpenClaw's read/write/edit/apply_patch and the native Read, Write, Edit,
// Glob, Grep, LS shapes): reads under protectedRoots.read and writes under protectedRoots.write are refused.
function decideOtherFile(tool: string, params: Record<string, unknown>, event: ToolEvent, ctx: ToolCtx, snap: Snapshot): Decision | null {
  const roots = protectedRoots(ctx, snap);
  const base = otherBase(ctx, snap);
  const home = snap.homeDir || "/";
  const reads = OTHER_READ_TOOLS.has(tool);
  const writes = OTHER_WRITE_TOOLS.has(tool);
  const check = (real: string): Decision | null => {
    if (reads && hitsRoot(real, roots.read, roots)) return block("G_PATH_DENIED", "this folder is protected for the job hunter (private/ and the OpenClaw state)");
    if (writes && hitsRoot(real, roots.write, roots)) return block("G_PATH_DENIED", "only the job hunter itself writes the repo, its workspaces and the OpenClaw state");
    return null;
  };
  const raws: string[] = [];
  for (const k of OTHER_PATH_KEYS) if (typeof params[k] === "string" && params[k]) raws.push(params[k] as string);
  if (tool === "apply_patch") raws.push(...patchPaths(params, event));
  if (tool === "glob" || tool === "grep") {
    const dir = typeof params.path === "string" && params.path ? resolveOtherPath(params.path, base, home) : path.resolve(base);
    const pattern = typeof params.pattern === "string" ? params.pattern : "";
    if (tool === "glob" && pattern) {
      const pre = globPrefix(pattern.startsWith("~") || pattern.startsWith("@") ? resolveOtherPath(pattern, base, home) : pattern);
      const d = check(safeReal(snap, pre === "" ? dir : path.resolve(dir, pre)));
      if (d) return d;
    }
    const realDir = safeReal(snap, dir);
    const d = check(realDir);
    if (d) return d;
    // grep reads file contents below its folder: a protected root inside that folder is refused too
    if (tool === "grep") {
      for (const root of roots.read) {
        if (under(root, realDir) && !(roots.ownWs && sameOrUnder(realDir, roots.ownWs) && root !== roots.privateDir)) {
          return block("G_PATH_DENIED", "a search over this folder would read a protected folder; search a narrower folder");
        }
      }
    }
  }
  for (const raw of raws) {
    const d = check(safeReal(snap, resolveOtherPath(raw, base, home)));
    if (d) return d;
  }
  return null;
}

// Every word of a shell command that looks like a path (contains "/", or starts with "~" or "."), and the
// value after "=" of an option or variable, with quotes and backslashes removed.
export function shellPathWords(command: string): string[] {
  const flat = command.replace(/["'\\]/g, "");
  const out: string[] = [];
  for (const word of flat.split(/[\s;&|()<>`]+/).slice(0, MAX_SHELL_WORDS)) {
    if (!word) continue;
    const cands = [word];
    const eq = word.indexOf("=");
    if (eq >= 0) cands.push(word.slice(eq + 1));
    for (const c of cands) if (c && (c.includes("/") || c.startsWith("~") || c.startsWith("."))) out.push(c);
  }
  return out;
}

function decideOtherExecPaths(command: string, params: Record<string, unknown>, ctx: ToolCtx, snap: Snapshot): Decision | null {
  const roots = protectedRoots(ctx, snap);
  const home = snap.homeDir || "/";
  const cwd = typeof params.workdir === "string" && path.isAbsolute(params.workdir) ? params.workdir : typeof ctx.cwd === "string" && path.isAbsolute(ctx.cwd) ? ctx.cwd : otherBase(ctx, snap);
  const denied = block("G_PATH_DENIED", "this command names a folder that is protected for the job hunter (private/ or the OpenClaw state)");
  for (const w of shellPathWords(command)) {
    let p = w;
    if (p === "~") p = home;
    else if (p.startsWith("~/")) p = path.join(home, p.slice(2));
    const abs = path.isAbsolute(p) ? path.normalize(p) : path.resolve(cwd, p);
    if (GLOB_CHARS.test(abs)) {
      const pre = globPrefix(abs);
      if (pre && hitsRoot(safeReal(snap, pre), roots.read, roots)) return denied;
      let matches: string[] = [];
      try {
        matches = snap.glob(abs);
      } catch {
        matches = [];
      }
      for (const m of matches) if (hitsRoot(safeReal(snap, m), roots.read, roots)) return denied;
    } else if (hitsRoot(safeReal(snap, abs), roots.read, roots)) return denied;
  }
  return null;
}

function decideOther(tool: string, params: Record<string, unknown>, event: ToolEvent, agentId: string, ctx: ToolCtx, snap: Snapshot): Decision {
  let blob = "";
  try {
    blob = JSON.stringify(params) || "";
  } catch {
    blob = "";
  }
  if (blob.includes("guard.key")) return block("G_PATH_DENIED", "private/guard.key is only for the jobhunter-guard plugin");
  // Any tool: the browser tool (profile, or importprofile `into`, which would copy Chrome cookies into the
  // jobhunter profile) and the dashboard tool (a browser widget whose props name the profile).
  if (namesJobhunterProfile(params)) {
    return block("G_BROWSER_PROFILE", "the jobhunter browser profile is reserved for the jobhunter agents");
  }
  // Cron, session, subagent, agent and gateway tools may not start, edit, message or reconfigure a jobhunter agent.
  if (CONTROL_TOOL_RE.test(tool) && /jobhunter/i.test(blob)) {
    return block("G_OTHER_AGENT_DENIED", "only the job hunter's own runners start or change the jobhunter agents and their jobs");
  }
  if (OTHER_READ_TOOLS.has(tool) || OTHER_WRITE_TOOLS.has(tool)) {
    return decideOtherFile(tool, params, event, ctx, snap) || { kind: "pass" };
  }
  if (tool !== "exec") return { kind: "pass" };
  const command = typeof params.command === "string" ? params.command : "";
  if (commandNames(command, "guard.key", "private")) return block("G_PATH_DENIED", "private/guard.key is only for the jobhunter-guard plugin");
  if (/\bJH_(AGENT_ID|AGENT_PROOF|SESSION_KEY|RUN_ID)\b/.test(blob)) {
    return block("G_EXEC_PARAM", "JH_* variables are set only by the jobhunter-guard plugin");
  }
  if (/(^|[^A-Za-z0-9_-])--agent-p/.test(command.replace(/["'\\]/g, ""))) {
    return block("G_EXEC_PARAM", AGENT_PROOF_PREFIX + "* options are set only by the jobhunter-guard plugin");
  }
  if (/(^|[^A-Za-z0-9_-])openclaw([^A-Za-z0-9_-]|$)/i.test(command) && OPENCLAW_CONTROL_RE.test(command) && /jobhunter/i.test(command)) {
    return block("G_OTHER_AGENT_DENIED", "only the job hunter's own installer and runners change the jobhunter agents, their jobs, config or plugin");
  }
  const pathBlock = decideOtherExecPaths(command, params, ctx, snap);
  if (pathBlock) return pathBlock;
  if (!/jh\.py/i.test(command) && !commandNames(command, "jh.py", null)) return { kind: "pass" };
  if (!snap.acl) return block("G_GUARD_UNHEALTHY", "the guard cannot read acl.json; use /jh in your chat");
  if (!snap.config.publicReadonlyAgents.includes(agentId)) {
    return block("G_EXEC_ACL", "this agent may not run jh.py; use /jh in your chat");
  }
  const parsed = parsePublicExec(command, {
    python: snap.config.python,
    repo: snap.config.repo,
    publicCommands: snap.acl.public_readonly || [],
    classes: snap.acl.value_classes || {},
  });
  if (!parsed.ok) return block(parsed.code, parsed.reason + "; use /jh in your chat");
  return { kind: "pass", command: parsed.command };
}
