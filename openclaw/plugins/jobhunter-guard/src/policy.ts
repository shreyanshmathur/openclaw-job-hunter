// decide(event, ctx, snapshot): the guard's pure decision function (design 1.4, rules R0 to R4 and R7, plus
// the per-site consent fence of the Chrome-session change).
// No I/O and no clock: the runtime builds the snapshot (files, ledger, session state) and applies the
// side effects the decision asks for (token log lines, guard log).

import path from "node:path";
import { blockingScopes, classifyBrowserCall, classifyUrl, tokenMatchesHost, type ActionItem, type HostInfo } from "./browser.ts";
import { parseAgentExec, parsePublicExec, trimSlash } from "./exec_parse.ts";
import { tsMs } from "./ledger.ts";
import type { Decision, Snapshot, TokenRecord, ToolCtx, ToolEvent } from "./types.ts";

export const JOBHUNTER_PREFIX = "jobhunter-";
export const EXEC_TIMEOUT_S = 90;
export const MAX_COMMITS_PER_TOKEN = 2;
export const DWELL_FLOOR_MS = 5000; // hard minimum of browser.dwell_seconds[0]
export const BROWSER_PROFILE = "jobhunter";

const MCP_PREFIX = "mcp__openclaw__";
// After a stop the agent may only report it and end the cycle; detect and breaker trip can only stop more.
const STOPPED_COMMANDS = new Set(["cycle end", "detect", "breaker trip"]);
const FILE_TOOLS = new Set(["read", "write", "edit", "apply_patch"]);
const EXEC_KEYS = new Set(["command", "workdir", "timeoutSeconds", "yieldMs", "host", "pty", "background", "elevated", "env", "description"]);
const CLASS_RANK: Record<string, number> = { read: 0, nav_click: 1, driver: 2, fill: 3, commit: 4 };
// Browser actions that do not read or act on the addressed tab's page: they open a page (judged by its
// target), manage tabs, or manage the browser. Only these run on a tab whose page the guard does not know.
// `tabs` is here so the agent can learn which pages its tabs show; on a known page it is judged by that
// page like any read (a tab list carries titles), and only `close` skips the consent and breaker checks.
export const PAGELESS_ACTIONS = new Set(["open", "navigate", "tabs", "close", "start", "status", "profiles", "doctor"]);

export function normalizeToolName(name: unknown): string {
  const n = typeof name === "string" ? name : "";
  return n.startsWith(MCP_PREFIX) ? n.slice(MCP_PREFIX.length) : n;
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
  const tool = normalizeToolName(event.toolName);
  const params = event.params && typeof event.params === "object" ? event.params : {};
  if (!agentId.startsWith(JOBHUNTER_PREFIX)) return decideOther(tool, params, agentId, snap);

  // R0 health
  if (!snap.health.ok || !snap.acl || !snap.hosts) {
    return block("G_GUARD_UNHEALTHY", "the guard cannot verify this install (" + (snap.health.reason || "not loaded") + "); end the cycle");
  }
  const agent = snap.acl.agents[agentId];
  if (!agent) return block("G_TOOL_DENIED", "unknown jobhunter agent " + agentId);

  // R5 consequence: a stopped session may only report the stop and end its cycle (and keep notes in its
  // own work/ folder, which the agent programs do before cycle end)
  if (snap.sessionStopped) {
    if (tool === "exec") {
      const d = decideExec(params, agentId, agent.commands, snap);
      if (d.kind === "allow" && d.command && STOPPED_COMMANDS.has(d.command)) return d;
    }
    if ((tool === "write" || tool === "read") && Array.isArray(agent.tools) && agent.tools.includes(tool)) {
      return decideFile(tool, params, event, agentId, snap);
    }
    return block("G_STOPPED", "a stop signature was seen in this session; run jh.py cycle end and reply NO_REPLY");
  }

  // R1 tool allowlist
  if (!Array.isArray(agent.tools) || !agent.tools.includes(tool)) {
    return block("G_TOOL_DENIED", "tool " + (tool || "(none)") + " is not allowed for " + agentId);
  }

  if (tool === "exec") return decideExec(params, agentId, agent.commands, snap);
  if (FILE_TOOLS.has(tool)) return decideFile(tool, params, event, agentId, snap);
  if (tool === "browser") return decideBrowser(params, agentId, snap);
  return { kind: "allow" };
}

// ------------------------------------------------------------------ R2 exec

