// Read-only ledger queries with Node's built-in node:sqlite (design 1.4). The guard never writes the
// database; every write goes through jh.py.

import { DatabaseSync } from "node:sqlite";
import type { DwellLock, OpenToken } from "./types.ts";

export class Ledger {
  private db: DatabaseSync | null = null;
  readonly dbPath: string;

  constructor(dbPath: string) {
    this.dbPath = dbPath;
  }

  private open(): DatabaseSync {
    if (this.db) return this.db;
    const db = new DatabaseSync(this.dbPath, { readOnly: true });
    try {
      db.exec("PRAGMA busy_timeout = 2000");
    } catch {
      // older builds: the default timeout applies
    }
    this.db = db;
    return db;
  }

  close(): void {
    try {
      this.db?.close();
    } catch {
      // ignore
    }
    this.db = null;
  }

  // Run fn with an open handle; on error reopen once (the file may have been replaced by a migration).
  private run<T>(fn: (db: DatabaseSync) => T): T {
    try {
      return fn(this.open());
    } catch (e) {
      this.close();
      return fn(this.open());
    }
  }

  installId(): string | null {
    return this.run((db) => {
      const row = db.prepare("SELECT value FROM meta WHERE key = 'install_id'").get() as { value?: string } | undefined;
      return row && typeof row.value === "string" ? row.value : null;
    });
  }

  openBreakers(): Set<string> {
    return this.run((db) => {
      const rows = db.prepare("SELECT scope FROM breakers WHERE state = 'open'").all() as Array<{ scope: string }>;
      return new Set(rows.map((r) => String(r.scope)));
    });
  }

  // The agent's open token (u_agent_open_token guarantees at most one reserved or armed row).
  openToken(agentId: string): OpenToken | null {
    return this.run((db) => {
      const row = db
        .prepare(
          "SELECT token, kind, platform, status, armed_at, expires_at FROM actions " +
            "WHERE agent_id = ? AND status IN ('reserved','armed') ORDER BY id DESC LIMIT 1",
        )
        .get(agentId) as Record<string, unknown> | undefined;
      if (!row) return null;
      return {
        token: String(row.token),
        kind: String(row.kind),
        platform: String(row.platform),
        status: String(row.status),
        armedAt: row.armed_at === null || row.armed_at === undefined ? null : String(row.armed_at),
        expiresAt: String(row.expires_at),
      };
    });
  }

  stagedPath(token: string): string | null {
    return this.run((db) => {
      const row = db
        .prepare("SELECT path FROM staged_files WHERE token = ? AND removed_at IS NULL ORDER BY staged_at DESC LIMIT 1")
        .get(token) as { path?: string } | undefined;
      return row && typeof row.path === "string" ? row.path : null;
    });
  }

  // pace wait --kind dwell stores the drawn target time in locks row "pace:<agent>:dwell"
  // (acquired_at = when it was drawn, expires_at = when the dwell has elapsed).
  dwellLock(agentId: string): DwellLock | null {
    return this.run((db) => {
      const row = db
        .prepare("SELECT acquired_at, expires_at FROM locks WHERE name = ?")
        .get("pace:" + agentId + ":dwell") as Record<string, unknown> | undefined;
      if (!row) return null;
      return { acquiredAt: String(row.acquired_at), expiresAt: String(row.expires_at) };
    });
  }
}

// Our timestamps are "YYYY-MM-DDTHH:MM:SSZ"; anything unparsable is NaN (and never satisfies a check).
export function tsMs(ts: string | null | undefined): number {
  if (typeof ts !== "string") return NaN;
  const t = ts.trim().replace(/\+00:00$/, "Z");
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/.test(t)) return NaN;
  return Date.parse(t);
}
