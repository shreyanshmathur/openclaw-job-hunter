// Chat grants for /jh commands (design 1.4 R6 and 12.19).
//
//   grant = "<unix_ts>.<nonce 16 hex>.<hex HMAC-SHA256(key, command + "\n" + json.dumps(args) + "\n" + ts + "\n" + nonce)>"
//
// `key` is the 32 bytes whose hex text is stored in private/guard.key. `command` is the command words
// joined by one space ("approve", "profile answer", "config lower"). `args` is the list of argv tokens
// that follow the command words. json.dumps is Python's default: ", " separators and ensure_ascii.

import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

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

// Proof that an exec environment was produced by the guard for this agent (sent as JH_AGENT_PROOF by
// the resolve_exec_env hook): "<unix_ts>.<hex HMAC-SHA256(key, "agent\n" + agent_id + "\n" + ts)>".
export function agentProof(key: Buffer, agentId: string, nowS?: number): string {
  const ts = String(Math.floor(nowS ?? Date.now() / 1000));
  const mac = createHmac("sha256", key).update("agent\n" + agentId + "\n" + ts, "utf8").digest("hex");
  return ts + "." + mac;
}