function decideExec(params: Record<string, unknown>, agentId: string, commands: Record<string, Record<string, string>>, snap: Snapshot): Decision {
  for (const k of Object.keys(params)) {
    if (!EXEC_KEYS.has(k)) return block("G_EXEC_SHAPE", "exec option " + k + " is not allowed");
  }
  if (params.pty === true) return block("G_EXEC_SHAPE", "pty is not allowed");
  if (params.background === true) return block("G_EXEC_SHAPE", "background exec is not allowed");
  if (params.elevated === true) return block("G_EXEC_SHAPE", "elevated exec is not allowed");
  if (params.env !== undefined && params.env !== null) {
    if (typeof params.env !== "object" || Object.keys(params.env as object).length > 0) return block("G_EXEC_SHAPE", "agents cannot set environment variables");
  }
  if (params.host !== undefined && params.host !== null && params.host !== "gateway") return block("G_EXEC_SHAPE", "exec host must be gateway");
  const role = roleOf(agentId);
  const wsRole = path.join(snap.wsRoot, role);
  const workRoot = path.join(wsRole, "work");
  if (params.workdir !== undefined && params.workdir !== null) {
    if (typeof params.workdir !== "string") return block("G_EXEC_SHAPE", "workdir must be a path");
    const wd = path.resolve(wsRole, params.workdir);
    if (wd !== wsRole && !under(wd, wsRole)) return block("G_EXEC_SHAPE", "workdir must be inside the agent's own workspace");
  }
  const parsed = parseAgentExec(params.command, {
    python: snap.config.python,
    repo: snap.config.repo,
    commands: commands || {},
    classes: (snap.acl && snap.acl.value_classes) || {},
    workRoots: [workRoot, path.join(wsRole, "inbox")],
  });
  if (!parsed.ok) return block(parsed.code, parsed.reason);
  const rewritten: Record<string, unknown> = { command: params.command, workdir: workRoot, timeoutSeconds: EXEC_TIMEOUT_S };
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

function decideFile(tool: string, params: Record<string, unknown>, event: ToolEvent, agentId: string, snap: Snapshot): Decision {
  const wsRole = path.join(snap.wsRoot, roleOf(agentId));
  let targets: string[];
  if (tool === "apply_patch") targets = patchPaths(params, event);
  else {
    const p = params.path ?? params.file_path ?? params.filePath;
    targets = typeof p === "string" && p.length ? [p] : [];
  }
  if (targets.length === 0) return block("G_PATH_DENIED", "no file path given");
  for (const t of targets) {
    if (t.includes("\u0000")) return block("G_PATH_DENIED", "invalid path");
    const abs = path.isAbsolute(t) ? path.normalize(t) : path.resolve(wsRole, t);
    let real: string;
    try {
      real = snap.realpath(abs);
    } catch {
      return block("G_PATH_DENIED", "path cannot be resolved");
    }
    if (tool === "read") {
      if (real !== wsRole && !under(real, wsRole)) return block("G_PATH_DENIED", "read is limited to the agent's own workspace");
    } else if (!under(real, path.join(wsRole, "work")) && !under(real, path.join(wsRole, "inbox"))) {
      return block("G_PATH_DENIED", "writes are limited to the agent's own work/ and inbox/ folders");
    }
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
  if (params.profile !== undefined && params.profile !== null && params.profile !== BROWSER_PROFILE) {
    return block("G_BROWSER_PROFILE", "browser profile must be " + BROWSER_PROFILE);
  }
  if (params.target !== undefined && params.target !== null && params.target !== "host") return block("G_BROWSER_PROFILE", "browser target must be the host browser");
  if (params.node !== undefined && params.node !== null) return block("G_BROWSER_PROFILE", "browser nodes are not allowed");
  if (params.dashboard !== undefined && params.dashboard !== null) return block("G_BROWSER_PROFILE", "browser dashboards are not allowed");
  const rewritten = params.profile === BROWSER_PROFILE ? undefined : { ...params, profile: BROWSER_PROFILE };

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
  if (!write) return { kind: "allow", params: rewritten, actionClass: cls, host: info.host };

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
  return { kind: "allow", params: rewritten, records, token: tok.token, actionClass: cls, host: info.host };
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

function decideOther(tool: string, params: Record<string, unknown>, agentId: string, snap: Snapshot): Decision {
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
  if (tool !== "exec") return { kind: "pass" };
  const command = typeof params.command === "string" ? params.command : "";
  if (commandNames(command, "guard.key", "private")) return block("G_PATH_DENIED", "private/guard.key is only for the jobhunter-guard plugin");
  if (/\bJH_(AGENT_ID|AGENT_PROOF|SESSION_KEY|RUN_ID)\b/.test(blob)) {
    return block("G_EXEC_PARAM", "JH_* variables are set only by the jobhunter-guard plugin");
  }
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
