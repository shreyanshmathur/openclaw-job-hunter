// Browser host classes, action classes and the snapshot ref cache (design 1.4 R4 and R5).
// Everything here except RefCache is a pure function.

import { createHash } from "node:crypto";
import type { HostsConfig, PlatformEntry, RefInfo } from "./types.ts";

// ------------------------------------------------------------------ guard-hosts.json

function compile(src: unknown, flags = "i"): RegExp | null {
  if (typeof src !== "string" || src.length === 0) return null;
  try {
    return new RegExp(src, flags);
  } catch {
    return null;
  }
}

// The consent site of a platform entry (a site name of private/consent.json): its "consent" value (false:
// public pages that need no login and no consent), else, failing closed for entries added without one, the
// key for LinkedIn, Gmail and every job board ("site:<name>" scope). ATS forms are public and need none.
export function consentSiteFor(key: string, scope: string, v: unknown): string | null {
  if (v === false) return null;
  if (typeof v === "string" && /^[a-z0-9_-]+$/.test(v)) return v;
  if (v !== undefined && v !== null) throw new Error("bad consent value for platform " + key);
  if (scope === "linkedin" || scope === "gmail" || scope.startsWith("site:")) return key;
  return null;
}

// Parse openclaw/guard-hosts.json. Throws on a malformed file (the runtime then marks the guard unhealthy).
export function parseHostsConfig(raw: unknown): HostsConfig {
  if (!raw || typeof raw !== "object") throw new Error("guard-hosts.json is not an object");
  const o = raw as Record<string, any>;
  const never = (o.never || {}) as Record<string, any>;
  const platforms: PlatformEntry[] = [];
  for (const [key, v] of Object.entries((o.platforms || {}) as Record<string, any>)) {
    if (!v || typeof v.scope !== "string" || !Array.isArray(v.hosts)) throw new Error("bad platform entry " + key);
    // Optional whole-host regexes for platforms whose tenants share no registrable suffix (Oracle Cloud HCM:
    // <tenant>.fa.<region>.oraclecloud.com). Each must be anchored at both ends, so it names whole hosts only.
    const hostPatterns: RegExp[] = [];
    if (v.host_patterns !== undefined) {
      if (!Array.isArray(v.host_patterns)) throw new Error("bad host_patterns for platform " + key);
      for (const hp of v.host_patterns) {
        const re = typeof hp === "string" && hp.startsWith("^") && hp.endsWith("$") ? compile(hp) : null;
        if (!re) throw new Error("bad host pattern for platform " + key + ": " + String(hp));
        hostPatterns.push(re);
      }
    }
    platforms.push({
      key,
      scope: v.scope,
      hosts: v.hosts.map((h: string) => String(h).toLowerCase()),
      aliases: Array.isArray(v.aliases) ? v.aliases.map(String) : [],
      consent: consentSiteFor(key, v.scope, v.consent),
      hostPatterns,
    });
  }
  const neverUrlPatterns: RegExp[] = [];
  for (const p of Array.isArray(never.url_patterns) ? never.url_patterns : []) {
    const re = compile(p);
    if (!re) throw new Error("bad never url pattern " + p);
    neverUrlPatterns.push(re);
  }
  const harmlessNames: RegExp[] = [];
  for (const p of Array.isArray(o.harmless_names) ? o.harmless_names : []) {
    const re = compile(p);
    if (!re) throw new Error("bad harmless name " + p);
    harmlessNames.push(re);
  }
  const riskyNames = compile(o.risky_names);
  if (!riskyNames) throw new Error("risky_names missing or invalid");
  const prepareNames: Record<string, RegExp> = {};
  for (const [kind, p] of Object.entries((o.prepare_names || {}) as Record<string, unknown>)) {
    const re = compile(p);
    if (!re) throw new Error("bad prepare_names for " + kind);
    prepareNames[kind] = re;
  }
  const multilineNames: Record<string, RegExp> = {};
  for (const [kind, p] of Object.entries((o.multiline_names || {}) as Record<string, unknown>)) {
    const re = compile(p);
    if (!re) throw new Error("bad multiline_names for " + kind);
    multilineNames[kind] = re;
  }
  // Fail closed: both keys are required (a file without them would turn the secret field and social sign-in
  // fences off silently).
  if (!Array.isArray(o.forbidden_names)) throw new Error("forbidden_names missing");
  const forbiddenNames: RegExp[] = [];
  for (const p of o.forbidden_names) {
    const re = compile(p);
    if (!re) throw new Error("bad forbidden name " + p);
    forbiddenNames.push(re);
  }
  const secretFieldNames = compile(o.secret_field_names);
  if (!secretFieldNames) throw new Error("secret_field_names missing or invalid");
  return {
    neverHosts: (Array.isArray(never.hosts) ? never.hosts : []).map((h: string) => String(h).toLowerCase()),
    neverUrlPatterns,
    allowedSchemes: Array.isArray(never.allowed_schemes) ? never.allowed_schemes.map(String) : ["http", "https"],
    allowAboutBlank: never.allow_about_blank !== false,
    blockLoopback: never.block_loopback !== false,
    blockPrivateNetworks: never.block_private_networks !== false,
    platforms,
    harmlessNames,
    riskyNames,
    prepareNames,
    multilineNames,
    forbiddenNames,
    secretFieldNames,
  };
}

