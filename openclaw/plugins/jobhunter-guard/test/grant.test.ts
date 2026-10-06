import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHmac } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import {
  ARGV_PROOF_RE,
  ENV_PROOF_RE,
  argvDigest,
  argvProof,
  envProof,
  makeGrant,
  parseKey,
  pyJsonDumpsStrList,
  redactProofs,
  sessionHash,
  verifyArgvProof,
  verifyEnvProof,
  verifyGrant,
  verifyOwnProof,
} from "../src/grant.ts";
import { tokenize } from "../src/exec_parse.ts";
import { REPO } from "./_helpers.ts";

const KEY_HEX = "00112233445566778899aabbccddeeff".repeat(2);
const key = parseKey(KEY_HEX + "\n");

test("key parsing", () => {
  assert.equal(key.length, 32);
  assert.throws(() => parseKey("abc"));
  assert.throws(() => parseKey(""));
  assert.throws(() => parseKey("zz" + KEY_HEX.slice(2)));
});

test("python json.dumps compatible list encoding", () => {
  assert.equal(pyJsonDumpsStrList([]), "[]");
  assert.equal(pyJsonDumpsStrList(["A7K2", "--by", "chat"]), "[\"A7K2\", \"--by\", \"chat\"]");
  assert.equal(pyJsonDumpsStrList(["a\"b\\c\nd\te"]), "[\"a\\\"b\\\\c\\nd\\te\"]");
  assert.equal(pyJsonDumpsStrList(["caf\u00e9 \u20ac \u{1F600} \u007f \u0001"]), "[\"caf\\u00e9 \\u20ac \\ud83d\\ude00 \\u007f \\u0001\"]");
});

test("grant format, verification, expiry and binding", () => {
  const now = 1790000000;
  const g = makeGrant(key, "approve", ["A7K2", "--by", "chat"], now, "0123456789abcdef");
  assert.match(g, /^1790000000\.0123456789abcdef\.[0-9a-f]{64}$/);
  const want = createHmac("sha256", key).update("approve\n[\"A7K2\", \"--by\", \"chat\"]\n1790000000\n0123456789abcdef").digest("hex");
  assert.equal(g.split(".")[2], want);
  assert.equal(verifyGrant(key, g, "approve", ["A7K2", "--by", "chat"], now + 60), true);
  assert.equal(verifyGrant(key, g, "approve", ["A7K2", "--by", "chat"], now + 121), false);
  assert.equal(verifyGrant(key, g, "approve", ["A7K3", "--by", "chat"], now), false);
  assert.equal(verifyGrant(key, g, "skip", ["A7K2", "--by", "chat"], now), false);
  assert.equal(verifyGrant(parseKey("ff" + KEY_HEX.slice(2)), g, "approve", ["A7K2", "--by", "chat"], now), false);
  const g2 = makeGrant(key, "approve", ["A7K2"]);
  assert.notEqual(g2.split(".")[1], makeGrant(key, "approve", ["A7K2"]).split(".")[1]); // fresh nonce
});

// The one vector file shared with the core (tests/test_auth.py), frozen with the proof format (CLI route design
// 15, day 1 contract). It lives with the core's fixtures; the guard only reads it.
// Field names: the hex values are named after their hash (hmac_sha256_key_hex, digest_sha256,
// token_hmac_sha256); the short names are read too.
type RawVector = Record<string, unknown>;
type Vector = { kind: "argv" | "env"; agent_id: string; ts: number; nonce: string; session_key: string; rest: string[]; sk: string; digest: string; token: string };
const VECTOR_FILE = path.join(REPO, "tests", "fixtures", "core", "proof_vectors.json");
const RAW = JSON.parse(fs.readFileSync(VECTOR_FILE, "utf8")) as { version: number; argv_regex: string; env_regex: string; cases: RawVector[] } & Record<string, unknown>;
const str = (o: Record<string, unknown>, ...keys: string[]): string => {
  for (const k of keys) if (typeof o[k] === "string") return o[k] as string;
  throw new Error("proof_vectors.json: none of " + keys.join(", "));
};
const VECTORS = {
  version: RAW.version,
  argv_regex: RAW.argv_regex,
  env_regex: RAW.env_regex,
  key_hex: str(RAW, "hmac_sha256_key_hex", "key_hex"),
  cases: RAW.cases.map((c): Vector => ({
    kind: c.kind as "argv" | "env",
    agent_id: str(c, "agent_id"),
    ts: Number(c.ts),
    nonce: str(c, "nonce"),
    session_key: str(c, "session_key"),
    rest: Array.isArray(c.rest) ? (c.rest as string[]) : [],
    sk: str(c, "sk"),
    digest: c.kind === "argv" ? str(c, "digest_sha256", "digest") : "",
    token: str(c, "token_hmac_sha256", "token"),
  })),
};
const VKEY = parseKey(VECTORS.key_hex);
const ARGV_V = VECTORS.cases.filter((c) => c.kind === "argv");
const ENV_V = VECTORS.cases.filter((c) => c.kind === "env");

