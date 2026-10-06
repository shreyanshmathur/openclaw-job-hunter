// Chat grants for /jh commands (design 1.4 R6 and 12.19).
//
//   grant = "<unix_ts>.<nonce 16 hex>.<hex HMAC-SHA256(key, command + "\n" + json.dumps(args) + "\n" + ts + "\n" + nonce)>"
//
// `key` is the 32 bytes whose hex text is stored in private/guard.key. `command` is the command words
// joined by one space ("approve", "profile answer", "config lower"). `args` is the list of argv tokens
// that follow the command words. json.dumps is Python's default: ", " separators and ensure_ascii.

import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";

// Python json.dumps(str) with the default ensure_ascii=True.
export function pyJsonString(s: string): string {
  let out = "\"";
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    const ch = s[i];
    if (ch === "\"") out += "\\\"";
    else if (ch === "\\") out += "\\\\";
    else if (c >= 0x20 && c <= 0x7e) out += ch;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (ch === "\b") out += "\\b";
    else if (ch === "\f") out += "\\f";
    else out += "\\u" + c.toString(16).padStart(4, "0");
  }
  return out + "\"";
}

// Python json.dumps(list_of_str).
export function pyJsonDumpsStrList(args: string[]): string {
  return "[" + args.map(pyJsonString).join(", ") + "]";
}

// Decode private/guard.key (64 hex characters, surrounding whitespace ignored). Throws otherwise.
export function parseKey(text: string): Buffer {
  const hex = String(text).trim();
  if (!/^[0-9a-fA-F]{64}$/.test(hex)) throw new Error("guard.key must hold 32 bytes as 64 hex characters");
  return Buffer.from(hex, "hex");
}

export function grantMessage(command: string, args: string[], ts: string, nonce: string): string {
  return command + "\n" + pyJsonDumpsStrList(args) + "\n" + ts + "\n" + nonce;
}

export function makeGrant(key: Buffer, command: string, args: string[], nowS?: number, nonce?: string): string {
  const ts = String(Math.floor(nowS ?? Date.now() / 1000));
  const n = nonce ?? randomBytes(8).toString("hex");
  const mac = createHmac("sha256", key).update(grantMessage(command, args, ts, n), "utf8").digest("hex");
  return ts + "." + n + "." + mac;
}

// Verification mirror of jobhunter.auth.verify_grant (without the nonce store); used by tests.
export function verifyGrant(key: Buffer, grant: string, command: string, args: string[], nowS: number, maxAgeS = 120): boolean {
  const m = /^([0-9]{1,12})\.([0-9a-f]{16})\.([0-9a-f]{64})$/.exec(grant);
  if (!m) return false;
  const ts = Number(m[1]);
  if (!(nowS - ts <= maxAgeS && ts - nowS <= 5)) return false;
  const want = createHmac("sha256", key).update(grantMessage(command, args, m[1], m[2]), "utf8").digest();
  const got = Buffer.from(m[3], "hex");
  return got.length === want.length && timingSafeEqual(got, want);
}

// ------------------------------------------------------------------ agent identity proofs, version 2
//
// The guard proves to jh.py which jobhunter agent runs a command (design CLI route, 5.1 and 5.2). Both proofs
// are minted per exec call, are valid for 120 seconds, carry a single-use nonce (jh.py stores it in
// grants_used) and name the agent and the hash of the session key:
//
//   argv proof (inserted as `--agent-proof <T>` right after jh.py by the R2 rewrite):
//     T   = "jhp2." + agent + "." + ts + "." + nonce + "." + sk + "." + mac
//     mac = HMAC-SHA256(key, "jh-agent-proof\n2\n" + agent + "\n" + ts + "\n" + nonce + "\n" + sk + "\n" + digest)
//     digest = hex sha256("\n".join(rest)), rest = the jh.py arguments after the proof pair
//   env proof (JH_AGENT_PROOF from resolve_exec_env, R8; that hook does not see the command):
//     T   = "jhe2." + agent + "." + ts + "." + nonce + "." + sk + "." + mac
//     mac = HMAC-SHA256(key, "jh-agent-env\n2\n" + agent + "\n" + ts + "\n" + nonce + "\n" + sk)
//
//   agent = ^jobhunter-[a-z]{2,20}$, ts = 10 digit unix seconds, nonce = 16 lowercase hex (8 random bytes),
//   sk = first 16 hex of sha256(session key or ""), mac = 64 lowercase hex. jh.py (scripts/jobhunter/auth.py)
//   verifies both with the same constants; tests/fixtures/core/proof_vectors.json (the core's fixture) holds the shared test vectors.

export const PROOF_VERSION = 2;
export const PROOF_TTL_S = 120;
export const PROOF_FUTURE_S = 10;
export const AGENT_ID_RE = /^jobhunter-[a-z]{2,20}$/;
export const ARGV_PROOF_RE = /^jhp2\.(jobhunter-[a-z]{2,20})\.([0-9]{10})\.([0-9a-f]{16})\.([0-9a-f]{16})\.([0-9a-f]{64})$/;
export const ENV_PROOF_RE = /^jhe2\.(jobhunter-[a-z]{2,20})\.([0-9]{10})\.([0-9a-f]{16})\.([0-9a-f]{16})\.([0-9a-f]{64})$/;

export type ProofParts = { agent: string; ts: number; nonce: string; sk: string };