// ------------------------------------------------------------------ hosts

export function domainMatch(host: string, domain: string): boolean {
  return host === domain || host.endsWith("." + domain);
}

function ipv4(host: string): number[] | null {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (!m) return null;
  const parts = m.slice(1).map(Number);
  return parts.every((n) => n >= 0 && n <= 255) ? parts : null;
}

export function isLoopbackHost(host: string): boolean {
  if (host === "localhost" || host.endsWith(".localhost") || host === "0.0.0.0") return true;
  if (host === "[::1]" || host === "::1" || host === "[::]") return true;
  const v4 = ipv4(host);
  return v4 !== null && v4[0] === 127;
}

export function isPrivateHost(host: string): boolean {
  if (host.endsWith(".local") || host.endsWith(".internal") || host.endsWith(".lan") || host.endsWith(".home.arpa")) return true;
  const v4 = ipv4(host);
  if (v4) {
    const [a, b] = v4;
    return a === 10 || a === 0 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254) || (a === 100 && b >= 64 && b <= 127);
  }
  if (host.startsWith("[")) {
    const h = host.slice(1, -1).toLowerCase();
    return h.startsWith("fc") || h.startsWith("fd") || h.startsWith("fe80") || h === "::1" || h === "::";
  }
  return false;
}

export type HostInfo = {
  url: string | null;
  host: string | null;
  never: boolean;
  neverReason: string | null;
  platform: PlatformEntry | null;
};

// Classify a URL. `null` (the tab URL is not known yet) is not "never" and has no platform.
export function classifyUrl(url: string | null | undefined, cfg: HostsConfig): HostInfo {
  if (url === null || url === undefined || url === "") return { url: null, host: null, never: false, neverReason: null, platform: null };
  const raw = String(url).trim();
  if (cfg.allowAboutBlank && raw.toLowerCase() === "about:blank") return { url: raw, host: null, never: false, neverReason: null, platform: null };
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return { url: raw, host: null, never: true, neverReason: "unparsable URL", platform: null };
  }
  const scheme = u.protocol.replace(/:$/, "").toLowerCase();
  if (!cfg.allowedSchemes.includes(scheme)) return { url: raw, host: null, never: true, neverReason: "scheme " + scheme + ": is never allowed", platform: null };
  let host = u.hostname.toLowerCase();
  if (host.endsWith(".")) host = host.slice(0, -1);
  if (u.username || u.password) return { url: raw, host, never: true, neverReason: "URLs with credentials are never allowed", platform: null };
  if (cfg.blockLoopback && isLoopbackHost(host)) return { url: raw, host, never: true, neverReason: "loopback hosts are never allowed", platform: null };
  if (cfg.blockPrivateNetworks && isPrivateHost(host)) return { url: raw, host, never: true, neverReason: "private network hosts are never allowed", platform: null };
  for (const d of cfg.neverHosts) {
    if (domainMatch(host, d)) return { url: raw, host, never: true, neverReason: host + " is on the never list", platform: null };
  }
  // Patterns see the URL as given and as the browser reads it (host lower-cased and percent-decoded,
  // backslashes turned into slashes), so a spelling of the host cannot slip past a pattern.
  for (const re of cfg.neverUrlPatterns) {
    if (re.test(raw) || re.test(u.href)) return { url: raw, host, never: true, neverReason: "this page is on the never list", platform: null };
  }
  let platform: PlatformEntry | null = null;
  for (const p of cfg.platforms) {
    if (p.hosts.some((d) => domainMatch(host, d))) {
      platform = p;
      break;
    }
  }
  // host_patterns only after every suffix host, so a listed domain always wins
  if (!platform) {
    for (const p of cfg.platforms) {
      if (p.hostPatterns && p.hostPatterns.some((re) => re.test(host))) {
        platform = p;
        break;
      }
    }
  }
  return { url: raw, host, never: false, neverReason: null, platform };
}