test("the shared vector file describes the same format", () => {
  assert.equal(VECTORS.version, 2);
  assert.equal(VECTORS.argv_regex, ARGV_PROOF_RE.source);
  assert.equal(VECTORS.env_regex, ENV_PROOF_RE.source);
  assert.ok(ARGV_V.length >= 2 && ENV_V.length >= 2);
});

test("argv proof v2 matches the shared vectors (same constants as tests/test_auth.py)", () => {
  for (const v of ARGV_V) {
    const rest = v.rest;
    assert.equal(sessionHash(v.session_key), v.sk);
    assert.equal(argvDigest(rest), v.digest);
    const t = argvProof(VKEY, v.agent_id, v.session_key, rest, v.ts, v.nonce);
    assert.equal(t, v.token, v.agent_id);
    assert.match(t, ARGV_PROOF_RE);
    const msg = "jh-agent-proof\n2\n" + v.agent_id + "\n" + v.ts + "\n" + v.nonce + "\n" + v.sk + "\n" + v.digest;
    assert.equal(createHmac("sha256", VKEY).update(msg, "utf8").digest("hex"), t.split(".").pop());
    assert.deepEqual(verifyArgvProof(VKEY, t, rest, v.ts + 120), { agent: v.agent_id, ts: v.ts, nonce: v.nonce, sk: v.sk });
    assert.notEqual(verifyArgvProof(VKEY, t, rest, v.ts - 10), null);
    assert.equal(verifyArgvProof(VKEY, t, rest, v.ts + 121), null, "too old");
    assert.equal(verifyArgvProof(VKEY, t, rest, v.ts - 11), null, "from the future");
    assert.equal(verifyArgvProof(VKEY, t, [...rest, "x"], v.ts), null, "added token");
    if (rest.length > 0) assert.equal(verifyArgvProof(VKEY, t, rest.slice(1), v.ts), null, "removed token");
    if (rest.length > 1 && rest[0] !== rest[1]) assert.equal(verifyArgvProof(VKEY, t, [rest[1], rest[0], ...rest.slice(2)], v.ts), null, "reordered");
    assert.equal(verifyArgvProof(parseKey("11".repeat(32)), t, rest, v.ts), null, "other key");
    assert.equal(verifyArgvProof(VKEY, t.replace("jhp2.", "jhe2."), rest, v.ts), null);
    assert.equal(verifyArgvProof(VKEY, t.slice(0, -1) + (t.endsWith("0") ? "1" : "0"), rest, v.ts), null);
    // every character is in the R2 token class, so the rewritten command still tokenizes
    assert.deepEqual(tokenize("--agent-proof " + t), ["--agent-proof", t]);
  }
});

test("env proof v2 matches the shared vectors", () => {
  for (const v of ENV_V) {
    assert.equal(sessionHash(v.session_key), v.sk);
    const t = envProof(VKEY, v.agent_id, v.session_key, v.ts, v.nonce);
    assert.equal(t, v.token, v.agent_id);
    assert.match(t, ENV_PROOF_RE);
    const msg = "jh-agent-env\n2\n" + v.agent_id + "\n" + v.ts + "\n" + v.nonce + "\n" + v.sk;
    assert.equal(createHmac("sha256", VKEY).update(msg, "utf8").digest("hex"), t.split(".").pop());
    assert.deepEqual(verifyEnvProof(VKEY, t, v.session_key, v.ts + 60), { agent: v.agent_id, ts: v.ts, nonce: v.nonce, sk: v.sk });
    assert.notEqual(verifyEnvProof(VKEY, t, undefined, v.ts), null, "no session key to compare");
    assert.equal(verifyEnvProof(VKEY, t, "agent:other:session", v.ts), null, "other session");
    assert.equal(verifyEnvProof(VKEY, t, v.session_key, v.ts + 121), null);
    assert.equal(verifyEnvProof(VKEY, t, v.session_key, v.ts - 11), null);
  }
});

test("env proofs carry a fresh nonce per mint; argv and env macs are domain separated", () => {
  const a = envProof(key, "jobhunter-scout", "s", 1790000000);
  const b = envProof(key, "jobhunter-scout", "s", 1790000000);
  assert.notEqual(a.split(".")[3], b.split(".")[3]);
  const p = argvProof(key, "jobhunter-scout", "s", [], 1790000000, "0123456789abcdef");
  const e = envProof(key, "jobhunter-scout", "s", 1790000000, "0123456789abcdef");
  assert.notEqual(p.split(".").pop(), e.split(".").pop());
  assert.throws(() => argvProof(key, "main", "s", [], 1790000000));
  assert.throws(() => argvProof(key, "jobhunter-Scout", "s", [], 1790000000));
  assert.throws(() => envProof(key, "jobhunter-scout", "s", 1790000000, "XYZ"));
});

