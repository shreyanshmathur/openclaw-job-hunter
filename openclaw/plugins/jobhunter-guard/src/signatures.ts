// Stop signatures from scripts/jobhunter/detect/*.json (design 1.4 R5 and 4.5), and the matcher the
// after_tool_call observer runs on every browser result.
//
// The detect files are owned by U1. This loader accepts these shapes (anything else in a file is ignored):
//   {"platform": "linkedin", "hosts": ["linkedin.com"],
//    "signatures": [{"id": "li_checkpoint", "verdict": "stop", "trip": true, "url": "...", "title": "...",
//                    "text": "...", "http_status": [999]}]}
//   {"platform": "linkedin", "stop": {"url": ["..."], "text": ["..."]}}
//   [ {...signature...}, ... ]                       (platform taken from the file name)
// Per signature, url may also be spelled url_regex|url_patterns|urls, title title_regex, and text
// text_regex|text_patterns|texts|patterns|pattern|regex; the id may be spelled code or name. Entries whose
// verdict is not "stop" (for example "after_click_error" or "unknown_state") are not used by the guard.
// "trip": false marks a job-level stop (for example a CAPTCHA on one ATS form): the guard reports it and
// stops write actions on that site for the session instead of stopping the whole session. A leading "(?i)"
// is accepted (Python style); matching is case-insensitive unless "flags" says otherwise.
// When a page matches several signatures the most severe one wins, as in `jh.py detect` (detect.match): a
// tripping stop before a job-level stop, then the SEVERITY order of its reason_code, then load order.

import fs from "node:fs";
import path from "node:path";

export type Signature = {
  file: string;
  platform: string; // the file's platform ("linkedin", "gmail", "boards", "ats", ...)
  code: string;
  urlRes: RegExp[];
  titleRes: RegExp[];
  textRes: RegExp[];
  httpStatus: number[];
  trip: boolean;
  reasonCode: string | null; // the signature's reason_code (the breaker reason), used for SEVERITY
};

export type SignatureSet = { signatures: Signature[]; hostsByPlatform: Map<string, string[]>; warnings: string[] };

function toList(v: unknown): string[] {
  if (typeof v === "string") return v.length ? [v] : [];
  if (Array.isArray(v)) return v.filter((x) => typeof x === "string" && x.length > 0) as string[];
  return [];
}

function compilePy(src: string, flags: string, warnings: string[], where: string): RegExp | null {
  let s = src;
  let f = flags;
  const m = /^\(\?([aiLmsux]+)\)/.exec(s);
  if (m) {
    s = s.slice(m[0].length);
    if (m[1].includes("i") && !f.includes("i")) f += "i";
    if (m[1].includes("s") && !f.includes("s")) f += "s";
    if (m[1].includes("m") && !f.includes("m")) f += "m";
  }
  try {
    return new RegExp(s, f);
  } catch (e) {
    warnings.push(where + ": invalid regex " + JSON.stringify(src));
    return null;
  }
}

function pick(o: Record<string, unknown>, names: string[]): string[] {
  const out: string[] = [];
  for (const n of names) out.push(...toList(o[n]));
  return out;
}

function sigFrom(o: Record<string, unknown>, file: string, platform: string, idx: number, warnings: string[]): Signature | null {
  const verdict = typeof o.verdict === "string" ? o.verdict : typeof o.kind === "string" ? o.kind : "stop";
  if (verdict !== "stop") return null;
  const flags = typeof o.flags === "string" ? o.flags.replace(/[^imsu]/g, "") : "i";
  const where = file + "#" + idx;
  const urlRes = pick(o, ["url_regex", "url", "url_patterns", "urls"]).map((s) => compilePy(s, flags, warnings, where)).filter(Boolean) as RegExp[];
  const textRes = pick(o, ["text_regex", "text", "text_patterns", "texts", "patterns", "pattern", "regex"])
    .map((s) => compilePy(s, flags, warnings, where))
    .filter(Boolean) as RegExp[];
  const titleRes = pick(o, ["title_regex", "title"]).map((s) => compilePy(s, flags, warnings, where)).filter(Boolean) as RegExp[];
  const statusRaw = Array.isArray(o.http_status) ? o.http_status : typeof o.http_status === "number" ? [o.http_status] : [];
  const httpStatus = statusRaw.filter((n) => typeof n === "number") as number[];
  if (urlRes.length === 0 && textRes.length === 0 && titleRes.length === 0 && httpStatus.length === 0) return null;
  const code = typeof o.code === "string" ? o.code : typeof o.id === "string" ? o.id : typeof o.name === "string" ? o.name : platform + "_" + idx;
  const sigPlatform = typeof o.platform === "string" ? o.platform : platform;
  const reasonCode = typeof o.reason_code === "string" && o.reason_code.length ? o.reason_code : null;
  return { file, platform: sigPlatform, code, urlRes, titleRes, textRes, httpStatus, trip: o.trip !== false, reasonCode };
}

