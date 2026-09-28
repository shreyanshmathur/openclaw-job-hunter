"""Email finder (enrichment), unit U10. Off by default.

For one contact the outreach agent already selected (on the hiring team of an eligible or applied job, in
a target role, passing every exclusion and dedup rule), it runs free checks first (MX, a published
pattern), then at most two finder APIs from the person's OWN free-tier keys, then at most one verifier,
grades the result on the existing A/B/C scale and writes only sendable results to the contact. The gate,
QC, approval and mailer then treat it like any other address.

It never scrapes LinkedIn, never uses browser extensions, never probes SMTP, never requests or stores phone
numbers, never accepts or searches for keys found online (they belong to other people), and has no
"look up anyone" command. docs/EMAIL-FINDER.md is the user guide.
"""
from __future__ import annotations

import os
import sqlite3

TABLES = ("enrich_requests", "enrich_request_keys", "enrich_calls", "enrich_provider_state")
TRIGGERS = ("t_enrich_budget", "t_enrich_reserve_max", "t_enrich_max_finders", "t_enrich_call_outcome_graph",
            "t_enrich_no_second_retry", "t_contact_provider_email_ins", "t_contact_provider_email_upd",
            "t_enrich_action_address", "t_enrich_requests_touch", "t_enrich_calls_touch")


def _migration_path() -> str:
    from .. import paths
    return os.path.join(paths.MIGRATIONS_DIR, "0002_enrich.sql")


def _check(name: str, fn) -> dict:
    try:
        ok, detail = fn()
    except Exception as exc:   # a selftest line never raises
        ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
    return {"name": name, "ok": bool(ok), "detail": detail}


def _objects() -> tuple:
    from .. import db
    conn = db.connect(write=False)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    finally:
        conn.close()
    missing = [n for n in TABLES + TRIGGERS if n not in names]
    missing += [c for c in ("email_source", "email_enrich_call_id") if c not in cols]
    return not missing, "ok" if not missing else "missing: " + ", ".join(missing)


def _trigger_fixture() -> tuple:
    """In memory, with no meta rows: a new provider call must be refused (budget 0, fail closed)."""
    from .. import paths
    mem = sqlite3.connect(":memory:")
    try:
        with open(paths.SCHEMA_FILE, "r", encoding="utf-8") as fh:
            mem.executescript(fh.read())
        with open(_migration_path(), "r", encoding="utf-8") as fh:
            mem.executescript(fh.read())
        ts = "2026-01-01T00:00:00Z"
        mem.execute("INSERT INTO enrich_requests (id, request_uid, status, created_by, started_at, created_at, "
                    "updated_at) VALUES (1, 'EAAAAAAA', 'running', 'system', ?, ?, ?)", (ts, ts, ts))
        try:
            mem.execute("INSERT INTO enrich_calls (request_id, provider, op, outcome, started_at, credits_charged, "
                        "created_at, updated_at) VALUES (1, 'hunter', 'find_name_domain', 'inflight', ?, 1, ?, ?)",
                        (ts, ts, ts))
            return False, "a call was allowed without budget meta rows"
        except sqlite3.DatabaseError as exc:
            return str(exc) == "E_CEILING", "refused: %s" % exc
    finally:
        mem.close()


def _keystore() -> tuple:
    from . import keystore
    be = keystore.backend()
    if be == "keychain" and not os.path.exists(keystore.SECURITY):
        return False, "keychain backend chosen but %s is missing; set enrich.key_store to file" % keystore.SECURITY
    ok, detail = keystore.file_mode_ok()
    return ok, "backend %s; key file %s" % (be, detail)


def _deps() -> tuple:
    from . import deps
    missing = ["%s.%s" % (deps.MODULE_OF[n], n) for n in sorted(deps.MODULE_OF) if not deps.available(n)]
    # a missing U1/U6 function fails closed (no lookup, nothing written), but the install is broken
    return (not missing), "all present" if not missing else "missing: " + ", ".join(missing)


def selftest_checks() -> list:
    """[{name, ok, detail}] for U1 `selftest` (INTEGRATION PATCH LIST U1-11)."""
    return [_check("enrich migration objects", _objects), _check("enrich trigger fixtures", _trigger_fixture),
            _check("enrich keystore", _keystore), _check("enrich dependencies", _deps)]