test("verifyOwnProof: same agent, session and arguments within the TTL only", () => {
  const rest = ["profile", "salary-record", "--file", "/w/scout/work/onboarding/s.json"];
  const t = argvProof(key, "jobhunter-scout", "agent:jobhunter-scout:cron:a", rest, 1790000000);
  assert.equal(verifyOwnProof(key, t, "jobhunter-scout", "agent:jobhunter-scout:cron:a", rest, 1790000100), true);
  assert.equal(verifyOwnProof(key, t, "jobhunter-evaluator", "agent:jobhunter-scout:cron:a", rest, 1790000100), false);
  assert.equal(verifyOwnProof(key, t, "jobhunter-scout", "agent:jobhunter-scout:cron:b", rest, 1790000100), false);
  assert.equal(verifyOwnProof(key, t, "jobhunter-scout", "agent:jobhunter-scout:cron:a", rest.slice(0, 2), 1790000100), false);
  assert.equal(verifyOwnProof(key, t, "jobhunter-scout", "agent:jobhunter-scout:cron:a", rest, 1790000121), false);
  assert.equal(verifyOwnProof(parseKey("ff".repeat(32)), t, "jobhunter-scout", "agent:jobhunter-scout:cron:a", rest, 1790000100), false);
});

test("proof redaction", () => {
  const p = argvProof(key, "jobhunter-scout", "s", ["whoami"], 1790000000);
  const e = envProof(key, "jobhunter-evaluator", "s", 1790000000);
  assert.equal(redactProofs("/py -I /r/scripts/jh.py --agent-proof " + p + " whoami"), "/py -I /r/scripts/jh.py --agent-proof jhp2.jobhunter-scout.REDACTED whoami");
  assert.equal(redactProofs("JH_AGENT_PROOF=" + e), "JH_AGENT_PROOF=jhe2.jobhunter-evaluator.REDACTED");
  assert.equal(redactProofs("x jhp2.jobhunter-scout.17900 y"), "x jhp2.jobhunter-scout.REDACTED y");
  assert.equal(redactProofs("no proof here"), "no proof here");
});

test("the v2 proofs match a Python stdlib implementation", { skip: !fs.existsSync("/usr/bin/python3") }, () => {
  const rest = ["profile", "infer-record", "--file", "/w/evaluator/work/onboarding/p.json"];
  const t = argvProof(key, "jobhunter-evaluator", "agent:jobhunter-evaluator:cron:x", rest, 1790000000, "0123456789abcdef");
  const py = [
    "import hashlib, hmac, json, sys",
    "key = bytes.fromhex(sys.argv[1]); rest = json.loads(sys.argv[2])",
    "sk = hashlib.sha256(sys.argv[3].encode('utf-8')).hexdigest()[:16]",
    "d = hashlib.sha256('\\n'.join(rest).encode('utf-8')).hexdigest()",
    "msg = 'jh-agent-proof\\n2\\njobhunter-evaluator\\n1790000000\\n0123456789abcdef\\n' + sk + '\\n' + d",
    "print('jhp2.jobhunter-evaluator.1790000000.0123456789abcdef.' + sk + '.' + hmac.new(key, msg.encode('utf-8'), hashlib.sha256).hexdigest())",
  ].join("\n");
  const r = spawnSync("/usr/bin/python3", ["-c", py, KEY_HEX, JSON.stringify(rest), "agent:jobhunter-evaluator:cron:x"], { encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stdout.trim(), t);
});

test("the grant matches a Python stdlib implementation", { skip: !fs.existsSync("/usr/bin/python3") }, () => {
  const args = ["Q3", "--value", "30 days, caf\u00e9 \"quoted\"\nline two"];
  const g = makeGrant(key, "profile answer", args, 1790000000, "fedcba9876543210");
  const py = [
    "import hmac, hashlib, json, sys",
    "key = bytes.fromhex(sys.argv[1])",
    "args = json.loads(sys.argv[2])",
    "msg = sys.argv[3] + '\\n' + json.dumps(args) + '\\n' + sys.argv[4] + '\\n' + sys.argv[5]",
    "print(hmac.new(key, msg.encode('utf-8'), hashlib.sha256).hexdigest())",
  ].join("\n");
  const r = spawnSync("/usr/bin/python3", ["-c", py, KEY_HEX, JSON.stringify(args), "profile answer", "1790000000", "fedcba9876543210"], { encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stdout.trim(), g.split(".")[2]);
});
