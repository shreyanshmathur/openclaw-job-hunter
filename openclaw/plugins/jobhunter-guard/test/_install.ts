// A temporary install for runtime tests (not a test file): a repo folder with the real acl.json and
// guard-hosts.json, fixture detect files and driver manifest, private/home.json, private/guard.key,
// a ledger built from the real schema, and agent workspaces. Plus a transcript replayer.

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import type { DatabaseSync } from "node:sqlite";
import { GuardRuntime } from "../src/runtime.ts";
import type { JhResult, JhRunner } from "../src/jhcall.ts";
import { parseKey, verifyArgvProof, verifyEnvProof } from "../src/grant.ts";
import type { GuardConfig } from "../src/types.ts";
import { ALL_CONSENT, DRIVER_TEXT, FIXTURES, PY, REPO, makeDb } from "./_helpers.ts";
import { sha256Hex } from "../src/browser.ts";

export const KEY_HEX = "0f".repeat(32);
export const INSTALL_ID = "IABCDEFGH";

export type TempInstall = {
  root: string;
  ws: string;
  home: string; // a fictional home folder inside the temp install (the runtime's `~`)
  db: DatabaseSync;
  jhCalls: string[][];
  now: { ms: number };
  runtime: GuardRuntime;
  runner: JhRunner;
  cleanup: () => void;
};

// private/consent.json as scripts/jobhunter/install.py record_consent and revoke_consent write it (fictional
// Chrome profile).
export function consentJson(sites: string[], revoked: string[] = []): string {
  const rows: Record<string, unknown> = {};
  for (const site of sites) {
    const off = revoked.includes(site);
    rows[site] = {
      site, status: off ? "revoked" : "granted", method: "chrome_import", domains: [site + ".example"],
      chrome_profile: "Profile 1", chrome_profile_name: "Personal", granted_at: "2026-09-26T08:00:00Z",
      revoked_at: off ? "2026-09-26T09:00:00Z" : null, declined_at: null, by: "owner",
    };
  }
  return JSON.stringify({ version: 1, updated_at: "2026-09-26T09:00:00Z", sites: rows }, null, 2) + "\n";
}

export function writeConsent(root: string, sites: string[] | null, revoked: string[] = [], mode = 0o600): void {
  const file = path.join(root, "private", "consent.json");
  fs.rmSync(file, { force: true });
  if (sites === null) return;
  fs.writeFileSync(file, consentJson(sites, revoked), { mode });
  fs.chmodSync(file, mode);
}