// Breaker scopes that block browser actions on a host (R4). Readers are blocked by the host's own
// scope; fill and commit also by pause:applications on ATS and board hosts.
export function blockingScopes(info: HostInfo, write: boolean): string[] {
  const out = ["global", "pause:all"];
  if (info.platform) {
    out.push(info.platform.scope, "pause:" + info.platform.scope);
    if (info.platform.scope !== info.platform.key) out.push("pause:" + info.platform.key);
    if (write && (info.platform.scope === "ats" || info.platform.scope.startsWith("site:"))) out.push("pause:applications");
    // the per-platform ATS breaker the core trips (ats_security, captcha_repeat, consent_revoked, code failures)
    if (info.platform.scope === "ats") out.push("ats:" + info.platform.key);
  }
  return out;
}

// A token may act on a host when its platform names that host's platform entry.
export function tokenMatchesHost(tokenPlatform: string, info: HostInfo): boolean {
  if (!info.platform) return false;
  const p = info.platform;
  return tokenPlatform === p.key || p.aliases.includes(tokenPlatform) || (p.scope.startsWith("site:") && tokenPlatform === p.scope);
}

// ------------------------------------------------------------------ action classes

export type ActionClass = "read" | "nav_click" | "driver" | "fill" | "commit" | "script_denied" | "forbidden";

export type ActionItem = {
  cls: ActionClass;
  action: string; // click | type | fill | select | upload | press | navigate | evaluate | ...
  ref: string | null;
  role: string | null;
  name: string | null;
  url: string | null; // navigation target, when the item navigates
  uploadPaths?: string[];
  why: string;
};

const READ_ACTIONS = new Set([
  "doctor", "status", "start", "stop", "profiles", "tabs", "open", "focus", "close", "snapshot", "screenshot",
  "navigate", "console", "requests", "errors", "text", "emulate", "pdf", "waitfordownload",
]);
const READ_ACT_KINDS = new Set(["hover", "scrollIntoView", "resize", "close"]);
const FILL_ROLES = new Set(["checkbox", "radio", "combobox", "option", "textbox", "tab", "searchbox", "spinbutton"]);
const READ_KEYS = new Set(["pagedown", "pageup", "arrowdown", "arrowup", "arrowleft", "arrowright", "home", "end", "escape", "esc"]);
// Keys that submit a form or activate the focused button: Enter (OpenClaw maps "return" to it, Playwright
// gives NumpadEnter the same key and maps "\n" and "\r" to Enter) and Space (" " and "Space").
const SUBMIT_KEYS = new Set(["enter", "return", "numpadenter", "space", "spacebar"]);

// The act request keys OpenClaw copies from the top level of a browser call into `request` when the request
// lacks them (LEGACY_BROWSER_ACT_REQUEST_KEYS and readActRequestParam in the browser plugin).
export const ACT_REQUEST_KEYS = [
  "kind", "actions", "stopOnError", "targetId", "ref", "doubleClick", "button", "modifiers", "x", "y", "text",
  "submit", "slowly", "key", "delayMs", "startRef", "endRef", "values", "fields", "width", "height", "timeMs",
  "textGone", "selector", "url", "loadState", "fn", "timeoutMs",
];
// Snake case spellings OpenClaw also reads for some keys (readToolStringParam). The guard reads only the
// camel case keys, so a call that carries one of these is refused rather than judged on another value.
const SNAKE_KEYS = ["target_id", "target_url", "input_ref"];

export function sha256Hex(s: string): string {
  return createHash("sha256").update(s, "utf8").digest("hex");
}

// drivers/manifest.json holds the sha256 of each driver's file text with surrounding whitespace removed
// (tools/gen_drivers.py); the same strip is applied to the evaluate argument.
export function scriptAllowed(fn: unknown, hashes: Set<string>): boolean {
  if (typeof fn !== "string" || fn.length === 0) return false;
  return hashes.has(sha256Hex(fn)) || hashes.has(sha256Hex(fn.trim()));
}

