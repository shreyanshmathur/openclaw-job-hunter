import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHmac } from "node:crypto";
import fs from "node:fs";
import { agentProof, makeGrant, parseKey, pyJsonDumpsStrList, verifyGrant } from "../src/grant.ts";

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

test("agent proof", () => {
  const p = agentProof(key, "jobhunter-applier", 1790000000);
  const want = createHmac("sha256", key).update("agent\njobhunter-applier\n1790000000").digest("hex");
  assert.equal(p, "1790000000." + want);
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
