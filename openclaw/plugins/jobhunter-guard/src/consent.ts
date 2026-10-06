// Per-site consent for the agent's browser profile (change request "use the existing Chrome logins, with
// consent"): the owner allows each site (`./jobhunter init`, `./jobhunter browser consent`), which records it
// in private/consent.json; `./jobhunter browser forget` revokes it. The guard reads that file read only, on
// every browser call of a jobhunter agent, and refuses the sites that have no active consent row (policy.ts,
// G_NO_CONSENT). This is a second fence behind preflight and gate reserve, so it fails closed and is never
// more permissive than the core's reader (install.load_consent and install.consent_active, U7).
//
// The file, as scripts/jobhunter/install.py record_consent and revoke_consent write it:
//   {"version": 1, "updated_at": "<ts>",
//    "sites": {"linkedin": {"site": "linkedin", "status": "granted", "method": "chrome_import",
//                           "domains": ["linkedin.com", "www.linkedin.com"], "chrome_profile": "Profile 1",
//                           "chrome_profile_name": "...", "granted_at": "<ts>", "revoked_at": null,
//                           "declined_at": null, "by": "owner"}, ...}}
// A site has consent when its row (under its exact site name) has status "granted", a granted_at, no
// revoked_at, and no "site" field naming another site. A missing, unreadable or malformed file, a link, a
// file of another user or one that group or others can write gives no site consent.
//
// Per-capability consent (FEATURES-OTP-ACCOUNTS-CAPTCHA 1.2), in the same file under "capabilities":
//   {"capabilities": {"email_codes": {"workday": {"site": "workday", "capability": "email_codes",
//                     "status": "granted", "granted_at": "<ts>", "revoked_at": null, "declined_at": null,
//                     "by": "owner", "method": "pin"}, "host:careers.kestrel.example": {...}},
//                     "ats_accounts": {"workday": {..., "email": "alex.rivera@example.com"}}}}
// A row is active when its status is "granted", granted_at is a non-empty string, revoked_at is empty, and
// its "site" and "capability" fields equal their keys. Only the capabilities in CAPABILITIES are read; other
// names are ignored. Sites are a platform key of guard-hosts.json or "host:<host>" (a company portal; it
// covers that host and its subdomains). A missing "capabilities" object gives no capability; a file that
// cannot be used gives no consent of any kind. The guard does not compare an ats_accounts row's "email" with
// owner.gmail_address of private/config.json (it does not read that file): the core checks it before every
// code step, and the guard uses a capability only to skip a job-level soft stop, never to allow an action.

import fs from "node:fs";
import path from "node:path";

export const CONSENT_MAX_BYTES = 1024 * 1024;

export type ConsentState = {
  ok: boolean; // false: the file could not be used; no site has consent
  sites: Set<string>; // sites with an active consent row
  capabilities: Map<string, Set<string>>; // capability -> sites with an active capability row
  reason: string; // why ok is false, or "" when it is true
};

export const CAPABILITIES: readonly string[] = ["email_codes", "ats_accounts"];
const PLATFORM_SITE_RE = /^[a-z0-9_-]{1,40}$/;
const HOST_SITE_RE = /^host:(?=.{1,100}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/;

// Whether a capability site key has a valid shape: a platform key, or "host:<lower-case host without port>".
export function validCapabilitySite(site: string): boolean {
  return PLATFORM_SITE_RE.test(site) || HOST_SITE_RE.test(site);
}

function filled(v: unknown): boolean {
  return v !== undefined && v !== null && v !== "" && v !== false;
}

// The sites with an active consent row in a parsed consent.json (pure; exported for tests).
export function activeSites(raw: unknown): Set<string> {
  const out = new Set<string>();
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  const sites = (raw as Record<string, unknown>).sites;
  if (!sites || typeof sites !== "object" || Array.isArray(sites)) return out;
  for (const [site, r] of Object.entries(sites as Record<string, unknown>)) {
    if (!site || !r || typeof r !== "object" || Array.isArray(r)) continue;
    const row = r as Record<string, unknown>;
    if (row.site !== undefined && row.site !== site) continue;
    if (row.status !== "granted") continue;
    if (typeof row.granted_at !== "string" || row.granted_at.trim() === "") continue;
    if (filled(row.revoked_at)) continue;
    out.add(site);
  }
  return out;
}

// The active capability rows of a parsed consent.json (pure; exported for tests): capability -> sites.
// Every capability of CAPABILITIES has an entry (an empty set when nothing is granted).
export function activeCapabilities(raw: unknown): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>();
  for (const cap of CAPABILITIES) out.set(cap, new Set());
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  const caps = (raw as Record<string, unknown>).capabilities;
  if (!caps || typeof caps !== "object" || Array.isArray(caps)) return out;
  for (const cap of CAPABILITIES) {
    const rows = (caps as Record<string, unknown>)[cap];
    if (!rows || typeof rows !== "object" || Array.isArray(rows)) continue;
    const set = out.get(cap)!;
    for (const [site, r] of Object.entries(rows as Record<string, unknown>)) {
      if (!site || !validCapabilitySite(site) || !r || typeof r !== "object" || Array.isArray(r)) continue;
      const row = r as Record<string, unknown>;
      if (row.site !== site || row.capability !== cap) continue;
      if (row.status !== "granted") continue;
      if (typeof row.granted_at !== "string" || row.granted_at.trim() === "") continue;
      if (filled(row.revoked_at)) continue;
      set.add(site);
    }
  }
  return out;
}

// Whether a capability is active for a page: its site is the platform key of the host in guard-hosts.json
// (`platformKey`), else "host:<host>", and a "host:" row covers that host and its subdomains.
export function capabilityActive(caps: Map<string, Set<string>> | undefined, capability: string, platformKey: string | null, host: string | null): boolean {
  const set = caps ? caps.get(capability) : undefined;
  if (!set || set.size === 0) return false;
  if (platformKey) return set.has(platformKey);
  if (!host) return false;
  const h = host.toLowerCase();
  for (const site of set) {
    if (!site.startsWith("host:")) continue;
    const d = site.slice(5);
    if (h === d || h.endsWith("." + d)) return true;
  }
  return false;
}

// Read private/consent.json (read only). Never throws.
export function readConsent(file: string): ConsentState {
  const fail = (reason: string): ConsentState => ({ ok: false, sites: new Set(), capabilities: activeCapabilities(null), reason: path.basename(file) + " " + reason });
  let st: fs.Stats;
  try {
    st = fs.lstatSync(file);
  } catch (e) {
    const code = (e as { code?: string }).code;
    return fail(code === "ENOENT" ? "is missing (no site has consent yet)" : "cannot be read");
  }
  if (st.isSymbolicLink()) return fail("is a link");
  if (!st.isFile()) return fail("is not a file");
  if ((st.mode & 0o022) !== 0) return fail("is writable by other users");
  if (typeof process.getuid === "function" && st.uid !== process.getuid()) return fail("belongs to another user");
  if (st.size > CONSENT_MAX_BYTES) return fail("is too large");
  let raw: unknown;
  try {
    raw = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fail("is not valid JSON");
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return fail("is not a JSON object");
  const sites = (raw as Record<string, unknown>).sites;
  if (!sites || typeof sites !== "object" || Array.isArray(sites)) return fail("has no sites object");
  return { ok: true, sites: activeSites(raw), capabilities: activeCapabilities(raw), reason: "" };
}