function str(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function has(o: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(o, key);
}

// A boolean option as the guard reads it: anything but absent, null or false counts as set (OpenClaw turns
// "true", "yes", 1 and, for dialog accept, any truthy value into true).
function flagOn(v: unknown): boolean {
  return v !== undefined && v !== null && v !== false;
}

// The act request OpenClaw runs for a browser `act` call (readActRequestParam): `request` with the missing
// act keys filled in from the top level (only targetId when the two name different kinds), or, without a
// `request` object, the act keys of the top level. null when OpenClaw would refuse the call for want of one.
export function actRequestOf(params: Record<string, unknown>): Record<string, unknown> | null {
  const rp = params.request;
  if (rp && typeof rp === "object") {
    const req: Record<string, unknown> = { ...(rp as Record<string, unknown>) };
    const mismatched = typeof req.kind === "string" && typeof params.kind === "string" && req.kind !== params.kind;
    for (const key of ACT_REQUEST_KEYS) {
      if (has(req, key) || !has(params, key)) continue;
      if (mismatched && key !== "targetId") continue;
      req[key] = params[key];
    }
    return req;
  }
  if (typeof params.kind !== "string" || params.kind.trim() === "") return null;
  const req: Record<string, unknown> = {};
  for (const key of ACT_REQUEST_KEYS) if (has(params, key)) req[key] = params[key];
  return req;
}

export type ClassifyOpts = {
  hosts: HostsConfig;
  driverHashes: Set<string>;
  tokenKind: string | null;
  lookupRef: (ref: string) => RefInfo | undefined;
};

function classifyClick(ref: string | null, action: string, opts: ClassifyOpts): ActionItem {
  if (!ref) return { cls: "commit", action, ref: null, role: null, name: null, url: null, why: "click without a snapshot ref" };
  const info = opts.lookupRef(ref);
  if (!info) return { cls: "commit", action, ref, role: null, name: null, url: null, why: "ref not in the last snapshot" };
  const role = info.role.toLowerCase();
  const name = (info.name || "").trim();
  const base = { action, ref, role, name, url: null };
  const prep = opts.tokenKind ? opts.hosts.prepareNames[opts.tokenKind] : undefined;
  if (role === "link") {
    if (prep && prep.test(name)) return { ...base, cls: "fill", why: "preparatory link for " + opts.tokenKind };
    if (opts.hosts.riskyNames.test(name)) return { ...base, cls: "commit", why: "link with an action name" };
    return { ...base, cls: "nav_click", why: "link" };
  }
  if (FILL_ROLES.has(role)) return { ...base, cls: "fill", why: "form control" };
  if (role === "button") {
    if (prep && prep.test(name)) return { ...base, cls: "fill", why: "preparatory button for " + opts.tokenKind };
    if (opts.hosts.harmlessNames.some((re) => re.test(name)) && !opts.hosts.riskyNames.test(name)) {
      return { ...base, cls: "nav_click", why: "harmless button" };
    }
  }
  return { ...base, cls: "commit", why: role + " click" };
}

// A newline typed key by key is an Enter press. It only inserts a line break in a textbox whose name the
// open token's kind lists in guard-hosts.json multiline_names (for example the Gmail message body).
function multilineOk(info: RefInfo | undefined, opts: ClassifyOpts): boolean {
  if (!info || info.role.toLowerCase() !== "textbox" || !opts.tokenKind) return false;
  const re = opts.hosts.multilineNames[opts.tokenKind];
  return !!re && re.test((info.name || "").trim());
}

function classifyType(req: Record<string, unknown>, opts: ClassifyOpts): ActionItem {
  const ref = str(req.ref);
  const info = ref ? opts.lookupRef(ref) : undefined;
  const base = { action: "type", ref, role: info?.role ?? null, name: info?.name ?? null, url: null };
  if (flagOn(req.submit)) return { ...base, cls: "commit", why: "type with submit" };
  if (flagOn(req.slowly)) {
    // OpenClaw clicks the element before it types slowly (a ref wins over a selector).
    if (!ref) return { ...base, cls: "commit", why: "type slowly clicks an element without a snapshot ref" };
    const click = classifyClick(ref, "type", opts);
    if (click.cls === "commit") return { ...click, why: "type slowly clicks: " + click.why };
    const text = typeof req.text === "string" ? req.text : "";
    if (/[\r\n]/.test(text) && !multilineOk(info, opts)) return { ...base, cls: "commit", why: "a line break typed slowly presses Enter" };
  }
  return { ...base, cls: "fill", why: "type" };
}

function classifyPress(req: Record<string, unknown>): ActionItem {
  const none = { action: "press", ref: null, role: null, name: null, url: null };
  const raw = typeof req.key === "string" || typeof req.key === "number" ? String(req.key) : "";
  // any whitespace: " " is Space, "\n" and "\r" are Enter, and padded chords are not plain keys
  if (/\s/.test(raw)) return { ...none, cls: "commit", why: "Space, Enter or a padded key" };
  const low = raw.toLowerCase();
  if (low.split("+").some((part) => SUBMIT_KEYS.has(part))) return { ...none, cls: "commit", why: "Enter or Space key" };
  if (READ_KEYS.has(low)) return { ...none, cls: "read", why: "scroll key" };
  return { ...none, cls: "fill", why: "key press" };
}

function classifyAct(req: Record<string, unknown>, opts: ClassifyOpts, depth: number): ActionItem[] {
  const kind = typeof req.kind === "string" ? req.kind : "";
  const ref = str(req.ref);
  const none = { ref: null, role: null, name: null, url: null };
  switch (kind) {
    case "click":
      return [classifyClick(ref, "click", opts)];
    case "clickCoords":
      return [{ cls: "commit", action: "click", ...none, why: "coordinate click" }];
    case "type":
      return [classifyType(req, opts)];
    case "press":
      return [classifyPress(req)];
    case "select":
    case "fill": {
      const info = ref ? opts.lookupRef(ref) : undefined;
      return [{ cls: "fill", action: kind, ref, role: info?.role ?? null, name: info?.name ?? null, url: null, why: kind }];
    }
    case "drag":
      return [{ cls: "commit", action: "drag", ...none, why: "drag" }];
    case "wait":
      if (req.fn !== undefined && req.fn !== null && req.fn !== "") {
        return [scriptAllowed(req.fn, opts.driverHashes)
          ? { cls: "driver", action: "wait", ...none, why: "allowlisted wait function" }
          : { cls: "script_denied", action: "wait", ...none, why: "wait function is not an allowlisted driver" }];
      }
      return [{ cls: "read", action: "wait", ...none, why: "wait" }];
    case "evaluate":
      return [scriptAllowed(req.fn, opts.driverHashes)
        ? { cls: "driver", action: "evaluate", ...none, why: "allowlisted driver" }
        : { cls: "script_denied", action: "evaluate", ...none, why: "script is not an allowlisted driver" }];
    case "batch": {
      if (depth > 2 || !Array.isArray(req.actions)) return [{ cls: "commit", action: "batch", ...none, why: "malformed batch" }];
      const out: ActionItem[] = [];
      for (const a of req.actions) {
        if (!a || typeof a !== "object") return [{ cls: "commit", action: "batch", ...none, why: "malformed batch item" }];
        out.push(...classifyAct(a as Record<string, unknown>, opts, depth + 1));
      }
      return out.length ? out : [{ cls: "read", action: "batch", ...none, why: "empty batch" }];
    }
    default:
      if (READ_ACT_KINDS.has(kind)) return [{ cls: "read", action: kind, ...none, why: kind }];
      return [{ cls: "commit", action: kind || "act", ...none, why: "unknown act kind" }];
  }
}

// Classify one browser tool call into one or more items (a batch yields several).
export function classifyBrowserCall(params: Record<string, unknown>, opts: ClassifyOpts): ActionItem[] {
  const action = typeof params.action === "string" ? params.action : "";
  const none = { ref: null, role: null, name: null, url: null };
  for (const k of SNAKE_KEYS) {
    if (has(params, k)) return [{ cls: "forbidden", action: action || "unknown", ...none, why: "use camelCase keys, not " + k }];
  }
  if (action === "open" || action === "navigate") {
    const target = str(params.targetUrl) ?? str(params.url);
    return [{ cls: "read", action, ref: null, role: null, name: null, url: target, why: action }];
  }
  if (READ_ACTIONS.has(action)) return [{ cls: "read", action, ...none, why: action }];
  if (action === "importprofile") return [{ cls: "forbidden", action, ...none, why: "cookie import is a human step" }];
  if (action === "upload") {
    const paths = Array.isArray(params.paths) ? params.paths.map((p) => String(p)) : [];
    const ref = str(params.ref);
    if (ref) {
      // OpenClaw clicks `ref` to open the file chooser: judge that click, at least as a fill.
      const click = classifyClick(ref, "upload", opts);
      const commit = click.cls === "commit";
      return [{ ...click, cls: commit ? "commit" : "fill", uploadPaths: paths, why: commit ? "upload clicks: " + click.why : "upload" }];
    }
    const inputRef = str(params.inputRef);
    const info = inputRef ? opts.lookupRef(inputRef) : undefined;
    return [{ cls: "fill", action: "upload", ref: inputRef, role: info?.role ?? null, name: info?.name ?? null, url: null, uploadPaths: paths, why: "upload" }];
  }
  if (action === "dialog") {
    if (flagOn(params.accept)) return [{ cls: "commit", action: "dialog", ...none, why: "dialog accept" }];
    return [{ cls: "nav_click", action: "dialog", ...none, why: "dialog dismiss" }];
  }
  if (action === "download") return [classifyClick(str(params.ref), "download", opts)];
  if (action === "act") {
    const inner = params.request && typeof params.request === "object" ? tabIdValue((params.request as Record<string, unknown>).targetId) : null;
    const top = tabIdValue(params.targetId);
    if (inner && top && inner !== top) {
      return [{ cls: "forbidden", action: "act", ...none, why: "the call and its request name different tabs" }];
    }
    const req = actRequestOf(params);
    if (!req) return [{ cls: "commit", action: "act", ...none, why: "act without a kind" }];
    return classifyAct(req, opts, 0);
  }
  return [{ cls: "commit", action: action || "unknown", ...none, why: "unknown browser action" }];
}

// ------------------------------------------------------------------ secret fields and forbidden names
// Passwords and verification codes are filled by code (jh.py account create, account signin, code submit over
// the agent's own browser profile), never by a model; social sign-in buttons and CAPTCHA widgets are never
// clicked. These checks look only at the refs of the last snapshot of the addressed tab and at the order of
// the actions in one call (a batch), and hold with or without a token.

export const SECRET_FIELD_REASON = "secret fields are filled by code: jh.py account create, account signin or code submit";
export const FORBIDDEN_NAME_REASON = "social sign-in buttons and CAPTCHA widgets are never clicked by an agent: the owner signs in or solves the CAPTCHA";
const SECRET_ROLES = new Set(["textbox", "searchbox", "spinbutton"]);
// a CSS selector that names a password input or a secret field by its id, name or label
const SECRET_SELECTOR_RE = /type\s*=\s*["']?password/i;

export function isSecretField(info: RefInfo | undefined, hosts: HostsConfig): boolean {
  if (!info || !SECRET_ROLES.has(String(info.role).toLowerCase())) return false;
  const re = hosts.secretFieldNames;
  re.lastIndex = 0;
  return re.test((info.name || "").trim());
}

export function isForbiddenName(info: RefInfo | undefined, hosts: HostsConfig): boolean {
  if (!info) return false;
  const name = (info.name || "").trim();
  return hosts.forbiddenNames.some((re) => {
    re.lastIndex = 0;
    return re.test(name);
  });
}

function secretSelector(sel: unknown, hosts: HostsConfig): boolean {
  if (typeof sel !== "string" || sel.length === 0) return false;
  hosts.secretFieldNames.lastIndex = 0;
  return SECRET_SELECTOR_RE.test(sel) || hosts.secretFieldNames.test(sel);
}

// The outcome of the secret field and forbidden name checks of one browser call: a block (code and reason),
// and the addressed tab's secretFocus flag after the call (undefined: the call does not change it).
export type SecretCheck = { block: { code: string; reason: string } | null; focus: boolean | undefined };

// `focus`: the tab's secretFocus flag before the call (a click on a secret field set it; the next snapshot,
// navigation or click on another known ref clears it). A type or press without a ref while it is set types
// into that field, so it is refused like a type into the field itself. A click on an unknown ref keeps it.
export function checkSecretFields(params: Record<string, unknown>, hosts: HostsConfig, lookupRef: (ref: string) => RefInfo | undefined, focus: boolean): SecretCheck {
  const action = typeof params.action === "string" ? params.action : "";
  let cur = focus;
  let touched = false;
  const secret = (): SecretCheck => ({ block: { code: "G_SECRET_FIELD", reason: SECRET_FIELD_REASON }, focus: undefined });
  const denied = (): SecretCheck => ({ block: { code: "G_TOOL_DENIED", reason: FORBIDDEN_NAME_REASON }, focus: undefined });
  // a click on a ref: refused on a forbidden name; moves the focus when the ref is known
  const click = (ref: unknown): SecretCheck | null => {
    const r = str(ref);
    if (!r) return null;
    const info = lookupRef(r);
    if (!info) return null;
    if (isForbiddenName(info, hosts)) return denied();
    cur = isSecretField(info, hosts);
    touched = true;
    return null;
  };
  // typing (type, press, one fill field): into a known secret ref, by a secret selector, or ref-less while
  // the focus is in a secret field
  const typing = (ref: unknown, selector: unknown): boolean => {
    const r = str(ref);
    if (r) return isSecretField(lookupRef(r), hosts);
    if (secretSelector(selector, hosts)) return true;
    return cur;
  };
  const walk = (req: Record<string, unknown>, depth: number): SecretCheck | null => {
    const kind = typeof req.kind === "string" ? req.kind : "";
    switch (kind) {
      case "click":
        return click(req.ref);
      case "type":
        if (typing(req.ref, req.selector)) return secret();
        // type slowly clicks the element first
        if (flagOn(req.slowly)) return click(req.ref);
        return null;
      case "press":
        return typing(req.ref, req.selector) ? secret() : null;
      case "fill": {
        if (Array.isArray(req.fields)) {
          for (const f of req.fields) {
            const fo = f && typeof f === "object" ? (f as Record<string, unknown>) : {};
            if (typing(fo.ref, fo.selector)) return secret();
          }
          return null;
        }
        return typing(req.ref, req.selector) ? secret() : null;
      }
      case "batch":
        if (depth > 2 || !Array.isArray(req.actions)) return null; // classifyAct refuses it as a commit
        for (const a of req.actions) {
          if (!a || typeof a !== "object") return null;
          const r = walk(a as Record<string, unknown>, depth + 1);
          if (r) return r;
        }
        return null;
      default:
        return null;
    }
  };
  let res: SecretCheck | null = null;
  // `open` makes a new tab (which starts unmarked) and leaves the marked one as it is
  if (action === "snapshot" || action === "navigate") {
    cur = false;
    touched = true;
  } else if (action === "upload" || action === "download") {
    res = click(params.ref); // OpenClaw clicks `ref` (an upload's file chooser, a download link)
  } else if (action === "act") {
    const req = actRequestOf(params);
    if (req) res = walk(req, 0);
  }
  if (res) return res;
  return { block: null, focus: touched ? cur : undefined };
}

// ------------------------------------------------------------------ snapshot refs

const ROLE_LINE_RE = /^[ \t]*-[ \t]+([A-Za-z][\w-]*)(?:[ \t]+("(?:[^"\\\n]|\\.)*"))?[^\n]*?\[ref=([A-Za-z0-9_]+)\]/gm;

function decodeName(token: string | undefined): string {
  if (!token) return "";
  try {
    const v = JSON.parse(token);
    return typeof v === "string" ? v : "";
  } catch {
    return token.slice(1, -1);
  }
}

// Extract ref -> {role, name} from snapshot text: role and AI snapshots ("- button \"Send\" [ref=e12]")
// and, when present, a JSON payload with nodes carrying {ref, role, name} (aria format).
export function parseSnapshotRefs(text: string): Map<string, RefInfo> {
  const out = new Map<string, RefInfo>();
  if (typeof text !== "string" || text.length === 0) return out;
  ROLE_LINE_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = ROLE_LINE_RE.exec(text)) !== null) {
    out.set(m[3], { role: m[1].toLowerCase(), name: decodeName(m[2]) });
  }
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start >= 0 && end > start && text.includes("\"ref\"")) {
    try {
      walkJson(JSON.parse(text.slice(start, end + 1)), out, 0);
    } catch {
      // not JSON; the line parser above is authoritative
    }
  }
  return out;
}

