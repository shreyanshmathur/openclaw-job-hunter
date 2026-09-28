// Ledger reads against a temporary database built from the real schema.sql (read only for the guard).

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import type { DatabaseSync } from "node:sqlite";
import { Ledger, tsMs } from "../src/ledger.ts";
import { NOW, TOKEN, addToken, makeDb } from "./_helpers.ts";

test("ledger queries", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "jhg-ledger-"));
  const file = path.join(dir, "jobhunter.sqlite3");
  const db = makeDb(file);
  try {
    const led = new Ledger(file);
    assert.equal(led.installId(), "IABCDEFGH");
    assert.deepEqual([...led.openBreakers()], []);
    assert.equal(led.openToken("jobhunter-applier"), null);

    db.prepare("INSERT INTO breakers (scope, state, reason_code, updated_at) VALUES ('linkedin', 'open', 'li_checkpoint', ?)").run(NOW);
    db.prepare("INSERT INTO breakers (scope, state, reason_code, updated_at) VALUES ('gmail', 'closed', NULL, ?)").run(NOW);
    assert.deepEqual([...led.openBreakers()], ["linkedin"]);

    addToken(db, { token: "TAAAAAAAAAAA", status: "sent" });
    addToken(db, { status: "armed", armedAt: "2026-09-27T04:59:00Z" });
    const t = led.openToken("jobhunter-applier");
    assert.deepEqual(t, { token: TOKEN, kind: "application", platform: "greenhouse", status: "armed", armedAt: "2026-09-27T04:59:00Z", expiresAt: "2026-09-27T05:30:00Z" });
    assert.equal(led.openToken("jobhunter-outreach"), null);

    assert.equal(led.stagedPath(TOKEN), null);
    db.prepare("INSERT INTO staged_files (token, variant_id, path, sha256, staged_at) VALUES (?, 1, '/tmp/openclaw/uploads/a.pdf', 'x', ?)").run(TOKEN, NOW);
    assert.equal(led.stagedPath(TOKEN), "/tmp/openclaw/uploads/a.pdf");
    db.prepare("UPDATE staged_files SET removed_at = ? WHERE token = ?").run(NOW, TOKEN);
    assert.equal(led.stagedPath(TOKEN), null);

    assert.equal(led.dwellLock("jobhunter-applier"), null);
    db.prepare("INSERT INTO locks (name, holder, acquired_at, expires_at) VALUES ('pace:jobhunter-applier:dwell', 'jobhunter-applier', ?, '2026-09-27T05:00:20Z')").run(NOW);
    assert.deepEqual(led.dwellLock("jobhunter-applier"), { acquiredAt: NOW, expiresAt: "2026-09-27T05:00:20Z" });

    // the guard's handle is read only
    assert.throws(() => (led as unknown as { open: () => DatabaseSync }).open().exec("DELETE FROM breakers"));
    led.close();
  } finally {
    db.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("a missing database throws (the runtime then reports unhealthy)", () => {
  const led = new Ledger(path.join(os.tmpdir(), "jhg-no-such-dir-" + process.pid, "x.sqlite3"));
  assert.throws(() => led.installId());
});

test("timestamp parsing", () => {
  assert.equal(tsMs("2026-09-27T05:00:00Z"), Date.parse("2026-09-27T05:00:00Z"));
  assert.equal(tsMs("2026-09-27T05:00:00+00:00"), Date.parse("2026-09-27T05:00:00Z"));
  assert.ok(Number.isNaN(tsMs("yesterday")));
  assert.ok(Number.isNaN(tsMs(null)));
});
