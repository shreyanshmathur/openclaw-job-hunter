// The stateful side of the guard: loads the install's files, reads the ledger, keeps per-session state
// (snapshot refs, tab URLs, stopped sessions), writes the heartbeat and the guard logs, and runs jh.py.
// All decisions are made by the pure decide() in policy.ts.

import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { actRequestOf, classifyUrl, parseHostsConfig, parseSnapshotRefs, RefCache, resultTab, resultTabList, scriptAllowed, targetIdOf } from "./browser.ts";
import { handleJh, type CommandCtx } from "./command.ts";
import { readConsent } from "./consent.ts";
import { agentProof, parseKey } from "./grant.ts";
import { makeJhRunner, type JhRunner } from "./jhcall.ts";
import { Ledger } from "./ledger.ts";
import { decide, effectiveAgentId, JOBHUNTER_PREFIX, normalizeToolName } from "./policy.ts";
import { driverScanText, loadSignatures, matchStop, resultMeta, resultText, type SignatureSet } from "./signatures.ts";
import type { Acl, Decision, GuardConfig, HostsConfig, Snapshot, TokenRecord, ToolCtx, ToolEvent } from "./types.ts";

export const GUARD_VERSION = "2.0.0";
const TOKEN_RE = /^T[A-Z2-7]{11}$/;
const HEALTH_TTL_MS = 15000;
const STATIC_TTL_MS = 60000;
const MAX_DETECT_TRIES = 12; // about one hour of heartbeats

export type Logger = { info: (m: string) => void; warn: (m: string) => void; error: (m: string) => void };

export type PolicyResult = { block: true; blockReason: string } | { params: Record<string, unknown> } | undefined;

const DETECT_TEXT_MAX = 20000;

// The key a job-level stop is remembered under: the platform key of the host, else the host.
export function siteKey(info: { platform: { key: string } | null; host: string | null }): string {
  return info.platform ? info.platform.key : info.host || "";
}

// Platform for the detect payload (12.11): the signature file's platform ("linkedin", "gmail", "ats"),
// or for board and generic files the host's site scope.
export function stopPlatform(sigPlatform: string, hostScope: string | null, host: string | null): string {
  if (sigPlatform && !sigPlatform.includes("*") && !["any", "all", "generic", "common", "boards", "board", "sites", "site"].includes(sigPlatform)) {
    return sigPlatform;
  }
  if (hostScope) return hostScope;
  const label = (host || "").split(".").filter((x) => x && x !== "www")[0] || "unknown";
  return "site:" + label.replace(/[^a-z0-9_-]/g, "").slice(0, 34);
}

// At most `max` characters of the text, positioned so that a text match at `index` is inside it
// (jh.py detect re-matches the payload and reads only the first 20,000 characters).
export function textWindow(text: string, index: number, max: number): string {
  if (text.length <= max || index < 0 || index < max - 2000) return text.slice(0, max);
  const start = Math.max(0, Math.min(index - 1000, text.length - max));
  return text.slice(start, start + max);
}

// JSON with sorted object keys (a stable identity for a tool call's parameters).
export function stableJson(v: unknown): string {
  if (v === null || typeof v !== "object") return JSON.stringify(v) ?? "null";
  if (Array.isArray(v)) return "[" + v.map(stableJson).join(",") + "]";
  const o = v as Record<string, unknown>;
  return "{" + Object.keys(o).sort().map((k) => JSON.stringify(k) + ":" + stableJson(o[k])).join(",") + "}";
}