// Parse one detect file's JSON (exported for tests).
export function parseDetectJson(raw: unknown, file: string, warnings: string[]): { signatures: Signature[]; platform: string; hosts: string[] } {
  const base = path.basename(file, ".json");
  const sigs: Signature[] = [];
  let platform = base;
  let hosts: string[] = [];
  let entries: unknown[] = [];
  if (Array.isArray(raw)) {
    entries = raw;
  } else if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    if (typeof o.platform === "string") platform = o.platform;
    hosts = toList(o.hosts).map((h) => h.toLowerCase());
    for (const key of ["signatures", "rules", "patterns", "items"]) {
      if (Array.isArray(o[key])) entries = entries.concat(o[key] as unknown[]);
    }
    if (o.stop && typeof o.stop === "object" && !Array.isArray(o.stop)) {
      entries.push({ code: platform + "_stop", ...(o.stop as Record<string, unknown>) });
    } else if (Array.isArray(o.stop)) {
      entries = entries.concat(o.stop as unknown[]);
    }
  }
  entries.forEach((e, i) => {
    if (e && typeof e === "object" && !Array.isArray(e)) {
      const s = sigFrom(e as Record<string, unknown>, file, platform, i, warnings);
      if (s) sigs.push(s);
    }
  });
  return { signatures: sigs, platform, hosts };
}

// Load every detect/*.json. Throws when the folder cannot be read or no browser signature was found
// (the guard is then unhealthy: it will not run agents without its own stop scan).
export function loadSignatures(dir: string): SignatureSet {
  const warnings: string[] = [];
  const names = fs.readdirSync(dir).filter((n) => n.endsWith(".json")).sort();
  const signatures: Signature[] = [];
  const hostsByPlatform = new Map<string, string[]>();
  for (const n of names) {
    const file = path.join(dir, n);
    let raw: unknown;
    try {
      raw = JSON.parse(fs.readFileSync(file, "utf8"));
    } catch (e) {
      throw new Error("detect file " + n + " is not valid JSON");
    }
    const parsed = parseDetectJson(raw, n, warnings);
    signatures.push(...parsed.signatures);
    if (parsed.hosts.length) hostsByPlatform.set(parsed.platform, parsed.hosts);
  }
  if (!signatures.some((s) => s.urlRes.length > 0 || s.textRes.length > 0 || s.titleRes.length > 0)) {
    throw new Error("no browser stop signatures found in " + dir);
  }
  return { signatures, hostsByPlatform, warnings };
}

const GENERIC = new Set(["any", "all", "*", "generic", "common"]);
const BOARD_FILES = new Set(["boards", "board", "sites", "site"]);

// Which signatures apply to a page on a host whose guard-hosts platform scope is `scope` (null for hosts
// that are in no platform set) and platform key is `key`.
export function applicable(sig: Signature, scope: string | null, key: string | null, host: string | null, set: SignatureSet): boolean {
  const p = sig.platform;
  if (GENERIC.has(p)) return true;
  if (host) {
    const hs = set.hostsByPlatform.get(p);
    if (hs && hs.some((d) => host === d || host.endsWith("." + d))) return true;
  }
  if (scope === null) return false;
  if (p === scope || p === key) return true;
  if (scope.startsWith("site:") && (BOARD_FILES.has(p) || p === "site:*" || p === scope.slice(5))) return true;
  if (scope === "ats" && (p === "ats" || p === key)) return true;
  return false;
}

export type StopMatch = {
  code: string;
  file: string;
  platform: string; // the signature's platform ("linkedin", "gmail", "ats", "site:*", ...)
  trip: boolean;
  reasonCode: string | null;
  matched: string;
  where: "url" | "title" | "text" | "http_status";
  index: number; // position of the match in the text (text matches only, else -1)
};