function walkJson(v: unknown, out: Map<string, RefInfo>, depth: number): void {
  if (depth > 60 || v === null || typeof v !== "object") return;
  if (Array.isArray(v)) {
    for (const x of v) walkJson(x, out, depth + 1);
    return;
  }
  const o = v as Record<string, unknown>;
  if (typeof o.ref === "string" && typeof o.role === "string") {
    out.set(o.ref, { role: o.role.toLowerCase(), name: typeof o.name === "string" ? o.name : "" });
  }
  for (const x of Object.values(o)) walkJson(x, out, depth + 1);
}

type TabState = { refs: Map<string, RefInfo>; url: string | null; secretFocus: boolean };
type SessionTabs = { tabs: Map<string, TabState>; lastTab: string; aliases: Map<string, string> };

const MAX_ALIASES = 500;

// Per session and tab: the refs of the last snapshot and the last known URL. OpenClaw accepts several
// handles for one tab (its raw targetId, a tab id such as "t1", a label, the suggested id); the handles its
// results report for a tab are kept as aliases of that tab's targetId, so each handle finds the same state.
export class RefCache {
  private sessions = new Map<string, SessionTabs>();

  private session(key: string): SessionTabs {
    let s = this.sessions.get(key);
    if (!s) {
      s = { tabs: new Map(), lastTab: "_", aliases: new Map() };
      this.sessions.set(key, s);
      if (this.sessions.size > 200) {
        const first = this.sessions.keys().next().value;
        if (first !== undefined && first !== key) this.sessions.delete(first);
      }
    }
    return s;
  }