// `consent`: the sites with an active consent row (default every consent site of guard-hosts.json), or
// null for an install without private/consent.json.
export function makeInstall(opts: { ownerFallback?: Array<{ channel: string; senderId: string }>; jhExit?: number; consent?: string[] | null } = {}): TempInstall {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "jhg-install-")));
  const ws = path.join(root, "ws");
  const mk = (...p: string[]) => fs.mkdirSync(path.join(root, ...p), { recursive: true });
  mk("scripts", "jobhunter", "detect");
  mk("openclaw");
  mk("drivers");
  mk("private");
  mk("state", "guard");
  mk("logs");
  mk("home", ".openclaw", "workspace");
  for (const role of ["scout", "evaluator", "applier", "outreach", "qc"]) {
    for (const sub of ["work", "inbox"]) fs.mkdirSync(path.join(ws, role, sub), { recursive: true });
  }
  fs.copyFileSync(path.join(REPO, "scripts", "jobhunter", "acl.json"), path.join(root, "scripts", "jobhunter", "acl.json"));
  fs.copyFileSync(path.join(REPO, "openclaw", "guard-hosts.json"), path.join(root, "openclaw", "guard-hosts.json"));
  for (const n of fs.readdirSync(path.join(FIXTURES, "detect"))) {
    fs.copyFileSync(path.join(FIXTURES, "detect", n), path.join(root, "scripts", "jobhunter", "detect", n));
  }
  fs.writeFileSync(path.join(root, "drivers", "manifest.json"), JSON.stringify({ detect_page: sha256Hex(DRIVER_TEXT) }));
  const dbPath = path.join(root, "state", "jobhunter.sqlite3");
  const db = makeDb(dbPath, INSTALL_ID);
  const homeFile = path.join(root, "private", "home.json");
  fs.writeFileSync(homeFile, JSON.stringify({ install_id: INSTALL_ID, repo: root, db_path: dbPath, ws_root: ws, python: PY }));
  fs.writeFileSync(path.join(root, "private", "guard.key"), KEY_HEX + "\n", { mode: 0o600 });
  writeConsent(root, opts.consent === undefined ? [...ALL_CONSENT] : opts.consent);
  const jhCalls: string[][] = [];
  const now = { ms: Date.parse("2026-09-27T05:00:00Z") };
  const runner = async (argv: string[]): Promise<JhResult> => {
    jhCalls.push(argv);
    const exitCode = opts.jhExit ?? 0;
    return { exitCode, stdout: "OK from fake jh.py", stderr: "", envelope: { ok: exitCode === 0 }, error: null };
  };
  const home = path.join(root, "home");
  const runtime = new GuardRuntime(
    { repo: root, python: PY, homeFile, publicReadonlyAgents: ["main"], ownerFallback: opts.ownerFallback },
    { runner, nowMs: () => now.ms, homeDir: home },
  );
  return {
    root,
    ws,
    home,
    db,
    jhCalls,
    now,
    runtime,
    runner,
    cleanup: () => {
      runtime.close();
      db.close();
      fs.rmSync(root, { recursive: true, force: true });
    },
  };
}

function subst(v: unknown, inst: TempInstall): unknown {
  if (typeof v === "string") {
    return v
      .replace(/@JH@/g, PY + " " + inst.root + "/scripts/jh.py")
      .replace(/@PY@/g, PY)
      .replace(/@REPO@/g, inst.root)
      .replace(/@WS@/g, inst.ws)
      .replace(/@HOME@/g, inst.home)
      .replace(/@DRIVER@/g, DRIVER_TEXT);
  }
  if (Array.isArray(v)) return v.map((x) => subst(x, inst));
  if (v && typeof v === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, x] of Object.entries(v as Record<string, unknown>)) out[k] = subst(x, inst);
    return out;
  }
  return v;
}

function outcome(r: unknown, agent: string): string {
  if (r && typeof r === "object" && (r as Record<string, unknown>).block === true) {
    return String((r as Record<string, unknown>).blockReason).split(":")[0];
  }
  return agent.startsWith("jobhunter-") ? "allow" : "pass";
}

function readJsonl(file: string): Record<string, unknown>[] {
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, "utf8").split("\n").filter((l) => l.trim()).map((l) => JSON.parse(l));
}

// Replay a recorded transcript (see README "Transcript replay") against a temporary install.
export function replay(file: string, inst: TempInstall): void {
  const t = JSON.parse(fs.readFileSync(path.join(FIXTURES, file), "utf8")) as { name: string; config?: Partial<GuardConfig>; steps: Record<string, any>[] };
  const runtimes = new Map<string, GuardRuntime>();
  const runtimeFor = (mode: string | undefined): GuardRuntime => {
    const key = mode || "default";
    if (!t.config && key === "default") return inst.runtime;
    let rt = runtimes.get(key);
    if (!rt) {
      const over = { ...((subst(t.config || {}, inst) as Partial<GuardConfig>) || {}), ...(mode === "gate" ? { claudeNativeTools: "gate" as const } : {}) };
      rt = new GuardRuntime({ ...inst.runtime.config, ...over }, { runner: inst.runner, nowMs: () => inst.now.ms, homeDir: inst.home });
      runtimes.set(key, rt);
    }
    return rt;
  };
  const key = parseKey(KEY_HEX);
  try {
    t.steps.forEach((raw, i) => replayStep(subst(raw, inst) as Record<string, any>, t.name + " step " + (i + 1), inst, runtimeFor, key));
  } finally {
    for (const rt of runtimes.values()) rt.close();
  }
}

