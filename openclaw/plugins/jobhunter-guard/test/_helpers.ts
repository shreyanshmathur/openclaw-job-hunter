// Test helpers (not a test file). Builds snapshots for decide() from the real acl.json and
// guard-hosts.json of this repo, with fictional workspace paths.

import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import { parseHostsConfig, sha256Hex } from "../src/browser.ts";
import { argvProof, parseKey, verifyOwnProof } from "../src/grant.ts";
import type { Acl, GuardConfig, HostsConfig, RefInfo, Snapshot } from "../src/types.ts";

export const PLUGIN_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const REPO = path.resolve(PLUGIN_DIR, "..", "..", "..");
export const FIXTURES = path.join(PLUGIN_DIR, "test", "fixtures");

export const PY = "/usr/bin/python3";
export const REPO_PATH = "/opt/jobhunter-test/openclaw-job-hunter";
export const WS = "/opt/jobhunter-test/ws";
export const JH = PY + " " + REPO_PATH + "/scripts/jh.py";
export const T0 = Date.parse("2026-09-27T05:00:00Z");

export const TOKEN = "TABCDEFGHJKM";
export const HOME = "/opt/jobhunter-test/home";

// The test guard key and the fixed nonce of the snapshot's proof minter (decide() stays deterministic).
export const TEST_KEY_HEX = "5a".repeat(32);
export const TEST_KEY = parseKey(TEST_KEY_HEX);
export const TEST_NONCE = "0123456789abcdef";
export const T0S = Math.floor(Date.parse("2026-09-27T05:00:00Z") / 1000);

// The command the R2 rewrite produces for `rest` (the jh.py arguments) of `agent` in `session`.
export function rewritten(agent: string, rest: string, session = ""): string {
  const words = rest.split(" ");
  return PY + " -I " + REPO_PATH + "/scripts/jh.py --agent-proof " + argvProof(TEST_KEY, agent, session, words, T0S, TEST_NONCE) + " " + rest;
}
export const CYCLE = "C20260927T050000ZABCD";

export function loadAcl(): Acl {
  return JSON.parse(fs.readFileSync(path.join(REPO, "scripts", "jobhunter", "acl.json"), "utf8")) as Acl;
}

export function loadHosts(): HostsConfig {
  return parseHostsConfig(JSON.parse(fs.readFileSync(path.join(REPO, "openclaw", "guard-hosts.json"), "utf8")));
}

export const DRIVER_TEXT = "() => { return { ok: true, title: document.title }; }";
export const DRIVER_HASHES = new Set([sha256Hex(DRIVER_TEXT)]);

export function config(over: Partial<GuardConfig> = {}): GuardConfig {
  return { repo: REPO_PATH, python: PY, homeFile: "/opt/jobhunter-test/openclaw-job-hunter/private/home.json", publicReadonlyAgents: ["main"], ...over };
}

const ACL = loadAcl();
const HOSTS = loadHosts();

// Every consent site of guard-hosts.json, as if the owner had allowed them all (snap() default).
export const ALL_CONSENT = new Set(HOSTS.platforms.map((p) => p.consent).filter((c): c is string => !!c));

export function consentOf(sites: string[] | null, reason = ""): { ok: boolean; sites: Set<string>; reason: string } {
  return sites === null ? { ok: false, sites: new Set(), reason: reason || "consent.json: missing" } : { ok: true, sites: new Set(sites), reason: "" };
}

export function snap(over: Partial<Snapshot> & { refs?: Record<string, RefInfo> } = {}): Snapshot {
  const refs = over.refs || {};
  const base: Snapshot = {
    nowMs: T0,
    health: { ok: true, reason: "" },
    config: config(),
    acl: ACL,
    hosts: HOSTS,
    wsRoot: WS,
    driverHashes: DRIVER_HASHES,
    sessionStopped: false,
    writeBlocked: new Set(),
    consent: { ok: true, sites: ALL_CONSENT, reason: "" },
    paused: false,
    openBreakers: new Set(),
    token: null,
    stagedPath: null,
    dwell: null,
    commitsUsed: 0,
    currentUrl: null,
    lookupRef: (r: string) => refs[r],
    realpath: (p: string) => p,
    mintProof: (agentId: string, sessionKey: string, rest: string[]) => argvProof(TEST_KEY, agentId, sessionKey, rest, T0S, TEST_NONCE),
    verifyOwnProof: (token: string, agentId: string, sessionKey: string, rest: string[]) => verifyOwnProof(TEST_KEY, token, agentId, sessionKey, rest, T0S),
    homeDir: HOME,
    glob: () => [],
  };
  const out = { ...base, ...over } as Snapshot & { refs?: unknown };
  delete out.refs;
  if (over.refs) out.lookupRef = (r: string) => refs[r];
  return out;
}

export function iso(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function reservedToken(platform = "greenhouse", kind = "application") {
  return { token: TOKEN, kind, platform, status: "reserved", armedAt: null, expiresAt: iso(T0 + 20 * 60000) };
}

export function armedToken(platform = "greenhouse", kind = "application", armedAgoS = 60) {
  return { token: TOKEN, kind, platform, status: "armed", armedAt: iso(T0 - armedAgoS * 1000), expiresAt: iso(T0 + 20 * 60000) };
}

// A dwell drawn after arm and already elapsed.
export function dwellDone(armedAgoS = 60) {
  return { acquiredAt: iso(T0 - (armedAgoS - 5) * 1000), expiresAt: iso(T0 - 10 * 1000) };
}

// ------------------------------------------------------------------ temporary ledger

export const NOW = "2026-09-27T05:00:00Z";

export function makeDb(file: string, installId = "IABCDEFGH"): DatabaseSync {
  const db = new DatabaseSync(file);
  db.exec("PRAGMA journal_mode = WAL");
  db.exec(fs.readFileSync(path.join(REPO, "scripts", "jobhunter", "schema.sql"), "utf8"));
  db.prepare("INSERT INTO meta (key, value, updated_at, updated_by) VALUES ('install_id', ?, ?, 'init')").run(installId, NOW);
  return db;
}

export function addToken(db: DatabaseSync, opts: { token?: string; agent?: string; status?: string; platform?: string; kind?: string; armedAt?: string | null; expiresAt?: string }) {
  db.exec("PRAGMA foreign_keys = OFF");
  db.prepare(
    "INSERT INTO actions (token, kind, route, first_touch, platform, agent_id, status, reserved_at, armed_at, expires_at, created_at, updated_at) " +
      "VALUES (?, ?, 'browser', 0, ?, ?, ?, ?, ?, ?, ?, ?)",
  ).run(
    opts.token ?? TOKEN,
    opts.kind ?? "application",
    opts.platform ?? "greenhouse",
    opts.agent ?? "jobhunter-applier",
    opts.status ?? "reserved",
    NOW,
    opts.armedAt ?? null,
    opts.expiresAt ?? "2026-09-27T05:30:00Z",
    NOW,
    NOW,
  );
}