  private resolve(s: SessionTabs, tabId: string | null): string {
    const id = tabId || s.lastTab || "_";
    return s.aliases.get(id) ?? id;
  }

  private tab(key: string, tabId: string | null, touch = true): TabState {
    const s = this.session(key);
    const id = this.resolve(s, tabId);
    let t = s.tabs.get(id);
    if (!t) {
      t = { refs: new Map(), url: null, secretFocus: false };
      s.tabs.set(id, t);
    }
    if (touch) s.lastTab = id;
    return t;
  }

  // `alias` names the tab whose targetId is `targetId` (from a browser result, never from the agent alone).
  addAlias(key: string, alias: string | null, targetId: string | null): void {
    if (!alias || !targetId || alias === targetId) return;
    const s = this.session(key);
    if (s.aliases.size >= MAX_ALIASES && !s.aliases.has(alias)) return;
    s.aliases.set(alias, targetId);
  }

  setUrl(key: string, tabId: string | null, url: string | null): void {
    if (!url) return;
    this.tab(key, tabId).url = url;
  }

  // The URL of a tab listed by a `tabs` result: known from now on, without making it the last used tab.
  learnUrl(key: string, tabId: string, url: string | null): void {
    if (!url || !tabId) return;
    this.tab(key, tabId, false).url = url;
  }