function replayStep(step: Record<string, any>, where: string, inst: TempInstall, runtimeFor: (mode: string | undefined) => GuardRuntime, key: Buffer): void {
  {
    if (step.call) {
      const c = step.call;
      const ctx = { agentId: c.agent, sessionKey: c.session, ...(c.ctx || {}) };
      const r = runtimeFor(step.mode).evaluate({ toolName: c.tool, params: c.params }, ctx);
      assert.equal(outcome(r, c.agent), step.expect, where + ": " + JSON.stringify(r));
      if (step.expect_rewrite) {
        // the R2 rewrite: "<PY> -I <jh.py> --agent-proof <proof for this agent, session and rest> <rest>"
        const want = step.expect_rewrite as { rest: string; workdir?: string; timeout?: number };
        const params = (r as { params?: Record<string, unknown> } | undefined)?.params;
        assert.ok(params, where + ": no rewrite");
        const toks = String(params!.command).split(" ");
        const jh = inst.root + "/scripts/jh.py";
        assert.deepEqual(toks.slice(0, 4), [PY, "-I", jh, "--agent-proof"], where);
        assert.equal(toks.slice(5).join(" "), want.rest, where);
        const parts = verifyArgvProof(key, toks[4], toks.slice(5), Math.floor(inst.now.ms / 1000));
        assert.ok(parts && parts.agent === c.agent, where + ": proof " + toks[4]);
        if (want.workdir !== undefined) {
          assert.equal(params!.workdir, want.workdir, where);
          assert.equal(params!.timeoutSeconds, 90, where);
        }
        if (want.timeout !== undefined) assert.equal(params!.timeout, want.timeout, where);
      }
    } else if (step.env) {
      const e = step.env;
      const env = runtimeFor(step.mode).execEnv({ agentId: e.agent, sessionKey: e.session, runId: e.runId });
      if (step.expect_env === false) {
        assert.equal(env, undefined, where);
      } else {
        assert.equal(env?.JH_AGENT_ID, e.agent, where);
        const parts = verifyEnvProof(key, String(env?.JH_AGENT_PROOF), e.session, Math.floor(inst.now.ms / 1000));
        assert.ok(parts && parts.agent === e.agent, where + ": env proof");
      }
    } else if (step.expect_log) {
      const want = step.expect_log as { kind: string; count: number };
      const month = new Date(inst.now.ms).toISOString().slice(0, 7);
      const lines = readJsonl(path.join(inst.root, "logs", "guard-" + month + ".jsonl")).filter((l) => l.kind === want.kind);
      assert.equal(lines.length, want.count, where + ": " + want.kind + " lines");
    } else if (step.result) {
      const c = step.result;
      const r = inst.runtime.observe({ toolName: c.tool, params: c.params, result: c.result, error: c.error }, { agentId: c.agent, sessionKey: c.session });
      assert.equal(r.stopped, step.expect_stop === true, where + " stop");
    } else if (step.sql) {
      inst.db.exec("PRAGMA foreign_keys = OFF");
      inst.db.prepare(step.sql).run(...(step.args || []));
    } else if (step.clock) {
      inst.now.ms = Date.parse(step.clock);
    } else if (step.expect_jh) {
      const want = step.expect_jh as string[];
      const hit = inst.jhCalls.some((argv) => argv.length === want.length && argv.every((a, k) => want[k] === "*" || want[k] === a));
      assert.ok(hit, where + ": no jh.py call like " + JSON.stringify(want) + " in " + JSON.stringify(inst.jhCalls));
    } else {
      throw new Error(where + ": unknown step");
    }
  }
}