export function fmtTs(ms: number): string {
  return new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function sha256File(file: string): string {
  return createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

// realpath that also works for files that do not exist yet (resolves the nearest existing parent).
export function realpathLoose(p: string): string {
  try {
    return fs.realpathSync.native(p);
  } catch {
    const dir = path.dirname(p);
    if (dir === p) return p;
    return path.join(realpathLoose(dir), path.basename(p));
  }
}

// Validate the plugin config (the manifest schema is enforced by OpenClaw too; this is the runtime check).
export function parseConfig(raw: unknown): GuardConfig {
  const o = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const abs = (k: string) => {
    const v = o[k];
    if (typeof v !== "string" || !path.isAbsolute(v) || v.includes(" ")) throw new Error("plugin config " + k + " must be an absolute path without spaces");
    return v;
  };
  const agents = Array.isArray(o.publicReadonlyAgents) ? o.publicReadonlyAgents.filter((a) => typeof a === "string") as string[] : [];
  const fallback = Array.isArray(o.ownerFallback)
    ? (o.ownerFallback as unknown[]).filter((r): r is { channel: string; senderId: string } => {
        const x = r as Record<string, unknown>;
        return !!x && typeof x.channel === "string" && typeof x.senderId === "string";
      })
    : undefined;
  return { repo: abs("repo"), python: abs("python"), homeFile: abs("homeFile"), publicReadonlyAgents: agents.filter((a) => !a.startsWith(JOBHUNTER_PREFIX)), ownerFallback: fallback };
}

// Every sha256 hex value in drivers/manifest.json ({name: sha256} or {name: {sha256}}).
export function manifestHashes(raw: unknown): Set<string> {
  const out = new Set<string>();
  const visit = (v: unknown, depth: number) => {
    if (depth > 5) return;
    if (typeof v === "string" && /^[0-9a-f]{64}$/i.test(v)) out.add(v.toLowerCase());
    else if (v && typeof v === "object") for (const x of Object.values(v as Record<string, unknown>)) visit(x, depth + 1);
  };
  visit(raw, 0);
  return out;
}

export type RuntimeOptions = {
  logger?: Logger;
  runner?: JhRunner;
  nowMs?: () => number;
};

export class GuardRuntime {
  readonly config: GuardConfig;
  readonly dataRoot: string;
  private logger: Logger;
  private runner: JhRunner;
  private nowMs: () => number;

  private acl: Acl | null = null;
  private hosts: HostsConfig | null = null;
  private signatures: SignatureSet | null = null;
  private driverHashes: Set<string> = new Set();
  private aclSha = "";
  private hostsSha = "";
  private staticError = "not loaded";
  private staticAt = 0;

  private home: Record<string, unknown> | null = null;
  private key: Buffer | null = null;
  private ledger: Ledger | null = null;
  private healthState = { ok: false, reason: "not checked" };
  private healthAt = 0;
  private loadedAt: number;

  private refs = new RefCache();
  private stopped = new Set<string>();
  private softStops = new Map<string, Set<string>>();
  private commitCounts = new Map<string, number>();
  private pendingDetects = new Map<string, number>();
  private trustedCalls = new Map<string, number>();

  constructor(config: GuardConfig, opts: RuntimeOptions = {}) {
    this.config = config;
    this.dataRoot = path.dirname(path.dirname(config.homeFile));
    this.logger = opts.logger || { info: () => {}, warn: () => {}, error: () => {} };
    this.runner = opts.runner || makeJhRunner(config.python, path.join(config.repo, "scripts", "jh.py"), config.repo);
    this.nowMs = opts.nowMs || (() => Date.now());
    this.loadedAt = this.nowMs();
  }

  // ---------------------------------------------------------------- paths
  guardDir(): string {
    return path.join(this.dataRoot, "state", "guard");
  }
  private pausedFile(): string {
    return path.join(this.dataRoot, "state", "PAUSED");
  }
  // Written only by the owner's terminal commands (init, browser forget); the guard only reads it.
  consentFile(): string {
    return path.join(this.dataRoot, "private", "consent.json");
  }
  private logsDir(): string {
    return path.join(this.dataRoot, "logs");
  }

  // ---------------------------------------------------------------- loading and health
  private loadStatic(force = false): void {
    const now = this.nowMs();
    if (!force && now - this.staticAt < STATIC_TTL_MS && this.staticError === "") return;
    if (!force && this.staticError !== "" && now - this.staticAt < 2000) return;
    this.staticAt = now;
    try {
      const aclFile = path.join(this.config.repo, "scripts", "jobhunter", "acl.json");
      const acl = JSON.parse(fs.readFileSync(aclFile, "utf8")) as Acl;
      if (!acl || typeof acl !== "object" || !acl.agents || !acl.value_classes) throw new Error("acl.json has no agents or value_classes");
      const hostsFile = path.join(this.config.repo, "openclaw", "guard-hosts.json");
      const hosts = parseHostsConfig(JSON.parse(fs.readFileSync(hostsFile, "utf8")));
      const sigs = loadSignatures(path.join(this.config.repo, "scripts", "jobhunter", "detect"));
      let hashes = new Set<string>();
      try {
        hashes = manifestHashes(JSON.parse(fs.readFileSync(path.join(this.config.repo, "drivers", "manifest.json"), "utf8")));
      } catch {
        hashes = new Set(); // no manifest: every evaluate is refused
      }
      this.acl = acl;
      this.hosts = hosts;
      this.signatures = sigs;
      this.driverHashes = hashes;
      this.aclSha = sha256File(aclFile);
      this.hostsSha = sha256File(hostsFile);
      for (const w of sigs.warnings) this.logger.warn("jobhunter-guard: " + w);
      this.staticError = "";
    } catch (e) {
      this.staticError = (e as Error).message || String(e);
    }
  }

  health(force = false): { ok: boolean; reason: string } {
    const now = this.nowMs();
    if (!force && now - this.healthAt < HEALTH_TTL_MS) return this.healthState;
    this.healthAt = now;
    this.loadStatic(force);
    let state = { ok: true, reason: "" };
    // The key is read on its own so that /jh (for example /jh pause) keeps working while another part
    // of the install is broken.
    let keyError = "";
    try {
      this.key = parseKey(fs.readFileSync(path.join(this.dataRoot, "private", "guard.key"), "utf8"));
    } catch (e) {
      this.key = null;
      keyError = "private/guard.key: " + ((e as Error).message || String(e));
    }
    try {
      if (this.staticError) throw new Error(this.staticError);
      if (keyError) throw new Error(keyError);
      const home = JSON.parse(fs.readFileSync(this.config.homeFile, "utf8")) as Record<string, unknown>;
      if (!home || typeof home.install_id !== "string" || !home.install_id) throw new Error("home.json has no install_id");
      if (typeof home.ws_root !== "string" || !path.isAbsolute(home.ws_root)) throw new Error("home.json has no ws_root");
      if (typeof home.db_path !== "string" || !path.isAbsolute(home.db_path)) throw new Error("home.json has no db_path");
      this.home = home;
      if (!this.ledger || this.ledger.dbPath !== home.db_path) {
        this.ledger?.close();
        this.ledger = new Ledger(home.db_path);
      }
      const dbInstall = this.ledger.installId();
      if (dbInstall !== home.install_id) throw new Error("install_id differs between home.json and the database");
    } catch (e) {
      state = { ok: false, reason: (e as Error).message || String(e) };
    }
    if (state.ok !== this.healthState.ok) {
      if (state.ok) this.logger.info("jobhunter-guard: healthy");
      else this.logger.error("jobhunter-guard: unhealthy: " + state.reason);
    }
    this.healthState = state;
    return state;
  }

  installId(): string | null {
    return this.home && typeof this.home.install_id === "string" ? this.home.install_id : null;
  }

  // ---------------------------------------------------------------- session helpers
  sessionOf(ctx: ToolCtx, agentId: string): string {
    return ctx.sessionKey || ctx.sessionId || ctx.runId || "agent:" + agentId;
  }

  isStopped(session: string): boolean {
    return this.stopped.has(session);
  }

  private commitsUsed(token: string): number {
    const cached = this.commitCounts.get(token);
    if (cached !== undefined) return cached;
    let n = 0;
    try {
      const text = fs.readFileSync(path.join(this.guardDir(), token + ".jsonl"), "utf8");
      for (const line of text.split("\n")) {
        if (!line.trim()) continue;
        try {
          if ((JSON.parse(line) as Record<string, unknown>).class === "commit") n += 1;
        } catch {
          n += 1; // an unreadable line counts against the budget
        }
      }
    } catch {
      n = 0;
    }
    this.commitCounts.set(token, n);
    return n;
  }

  // ---------------------------------------------------------------- trusted tier bookkeeping
  // The policy is registered in the trusted tier and, as a backstop, as an ordinary before_tool_call hook
  // (a trusted registration that the host refuses is only reported, not thrown). The trusted tier runs
  // first and marks the call; the hook then skips calls the trusted tier already decided.
  callKey(event: ToolEvent, ctx: ToolCtx & { toolCallId?: string }): string {
    const id = event.toolCallId || ctx.toolCallId;
    if (id) return "id:" + id;
    const body = (ctx.sessionKey || "") + "|" + (ctx.runId || event.runId || "") + "|" + String(event.toolName) + "|" + stableJson(event.params);
    return "p:" + createHash("sha256").update(body).digest("hex");
  }

  markTrusted(event: ToolEvent, ctx: ToolCtx): void {
    const now = this.nowMs();
    if (this.trustedCalls.size > 500) {
      for (const [k, t] of this.trustedCalls) if (now - t > 120000) this.trustedCalls.delete(k);
    }
    this.trustedCalls.set(this.callKey(event, ctx), now);
  }

  consumeTrusted(event: ToolEvent, ctx: ToolCtx): boolean {
    const key = this.callKey(event, ctx);
    const t = this.trustedCalls.get(key);
    if (t === undefined) return false;
    this.trustedCalls.delete(key);
    return this.nowMs() - t <= 120000;
  }

  // ---------------------------------------------------------------- snapshot
  buildSnapshot(event: ToolEvent, ctx: ToolCtx): Snapshot {
    const agentId = effectiveAgentId(ctx);
    const tool = normalizeToolName(event.toolName);
    const session = this.sessionOf(ctx, agentId);
    const params = (event.params || {}) as Record<string, unknown>;
    const tabId = tool === "browser" ? targetIdOf(params) : null;
    const isJh = agentId.startsWith(JOBHUNTER_PREFIX);
    let health = { ok: true, reason: "" };
    if (isJh) health = this.health();
    else this.loadStatic();
    const snap: Snapshot = {
      nowMs: this.nowMs(),
      health,
      config: this.config,
      acl: this.acl,
      hosts: this.hosts,
      wsRoot: this.home && typeof this.home.ws_root === "string" ? this.home.ws_root : "/nonexistent-ws-root",
      driverHashes: this.driverHashes,
      sessionStopped: isJh && this.stopped.has(session),
      writeBlocked: (isJh && this.softStops.get(session)) || new Set(),
      paused: false,
      openBreakers: new Set(),
      token: null,
      stagedPath: null,
      dwell: null,
      commitsUsed: 0,
      currentUrl: this.refs.url(session, tabId),
      lookupRef: (ref: string) => this.refs.lookup(session, tabId, ref),
      realpath: realpathLoose,
    };
    if (isJh && tool === "browser") snap.consent = readConsent(this.consentFile());
    if (isJh && health.ok && tool === "browser" && this.ledger) {
      try {
        snap.paused = fs.existsSync(this.pausedFile());
        snap.openBreakers = this.ledger.openBreakers();
        snap.token = this.ledger.openToken(agentId);
        if (snap.token && TOKEN_RE.test(snap.token.token)) {
          snap.stagedPath = this.ledger.stagedPath(snap.token.token);
          snap.dwell = this.ledger.dwellLock(agentId);
          snap.commitsUsed = this.commitsUsed(snap.token.token);
        }
      } catch (e) {
        snap.health = { ok: false, reason: "database read failed: " + ((e as Error).message || String(e)) };
      }
    }
    return snap;
  }

  // ---------------------------------------------------------------- before_tool_call
  evaluate(event: ToolEvent, ctx: ToolCtx): PolicyResult {
    const agentId = effectiveAgentId(ctx);
    let decision: Decision;
    try {
      decision = decide(event, ctx, this.buildSnapshot(event, ctx));
    } catch (e) {
      if (!agentId.startsWith(JOBHUNTER_PREFIX)) return undefined;
      decision = { kind: "block", code: "G_GUARD_UNHEALTHY", reason: "internal guard error; end the cycle" };
      this.logger.error("jobhunter-guard: decide failed: " + ((e as Error).stack || String(e)));
    }
    const tool = normalizeToolName(event.toolName);
    if (agentId.startsWith(JOBHUNTER_PREFIX) || decision.kind === "block") {
      this.logDecision(agentId, ctx, tool, decision);
    }
    if (decision.kind === "block") return { block: true, blockReason: decision.code + ": " + decision.reason };
    if (decision.kind === "allow") {
      if (decision.records && decision.records.length) {
        // No fill or commit without its line in the token log (gate fail relies on it): fail closed.
        if (!decision.token || !this.appendTokenRecords(decision.token, agentId, decision.host ?? null, decision.records)) {
          const blocked: Decision = { kind: "block", code: "G_GUARD_UNHEALTHY", reason: "the guard cannot write the token log; end the cycle" };
          this.logDecision(agentId, ctx, tool, blocked);
          return { block: true, blockReason: blocked.code + ": " + blocked.reason };
        }
      }
      if (decision.params) return { params: decision.params };
    }
    return undefined;
  }

  appendTokenRecords(token: string, agentId: string, host: string | null, records: TokenRecord[]): boolean {
    if (!TOKEN_RE.test(token)) return false;
    const ts = fmtTs(this.nowMs());
    const lines = records
      .map((r) => JSON.stringify({ ts, token, agent: agentId, class: r.class, action: r.action, host, ref: r.ref, role: r.role, name: r.name }))
      .join("\n") + "\n";
    try {
      fs.mkdirSync(this.guardDir(), { recursive: true, mode: 0o700 });
      fs.appendFileSync(path.join(this.guardDir(), token + ".jsonl"), lines, { mode: 0o600 });
    } catch (e) {
      this.logger.error("jobhunter-guard: cannot write the token log: " + (e as Error).message);
      return false;
    }
    this.commitCounts.delete(token); // recounted from the file on the next call
    return true;
  }

  private logDecision(agentId: string, ctx: ToolCtx, tool: string, d: Decision): void {
    const entry: Record<string, unknown> = {
      ts: fmtTs(this.nowMs()),
      kind: "decision",
      agent: agentId || null,
      session: ctx.sessionKey || null,
      tool,
      decision: d.kind,
    };
    if (d.kind === "block") entry.code = d.code;
    if (d.kind !== "pass" && d.actionClass) entry.class = d.actionClass;
    if (d.kind !== "pass" && d.host) entry.host = d.host;
    if (d.command) entry.command = d.command;
    this.guardLog(entry);
  }

  guardLog(entry: Record<string, unknown>): void {
    const dir = this.logsDir();
    if (!fs.existsSync(dir)) return; // before `init` nothing is created
    const ts = typeof entry.ts === "string" ? entry.ts : fmtTs(this.nowMs());
    const file = path.join(dir, "guard-" + ts.slice(0, 7) + ".jsonl");
    try {
      fs.appendFileSync(file, JSON.stringify({ ts, ...entry }) + "\n", { mode: 0o600 });
    } catch {
      // logging never blocks a decision
    }
  }

  // ---------------------------------------------------------------- after_tool_call (R5)
  observe(event: { toolName: string; params: Record<string, unknown>; result?: unknown; error?: string }, ctx: ToolCtx): { stopped: boolean; softStopped?: boolean; code?: string } {
    const agentId = effectiveAgentId(ctx);
    if (!agentId.startsWith(JOBHUNTER_PREFIX)) return { stopped: false };
    if (normalizeToolName(event.toolName) !== "browser") return { stopped: false };
    const session = this.sessionOf(ctx, agentId);
    const params = (event.params || {}) as Record<string, unknown>;
    const action = typeof params.action === "string" ? params.action : "";
    const meta = resultMeta(event.result);
    const used = targetIdOf(params);
    const tabId = meta.targetId || used;
    const text = resultText(event.result, event.error);
    if (!event.error) {
      // The handles OpenClaw resolved or reported for this tab name the same tab from now on, and a tab
      // list tells the guard the page of every listed tab (so a read there is judged by its real host).
      const tab = resultTab(event.result);
      if (tab) {
        if (used) this.refs.addAlias(session, used, tab.targetId);
        for (const a of tab.aliases) this.refs.addAlias(session, a, tab.targetId);
      }
      if (action === "tabs") {
        for (const t of resultTabList(event.result)) {
          for (const a of t.aliases) this.refs.addAlias(session, a, t.targetId);
          this.refs.learnUrl(session, t.targetId, t.url);
        }
      }
    }
    if (meta.url) this.refs.setUrl(session, tabId, meta.url);
    else if ((action === "navigate" || action === "open") && !event.error) {
      const u = typeof params.targetUrl === "string" ? params.targetUrl : typeof params.url === "string" ? params.url : null;
      this.refs.setUrl(session, tabId, u);
    }
    if (action === "snapshot" || action === "navigate" || action === "open" || action === "act") {
      const refs = parseSnapshotRefs(text);
      if (refs.size > 0) this.refs.replaceRefs(session, tabId, refs);
      else if (action === "navigate" || action === "open") this.refs.clearRefs(session, tabId);
    }
    this.loadStatic();
    if (!this.signatures || !this.hosts) return { stopped: false };
    // An allowlisted driver's output is JSON: scan its values and act on its flags, not its key names.
    const scanText = this.isDriverCall(params) ? driverScanText(text) : text;
    const url = meta.url || this.refs.url(session, tabId);
    const info = classifyUrl(url, this.hosts);
    const m = matchStop(this.signatures, {
      url,
      title: meta.title,
      text: scanText,
      httpStatus: meta.httpStatus,
      scope: info.platform ? info.platform.scope : null,
      key: info.platform ? info.platform.key : null,
      host: info.host,
    });
    if (!m) return { stopped: false };
    const payload = {
      platform: stopPlatform(m.platform, info.platform ? info.platform.scope : null, info.host),
      url,
      title: meta.title,
      http_status: meta.httpStatus,
      text: textWindow(scanText, m.index, DETECT_TEXT_MAX),
    };
    this.guardLog({ kind: "stop", agent: agentId, session: ctx.sessionKey || null, code: m.code, reason: m.reasonCode, file: m.file, where: m.where, trip: m.trip, host: info.host });
    if (m.trip) this.stopped.add(session);
    else {
      // job-level stop (for example a CAPTCHA on one ATS form): no more writes on this site in this session
      let set = this.softStops.get(session);
      if (!set) {
        set = new Set();
        this.softStops.set(session, set);
      }
      set.add(siteKey(info));
    }
    const file = this.writeStopFile(payload);
    if (file) void this.runDetect(file);
    return { stopped: m.trip, softStopped: !m.trip, code: m.code };
  }

  // A single `act` evaluate of an allowlisted driver (drivers/manifest.json), judged on the act request
  // OpenClaw runs (request merged with the top-level act keys). Batches, wait functions and anything else
  // keep the plain text scan.
  isDriverCall(params: Record<string, unknown>): boolean {
    if (params.action !== "act") return false;
    const req = actRequestOf(params);
    if (!req || req.kind !== "evaluate") return false;
    this.loadStatic();
    return scriptAllowed(req.fn, this.driverHashes);
  }

  private writeStopFile(payload: Record<string, unknown>): string | null {
    try {
      fs.mkdirSync(this.guardDir(), { recursive: true, mode: 0o700 });
      const stamp = fmtTs(this.nowMs()).replace(/[-:]/g, "");
      const file = path.join(this.guardDir(), "stop-" + stamp + "-" + Math.random().toString(36).slice(2, 8) + ".json");
      fs.writeFileSync(file, JSON.stringify(payload), { mode: 0o600 });
      return file;
    } catch (e) {
      this.logger.error("jobhunter-guard: cannot write the stop file: " + (e as Error).message);
      return null;
    }
  }

  async runDetect(file: string): Promise<boolean> {
    const res = await this.runner(["detect", "--file", file, "--source", "guard"]);
    const ok = res.exitCode === 0 || res.exitCode === 5; // 5: the stop was recorded and the breaker is open
    if (ok) this.pendingDetects.delete(file);
    else {
      const tries = (this.pendingDetects.get(file) || 0) + 1;
      if (tries >= MAX_DETECT_TRIES) this.pendingDetects.delete(file);
      else this.pendingDetects.set(file, tries);
      this.logger.error("jobhunter-guard: jh.py detect failed (exit " + res.exitCode + ", try " + tries + ")");
    }
    this.guardLog({ kind: "detect", file: path.basename(file), exit: res.exitCode });
    return ok;
  }

  // ---------------------------------------------------------------- resolve_exec_env (R8)
  execEnv(ctx: { agentId?: string; sessionKey?: string; runId?: string }): Record<string, string> | undefined {
    const agentId = effectiveAgentId(ctx);
    if (!agentId.startsWith(JOBHUNTER_PREFIX)) return undefined;
    const env: Record<string, string> = { JH_AGENT_ID: agentId };
    if (ctx.sessionKey) env.JH_SESSION_KEY = ctx.sessionKey;
    if (ctx.runId) env.JH_RUN_ID = ctx.runId;
    this.health();
    if (this.key) env.JH_AGENT_PROOF = agentProof(this.key, agentId, Math.floor(this.nowMs() / 1000));
    return env;
  }

  // ---------------------------------------------------------------- heartbeat (12.18)
  heartbeat(): boolean {
    const h = this.health(true);
    for (const f of [...this.pendingDetects.keys()]) void this.runDetect(f);
    if (!h.ok) return false;
    const install = this.installId();
    if (!install) return false;
    const body = {
      install_id: install,
      version: GUARD_VERSION,
      loaded_at: fmtTs(this.loadedAt),
      beat_at: fmtTs(this.nowMs()),
      acl_sha256: this.aclSha,
      hosts_sha256: this.hostsSha,
    };
    try {
      fs.mkdirSync(this.guardDir(), { recursive: true, mode: 0o700 });
      const file = path.join(this.guardDir(), "heartbeat.json");
      const tmp = file + ".tmp";
      fs.writeFileSync(tmp, JSON.stringify(body) + "\n", { mode: 0o600 });
      fs.renameSync(tmp, file);
      return true;
    } catch (e) {
      this.logger.error("jobhunter-guard: cannot write the heartbeat: " + (e as Error).message);
      return false;
    }
  }

  // ---------------------------------------------------------------- /jh (R6)
  async command(ctx: CommandCtx): Promise<{ text: string }> {
    return handleJh(ctx, {
      key: () => {
        this.health();
        return this.key;
      },
      run: this.runner,
      ownerFallback: this.config.ownerFallback,
      log: (e) => this.guardLog({ ...e, ts: fmtTs(this.nowMs()) }),
      nowS: () => Math.floor(this.nowMs() / 1000),
    });
  }

  close(): void {
    this.ledger?.close();
    this.ledger = null;
  }
}