  replaceRefs(key: string, tabId: string | null, refs: Map<string, RefInfo>): void {
    this.tab(key, tabId).refs = refs;
  }

  clearRefs(key: string, tabId: string | null): void {
    this.tab(key, tabId).refs = new Map();
  }

  // The tab's secretFocus flag (checkSecretFields); set without making the tab the last used one.
  setSecretFocus(key: string, tabId: string | null, on: boolean): void {
    this.tab(key, tabId, false).secretFocus = on;
  }

  secretFocus(key: string, tabId: string | null): boolean {
    const s = this.sessions.get(key);
    if (!s) return false;
    const t = s.tabs.get(this.resolve(s, tabId));
    return t ? t.secretFocus === true : false;
  }

  // The tab key a handle names (a tab id, label or suggested id resolved to the raw targetId; no handle: the
  // tab this session used last), or null when the session has used no tab.
  tabKey(key: string, tabId: string | null): string | null {
    const s = this.sessions.get(key);
    if (!s) return tabId;
    const id = this.resolve(s, tabId);
    return id === "_" ? null : id;
  }

  lookup(key: string, tabId: string | null, ref: string): RefInfo | undefined {
    const s = this.sessions.get(key);
    if (!s) return undefined;
    const t = s.tabs.get(this.resolve(s, tabId));
    return t ? t.refs.get(ref) : undefined;
  }