// Stop reasons from most to least severe. The same order as SEVERITY in scripts/jobhunter/detect/__init__.py
// (U1; a test keeps the two equal): longer cooldowns, manual-only weeks and warm-up restarts first. LinkedIn
// serves restriction and identity pages under /checkpoint/, which must not be reported as a plain challenge.
export const SEVERITY: readonly string[] = [
  "li_restricted", "li_identity_mismatch", "li_challenge", "li_security_email", "li_logged_out",
  "li_invite_limit", "li_easy_apply_limit", "li_messaging_blocked", "li_email_needed",
  "li_commercial_limit", "li_http_429", "li_unknown_modal",
  "gmail_security", "gmail_auth_failed", "gmail_identity_mismatch", "gmail_logged_out",
  "gmail_sending_limit", "gmail_unexpected_state", "ats_blocked", "site_challenge", "site_logged_out",
];

// Sort key of a matching signature (smaller wins), as detect._rank: a tripping stop before a job-level stop,
// then the severity of its reason_code (unknown reasons after the listed ones), then load order.
export function stopRank(sig: Signature, order: number): [number, number, number] {
  const sev = sig.reasonCode !== null ? SEVERITY.indexOf(sig.reasonCode) : -1;
  return [sig.trip ? 0 : 1, sev >= 0 ? sev : SEVERITY.length, order];
}

function rankLess(a: [number, number, number], b: [number, number, number]): boolean {
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] < b[i];
  return false;
}

type Hit = { matched: string; where: StopMatch["where"]; index: number };

// The first field of the page a signature matches: url, then title, then text, then the HTTP status.
function hitOf(sig: Signature, page: { url: string | null; title?: string | null; httpStatus: number | null }, text: string): Hit | null {
  if (page.url) {
    for (const re of sig.urlRes) {
      re.lastIndex = 0;
      const m = re.exec(page.url);
      if (m) return { matched: m[0].slice(0, 200), where: "url", index: -1 };
    }
  }
  if (page.title) {
    for (const re of sig.titleRes) {
      re.lastIndex = 0;
      const m = re.exec(page.title);
      if (m) return { matched: m[0].slice(0, 200), where: "title", index: -1 };
    }
  }
  if (text) {
    for (const re of sig.textRes) {
      re.lastIndex = 0;
      const m = re.exec(text);
      if (m) return { matched: m[0].slice(0, 200), where: "text", index: m.index };
    }
  }
  if (page.httpStatus !== null && sig.httpStatus.includes(page.httpStatus)) {
    return { matched: String(page.httpStatus), where: "http_status", index: -1 };
  }
  return null;
}

// The most severe stop signature that matches the page (stopRank), or null. Every applicable signature is
// tried, not only the first one that matches. `text` is the visible text of the result (snapshot, page text
// or error); it is scanned up to 200,000 characters.
export function matchStop(
  set: SignatureSet,
  page: { url: string | null; title?: string | null; text: string; httpStatus: number | null; scope: string | null; key: string | null; host: string | null },
): StopMatch | null {
  const text = page.text.length > 200000 ? page.text.slice(0, 200000) : page.text;
  let best: StopMatch | null = null;
  let bestRank: [number, number, number] | null = null;
  for (let order = 0; order < set.signatures.length; order++) {
    const sig = set.signatures[order];
    if (!applicable(sig, page.scope, page.key, page.host, set)) continue;
    const rank = stopRank(sig, order);
    if (bestRank !== null && !rankLess(rank, bestRank)) continue;
    const hit = hitOf(sig, page, text);
    if (!hit) continue;
    best = { code: sig.code, file: sig.file, platform: sig.platform, trip: sig.trip, reasonCode: sig.reasonCode, ...hit };
    bestRank = rank;
  }
  return best;
}

// Visible text of a tool result: every text content item, plus a string result or error.
export function resultText(result: unknown, error: unknown): string {
  const parts: string[] = [];
  const visit = (r: unknown) => {
    if (typeof r === "string") {
      parts.push(r);
      return;
    }
    if (!r || typeof r !== "object") return;
    const o = r as Record<string, unknown>;
    if (Array.isArray(o.content)) {
      for (const c of o.content) {
        if (c && typeof c === "object" && typeof (c as Record<string, unknown>).text === "string") parts.push((c as Record<string, string>).text);
      }
    }
    if (typeof o.text === "string") parts.push(o.text);
    if (typeof o.snapshot === "string") parts.push(o.snapshot);
  };
  visit(result);
  if (typeof error === "string") parts.push(error);
  return parts.join("\n");
}