function sha256Hex(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

// First 16 hex characters of sha256 of the session key (an absent key counts as "").
export function sessionHash(sessionKey: string | null | undefined): string {
  return sha256Hex(typeof sessionKey === "string" ? sessionKey : "").slice(0, 16);
}

// sha256 of the jh.py arguments after the proof pair, joined by newlines.
export function argvDigest(rest: readonly string[]): string {
  return sha256Hex(rest.join("\n"));
}

export function argvProofMessage(agent: string, ts: string, nonce: string, sk: string, digest: string): string {
  return "jh-agent-proof\n2\n" + agent + "\n" + ts + "\n" + nonce + "\n" + sk + "\n" + digest;
}

export function envProofMessage(agent: string, ts: string, nonce: string, sk: string): string {
  return "jh-agent-env\n2\n" + agent + "\n" + ts + "\n" + nonce + "\n" + sk;
}

function mintParts(agentId: string, nowS: number | undefined, nonce: string | undefined): { ts: string; nonce: string } {
  if (!AGENT_ID_RE.test(agentId)) throw new Error("not a jobhunter agent id: " + agentId);
  const ts = String(Math.floor(nowS ?? Date.now() / 1000));
  if (!/^[0-9]{10}$/.test(ts)) throw new Error("proof timestamp out of range");
  const n = nonce ?? randomBytes(8).toString("hex");
  if (!/^[0-9a-f]{16}$/.test(n)) throw new Error("proof nonce must be 16 lowercase hex characters");
  return { ts, nonce: n };
}

function hmacHex(key: Buffer, message: string): string {
  return createHmac("sha256", key).update(message, "utf8").digest("hex");
}

// The argv proof for one jh.py call: `rest` are the jh.py arguments that follow `--agent-proof <T>`.
export function argvProof(key: Buffer, agentId: string, sessionKey: string | null | undefined, rest: readonly string[], nowS?: number, nonce?: string): string {
  const p = mintParts(agentId, nowS, nonce);
  const sk = sessionHash(sessionKey);
  return "jhp2." + agentId + "." + p.ts + "." + p.nonce + "." + sk + "." + hmacHex(key, argvProofMessage(agentId, p.ts, p.nonce, sk, argvDigest(rest)));
}

// The env proof (JH_AGENT_PROOF) for one exec call of `agentId` in `sessionKey`.
export function envProof(key: Buffer, agentId: string, sessionKey: string | null | undefined, nowS?: number, nonce?: string): string {
  const p = mintParts(agentId, nowS, nonce);
  const sk = sessionHash(sessionKey);
  return "jhe2." + agentId + "." + p.ts + "." + p.nonce + "." + sk + "." + hmacHex(key, envProofMessage(agentId, p.ts, p.nonce, sk));
}

function macEqual(hex: string, want: string): boolean {
  const a = Buffer.from(hex, "hex");
  const b = Buffer.from(want, "hex");
  return a.length === b.length && a.length === 32 && timingSafeEqual(a, b);
}

function fresh(ts: number, nowS: number): boolean {
  const age = nowS - ts;
  return age <= PROOF_TTL_S && age >= -PROOF_FUTURE_S;
}

// Verification mirror of jobhunter.auth.verify_argv_proof (without the nonce store). Null when the token is
// malformed, too old, from the future, or its mac does not cover `rest`.
export function verifyArgvProof(key: Buffer, token: string, rest: readonly string[], nowS: number): ProofParts | null {
  const m = typeof token === "string" ? ARGV_PROOF_RE.exec(token) : null;
  if (!m) return null;
  const ts = Number(m[2]);
  if (!fresh(ts, nowS)) return null;
  if (!macEqual(m[5], hmacHex(key, argvProofMessage(m[1], m[2], m[3], m[4], argvDigest(rest))))) return null;
  return { agent: m[1], ts, nonce: m[3], sk: m[4] };
}

// Verification mirror of jobhunter.auth.verify_env_proof (without the nonce store). When a session key is
// given its hash must equal the proof's.
export function verifyEnvProof(key: Buffer, token: string, sessionKey: string | null | undefined, nowS: number): ProofParts | null {
  const m = typeof token === "string" ? ENV_PROOF_RE.exec(token) : null;
  if (!m) return null;
  const ts = Number(m[2]);
  if (!fresh(ts, nowS)) return null;
  if (!macEqual(m[5], hmacHex(key, envProofMessage(m[1], m[2], m[3], m[4])))) return null;
  if (typeof sessionKey === "string" && sessionKey.length > 0 && sessionHash(sessionKey) !== m[4]) return null;
  return { agent: m[1], ts, nonce: m[3], sk: m[4] };
}

// Whether `token` is an argv proof this guard minted for the same agent, session and arguments within the TTL
// (a second decision of one call, for example the host deciding the rewritten params again). Only then may R2
// strip it; any other `--agent-p*` token is refused.
export function verifyOwnProof(key: Buffer, token: string, agentId: string, sessionKey: string | null | undefined, rest: readonly string[], nowS: number): boolean {
  const p = verifyArgvProof(key, token, rest, nowS);
  return p !== null && p.agent === agentId && p.sk === sessionHash(sessionKey);
}

// Replace every argv or env proof in a text by "jhp2.<agent>.REDACTED" / "jhe2.<agent>.REDACTED" (event
// recording, logs). Also catches cut or malformed proofs.
export function redactProofs(text: string): string {
  return String(text).replace(/\bjh([pe])2\.(jobhunter-[a-z]{2,20})\.[0-9a-zA-Z.]*/g, "jh$12.$2.REDACTED");
}