  url(key: string, tabId: string | null): string | null {
    const s = this.sessions.get(key);
    if (!s) return null;
    const t = s.tabs.get(this.resolve(s, tabId));
    return t ? t.url : null;
  }

  dropSession(key: string): void {
    this.sessions.delete(key);
  }
}

export type TabHandles = { targetId: string; aliases: string[]; url: string | null };

function tabHandlesOf(o: unknown): TabHandles | null {
  if (!o || typeof o !== "object" || Array.isArray(o)) return null;
  const r = o as Record<string, unknown>;
  const targetId = tabIdValue(r.targetId);
  if (!targetId || typeof r.targetId !== "string") return null;
  const aliases: string[] = [];
  for (const k of ["tabId", "label", "suggestedTargetId"]) {
    const v = typeof r[k] === "string" ? tabIdValue(r[k]) : null;
    if (v && v !== targetId && !aliases.includes(v)) aliases.push(v);
  }
  return { targetId, aliases, url: typeof r.url === "string" && r.url.length > 0 ? r.url : null };
}

// The tab a browser result describes (an `open` result names its targetId with its tab id, label and
// suggested id; other results name the targetId), from `details` first, then the top level.
export function resultTab(result: unknown): TabHandles | null {
  if (!result || typeof result !== "object") return null;
  const o = result as Record<string, unknown>;
  return tabHandlesOf(o.details) ?? tabHandlesOf(o);
}

// The tabs a `tabs` result lists (`details.tabs`, each with its targetId, handles and URL).
export function resultTabList(result: unknown): TabHandles[] {
  if (!result || typeof result !== "object") return [];
  const o = result as Record<string, unknown>;
  const d = o.details && typeof o.details === "object" ? (o.details as Record<string, unknown>) : o;
  if (!Array.isArray(d.tabs)) return [];
  const out: TabHandles[] = [];
  for (const t of d.tabs.slice(0, 200)) {
    const h = tabHandlesOf(t);
    if (h) out.push(h);
  }
  return out;
}

// A targetId as OpenClaw reads it (a string, number or boolean, trimmed), or null.
function tabIdValue(v: unknown): string | null {
  if (typeof v !== "string" && typeof v !== "number" && typeof v !== "boolean") return null;
  const t = String(v).trim();
  return t.length > 0 ? t : null;
}

// The tab a browser call addresses, as OpenClaw picks it: for `act` the targetId of the act request it runs
// (request.targetId first, then the top level), for every other action the top-level targetId.
export function targetIdOf(params: Record<string, unknown>): string | null {
  if (params.action === "act") {
    const req = actRequestOf(params);
    return req ? tabIdValue(req.targetId) : null;
  }
  return tabIdValue(params.targetId);
}