// Boolean flags that the allowlisted read-only drivers report (drivers/read_form.js). The guard acts on
// their values, never on their key names: a flag that is true adds its line to the scan text, worded so
// that the platform's own text signatures match it (and so does `jh.py detect`, which re-matches the stop
// file): captcha_visible as a CAPTCHA (ats_captcha on an ATS form, site_captcha on a job board),
// account_wall as an account wall (ats_account_wall). A false flag adds nothing.
export const DRIVER_FLAG_TEXT: Readonly<Record<string, string>> = {
  captcha_visible: "captcha_visible: true (the driver saw a CAPTCHA on the page)",
  account_wall: "account_wall: true (the driver saw an account wall: create an account to apply)",
};

const JSON_KEY_RE = /"[A-Za-z_][A-Za-z0-9_]*"\s*:/g;
const FLAG_TRUE_RE = /"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*true\b/g;
const MAX_DRIVER_DEPTH = 24;

function isFlag(k: string): boolean {
  return Object.prototype.hasOwnProperty.call(DRIVER_FLAG_TEXT, k);
}

function parseJsonContainer(s: string): unknown {
  const t = s.trim();
  if (t.length < 2 || !(t.startsWith("{") || t.startsWith("["))) return undefined;
  try {
    const v = JSON.parse(t);
    return v !== null && typeof v === "object" ? v : undefined;
  } catch {
    return undefined;
  }
}

// The text the observer scans for the result of an allowlisted driver (browser act evaluate). A driver
// returns JSON whose key names are not page text ("captcha_visible": false must not match a "captcha"
// signature), so the scan text is the JSON's string values (page text, labels, URLs; a string value that
// is itself JSON is read the same way), preceded by one DRIVER_FLAG_TEXT line per flag that is true.
// Output that is not valid JSON (cut short, or an error message) has its "key": names removed instead.
export function driverScanText(text: string): string {
  const flags = new Set<string>();
  const values: string[] = [];
  const parsed = parseJsonContainer(text);
  if (parsed !== undefined) {
    const visit = (v: unknown, depth: number): void => {
      if (depth > MAX_DRIVER_DEPTH) return;
      if (typeof v === "string") {
        const inner = parseJsonContainer(v);
        if (inner !== undefined) visit(inner, depth + 1);
        else if (v.length) values.push(v);
        return;
      }
      if (Array.isArray(v)) {
        for (const x of v) visit(x, depth + 1);
        return;
      }
      if (v && typeof v === "object") {
        for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
          if (x === true && isFlag(k)) flags.add(k);
          visit(x, depth + 1);
        }
      }
    };
    visit(parsed, 0);
  } else {
    for (const m of text.matchAll(FLAG_TRUE_RE)) if (isFlag(m[1])) flags.add(m[1]);
    values.push(text.replace(JSON_KEY_RE, " "));
  }
  const lines = Object.keys(DRIVER_FLAG_TEXT).filter((k) => flags.has(k)).map((k) => DRIVER_FLAG_TEXT[k]);
  return lines.concat(values).join("\n");
}

// URL, tab and HTTP status reported by a browser result (details first, then the top level).
export function resultMeta(result: unknown): { url: string | null; targetId: string | null; httpStatus: number | null; title: string | null } {
  const out = { url: null as string | null, targetId: null as string | null, httpStatus: null as number | null, title: null as string | null };
  if (!result || typeof result !== "object") return out;
  const o = result as Record<string, any>;
  for (const src of [o.details, o]) {
    if (!src || typeof src !== "object") continue;
    if (out.url === null && typeof src.url === "string") out.url = src.url;
    if (out.targetId === null && typeof src.targetId === "string") out.targetId = src.targetId;
    if (out.title === null && typeof src.title === "string") out.title = src.title;
    const st = src.status ?? src.httpStatus ?? src.http_status;
    if (out.httpStatus === null && typeof st === "number") out.httpStatus = st;
    if (out.url === null && src.pageState && typeof src.pageState.url === "string") out.url = src.pageState.url;
  }
  return out;
}
